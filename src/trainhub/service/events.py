"""Internal event bus (asynchronous publish/subscribe, sequence numbers)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("trainhub.events")


@dataclass(frozen=True)
class Event:
    """Published event: ``type`` (e.g. ``run.updated``), serializable ``data``, ``seq``."""

    type: str
    data: dict[str, Any]
    seq: int

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type, "seq": self.seq, "data": self.data}


@dataclass(eq=False)
class Subscription:
    """Bus subscription; ``overflowed`` becomes ``True`` if the subscriber is too slow."""

    queue: asyncio.Queue[Event | None] = field(default_factory=lambda: asyncio.Queue(10_000))
    overflowed: bool = False

    async def __aiter__(self) -> AsyncIterator[Event]:
        while True:
            ev = await self.queue.get()
            if ev is None:
                return
            yield ev


class EventBus:
    """Broadcast events to every subscriber."""

    def __init__(self) -> None:
        self._subs: set[Subscription] = set()
        self.seq = 0

    def publish(self, type_: str, data: dict[str, Any]) -> Event:
        self.seq += 1
        ev = Event(type_, data, self.seq)
        for sub in list(self._subs):
            try:
                sub.queue.put_nowait(ev)
            except asyncio.QueueFull:
                # Subscriber too slow: drop it, it will have to resync (snapshot).
                log.warning("subscriber too slow, unsubscribed")
                sub.overflowed = True
                self._subs.discard(sub)
                while not sub.queue.empty():
                    sub.queue.get_nowait()
                sub.queue.put_nowait(None)
        return ev

    def subscribe(self) -> Subscription:
        sub = Subscription()
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subs.discard(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)
