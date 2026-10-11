"""Base des exécuteurs à sous-processus (``local`` et ``ssh``)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import AsyncIterator, Sequence

from ..core.models import Machine
from .base import ExecResult, Executor, ExecutorError, LogStream, MachineUnreachableError


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
