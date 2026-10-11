"""Hub de logs : un seul ``docker logs -f`` par run observé, partagé entre abonnés.

- tampon circulaire (2 Mo par défaut) envoyé à chaque nouvel abonné, puis le flux direct ;
- le processus s'arrête ``idle_timeout`` secondes (30 s) après le départ du dernier abonné ;
- quand le suivi redémarre (relance du conteneur, retour d'une machine), le tampon est vidé
  et un message ``reset`` est envoyé aux abonnés avant le nouveau contenu.

Les logs restent des octets bruts (ANSI et ``\\r`` compris).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from ..executors.base import LogStream

log = logging.getLogger("trainhub.logs")

BUFFER_SIZE = 2 * 1024 * 1024
IDLE_TIMEOUT_S = 30.0
RETRY_DELAY_S = 3.0

LogKind = Literal["data", "reset", "end"]


@dataclass(frozen=True)
class LogMessage:
    kind: LogKind
    data: bytes = b""


@dataclass(eq=False)
class LogSubscriber:
    """Abonné à un flux de logs ; itérable de façon asynchrone."""

    run_id: str
    queue: asyncio.Queue[LogMessage] = field(default_factory=asyncio.Queue)
    closed: bool = False

    async def messages(self) -> AsyncIterator[LogMessage]:
        while not self.closed:
            msg = await self.queue.get()
            yield msg

    async def chunks(self, *, until_end: bool = True) -> AsyncIterator[bytes]:
        """Octets bruts ; s'arrête à la fin du conteneur si ``until_end``."""
        async for msg in self.messages():
            if msg.kind == "data":
                yield msg.data
            elif msg.kind == "end" and until_end:
                return


@dataclass(eq=False)
class _Channel:
    run_id: str
    buffer: bytearray = field(default_factory=bytearray)
    subscribers: set[LogSubscriber] = field(default_factory=set)
    task: asyncio.Task[None] | None = None
    stream: LogStream | None = None
    idle_task: asyncio.Task[None] | None = None
    complete: bool = False

    def broadcast(self, msg: LogMessage) -> None:
        for sub in self.subscribers:
            sub.queue.put_nowait(msg)


OpenStream = Callable[[str], Awaitable[LogStream]]
IsFinished = Callable[[str], Awaitable[bool]]


