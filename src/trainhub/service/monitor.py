"""Suivi des machines : une boucle par machine exécute ``docker ps`` toutes les 3 s.

Une seule commande par machine, quel que soit le nombre de runs. Au premier contact, et
quand une machine redevient joignable, les conteneurs trouvés par label sont réconciliés avec
la base (import des conteneurs inconnus, échec explicite des runs sans conteneur).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

from ..core import commands as cmd
from ..core.models import UnknownResourceError
from ..core.status import parse_ps
from ..executors.base import MONITOR_TIMEOUT_S, ExecutorError, MachineUnreachableError

if TYPE_CHECKING:
    from .runs import RunService

log = logging.getLogger("trainhub.monitor")


class Monitor:
    """Boucles de suivi, une par machine de l'inventaire."""

    def __init__(self, service: RunService, interval: float = 3.0) -> None:
        self.svc = service
        self.interval = interval
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running = False

    def start(self) -> None:
        self._running = True
        self.sync_machines()

    def sync_machines(self) -> None:
        """Ajoute/retire des boucles selon les machines de l'inventaire."""
        if not self._running:
            return
        wanted = {m.id for m in self.svc.inventory.machines}
        for mid in list(self._tasks):
            if mid not in wanted:
                self._tasks.pop(mid).cancel()
        for mid in wanted - set(self._tasks):
            self._tasks[mid] = asyncio.get_running_loop().create_task(self._loop(mid))

    async def stop(self) -> None:
        self._running = False
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    async def _loop(self, machine_id: str) -> None:
        while True:
            t0 = time.monotonic()
            try:
                await self.poll(machine_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("erreur dans la boucle de suivi", extra={"machine": machine_id})
            await asyncio.sleep(max(0.2, self.interval - (time.monotonic() - t0)))

    async def poll(self, machine_id: str) -> None:
        """Un tour de suivi pour une machine (utilisable directement dans les tests)."""
        try:
            machine = self.svc.inventory.machine(machine_id)
        except UnknownResourceError:
            return
        t0 = time.monotonic()
        try:
            res = await self.svc.executor.exec(machine, cmd.docker_ps(), timeout=MONITOR_TIMEOUT_S)
        except MachineUnreachableError as exc:
            await self.svc.mark_machine_unreachable(machine_id, exc.reason)
            return
        except ExecutorError as exc:
            await self.svc.mark_machine_unreachable(machine_id, str(exc))
            return
        if not res.ok:
            await self.svc.mark_machine_unreachable(
                machine_id, f"docker indisponible : {res.error_message()}"
            )
            return
        was_reachable = self.svc.machine_state(machine_id).reachable
        self.svc.set_machine_status(
            machine_id, reachable=True, latency_ms=round((time.monotonic() - t0) * 1000, 1)
        )
        containers = [
            c
            for c in parse_ps(res.out)
            if c.labels.get(cmd.LABEL_MACHINE, machine_id) == machine_id
        ]
        await self.svc.apply_container_states(
            machine_id, containers, reconcile=was_reachable is not True
        )
