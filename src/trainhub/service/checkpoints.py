"""Checkpoint sources: scan (one ``find`` per source, cached), listing and deletion."""

from __future__ import annotations

import asyncio
import logging
import posixpath
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from ..core import checkpoints as ck
from ..core.models import (
    CheckpointInference,
    CheckpointSource,
    Machine,
    MlflowRunRef,
    Run,
    UnknownResourceError,
)
from ..core.status import utcnow
from ..executors.base import ExecutorError, MachineUnreachableError
from .mlflow import MlflowError, MlflowHit
from .schemas import (
    CheckpointDeleteResult,
    CheckpointFile,
    CheckpointGroup,
    CheckpointListing,
    CheckpointMlflowStatus,
    CheckpointRun,
    CheckpointRunDeleteResult,
    CheckpointSourceView,
    MlflowDeleteResult,
    MlflowOrphan,
    MlflowOrphanListing,
    RunCount,
)

if TYPE_CHECKING:
    from .runs import RunService

log = logging.getLogger("trainhub.checkpoints")

#: A scan can be long on a big shared disk: cached, refreshed on demand (↻) after that.
CHECKPOINT_CACHE_TTL_S = 300.0
SCAN_TIMEOUT_S = 60.0
DELETE_TIMEOUT_S = 120.0
#: Machine of a source without ``machine``: the TrainHub host, commands run without SSH.
SERVER_MACHINE = Machine(id="trainhub-server", label="TrainHub server", host="localhost")


@dataclass
class _Scan:
    at_monotonic: float
    at: datetime
    entries: list[ck.Entry]
    error: str | None


#: The two MLflow searches of a source: training runs of its directories, inferences.
Search = Literal["training", "inference"]


@dataclass
class _MlflowSearch:
    """MLflow runs having the ``match`` key of a source, with the logged path."""

    key: tuple[object, ...]
    """(tracking_uri, match, experiments, extra keys): new settings invalidate the cache."""
    match: str
    run_tag: str
    at_monotonic: float
    at: datetime | None
    hits: list[MlflowHit]
    truncated: bool
    error: str | None


