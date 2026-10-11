"""WebSocket ``/ws`` : état en direct et logs.

Serveur → client :

- ``{"type": "snapshot", "seq", "inventory", "runs", "ui_state", "server_time"}`` à la
  connexion et sur demande (``snapshot.request``) ;
- événements ``{"type": "run.created" | "run.updated" | "run.deleted" | "inventory.updated"
  | "machine.status" | "ui_state.updated", "seq", ...data}`` ;
- logs en **trames binaires** : 8 octets d'en-tête (id du run en ASCII, complété par des
  espaces) suivis des octets bruts ;
- ``{"type": "logs.reset", "run_id"}`` (vider le terminal avant un nouveau contenu) et
  ``{"type": "logs.end", "run_id"}`` (le conteneur est terminé).

Client → serveur : ``logs.subscribe {run_id}``, ``logs.unsubscribe {run_id}``,
``snapshot.request``, ``ping``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..service import RunService, ServiceError
from ..service.events import Event, Subscription
from ..service.log_hub import LogSubscriber
from .auth import websocket_authorized

log = logging.getLogger("trainhub.ws")
router = APIRouter()

HEADER_SIZE = 8
MAX_FRAME = 64 * 1024


def log_frame(run_id: str, data: bytes) -> bytes:
    """Trame binaire de logs : en-tête de 8 octets (id du run) + données."""
    header = run_id.encode("ascii", "replace")[:HEADER_SIZE].ljust(HEADER_SIZE, b" ")
    return header + data


def parse_log_frame(frame: bytes) -> tuple[str, bytes]:
    return frame[:HEADER_SIZE].decode("ascii").rstrip(), frame[HEADER_SIZE:]


class _Connection:
    def __init__(self, ws: WebSocket, svc: RunService) -> None:
        self.ws = ws
        self.svc = svc
        self.out: asyncio.Queue[str | bytes | None] = asyncio.Queue(maxsize=4096)
        self.logs: dict[str, tuple[LogSubscriber, asyncio.Task[None]]] = {}
        self.sub: Subscription | None = None
        self.min_seq = 0

    async def send_json(self, data: dict[str, Any]) -> None:
        await self.out.put(json.dumps(data, default=str, ensure_ascii=False))

    async def writer(self) -> None:
        while True:
            item = await self.out.get()
            if item is None:
                return
            if isinstance(item, bytes):
                await self.ws.send_bytes(item)
            else:
                await self.ws.send_text(item)

    async def snapshot(self) -> None:
        snap = await self.svc.snapshot()
        self.min_seq = snap.seq
        await self.out.put(snap.model_dump_json())

    async def events(self) -> None:
        while True:
            self.sub = self.svc.bus.subscribe()
            await self.snapshot()
            async for ev in self.sub:
                if ev.seq <= self.min_seq:
                    continue
                await self.send_json(_event_json(ev))
            # abonné trop lent : nouvel abonnement et snapshot complet
            if not self.sub.overflowed:
                return

    async def subscribe_logs(self, run_id: str) -> None:
        if run_id in self.logs:
            return
        try:
            sub = await self.svc.subscribe_logs(run_id)
        except ServiceError as exc:
            await self.send_json({"type": "error", "message": exc.message, "run_id": run_id})
            return
        task = asyncio.get_running_loop().create_task(self._pump_logs(sub))
        self.logs[run_id] = (sub, task)

    async def _pump_logs(self, sub: LogSubscriber) -> None:
        async for msg in sub.messages():
            if msg.kind == "data":
                data = msg.data
                for i in range(0, len(data), MAX_FRAME):
                    await self.out.put(log_frame(sub.run_id, data[i : i + MAX_FRAME]))
            elif msg.kind == "reset":
                await self.send_json({"type": "logs.reset", "run_id": sub.run_id})
            elif msg.kind == "end" and not sub.closed:
                await self.send_json({"type": "logs.end", "run_id": sub.run_id})

    def unsubscribe_logs(self, run_id: str) -> None:
        entry = self.logs.pop(run_id, None)
        if entry is not None:
            sub, task = entry
            self.svc.unsubscribe_logs(sub)
            task.cancel()

    async def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "logs.subscribe":
            await self.subscribe_logs(str(msg.get("run_id", "")))
        elif kind == "logs.unsubscribe":
            self.unsubscribe_logs(str(msg.get("run_id", "")))
        elif kind == "snapshot.request":
            await self.snapshot()
        elif kind == "ping":
            await self.send_json({"type": "pong", "t": msg.get("t")})
        else:
            await self.send_json({"type": "error", "message": f"message inconnu : {kind!r}"})

    def close(self) -> None:
        for run_id in list(self.logs):
            self.unsubscribe_logs(run_id)
        if self.sub is not None:
            self.svc.bus.unsubscribe(self.sub)


def _event_json(ev: Event) -> dict[str, Any]:
    return {"type": ev.type, "seq": ev.seq, **ev.data}


@router.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    if not websocket_authorized(ws):
        await ws.close(code=4401, reason="jeton requis")
        return
    await ws.accept()
    svc: RunService = ws.app.state.service
    conn = _Connection(ws, svc)
    writer = asyncio.get_running_loop().create_task(conn.writer())
    events = asyncio.get_running_loop().create_task(conn.events())
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await conn.send_json({"type": "error", "message": "JSON invalide"})
                continue
            if isinstance(msg, dict):
                await conn.handle(msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("erreur WebSocket")
    finally:
        conn.close()
        for t in (events, writer):
            t.cancel()
        for t in (events, writer):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
