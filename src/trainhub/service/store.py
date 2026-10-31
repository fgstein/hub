"""SQLite persistence (aiosqlite): runs, overridden project commands, UI state."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from ..core.models import ExternalRun, Run, RunStatus

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
    stop_requested INTEGER NOT NULL DEFAULT 0,
    container_alive INTEGER NOT NULL DEFAULT 0,
    params TEXT NOT NULL DEFAULT '{}',
    checkpoint TEXT,
    mlflow TEXT,
    series TEXT,
    unseen_failure INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS runs_project ON runs(project_id);
CREATE INDEX IF NOT EXISTS runs_machine ON runs(machine_id);
CREATE TABLE IF NOT EXISTS project_commands (
    project_id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS external_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    hidden INTEGER NOT NULL DEFAULT 0,
    data TEXT NOT NULL
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
    "container_alive",
    "params",
    "checkpoint",
    "mlflow",
    "series",
    "unseen_failure",
]
#: Columns added after the first version (migrated on open).
_ADDED_COLUMNS = {
    "container_alive": "INTEGER NOT NULL DEFAULT 0",
    "params": "TEXT NOT NULL DEFAULT '{}'",
    "checkpoint": "TEXT",
    "mlflow": "TEXT",
    "series": "TEXT",
    "unseen_failure": "INTEGER NOT NULL DEFAULT 0",
}
_BOOL_COLS = {"stop_requested", "container_alive", "unseen_failure"}
_JSON_COLS = {"full_command", "overrides", "params", "mlflow", "series"}
_NULLABLE_JSON_COLS = {"mlflow", "series"}
_DT_COLS = {"created_at", "started_at", "finished_at", "paused_at"}


def _to_row(run: Run) -> list[Any]:
    d = run.model_dump(mode="python")
    as_json = run.model_dump(mode="json", include=_JSON_COLS)
    row: list[Any] = []
    for col in _COLUMNS:
        v = d[col]
        if col in _JSON_COLS:
            v = json.dumps(as_json[col])
        elif col in _DT_COLS:
            v = v.isoformat() if isinstance(v, datetime) else None
        elif col in _BOOL_COLS:
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
            if col in _NULLABLE_JSON_COLS:
                v = json.loads(v) if v else None
            else:
                v = json.loads(v) if v else ({} if col == "params" else [])
        elif col in _BOOL_COLS:
            v = bool(v)
        d[col] = v
    return Run.model_validate(d)


class Store:
    """SQLite database access. A single connection, serialized by aiosqlite."""

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
        async with self._db.execute("PRAGMA table_info(runs)") as cur:
            existing = {row[1] for row in await cur.fetchall()}
        for col, decl in _ADDED_COLUMNS.items():
            if col not in existing:
                await self._db.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("store not open")
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

    async def count_runs_by_config(self, project: str) -> dict[str, Any]:
        """Number of runs per config of a project: ``{config: {"total", "active"}}``."""
        active = [str(s) for s in RunStatus if s.is_active]
        sql = (
            "SELECT config, COUNT(*) AS total, "
            f"SUM(CASE WHEN status IN ({','.join('?' for _ in active)}) THEN 1 ELSE 0 END) "
            "AS active FROM runs WHERE project_id = ? GROUP BY config"
        )
        async with self.db.execute(sql, [*active, project]) as cur:
            rows = await cur.fetchall()
        return {r["config"]: {"total": r["total"], "active": r["active"] or 0} for r in rows}

    async def count_runs_by_checkpoint(self) -> dict[str, Any]:
        """Number of runs per checkpoint (path on the machine): ``{path: {"total", "active"}}``."""
        active = [str(s) for s in RunStatus if s.is_active]
        sql = (
            "SELECT checkpoint, COUNT(*) AS total, "
            f"SUM(CASE WHEN status IN ({','.join('?' for _ in active)}) THEN 1 ELSE 0 END) "
            "AS active FROM runs WHERE checkpoint IS NOT NULL GROUP BY checkpoint"
        )
        async with self.db.execute(sql, active) as cur:
            rows = await cur.fetchall()
        return {r["checkpoint"]: {"total": r["total"], "active": r["active"] or 0} for r in rows}

    async def delete_run(self, run_id: str) -> None:
        await self.db.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        await self.db.commit()

    # -- external runs (found in MLflow, not launched by TrainHub) ---------------

    async def save_external(self, run: ExternalRun, *, hidden: bool = False) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO external_runs VALUES (?, ?, ?, ?)",
            (run.id, run.project_id, int(hidden), run.model_dump_json()),
        )
        await self.db.commit()

    async def get_external(self, run_id: str) -> tuple[ExternalRun, bool] | None:
        """The external run and whether it is hidden (deleted from TrainHub)."""
        async with self.db.execute(
            "SELECT data, hidden FROM external_runs WHERE id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
        return (ExternalRun.model_validate_json(row["data"]), bool(row["hidden"])) if row else None

    async def list_external(self, *, hidden: bool = False) -> list[ExternalRun]:
        """External runs shown (``hidden``: those deleted from TrainHub instead)."""
        async with self.db.execute(
            "SELECT data FROM external_runs WHERE hidden = ?", (int(hidden),)
        ) as cur:
            rows = await cur.fetchall()
        return [ExternalRun.model_validate_json(r["data"]) for r in rows]

    async def external_ids(self) -> set[str]:
        """Ids of every external run known, hidden or not."""
        async with self.db.execute("SELECT id FROM external_runs") as cur:
            return {r["id"] for r in await cur.fetchall()}

    async def forget_external(self, run_id: str) -> None:
        await self.db.execute("DELETE FROM external_runs WHERE id = ?", (run_id,))
        await self.db.commit()

    # -- project commands ------------------------------------------------------

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

    # -- key/value (UI state) -------------------------------------------------

    async def get_kv(self, key: str) -> Any:
        async with self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return json.loads(row["value"]) if row else None

    async def set_kv(self, key: str, value: Any) -> None:
        await self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))
        await self.db.commit()
