"""Base des exécuteurs à sous-processus (``local`` et ``ssh``)."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import pty
import signal
import struct
import termios
import time
from collections.abc import AsyncIterator, Sequence

from ..core.models import Machine
from .base import (
    ExecResult,
    Executor,
    ExecutorError,
    LogStream,
    MachineUnreachableError,
    ShellSession,
)

log = logging.getLogger("trainhub.exec")


class ProcessLogStream(LogStream):
    """Sortie (stdout + stderr fusionnés) d'un processus local."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    async def _iter(self) -> AsyncIterator[bytes]:
        assert self._proc.stdout is not None
        while True:
            chunk = await self._proc.stdout.read(65536)
            if not chunk:
                break
            yield chunk
        with contextlib.suppress(ProcessLookupError):
            await self._proc.wait()

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iter()

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    async def close(self) -> None:
        await _terminate(self._proc)


async def _terminate(proc: asyncio.subprocess.Process, grace: float = 2.0) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _take_controlling_tty() -> None:  # pragma: no cover - exécuté dans le processus fils
    # Après setsid (start_new_session) : le PTY devient le terminal de contrôle, pour que
    # ssh/docker reçoivent SIGWINCH lors d'un redimensionnement.
    with contextlib.suppress(OSError):
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class PtyShellSession(ShellSession):
    """Processus local (``ssh -tt … docker exec -it`` ou ``docker exec -it``) relié à un PTY."""

    def __init__(self, proc: asyncio.subprocess.Process, master: int) -> None:
        self._proc = proc
        self._master = master
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._closed = False
        os.set_blocking(master, False)
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(master, self._on_readable)

    def _on_readable(self) -> None:
        try:
            data = os.read(self._master, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO : le processus a fermé le terminal
            data = b""
        if not data:
            self._loop.remove_reader(self._master)
        self._queue.put_nowait(data)

    async def read(self) -> bytes:
        if self._closed:
            return b""
        return await self._queue.get()

    def write(self, data: bytes) -> None:
        if self._closed:
            return
        view = memoryview(data)
        deadline = time.monotonic() + 2.0
        while view and time.monotonic() < deadline:
            try:
                n = os.write(self._master, view)
            except BlockingIOError:
                time.sleep(0.005)
                continue
            except OSError:
                return
            view = view[n:]

    def resize(self, cols: int, rows: int) -> None:
        if not self._closed:
            with contextlib.suppress(OSError):
                _set_winsize(self._master, max(1, cols), max(1, rows))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.remove_reader(self._master)
        await _terminate(self._proc, grace=1.0)
        with contextlib.suppress(OSError):
            os.close(self._master)
        self._queue.put_nowait(b"")


class SubprocessExecutor(Executor):
    """Lance les commandes via ``asyncio.create_subprocess_exec`` (jamais de shell local)."""

    #: Code de retour signalant une erreur de transport (255 pour ssh).
    transport_error_code: int | None = None

    def _env(self) -> dict[str, str] | None:
        return None

    async def _exec(self, machine: Machine, remote: list[str], *, timeout: float) -> ExecResult:
        argv = self.full_argv(machine, remote)
        t0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=self._env(),
            )
        except FileNotFoundError as exc:
            raise ExecutorError(f"binaire introuvable : {argv[0]}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError as exc:
            await _terminate(proc)
            raise MachineUnreachableError(
                machine.id, f"délai dépassé ({timeout:.0f} s) pour {remote[:2]}"
            ) from exc
        except asyncio.CancelledError:
            await _terminate(proc)
            raise
        res = ExecResult(
            argv=argv,
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_s=time.monotonic() - t0,
        )
        if self.transport_error_code is not None and res.returncode == self.transport_error_code:
            raise MachineUnreachableError(machine.id, res.error_message())
        return res

    def shell_argv(self, machine: Machine, remote: Sequence[str]) -> list[str]:
        """argv local d'un shell interactif (``ssh -tt`` pour les machines distantes)."""
        return self.full_argv(machine, remote)

    async def open_shell(self, machine: Machine, remote: Sequence[str]) -> ShellSession:
        argv = self.shell_argv(machine, remote)
        master, slave = pty.openpty()
        _set_winsize(master, 120, 32)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                preexec_fn=_take_controlling_tty,
                env=self._env(),
            )
        except FileNotFoundError as exc:
            os.close(master)
            raise ExecutorError(f"binaire introuvable : {argv[0]}") from exc
        finally:
            os.close(slave)
        log.info("shell ouvert", extra={"machine": machine.id, "argv": list(remote)})
        return PtyShellSession(proc, master)

    async def stream(self, machine: Machine, remote: Sequence[str]) -> LogStream:
        argv = self.full_argv(machine, remote)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                env=self._env(),
            )
        except FileNotFoundError as exc:
            raise ExecutorError(f"binaire introuvable : {argv[0]}") from exc
        return ProcessLogStream(proc)