class CheckpointService:
    """Checkpoint tab: what is on disk, whatever launched the runs."""

    def __init__(self, svc: RunService) -> None:
        self.svc = svc
        self._cache: dict[str, _Scan] = {}
        self._mlflow_cache: dict[tuple[str, Search], _MlflowSearch] = {}

    def clear(self) -> None:
        self._cache.clear()
        self._mlflow_cache.clear()

    def _source(self, source_id: str) -> CheckpointSource:
        from .runs import NotFoundError

        try:
            return self.svc.inventory.checkpoint_source(source_id)
        except UnknownResourceError as exc:
            raise NotFoundError(str(exc)) from exc

    def _machine(self, src: CheckpointSource) -> Machine:
        return SERVER_MACHINE if src.machine is None else self.svc._machine(src.machine)

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
            mlflow_match=src.mlflow.match if src.mlflow and src.mlflow.enabled else None,
        )

    def sources(self) -> list[CheckpointSourceView]:
        return [self.source_view(s) for s in self.svc.inventory.checkpoints]

    async def listing(self, source_id: str, *, refresh: bool = False) -> CheckpointListing:
        """Runs and checkpoints of a source (scan cached 5 min, ``refresh`` forces it)."""
        src = self._source(source_id)
        scan, ml, inf = await asyncio.gather(
            self._scan(src, refresh=refresh),
            self._search_mlflow(src, "training", refresh=refresh),
            self._search_mlflow(src, "inference", refresh=refresh),
        )
        counts = await self.svc.store.count_runs_by_checkpoint()
        runs = await self.svc.store.list_runs()
        return build_listing(
            src,
            scan.entries,
            counts,
            error=scan.error,
            scanned_at=scan.at,
            mlflow=ml,
            inference=inf,
            runs=runs,
        )

    async def _search_mlflow(
        self, src: CheckpointSource, which: Search, *, refresh: bool
    ) -> _MlflowSearch | None:
        """MLflow runs of the source (cached like the scan); ``None``: not configured."""
        inference = which == "inference"
        cfg = self.svc.inventory.checkpoint_mlflow(src, inference=inference)
        ml = src.mlflow
        match = (ml.inference.match if inference and ml.inference else ml.match) if ml else None
        if cfg is None or match is None:
            self._mlflow_cache.pop((src.id, which), None)
            return None
        # Inferences: also the TrainHub run id (tag), to merge with the TrainHub runs.
        extra = (
            (*ml.inference.show, f"tags.{cfg.run_tag}") if inference and ml and ml.inference else ()
        )
        key = (cfg.tracking_uri, match, tuple(cfg.experiments or ()), extra)
        hit = self._mlflow_cache.get((src.id, which))
        if hit is not None and hit.key != key:
            hit = None
        if (
            hit is not None
            and not refresh
            and time.monotonic() - hit.at_monotonic < CHECKPOINT_CACHE_TTL_S
        ):
            return hit
        try:
            hits, truncated = await self.svc.mlflow.source.search_by_key(cfg, match, extra)
        except MlflowError as exc:
            log.warning(
                "MLflow search failed", extra={"source": src.id, "search": which, "error": str(exc)}
            )
            # Keep the last good result, with the error.
            if hit is not None:
                return _MlflowSearch(
                    key,
                    match,
                    cfg.run_tag,
                    hit.at_monotonic,
                    hit.at,
                    hit.hits,
                    hit.truncated,
                    str(exc),
                )
            return _MlflowSearch(
                key, match, cfg.run_tag, time.monotonic(), None, [], False, str(exc)
            )
        found = _MlflowSearch(
            key, match, cfg.run_tag, time.monotonic(), utcnow(), hits, truncated, None
        )
        self._mlflow_cache[(src.id, which)] = found
        return found

    async def mlflow_orphans(self, *, refresh: bool = False) -> MlflowOrphanListing:
        """MLflow runs (of the sources with ``mlflow.match``) whose directory is in no
        source: every source is scanned, so that a run is an orphan only if no disk has it."""
        sources = self.svc.inventory.checkpoints
        scans, searches = await asyncio.gather(
            asyncio.gather(*(self._scan(s, refresh=refresh) for s in sources)),
            asyncio.gather(*(self._search_mlflow(s, "training", refresh=refresh) for s in sources)),
        )
        names: set[str] = set()
        scan_errors: dict[str, str] = {}
        for src, scan in zip(sources, scans, strict=True):
            names |= {e.parts[0] for e in scan.entries if e.kind == "d" or len(e.parts) > 1}
            if scan.error and not scan.entries:
                scan_errors[src.id] = scan.error
        orphans: dict[str, MlflowOrphan] = {}
        linked: set[str] = set()
        seen: set[str] = set()
        for src, search in zip(sources, searches, strict=True):
            if search is None:
                continue
            for h in search.hits:
                seen.add(h.ref.run_id)
                if h.ref.run_id in linked or ck.run_in_path(h.value, names) is not None:
                    linked.add(h.ref.run_id)
                    continue
                o = orphans.setdefault(
                    h.ref.run_id,
                    MlflowOrphan(run=h.ref, path=h.value, match=search.match, sources=[]),
                )
                if src.id not in o.sources:
                    o.sources.append(src.id)
        done = [s for s in searches if s is not None]
        epoch = datetime.min.replace(tzinfo=UTC)
        return MlflowOrphanListing(
            enabled=bool(done),
            runs=sorted(orphans.values(), key=lambda o: o.run.started_at or epoch, reverse=True),
            searched=len(seen),
            linked=len(linked),
            truncated=any(s.truncated for s in done),
            searched_at=min((s.at for s in done if s.at), default=None),
            mlflow_errors={
                src.id: s.error
                for src, s in zip(sources, searches, strict=True)
                if s is not None and s.error
            },
            scan_errors=scan_errors,
        )

    async def delete_mlflow_orphans(self, run_ids: list[str]) -> MlflowDeleteResult:
        """Delete MLflow runs (MLflow trash), only orphans of the current listing: a run
        whose directory is in a source is refused, and so is everything while a source
        cannot be scanned (its runs would look like orphans)."""
        from .runs import InvalidRequestError

        listing = await self.mlflow_orphans()
        if listing.scan_errors:
            src, err = next(iter(listing.scan_errors.items()))
            raise InvalidRequestError(
                f"source '{src}' could not be scanned ({err}): its runs may not be orphans, "
                "rescan before deleting"
            )
        by_id = {o.run.run_id: o for o in listing.runs}
        ids = list(dict.fromkeys(run_ids))
        bad = [i for i in ids if i not in by_id]
        if bad:
            raise InvalidRequestError(
                f"not an MLflow run without directory: {', '.join(repr(b) for b in bad[:5])}"
            )
        inv = self.svc.inventory
        deleted: list[str] = []
        failed: dict[str, str] = {}
        sem = asyncio.Semaphore(8)

        async def one(run_id: str) -> None:
            o = by_id[run_id]
            cfg = next(
                (
                    c
                    for sid in o.sources
                    if (src := next((s for s in inv.checkpoints if s.id == sid), None))
                    and (c := inv.checkpoint_mlflow(src))
                ),
                None,
            )
            if cfg is None:
                failed[run_id] = "no MLflow settings for its source any more"
                return
            async with sem:
                try:
                    await self.svc.mlflow.source.delete_run(cfg, run_id)
                except MlflowError as exc:
                    failed[run_id] = str(exc)
                    return
            deleted.append(run_id)

        await asyncio.gather(*(one(i) for i in ids))
        gone = set(deleted)
        for search in self._mlflow_cache.values():
            search.hits = [h for h in search.hits if h.ref.run_id not in gone]
        log.info("MLflow runs deleted", extra={"deleted": len(deleted), "failed": len(failed)})
        return MlflowDeleteResult(
            deleted=[i for i in ids if i in gone],
            failed=failed,
            remaining=len(listing.runs) - len(gone),
        )

    async def _scan(self, src: CheckpointSource, *, refresh: bool) -> _Scan:
        hit = self._cache.get(src.id)
        if (
            hit is not None
            and not refresh
            and (time.monotonic() - hit.at_monotonic < CHECKPOINT_CACHE_TTL_S)
        ):
            return hit
        layout = ck.parse_layout(src.layout)
        machine = self._machine(src)
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
        machine = self._machine(src)
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

    async def delete_runs(self, source_id: str, names: list[str]) -> CheckpointRunDeleteResult:
        """Delete whole run directories of a source (``rm -rf``: checkpoints, logs, configs…).

        Each name must be a single path component; the machine checks that it is a real
        directory directly under the root (not a symbolic link) before removing it.
        """
        from .runs import InvalidRequestError, UnreachableError

        src = self._source(source_id)
        dirs = list(dict.fromkeys(n.strip() for n in names))
        bad = [n for n in dirs if not ck.valid_run_name(n)]
        if bad:
            raise InvalidRequestError(
                f"not a run directory of source '{src.id}' (a single directory name): "
                f"{', '.join(repr(b) for b in bad[:5])}"
            )
        machine = self._machine(src)
        try:
            res = await self.svc.executor.exec(
                machine, ck.delete_run_dirs(src.root, dirs), timeout=DELETE_TIMEOUT_S
            )
        except MachineUnreachableError as exc:
            raise UnreachableError(str(exc)) from exc
        except ExecutorError as exc:
            raise InvalidRequestError(f"deletion failed: {exc}") from exc
        outcome = ck.parse_delete_output(res.out, dirs)
        if outcome.root_missing:
            raise InvalidRequestError(f"root of source '{src.id}' not found: {src.root}")
        log.info(
            "run directories deleted",
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

            def under(e: ck.Entry) -> bool:
                return e.parts[0] in gone

            freed = sum(e.size for e in scan.entries if e.kind == "f" and under(e))
            scan.entries = [e for e in scan.entries if not under(e)]
        self._publish(src)
        view = self.source_view(src)
        return CheckpointRunDeleteResult(
            source=src.id,
            deleted=outcome.deleted,
            failed=outcome.failed,
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
    mlflow: _MlflowSearch | None = None,
    inference: _MlflowSearch | None = None,
    runs: list[Run] | None = None,
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

    names = set(run_dirs) | set(groups) | set(others)
    ml_runs: dict[str, list[MlflowRunRef]] = {}
    for h in mlflow.hits if mlflow else []:
        if (name := ck.run_in_path(h.value, names)) is not None:
            ml_runs.setdefault(name, []).append(h.ref)
    files = {e.path for e in entries if e.kind == "f"}
    inferences = _inferences(src, names, files, runs or [], inference)

    out: list[CheckpointRun] = []
    for name in names:
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
        out.append(
            CheckpointRun(
                name=name,
                size=sum(f.size for f in all_files),
                count=len(all_files),
                mtime=latest,
                groups=gs,
                others=extra,
                mlflow_runs=ml_runs.get(name, []),
                inferences=inferences.get(name, []),
            )
        )
    epoch = datetime.min.replace(tzinfo=UTC)
    out.sort(key=lambda r: (r.mtime or epoch, r.name), reverse=True)
    root_others.sort(key=lambda f: f.path)
    every = [f for r in out for g in r.groups for f in g.files] + [f for r in out for f in r.others]
    every += root_others
    return CheckpointListing(
        source=src.id,
        machine=src.machine,
        root=src.root,
        layout=src.layout,
        size=sum(f.size for f in every),
        count=len(every),
        runs=out,
        others=root_others,
        error=error,
        scanned_at=scanned_at,
        mlflow=_mlflow_status(mlflow),
        mlflow_inference=_mlflow_status(inference),
    )


def _inferences(
    src: CheckpointSource,
    names: set[str],
    files: set[str],
    runs: list[Run],
    search: _MlflowSearch | None,
) -> dict[str, list[CheckpointInference]]:
    """Inferences per run directory: TrainHub runs launched with one of its checkpoints,
    merged with the MLflow runs that logged the path of one of them."""
    by_trainhub: dict[str, CheckpointInference] = {}
    by_mlflow: dict[str, CheckpointInference] = {}
    out: list[CheckpointInference] = []
    prefix = src.root.rstrip("/") + "/"
    for r in runs:
        if not r.checkpoint or not r.checkpoint.startswith(prefix):
            continue
        rel = posixpath.normpath(r.checkpoint[len(prefix) :])
        if rel.split("/", 1)[0] not in names:
            continue
        ml = r.mlflow
        inf = CheckpointInference(
            checkpoint=rel,
            exists=rel in files,
            started_at=r.started_at or r.created_at,
            trainhub_run=r.id,
            project=r.project_id,
            config=r.config,
            status=str(r.status),
            mlflow=MlflowRunRef(
                run_id=ml.run_id, experiment_id=ml.experiment_id, status=ml.status, url=ml.url
            )
            if ml and ml.run_id
            else None,
        )
        by_trainhub[r.id] = inf
        if inf.mlflow:
            by_mlflow[inf.mlflow.run_id] = inf
        out.append(inf)
    trainhub = {r.id: r for r in runs}
    tag_key = f"tags.{search.run_tag}" if search else ""
    for h in search.hits if search else []:
        path = ck.path_from_run(h.value, names)
        if path is None:
            continue
        tag = h.logged.get(tag_key)
        logged = {k: v for k, v in h.logged.items() if k != tag_key}
        same = by_mlflow.get(h.ref.run_id) or (by_trainhub.get(tag) if tag else None)
        if same is not None:
            # Launched by TrainHub: the MLflow run completes it (run name, logged values).
            same.mlflow = h.ref
            same.logged = logged
            continue
        th = trainhub.get(tag) if tag else None
        out.append(
            CheckpointInference(
                checkpoint=path,
                exists=path in files,
                started_at=h.ref.started_at,
                trainhub_run=tag,
                project=th.project_id if th else None,
                config=th.config if th else None,
                status=str(th.status) if th else None,
                mlflow=h.ref,
                logged=logged,
            )
        )
    epoch = datetime.min.replace(tzinfo=UTC)
    out.sort(key=lambda i: i.started_at or epoch, reverse=True)
    per_run: dict[str, list[CheckpointInference]] = {}
    for inf in out:
        per_run.setdefault(inf.checkpoint.split("/", 1)[0], []).append(inf)
    return per_run


def _mlflow_status(search: _MlflowSearch | None) -> CheckpointMlflowStatus | None:
    if search is None:
        return None
    return CheckpointMlflowStatus(
        match=search.match,
        tracking_uri=str(search.key[0]),
        searched=len(search.hits),
        truncated=search.truncated,
        searched_at=search.at,
        error=search.error,
    )
