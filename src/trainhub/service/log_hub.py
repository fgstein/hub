"""Hub de logs : un seul ``docker logs`` par run, partagé entre abonnés et sauvegardé sur disque.

- tous les runs actifs sont suivis en permanence (même sans terminal ouvert) et leurs logs
  sont écrits au fil de l'eau dans ``<log_dir>/<run_id>.log`` : le fichier reste lisible quand
  le conteneur est supprimé, que la machine est injoignable ou que TrainHub redémarre ;
- tampon circulaire (2 Mo par défaut) envoyé à chaque nouvel abonné, puis le flux direct ;
  pour un run dont le suivi n'est pas actif, le tampon est rechargé depuis le fichier ;
- quand le suivi redémarre (relance du conteneur, retour d'une machine), ``docker logs``
  renvoie tout l'historique : le tampon est vidé, un message ``reset`` est envoyé aux abonnés
  et le fichier est réécrit (voir :class:`_LogFile`) ;
- le suivi d'un run terminé dont le conteneur reste démarré est arrêté par :meth:`finish` ;
  un fichier ``<run_id>.done`` indique alors que la sauvegarde est complète ;
- un canal sans abonné est oublié ``idle_timeout`` secondes (30 s) après la fin du run.

Les logs restent des octets bruts (ANSI et ``\\r`` compris).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Literal

from ..executors.base import LogStream

log = logging.getLogger("trainhub.logs")

BUFFER_SIZE = 2 * 1024 * 1024
IDLE_TIMEOUT_S = 30.0
RETRY_DELAY_S = 3.0
#: Délai laissé au flux pour livrer les dernières lignes quand un run se termine.
FINISH_GRACE_S = 1.5

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

LogKind = Literal["data", "reset", "end"]


class LogsGoneError(Exception):
    """Les logs ne sont plus disponibles côté Docker (conteneur supprimé)."""


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


class _LogFile:
    """Sauvegarde d'un run sur disque, réécrite à chaque session de suivi.

    Une session écrit d'abord dans ``<id>.log.part`` ; dès qu'elle a rattrapé la taille de
    l'ancien fichier, elle le remplace (renommage atomique) et l'écriture continue au même
    endroit, sans tampon. Si la session se termine plus courte que l'ancien fichier (logs
    Docker tronqués par une rotation, conteneur recréé…), l'ancien est conservé sous
    ``<id>.prev.log``. Une session interrompue avant d'avoir rattrapé l'ancien fichier est
    abandonnée : l'ancien reste la référence.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.part = path.with_name(path.name + ".part")
        self.prev = path.with_name(path.stem + ".prev.log")
        self.done_marker = path.with_name(path.stem + ".done")
        self.fh: BinaryIO | None = None
        self.size = 0
        self.old_size = 0
        self.promoted = False

    @property
    def complete(self) -> bool:
        return self.done_marker.exists()

    def _warn(self, msg: str, exc: OSError) -> None:
        log.warning(msg, extra={"path": str(self.path), "error": str(exc)})

    def tail(self, size: int) -> bytes:
        try:
            with self.path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                end = f.tell()
                f.seek(max(0, end - size))
                return f.read()
        except OSError:
            return b""

    def begin(self) -> None:
        self.close(final=False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.done_marker.unlink(missing_ok=True)
            self.old_size = self.path.stat().st_size if self.path.exists() else 0
            self.fh = self.part.open("wb", buffering=0)
        except OSError as exc:
            self._warn("sauvegarde des logs impossible", exc)
            self.fh = None
            return
        self.size = 0
        self.promoted = False
        if self.old_size == 0:
            self._promote()

    def _promote(self) -> None:
        with contextlib.suppress(OSError):
            os.replace(self.part, self.path)
        self.promoted = True

    def write(self, data: bytes) -> None:
        if self.fh is None:
            return
        try:
            self.fh.write(data)
        except OSError as exc:
            self._warn("écriture des logs impossible", exc)
            return
        self.size += len(data)
        if not self.promoted and self.size >= self.old_size:
            self._promote()

    def close(self, *, final: bool) -> None:
        """Termine la session ; ``final`` : le run est fini, la sauvegarde est complète."""
        if self.fh is None:
            return
        with contextlib.suppress(OSError):
            self.fh.close()
        self.fh = None
        try:
            if not self.promoted:
                if final and self.size > 0:
                    os.replace(self.path, self.prev)
                    os.replace(self.part, self.path)
                else:
                    self.part.unlink(missing_ok=True)
            if final:
                self.done_marker.touch()
        except OSError as exc:
            self._warn("finalisation des logs impossible", exc)


@dataclass(eq=False)
class _Channel:
    run_id: str
    file: _LogFile | None = None
    buffer: bytearray = field(default_factory=bytearray)
    subscribers: set[LogSubscriber] = field(default_factory=set)
    task: asyncio.Task[None] | None = None
    stream: LogStream | None = None
    idle_task: asyncio.Task[None] | None = None
    finish_task: asyncio.Task[None] | None = None
    complete: bool = False

    def broadcast(self, msg: LogMessage) -> None:
        for sub in self.subscribers:
            sub.queue.put_nowait(msg)

    @property
    def following(self) -> bool:
        return self.task is not None and not self.task.done()


OpenStream = Callable[[str], Awaitable[LogStream]]
IsFinished = Callable[[str], Awaitable[bool]]


class LogHub:
    """Partage les flux ``docker logs`` entre abonnés et les sauvegarde dans ``log_dir``."""

    def __init__(
        self,
        open_stream: OpenStream,
        is_finished: IsFinished,
        *,
        log_dir: Path | None = None,
        buffer_size: int = BUFFER_SIZE,
        idle_timeout: float = IDLE_TIMEOUT_S,
        retry_delay: float = RETRY_DELAY_S,
        finish_grace: float = FINISH_GRACE_S,
    ) -> None:
        self._open = open_stream
        self._is_finished = is_finished
        self.log_dir = log_dir
        self.buffer_size = buffer_size
        self.idle_timeout = idle_timeout
        self.retry_delay = retry_delay
        self.finish_grace = finish_grace
        self._channels: dict[str, _Channel] = {}

    # -- fichiers -------------------------------------------------------------------

    def log_path(self, run_id: str) -> Path | None:
        """Fichier de sauvegarde d'un run (``None`` sans répertoire ou id non sûr)."""
        if self.log_dir is None or not _SAFE_ID.match(run_id):
            return None
        return self.log_dir / f"{run_id}.log"

    def read_file(self, run_id: str) -> bytes | None:
        path = self.log_path(run_id)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def _channel(self, run_id: str) -> _Channel:
        ch = self._channels.get(run_id)
        if ch is None:
            ch = self._channels[run_id] = _Channel(run_id)
            path = self.log_path(run_id)
            if path is not None:
                ch.file = _LogFile(path)
                ch.buffer += ch.file.tail(self.buffer_size)
                ch.complete = ch.file.complete
        return ch

    # -- abonnements -------------------------------------------------------------

    async def subscribe(self, run_id: str) -> LogSubscriber:
        ch = self._channel(run_id)
        if ch.idle_task is not None:
            ch.idle_task.cancel()
            ch.idle_task = None
        sub = LogSubscriber(run_id)
        if ch.buffer:
            sub.queue.put_nowait(LogMessage("data", bytes(ch.buffer)))
        ch.subscribers.add(sub)
        if ch.complete:
            sub.queue.put_nowait(LogMessage("end"))
        elif not ch.following:
            self._start(ch)
        return sub

    def unsubscribe(self, sub: LogSubscriber) -> None:
        sub.closed = True
        sub.queue.put_nowait(LogMessage("end"))
        ch = self._channels.get(sub.run_id)
        if ch is None:
            return
        ch.subscribers.discard(sub)
        self._schedule_idle(ch)

    async def stream(self, run_id: str, *, until_end: bool = True) -> AsyncIterator[bytes]:
        """Itère sur les logs d'un run (tampon puis direct)."""
        sub = await self.subscribe(run_id)
        try:
            async for chunk in sub.chunks(until_end=until_end):
                yield chunk
        finally:
            self.unsubscribe(sub)

    # -- contrôle ------------------------------------------------------------------

    def follow(self, run_id: str) -> None:
        """Suit (et sauvegarde) les logs d'un run actif, abonnés ou non."""
        ch = self._channel(run_id)
        if ch.finish_task is not None and not ch.finish_task.done():
            return
        if not ch.following:
            ch.complete = False
            self._start(ch)

    def restart(self, run_id: str) -> None:
        """Nouvelle exécution du conteneur (``docker start``) : reprend le suivi depuis le début."""
        ch = self._channel(run_id)
        if ch.finish_task is not None:
            ch.finish_task.cancel()
            ch.finish_task = None
        ch.complete = False
        self._start(ch)

    def finish(self, run_id: str) -> None:
        """Le run est terminé : arrête le suivi après un court délai et clôt la sauvegarde.

        Nécessaire quand le conteneur reste démarré (``docker logs -f`` ne se termine pas).
        """
        ch = self._channels.get(run_id)
        if ch is None or ch.complete:
            return
        if ch.finish_task is None or ch.finish_task.done():
            ch.finish_task = asyncio.get_running_loop().create_task(self._finish_later(ch))

    def discard(self, run_id: str) -> None:
        """Oublie un run (supprimé) : termine les abonnés. Le fichier de logs est conservé."""
        ch = self._channels.pop(run_id, None)
        if ch is None:
            return
        ch.broadcast(LogMessage("end"))
        self._stop_follower(ch)
        if ch.finish_task is not None:
            ch.finish_task.cancel()
        if ch.file is not None:
            ch.file.close(final=False)

    def buffer(self, run_id: str) -> bytes | None:
        ch = self._channels.get(run_id)
        return bytes(ch.buffer) if ch is not None else None

    def is_following(self, run_id: str) -> bool:
        ch = self._channels.get(run_id)
        return ch is not None and ch.following

    def subscriber_count(self, run_id: str) -> int:
        ch = self._channels.get(run_id)
        return len(ch.subscribers) if ch else 0

    async def close(self) -> None:
        for ch in list(self._channels.values()):
            task = ch.task
            self._drop(ch)
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if ch.file is not None:
                ch.file.close(final=False)

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
        for t in (ch.idle_task, ch.finish_task):
            if t is not None:
                t.cancel()
        ch.idle_task = ch.finish_task = None
        if self._channels.get(ch.run_id) is ch:
            del self._channels[ch.run_id]

    def _schedule_idle(self, ch: _Channel) -> None:
        if not ch.subscribers and ch.complete and ch.idle_task is None:
            ch.idle_task = asyncio.get_running_loop().create_task(self._idle_stop(ch))

    async def _idle_stop(self, ch: _Channel) -> None:
        await asyncio.sleep(self.idle_timeout)
        if not ch.subscribers and ch.complete:
            ch.idle_task = None
            self._drop(ch)
            log.debug("canal de logs oublié", extra={"run_id": ch.run_id})

    def _complete(self, ch: _Channel, *, saved: bool) -> None:
        """Fin du suivi d'un run terminé ; ``saved`` : la sauvegarde est complète."""
        if ch.file is not None:
            if ch.file.fh is not None:
                ch.file.close(final=saved)
            elif saved and ch.file.path.exists():
                with contextlib.suppress(OSError):
                    ch.file.done_marker.touch()
        ch.complete = True
        ch.broadcast(LogMessage("end"))
        self._schedule_idle(ch)

    async def _finish_later(self, ch: _Channel) -> None:
        await asyncio.sleep(self.finish_grace)
        task = ch.task
        if task is not None and not task.done():
            self._stop_follower(ch)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if not ch.complete:
            self._complete(ch, saved=True)

    def _append(self, ch: _Channel, data: bytes) -> None:
        ch.buffer += data
        excess = len(ch.buffer) - self.buffer_size
        if excess > 0:
            del ch.buffer[:excess]
        if ch.file is not None:
            ch.file.write(data)
        ch.broadcast(LogMessage("data", data))

    async def _follow(self, ch: _Channel) -> None:
        while True:
            stream: LogStream | None = None
            ended = False
            try:
                stream = await self._open(ch.run_id)
                ch.stream = stream
                # docker logs renvoie tout l'historique : on repart de zéro.
                ch.buffer.clear()
                ch.broadcast(LogMessage("reset"))
                if ch.file is not None:
                    ch.file.begin()
                async for chunk in stream:
                    self._append(ch, chunk)
                ended = True
            except asyncio.CancelledError:
                if stream is not None:
                    await stream.close()
                raise
            except LogsGoneError:
                # Conteneur supprimé : le fichier (s'il existe) est tout ce qui reste.
                self._complete(ch, saved=True)
                return
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
                # Sauvegarde complète seulement si le flux est allé au bout.
                self._complete(ch, saved=ended)
                return
            # Run toujours actif (machine injoignable, connexion coupée) : on réessaie.
            if ch.file is not None:
                ch.file.close(final=False)
            await asyncio.sleep(self.retry_delay)
