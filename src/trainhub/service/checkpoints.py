"""Checkpoint sources: scan (one ``find`` per source, cached), listing and deletion."""

from __future__ import annotations

import logging
import posixpath
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..core import checkpoints as ck
from ..core.models import CheckpointSource, UnknownResourceError
from ..core.status import utcnow
from ..executors.base import ExecutorError, MachineUnreachableError
from .schemas import (
    CheckpointDeleteResult,
    CheckpointFile,
    CheckpointGroup,
    CheckpointListing,
    CheckpointRun,
    CheckpointSourceView,
    RunCount,
)

if TYPE_CHECKING:
    from .runs import RunService

log = logging.getLogger("trainhub.checkpoints")

#: A scan can be long on a big shared disk: cached, refreshed on demand (↻) after that.
CHECKPOINT_CACHE_TTL_S = 300.0
SCAN_TIMEOUT_S = 60.0
DELETE_TIMEOUT_S = 120.0


@dataclass
class _Scan:
    at_monotonic: float
    at: datetime
    entries: list[ck.Entry]
    error: str | None


class CheckpointService:
    """Checkpoint tab: what is on disk, whatever launched the runs."""

    def __init__(self, svc: RunService) -> None:
        self.svc = svc
        self._cache: dict[str, _Scan] = {}

    def clear(self) -> None:
        self._cache.clear()

    def _source(self, source_id: str) -> CheckpointSource:
        from .runs import NotFoundError

        try:
            return self.svc.inventory.checkpoint_source(source_id)
        except UnknownResourceError as exc:
            raise NotFoundError(str(exc)) from exc

    # -- views ---------------------------------------------------------------------------

    def source_view(self, src: CheckpointSource) -> CheckpointSourceView:
        scan = self._cache.get(src.id)
        files = [e for e in scan.entries if e.kind == "f"] if scan and not scan.error else None
        return CheckpointSourceView(
            id=src.id,
            label=src.display,
            machine=src.machine,
            root=src.root,
            layout=src.layout,
            size=sum(e.size for e in files) if files is not None else None,
            count=len(files) if files is not None else None,
            scanned_at=scan.at if scan else None,
        )

    def sources(self) -> list[CheckpointSourceView]:
        return [self.source_view(s) for s in self.svc.inventory.checkpoints]

    async def listing(self, source_id: str, *, refresh: bool = False) -> CheckpointListing:
        """Runs and checkpoints of a source (scan cached 5 min, ``refresh`` forces it)."""
        src = self._source(source_id)
        scan = await self._scan(src, refresh=refresh)
        counts = await self.svc.store.count_runs_by_checkpoint()
        return build_listing(src, scan.entries, counts, error=scan.error, scanned_at=scan.at)

    async def _scan(self, src: CheckpointSource, *, refresh: bool) -> _Scan:
        hit = self._cache.get(src.id)
        if (
            hit is not None
            and not refresh
            and (time.monotonic() - hit.at_monotonic < CHECKPOINT_CACHE_TTL_S)
        ):
            return hit
        layout = ck.parse_layout(src.layout)
        machine = self.svc._machine(src.machine)
        error: str | None = None
        entries: list[ck.Entry] = []
        try:
            res = await self.svc.executor.exec(
                machine, ck.list_checkpoints(src.root, layout), timeout=SCAN_TIMEOUT_S
            )
            entries = ck.parse_listing(res.out)
            # find exits non-zero on an unreadable subdirectory: keep what was listed.
            if not res.ok and not entries:
                error = f"checkpoint scan failed: {res.error_message()}"
        except (MachineUnreachableError, ExecutorError) as exc:
            error = str(exc)
        if error and hit is not None:
            # Keep the last good listing, with the error.
            scan = _Scan(hit.at_monotonic, hit.at, hit.entries, error)
        else:
            scan = _Scan(time.monotonic(), utcnow(), entries, error)
            self._cache[src.id] = scan
        self._publish(src)
        return scan

    # -- deletion ------------------------------------------------------------------------

    async def delete(self, source_id: str, paths: list[str]) -> CheckpointDeleteResult:
        """Delete checkpoint files of a source; group directories left empty are removed.

        Each path must be a checkpoint of a run of the source (relative, under a run
        directory, matching the file pattern of the layout); the machine checks again that
        it resolves under the root before deleting it.
        """
        from .runs import InvalidRequestError, UnreachableError

        src = self._source(source_id)
        layout = ck.parse_layout(src.layout)
        files = list(dict.fromkeys(p.strip() for p in paths))
        bad = [p for p in files if not ck.valid_relpath(p) or ck.place(p, layout) is None]
        if bad:
            raise InvalidRequestError(
                f"not a checkpoint of source '{src.id}' (pattern {layout.file}, under a run "
                f"directory): {', '.join(repr(b) for b in bad[:5])}"
            )
        dirs = sorted(
            {d for p in files if ck.is_group_dir(d := posixpath.dirname(p), layout)},
            key=lambda d: (-d.count("/"), d),
        )
        machine = self.svc._machine(src.machine)
        try:
            res = await self.svc.executor.exec(
                machine, ck.delete_checkpoints(src.root, files, dirs), timeout=DELETE_TIMEOUT_S
            )
        except MachineUnreachableError as exc:
            raise UnreachableError(str(exc)) from exc
        except ExecutorError as exc:
            raise InvalidRequestError(f"deletion failed: {exc}") from exc
        outcome = ck.parse_delete_output(res.out, files)
        if outcome.root_missing:
            raise InvalidRequestError(f"root of source '{src.id}' not found: {src.root}")
        log.info(
            "checkpoints deleted",
            extra={
                "source": src.id,
                "deleted": len(outcome.deleted),
                "failed": len(outcome.failed),
            },
        )
        freed = 0
        scan = self._cache.get(src.id)
        if scan is not None:
            gone = set(outcome.deleted)
            removed = set(outcome.removed_dirs)
            freed = sum(e.size for e in scan.entries if e.kind == "f" and e.path in gone)
            scan.entries = [
                e
                for e in scan.entries
                if not (e.kind == "f" and e.path in gone)
                and not (e.kind == "d" and e.path in removed)
            ]
        self._publish(src)
        view = self.source_view(src)
        return CheckpointDeleteResult(
            source=src.id,
            deleted=outcome.deleted,
            failed=outcome.failed,
            removed_dirs=outcome.removed_dirs,
            freed=freed,
            size=view.size or 0,
            count=view.count or 0,
        )

    def _publish(self, src: CheckpointSource) -> None:
        self.svc.bus.publish(
            "checkpoints.updated", {"source": self.source_view(src).model_dump(mode="json")}
        )


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def build_listing(
    src: CheckpointSource,
    entries: list[ck.Entry],
    counts: dict[str, dict[str, int]],
    *,
    error: str | None = None,
    scanned_at: datetime | None = None,
) -> CheckpointListing:
    """Scan entries → tree run / group / checkpoint, runs most recent first."""
    layout = ck.parse_layout(src.layout)

    def ckpt(e: ck.Entry) -> CheckpointFile:
        c = counts.get(posixpath.join(src.root, e.path))
        return CheckpointFile(
            path=e.path,
            name=e.parts[-1],
            size=e.size,
            mtime=_dt(e.mtime),
            runs=RunCount(**c) if c else None,
        )

    run_dirs: dict[str, float] = {}
    groups: dict[str, dict[str, list[CheckpointFile]]] = {}
    others: dict[str, list[CheckpointFile]] = {}
    root_others: list[CheckpointFile] = []
    for e in entries:
        parts = e.parts
        if e.kind == "d":
            if len(parts) == 1:
                run_dirs[parts[0]] = e.mtime
            elif ck.is_group_dir(e.path, layout):
                groups.setdefault(parts[0], {}).setdefault(parts[-1], [])
            continue
        where = ck.place(e.path, layout)
        if where is None:
            root_others.append(ckpt(e))
        elif where.group is None:
            others.setdefault(where.run, []).append(ckpt(e))
        else:
            groups.setdefault(where.run, {}).setdefault(where.group, []).append(ckpt(e))

    runs: list[CheckpointRun] = []
    for name in set(run_dirs) | set(groups) | set(others):
        gs = [
            CheckpointGroup(
                name=g,
                path=ck.group_dir(name, g, layout),
                size=sum(f.size for f in fs),
                count=len(fs),
                files=sorted(fs, key=lambda f: f.name),
            )
            for g, fs in sorted(groups.get(name, {}).items())
        ]
        extra = sorted(others.get(name, []), key=lambda f: f.path)
        all_files = [f for g in gs for f in g.files] + extra
        latest = max((f.mtime for f in all_files), default=None)
        if latest is None and name in run_dirs:
            latest = _dt(run_dirs[name])
        runs.append(
            CheckpointRun(
                name=name,
                size=sum(f.size for f in all_files),
                count=len(all_files),
                mtime=latest,
                groups=gs,
                others=extra,
            )
        )
    epoch = datetime.min.replace(tzinfo=UTC)
    runs.sort(key=lambda r: (r.mtime or epoch, r.name), reverse=True)
    root_others.sort(key=lambda f: f.path)
    every = [f for r in runs for g in r.groups for f in g.files] + [
        f for r in runs for f in r.others
    ]
    every += root_others
    return CheckpointListing(
        source=src.id,
        machine=src.machine,
        root=src.root,
        layout=src.layout,
        size=sum(f.size for f in every),
        count=len(every),
        runs=runs,
        others=root_others,
        error=error,
        scanned_at=scanned_at,
    )