class LogHub:
    """Partage les flux ``docker logs -f`` entre abonnés."""

    def __init__(
        self,
        open_stream: OpenStream,
        is_finished: IsFinished,
        *,
        buffer_size: int = BUFFER_SIZE,
        idle_timeout: float = IDLE_TIMEOUT_S,
        retry_delay: float = RETRY_DELAY_S,
    ) -> None:
        self._open = open_stream
        self._is_finished = is_finished
        self.buffer_size = buffer_size
        self.idle_timeout = idle_timeout
        self.retry_delay = retry_delay
        self._channels: dict[str, _Channel] = {}

    # -- abonnements -------------------------------------------------------------

    async def subscribe(self, run_id: str) -> LogSubscriber:
        ch = self._channels.get(run_id)
        if ch is None:
            ch = self._channels[run_id] = _Channel(run_id)
        if ch.idle_task is not None:
            ch.idle_task.cancel()
            ch.idle_task = None
        sub = LogSubscriber(run_id)
        if ch.buffer:
            sub.queue.put_nowait(LogMessage("data", bytes(ch.buffer)))
        ch.subscribers.add(sub)
        if ch.complete:
            sub.queue.put_nowait(LogMessage("end"))
        elif ch.task is None or ch.task.done():
            self._start(ch)
        return sub

    def unsubscribe(self, sub: LogSubscriber) -> None:
        sub.closed = True
        sub.queue.put_nowait(LogMessage("end"))
        ch = self._channels.get(sub.run_id)
        if ch is None:
            return
        ch.subscribers.discard(sub)
        if not ch.subscribers and ch.idle_task is None:
            ch.idle_task = asyncio.get_running_loop().create_task(self._idle_stop(ch))

    async def stream(self, run_id: str, *, until_end: bool = True) -> AsyncIterator[bytes]:
        """Itère sur les logs d'un run (tampon puis direct)."""
        sub = await self.subscribe(run_id)
        try:
            async for chunk in sub.chunks(until_end=until_end):
                yield chunk
        finally:
            self.unsubscribe(sub)

    # -- contrôle ------------------------------------------------------------------

    def restart(self, run_id: str) -> None:
        """Relance le suivi (après ``docker start`` ou retour d'une machine)."""
        ch = self._channels.get(run_id)
        if ch is None or not ch.subscribers:
            if ch is not None:
                self._drop(ch)
            return
        ch.complete = False
        self._start(ch)

    def ensure_following(self, run_id: str) -> None:
        """Relance le suivi s'il n'est plus actif alors que des abonnés attendent."""
        ch = self._channels.get(run_id)
        if (
            ch is not None
            and ch.subscribers
            and not ch.complete
            and (ch.task is None or ch.task.done())
        ):
            self._start(ch)

    def discard(self, run_id: str) -> None:
        """Oublie un run (supprimé) : termine les abonnés."""
        ch = self._channels.pop(run_id, None)
        if ch is None:
            return
        ch.broadcast(LogMessage("end"))
        self._stop_follower(ch)

    def buffer(self, run_id: str) -> bytes | None:
        ch = self._channels.get(run_id)
        return bytes(ch.buffer) if ch is not None else None

    def is_following(self, run_id: str) -> bool:
        ch = self._channels.get(run_id)
        return ch is not None and ch.task is not None and not ch.task.done()

    def subscriber_count(self, run_id: str) -> int:
        ch = self._channels.get(run_id)
        return len(ch.subscribers) if ch else 0

    async def close(self) -> None:
        for ch in list(self._channels.values()):
            self._drop(ch)
            if ch.task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await ch.task

    # -- interne -------------------------------------------------------------------

    def _start(self, ch: _Channel) -> None:
        self._stop_follower(ch)
        ch.task = asyncio.get_running_loop().create_task(self._follow(ch))

    def _stop_follower(self, ch: _Channel) -> None:
        if ch.task is not None and not ch.task.done():
            ch.task.cancel()
        ch.task = None

    def _drop(self, ch: _Channel) -> None:
        self._stop_follower(ch)
        if ch.idle_task is not None:
            ch.idle_task.cancel()
            ch.idle_task = None
        if self._channels.get(ch.run_id) is ch:
            del self._channels[ch.run_id]

    async def _idle_stop(self, ch: _Channel) -> None:
        await asyncio.sleep(self.idle_timeout)
        if not ch.subscribers:
            ch.idle_task = None
            self._drop(ch)
            log.debug("suivi des logs arrêté", extra={"run_id": ch.run_id})

    def _append(self, ch: _Channel, data: bytes) -> None:
        ch.buffer += data
        excess = len(ch.buffer) - self.buffer_size
        if excess > 0:
            del ch.buffer[:excess]
        ch.broadcast(LogMessage("data", data))

    async def _follow(self, ch: _Channel) -> None:
        first = True
        while True:
            stream: LogStream | None = None
            try:
                stream = await self._open(ch.run_id)
                ch.stream = stream
                if first or ch.buffer:
                    ch.buffer.clear()
                    ch.broadcast(LogMessage("reset"))
                first = False
                async for chunk in stream:
                    self._append(ch, chunk)
            except asyncio.CancelledError:
                if stream is not None:
                    await stream.close()
                raise
            except Exception as exc:
                log.info("flux de logs interrompu", extra={"run_id": ch.run_id, "error": str(exc)})
            finally:
                ch.stream = None
            if stream is not None:
                await stream.close()
            try:
                finished = await self._is_finished(ch.run_id)
            except Exception:
                finished = True
            if finished:
                ch.complete = True
                ch.broadcast(LogMessage("end"))
                return
            # Run toujours actif (machine injoignable, connexion coupée) : on réessaie.
            await asyncio.sleep(self.retry_delay)
            if not ch.subscribers:
                return
