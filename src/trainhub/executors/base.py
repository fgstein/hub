"""Common executor interface.

An executor receives the **remote** argv (``docker …``, ``ls …``) and a machine; it handles
the transport (SSH, direct local execution, or simulation) and returns the output.
"""

from __future__ import annotations

import abc
import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.models import Inventory, Machine

if TYPE_CHECKING:
    from ..service.mlflow import MlflowSource

log = logging.getLogger("trainhub.exec")

#: Maximum duration of an action (pause, stop…), excluding the ``docker stop`` timeout.
ACTION_TIMEOUT_S = 10.0
#: Maximum duration of a monitoring command (``docker ps``).
MONITOR_TIMEOUT_S = 5.0
#: Maximum duration of ``docker run -d`` (may include an image ``pull``).
LAUNCH_TIMEOUT_S = 600.0


class ExecutorError(Exception):
    """Generic execution error."""


class MachineUnreachableError(ExecutorError):
    """Transport failed: machine unreachable, SSH refused, timeout."""

    def __init__(self, machine: str, reason: str) -> None:
        self.machine = machine
        self.reason = reason
        super().__init__(f"machine '{machine}' unreachable: {reason}")


@dataclass(frozen=True)
class ExecResult:
    """Result of a command run on a machine."""

    argv: list[str]
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")

    def error_message(self) -> str:
        """Readable error message (last non-empty line of stderr, else stdout)."""
        for text in (self.err, self.out):
            lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
            if lines:
                msg = lines[-1]
                return msg.removeprefix("docker: ").removeprefix("Error response from daemon: ")
        return f"exit code {self.returncode}"


class LogStream(abc.ABC):
    """Byte stream of a ``docker logs -f`` (asynchronously iterable)."""

    @abc.abstractmethod
    def __aiter__(self) -> AsyncIterator[bytes]: ...

    @abc.abstractmethod
    async def close(self) -> None:
        """Stop the underlying process."""

    @property
    def returncode(self) -> int | None:
        return None


class ShellSession(abc.ABC):
    """Interactive session in a container (``docker exec -it``), behind a PTY."""

    @abc.abstractmethod
    async def read(self) -> bytes:
        """Next output bytes; ``b""`` once the session is over."""

    @abc.abstractmethod
    def write(self, data: bytes) -> None:
        """Send bytes (keystrokes) to the shell."""

    @abc.abstractmethod
    def resize(self, cols: int, rows: int) -> None:
        """Change the terminal size."""

    @abc.abstractmethod
    async def close(self) -> None:
        """End the session."""


class Executor(abc.ABC):
    """Run commands on the inventory machines."""

    kind: str = "base"

    def update_inventory(self, inventory: Inventory) -> None:  # noqa: B027
        """Called on every inventory (re)load."""

    @abc.abstractmethod
    def full_argv(self, machine: Machine, remote: Sequence[str]) -> list[str]:
        """Full local argv actually executed (audit, preview)."""

    async def exec(
        self, machine: Machine, remote: Sequence[str], *, timeout: float = ACTION_TIMEOUT_S
    ) -> ExecResult:
        """Run ``remote`` on ``machine``; log argv, duration and exit code.

        Raises :class:`MachineUnreachableError` if the machine cannot be reached.
        """
        t0 = time.monotonic()
        try:
            res = await self._exec(machine, list(remote), timeout=timeout)
        except MachineUnreachableError as exc:
            log.warning(
                "command failed (unreachable)",
                extra={
                    "machine": machine.id,
                    "argv": list(remote),
                    "duration_ms": round((time.monotonic() - t0) * 1000),
                    "error": exc.reason,
                },
            )
            raise
        level = logging.DEBUG if list(remote[:2]) == ["docker", "ps"] else logging.INFO
        log.log(
            level,
            "command executed",
            extra={
                "machine": machine.id,
                "argv": list(remote),
                "duration_ms": round(res.duration_s * 1000),
                "returncode": res.returncode,
            },
        )
        return res

    @abc.abstractmethod
    async def _exec(self, machine: Machine, remote: list[str], *, timeout: float) -> ExecResult: ...

    @abc.abstractmethod
    async def stream(self, machine: Machine, remote: Sequence[str]) -> LogStream:
        """Start a long-running command (``docker logs -f``) and return its output stream."""

    async def open_shell(self, machine: Machine, remote: Sequence[str]) -> ShellSession:
        """Open an interactive shell (``docker exec -it …``) on ``machine``."""
        raise ExecutorError(f"interactive shell not available with the {self.kind} executor")

    def mlflow_source(self) -> MlflowSource | None:
        """MLflow to use instead of the REST API (simulation); ``None``: the real one."""
        return None

    async def close(self) -> None:  # noqa: B027
        """Release resources (processes, connections)."""
