"""Persistance SQLite (aiosqlite) : runs, commandes de projets surchargées, état d'interface."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from ..core.models import Run, RunStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    machine_id TEXT NOT NULL,
    image_id TEXT NOT NULL,
    venv_id TEXT NOT NULL,
    config TEXT NOT NULL,
    command TEXT NOT NULL,
    full_command TEXT NOT NULL,
    container_name TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    paused_total_s REAL NOT NULL DEFAULT 0,
    paused_at TEXT,
    error TEXT,
    source TEXT NOT NULL,
    overrides TEXT NOT NULL DEFAULT '[]',
    cuda_visible_devices TEXT,
    stop_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS runs_project ON runs(project_id);
CREATE INDEX IF NOT EXISTS runs_machine ON runs(machine_id);
CREATE TABLE IF NOT EXISTS project_commands (
    project_id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_COLUMNS = [
    "id",
    "project_id",
    "machine_id",
    "image_id",
    "venv_id",
    "config",
    "command",
    "full_command",
    "container_name",
    "status",
    "exit_code",
    "created_at",
    "started_at",
    "finished_at",
    "paused_total_s",
    "paused_at",
    "error",
    "source",
    "overrides",
    "cuda_visible_devices",
    "stop_requested",
]
_JSON_COLS = {"full_command", "overrides"}
_DT_COLS = {"created_at", "started_at", "finished_at", "paused_at"}


def _to_row(run: Run) -> list[Any]:
    d = run.model_dump(mode="python")
    row: list[Any] = []
    for col in _COLUMNS:
        v = d[col]
        if col in _JSON_COLS:
            v = json.dumps(v)
        elif col in _DT_COLS:
            v = v.isoformat() if isinstance(v, datetime) else None
        elif col == "stop_requested":
            v = int(bool(v))
        elif col in ("status", "source"):
            v = str(v)
        row.append(v)
    return row


def _from_row(row: aiosqlite.Row) -> Run:
    d: dict[str, Any] = {}
    for col in _COLUMNS:
        v = row[col]
        if col in _JSON_COLS:
            v = json.loads(v) if v else []
        elif col == "stop_requested":
            v = bool(v)
        d[col] = v
    return Run.model_validate(d)


class Store:
    """Accès à la base SQLite. Une seule connexion, sérialisée par aiosqlite."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store non ouvert")
        return self._db

    # -- runs ------------------------------------------------------------------

    async def save_run(self, run: Run) -> None:
        placeholders = ",".join("?" for _ in _COLUMNS)
        await self.db.execute(
            f"INSERT OR REPLACE INTO runs ({','.join(_COLUMNS)}) VALUES ({placeholders})",
            _to_row(run),
        )
        await self.db.commit()

    async def get_run(self, run_id: str) -> Run | None:
        async with self.db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)) as cur:
            row = await cur.fetchone()
        return _from_row(row) if row else None

    async def list_runs(
        self,
        *,
        project: str | None = None,
        machine: str | None = None,
        statuses: set[RunStatus] | None = None,
        limit: int | None = None,
    ) -> list[Run]:
        sql = "SELECT * FROM runs"
        where: list[str] = []
        args: list[Any] = []
        if project:
            where.append("project_id = ?")
            args.append(project)
        if machine:
            where.append("machine_id = ?")
            args.append(machine)
        if statuses:
            where.append(f"status IN ({','.join('?' for _ in statuses)})")
            args += [str(s) for s in statuses]
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        async with self.db.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [_from_row(r) for r in rows]

    async def delete_run(self, run_id: str) -> None:
        await self.db.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        await self.db.commit()

    # -- commandes de projets --------------------------------------------------

    async def get_project_commands(self) -> dict[str, str]:
        async with self.db.execute("SELECT project_id, command FROM project_commands") as cur:
            rows = await cur.fetchall()
        return {r["project_id"]: r["command"] for r in rows}

    async def set_project_command(self, project_id: str, command: str | None) -> None:
        if command is None:
            await self.db.execute(
                "DELETE FROM project_commands WHERE project_id = ?", (project_id,)
            )
        else:
            await self.db.execute(
                "INSERT OR REPLACE INTO project_commands VALUES (?, ?, datetime('now'))",
                (project_id, command),
            )
        await self.db.commit()

    # -- clé/valeur (état d'interface) ----------------------------------------

    async def get_kv(self, key: str) -> Any:
        async with self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return json.loads(row["value"]) if row else None

    async def set_kv(self, key: str, value: Any) -> None:
        await self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))
        await self.db.commit()
