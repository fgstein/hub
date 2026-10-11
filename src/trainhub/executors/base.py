"""Interface commune des exécuteurs.

Un exécuteur reçoit l'argv **distant** (``docker …``, ``ls …``) et une machine ; il se charge
du transport (SSH, exécution locale directe, ou simulation) et renvoie la sortie.
"""

from __future__ import annotations

import abc
import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from ..core.models import Inventory, Machine

log = logging.getLogger("trainhub.exec")

#: Délai maximal d'une action (pause, stop…), hors délai de ``docker stop``.
ACTION_TIMEOUT_S = 10.0
#: Délai maximal d'une commande de suivi (``docker ps``).
MONITOR_TIMEOUT_S = 5.0
#: Délai maximal de ``docker run -d`` (peut inclure un ``pull`` d'image).
LAUNCH_TIMEOUT_S = 600.0


class ExecutorError(Exception):
    """Erreur d'exécution générique."""


class MachineUnreachableError(ExecutorError):
    """Transport impossible : machine injoignable, SSH refusé, délai dépassé."""

    def __init__(self, machine: str, reason: str) -> None:
        self.machine = machine
        self.reason = reason
        super().__init__(f"machine '{machine}' injoignable : {reason}")


@dataclass(frozen=True)
class ExecResult:
    """Résultat d'une commande exécutée sur une machine."""

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
        """Message d'erreur lisible (dernière ligne non vide de stderr, sinon stdout)."""
        for text in (self.err, self.out):
            lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
            if lines:
                msg = lines[-1]
                return msg.removeprefix("docker: ").removeprefix("Error response from daemon: ")
        return f"code de retour {self.returncode}"


class LogStream(abc.ABC):
    """Flux d'octets d'un ``docker logs -f`` (itérable de façon asynchrone)."""

    @abc.abstractmethod
    def __aiter__(self) -> AsyncIterator[bytes]: ...

    @abc.abstractmethod
    async def close(self) -> None:
        """Arrête le processus sous-jacent."""

    @property
    def returncode(self) -> int | None:
        return None


class Executor(abc.ABC):
    """Exécute des commandes sur les machines de l'inventaire."""

    kind: str = "base"

    def update_inventory(self, inventory: Inventory) -> None:  # noqa: B027
        """Appelé à chaque (re)chargement de l'inventaire."""

    @abc.abstractmethod
    def full_argv(self, machine: Machine, remote: Sequence[str]) -> list[str]:
        """argv local complet effectivement exécuté (audit, aperçu)."""

    async def exec(
        self, machine: Machine, remote: Sequence[str], *, timeout: float = ACTION_TIMEOUT_S
    ) -> ExecResult:
        """Exécute ``remote`` sur ``machine`` ; journalise argv, durée et code de retour.

        Lève :class:`MachineUnreachableError` si la machine n'est pas joignable.
        """
        t0 = time.monotonic()
        try:
            res = await self._exec(machine, list(remote), timeout=timeout)
        except MachineUnreachableError as exc:
            log.warning(
                "commande échouée (injoignable)",
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
            "commande exécutée",
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
        """Démarre une commande longue (``docker logs -f``) et renvoie son flux de sortie."""

    async def close(self) -> None:  # noqa: B027
        """Libère les ressources (processus, connexions)."""
