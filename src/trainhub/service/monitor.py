"""Machine monitoring: one loop per machine runs ``docker ps`` every 3 s.

A single command per machine, whatever the number of runs. On first contact, and when a
machine becomes reachable again, containers found by label are reconciled with the
database (unknown containers are imported, runs without a container explicitly fail).
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
    """Monitoring loops, one per inventory machine."""

    def __init__(self, service: RunService, interval: float = 3.0) -> None:
        self.svc = service
        self.interval = interval
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running = False

    def start(self) -> None:
        self._running = True
        self.sync_machines()

    def sync_machines(self) -> None:
        """Add/remove loops according to the inventory machines."""
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
                log.exception("error in the monitoring loop", extra={"machine": machine_id})
            await asyncio.sleep(max(0.2, self.interval - (time.monotonic() - t0)))

    async def poll(self, machine_id: str) -> None:
        """One monitoring round for a machine (usable directly in tests)."""
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
                machine_id, f"docker unavailable: {res.error_message()}"
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
