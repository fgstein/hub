"""Current inventory with hot reload (watching the modification time)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path

from ..core.inventory import InventoryError, load_inventory, parse_inventory
from ..core.models import Inventory

log = logging.getLogger("trainhub.inventory")


class InventoryManager:
    """Holds the most recent valid inventory; reloads when the file changes.

    A file that became invalid interrupts nothing: the previous inventory stays active and
    the error is exposed in :attr:`error`.
    """

    def __init__(self, path: str | Path | None = None, *, text: str | None = None) -> None:
        self.path = Path(path) if path is not None else None
        if text is not None:
            self.inventory = parse_inventory(text, "<inline>")
        elif self.path is not None:
            self.inventory = load_inventory(self.path)
        else:
            self.inventory = Inventory()
        self.error: str | None = None
        self._mtime = self._stat()
        self._listeners: list[Callable[[Inventory], None]] = []
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def from_inventory(cls, inventory: Inventory) -> InventoryManager:
        mgr = cls()
        mgr.inventory = inventory
        return mgr

    def _stat(self) -> float | None:
        if self.path is None:
            return None
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def on_change(self, cb: Callable[[Inventory], None]) -> None:
        self._listeners.append(cb)

    def check(self) -> bool:
        """Reload if the file changed; return ``True`` if the inventory changed."""
        if self.path is None:
            return False
        mtime = self._stat()
        if mtime == self._mtime:
            return False
        self._mtime = mtime
        try:
            inv = load_inventory(self.path)
        except InventoryError as exc:
            self.error = str(exc)
            log.error("invalid inventory, previous inventory kept", extra={"error": str(exc)})
            return False
        self.error = None
        if inv == self.inventory:
            return False
        self.inventory = inv
        log.info("inventory reloaded", extra={"path": str(self.path)})
        for cb in self._listeners:
            cb(inv)
        return True

    def start(self, interval: float = 1.0) -> None:
        if self.path is not None and self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._watch(interval))

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _watch(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                self.check()
            except Exception:  # pragma: no cover
                log.exception("error while reloading the inventory")
