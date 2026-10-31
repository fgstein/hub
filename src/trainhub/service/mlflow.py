"""MLflow tracking of the runs: progress (current epoch / maximum epoch) and training status.

TrainHub finds the MLflow run of a TrainHub run by a tag whose value is the TrainHub run id
(``MlflowConfig.run_tag``, ``trainhub.run_id`` by default). With ``inject_env`` (default),
TrainHub passes ``MLFLOW_RUN_CONTEXT={"<run_tag>": "<run id>"}`` to the container, and MLflow
adds that tag to every run it creates: no change is needed in the training code.

Each round (``poll_interval_s``, 30 s by default) searches the running MLflow runs of the
experiments concerned, and those started since the previous round (which catches a run that
started and failed in between), once per MLflow server: the TrainHub runs read their state
from that answer, and the runs of the experiments a project names that TrainHub did not
launch become *external runs* of that project. A run that stops running is then read once
with ``runs/get``.

The REST API of MLflow is used directly (``httpx``), so that TrainHub does not depend on the
``mlflow`` package. Credentials come from the usual MLflow variables of the TrainHub process:
``MLFLOW_TRACKING_TOKEN`` (bearer) or ``MLFLOW_TRACKING_USERNAME`` / ``MLFLOW_TRACKING_PASSWORD``.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import logging
import math
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from ..core.models import (
    MLFLOW_FAILED,
    MLFLOW_TERMINAL,
    ExternalRun,
    MlflowConfig,
    MlflowInfo,
    MlflowRunRef,
    Run,
    UnknownResourceError,
    split_mlflow_key,
    split_shown_key,
)
from ..core.status import utcnow

if TYPE_CHECKING:
    from .runs import RunService

log = logging.getLogger("trainhub.mlflow")

#: A finished run whose MLflow run is not terminal is still polled for this long (MLflow
#: ends its run slightly after the process, or never if the process was killed).
FINISHED_GRACE = timedelta(minutes=10)
#: Tolerance on the start of the MLflow run before the container's (clock skew).
START_MARGIN = timedelta(minutes=5)
#: Lifetime of the experiment ids cache.
EXPERIMENTS_TTL_S = 60.0
#: Queries to MLflow running at the same time.
MAX_CONCURRENCY = 8
#: Search of the runs of a checkpoint source: pages of 1000 runs, most recent first.
SEARCH_PAGE_SIZE = 1000
SEARCH_MAX_PAGES = 10
#: Overlap of the search of the runs started since the previous round (clock skew).
RECENT_OVERLAP = timedelta(minutes=1)
#: Look-back limit of that search after a long outage of MLflow.
MAX_CATCH_UP = timedelta(hours=24)
#: Filter of the running runs.
RUNNING_FILTER = "attributes.status = 'RUNNING'"


class MlflowError(Exception):
    """MLflow unreachable or error answer."""


@dataclass(frozen=True)
class MlflowHit:
    """A run found by :meth:`MlflowSource.search_by_key`."""

    value: str
    """Value of the searched key."""
    ref: MlflowRunRef
    logged: dict[str, str] = field(default_factory=dict)
    """Values of the extra keys (``params.<key>`` / ``tags.<key>``) the run has."""


def read_info(cfg: MlflowConfig, run: dict[str, Any], now: datetime | None = None) -> MlflowInfo:
    """:class:`MlflowInfo` of a run of the REST API (``{"info": …, "data": …}``)."""
    info = run.get("info") or {}
    data = run.get("data") or {}
    params = {p.get("key"): p.get("value") for p in data.get("params") or []}
    metrics = {m.get("key"): m.get("value") for m in data.get("metrics") or []}
    epoch = _number(metrics.get(cfg.epoch_metric))
    run_id = info.get("run_id") or info.get("run_uuid")
    exp = info.get("experiment_id")
    status = info.get("status")
    started = _time(info.get("start_time"))
    stamps = [t for m in data.get("metrics") or [] if (t := _time(m.get("timestamp")))]
    last = max(stamps) if stamps else None
    alive = max(t for t in (started, last) if t) if started or last else None
    stale = (
        status == "RUNNING"
        and alive is not None
        and (now or utcnow()) - alive > timedelta(minutes=cfg.stale_after_min)
    )
    return MlflowInfo(
        run_id=run_id,
        experiment_id=exp,
        status=status,
        epoch=None if epoch is None else epoch + cfg.epoch_offset,
        max_epochs=_number(params.get(cfg.max_epochs_param)),
        url=f"{cfg.tracking_uri}/#/experiments/{exp}/runs/{run_id}" if run_id and exp else None,
        started_at=started,
        finished_at=_time(info.get("end_time")),
        last_activity=last,
        stale=stale,
    )


def _time(ms: object) -> datetime | None:
    """Time of an MLflow timestamp (milliseconds since the epoch)."""
    n = _number(ms)
    return datetime.fromtimestamp(n / 1000, UTC) if n else None


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def is_deleted(run: dict[str, Any]) -> bool:
    """The run is in the MLflow trash."""
    return (run.get("info") or {}).get("lifecycle_stage") == "deleted"


def _run_id(run: dict[str, Any]) -> str | None:
    info = run.get("info") or {}
    rid = info.get("run_id") or info.get("run_uuid")
    return rid if isinstance(rid, str) else None


def read_ref(cfg: MlflowConfig, run: dict[str, Any]) -> MlflowRunRef | None:
    """:class:`MlflowRunRef` of a run of the REST API (``None`` without run id)."""
    info = run.get("info") or {}
    run_id = info.get("run_id") or info.get("run_uuid")
    if not run_id:
        return None
    exp = info.get("experiment_id")
    start = _number(info.get("start_time"))
    return MlflowRunRef(
        run_id=run_id,
        run_name=info.get("run_name") or None,
        experiment_id=exp,
        status=info.get("status"),
        started_at=datetime.fromtimestamp(start / 1000, UTC) if start else None,
        url=f"{cfg.tracking_uri}/#/experiments/{exp}/runs/{run_id}" if exp else None,
        deleted=info.get("lifecycle_stage") == "deleted",
    )


def logged_value(run: dict[str, Any], full_key: str) -> str | None:
    """Value of ``params.<key>`` / ``tags.<key>`` / ``metrics.<key>`` (last value, as text)
    in a run of the REST API."""
    kind, key = split_shown_key(full_key)
    for item in (run.get("data") or {}).get(kind) or []:
        if item.get("key") == key:
            value = item.get("value")
            if kind == "metrics":
                return format_metric(value)
            return value if isinstance(value, str) else None
    return None


def format_metric(value: object) -> str | None:
    """A metric value as text: ``12`` rather than ``12.0``, 6 significant digits otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):  # NaN / Infinity, as sent by some servers
        return value
    number = _number(value)
    if number is None:
        return None
    if math.isfinite(number) and number.is_integer() and abs(number) < 1e15:
        return str(int(number))
    return format(number, ".6g")


def _number(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class MlflowSource(abc.ABC):
    """Where the MLflow state of a run comes from (REST API, or simulation)."""

    @abc.abstractmethod
    async def fetch(self, cfg: MlflowConfig, run: Run) -> MlflowInfo:
        """State of the MLflow run of ``run``; raise :class:`MlflowError` on failure."""

    async def search_by_key(
        self, cfg: MlflowConfig, match: str, extra: Sequence[str] = ()
    ) -> tuple[list[MlflowHit], bool]:
        """Runs having the key ``match`` (``params.<key>`` / ``tags.<key>``), most recent
        first, with its value and those of ``extra``; and whether the search stopped early.
        Runs in the MLflow trash are included, with ``ref.deleted``."""
        raise MlflowError("MLflow search not available")

    async def delete_run(self, cfg: MlflowConfig, run_id: str) -> None:
        """Delete an MLflow run (soft: in the MLflow trash until ``mlflow gc``)."""
        raise MlflowError("MLflow deletion not available")

    async def experiments(self, cfg: MlflowConfig) -> dict[str, str]:
        """Experiments of the server of ``cfg``: id → name."""
        raise MlflowError("MLflow experiments not available")

    async def search_runs(
        self, cfg: MlflowConfig, experiment_ids: Sequence[str], filter_: str
    ) -> list[dict[str, Any]]:
        """Runs of the REST API (``{"info": …, "data": …}``) of these experiments matching
        ``filter_`` (MLflow search syntax), most recent first."""
        raise MlflowError("MLflow search not available")

    async def get_run(self, cfg: MlflowConfig, run_id: str) -> dict[str, Any] | None:
        """A run of the REST API; ``None`` if it does not exist."""
        raise MlflowError("MLflow runs not available")

    async def close(self) -> None:  # noqa: B027
        """Release resources."""


class HttpMlflow(MlflowSource):
    """MLflow REST API (``/api/2.0/mlflow``)."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        auth: httpx.Auth | None = None
        headers: dict[str, str] = {}
        if token := os.environ.get("MLFLOW_TRACKING_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
        elif user := os.environ.get("MLFLOW_TRACKING_USERNAME"):
            auth = httpx.BasicAuth(user, os.environ.get("MLFLOW_TRACKING_PASSWORD", ""))
        self._client = httpx.AsyncClient(
            timeout=10.0, transport=transport, auth=auth, headers=headers
        )
        self._experiments: dict[tuple[str, tuple[str, ...] | None], tuple[float, list[str]]] = {}
        self._names: dict[str, tuple[float, dict[str, str]]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(
        self, cfg: MlflowConfig, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        """JSON answer of the API; ``None`` for a missing resource."""
        url = f"{cfg.tracking_uri}/api/2.0/mlflow/{path}"
        try:
            res = await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise MlflowError(f"MLflow unreachable ({cfg.tracking_uri}): {exc}") from exc
        if res.status_code == 404:
            return None
        if res.status_code >= 400:
            try:
                msg = res.json().get("message") or res.text
            except ValueError:
                msg = res.text
            raise MlflowError(f"MLflow {res.status_code}: {msg.strip()[:300]}")
        try:
            data: dict[str, Any] = res.json()
        except ValueError as exc:
            raise MlflowError(f"invalid MLflow answer for {path}") from exc
        return data

    async def experiment_ids(self, cfg: MlflowConfig) -> list[str]:
        key = (cfg.tracking_uri, tuple(cfg.experiments) if cfg.experiments else None)
        hit = self._experiments.get(key)
        if hit and time.monotonic() - hit[0] < EXPERIMENTS_TTL_S:
            return hit[1]
        ids: list[str] = []
        if cfg.experiments:
            for name in cfg.experiments:
                data = await self._call(
                    cfg, "GET", "experiments/get-by-name", params={"experiment_name": name}
                )
                exp = (data or {}).get("experiment") or {}
                if exp.get("experiment_id"):
                    ids.append(exp["experiment_id"])
        else:
            token: str | None = None
            while True:
                body: dict[str, Any] = {"max_results": 1000}
                if token:
                    body["page_token"] = token
                data = await self._call(cfg, "POST", "experiments/search", json=body) or {}
                ids += [e["experiment_id"] for e in data.get("experiments") or []]
                token = data.get("next_page_token")
                if not token:
                    break
        self._experiments[key] = (time.monotonic(), ids)
        return ids

    async def fetch(self, cfg: MlflowConfig, run: Run) -> MlflowInfo:
        known = run.mlflow.run_id if run.mlflow else None
        if known and run.series is not None and run.status.is_active:
            known = None  # each repetition of a series creates its own MLflow run
        if known:
            data = await self._call(cfg, "GET", "runs/get", params={"run_id": known})
            if data and data.get("run"):
                return read_info(cfg, data["run"])
        ids = await self.experiment_ids(cfg)
        if not ids:
            names = ", ".join(cfg.experiments or [])
            raise MlflowError(f"MLflow experiment not found: {names or 'no experiment'}")
        query = f"tags.`{cfg.run_tag}` = '{run.id}'"
        if run.started_at is not None:
            # After a restart, ignore the MLflow run of the previous execution.
            since = run.started_at - START_MARGIN
            query += f" AND attributes.start_time >= {int(since.timestamp() * 1000)}"
        data = await self._call(
            cfg,
            "POST",
            "runs/search",
            json={
                "experiment_ids": ids,
                "filter": query,
                "max_results": 1,
                "order_by": ["attributes.start_time DESC"],
            },
        )
        runs = (data or {}).get("runs") or []
        return read_info(cfg, runs[0]) if runs else MlflowInfo()

    async def search_by_key(
        self, cfg: MlflowConfig, match: str, extra: Sequence[str] = ()
    ) -> tuple[list[MlflowHit], bool]:
        kind, key = split_mlflow_key(match)
        ids = await self.experiment_ids(cfg)
        if not ids:
            names = ", ".join(cfg.experiments or [])
            raise MlflowError(f"MLflow experiment not found: {names or 'no experiment'}")
        out: list[MlflowHit] = []
        token: str | None = None
        for _ in range(SEARCH_MAX_PAGES):
            body: dict[str, Any] = {
                "experiment_ids": ids,
                # Runs without the key are left out by the filter.
                "filter": f"{kind}.`{key}` LIKE '%'",
                "max_results": SEARCH_PAGE_SIZE,
                "order_by": ["attributes.start_time DESC"],
                # Deleted runs too (soft delete: still there until ``mlflow gc``).
                "run_view_type": "ALL",
            }
            if token:
                body["page_token"] = token
            data = await self._call(cfg, "POST", "runs/search", json=body) or {}
            for run in data.get("runs") or []:
                value = logged_value(run, match)
                ref = read_ref(cfg, run)
                if value is None or ref is None:
                    continue
                logged = {k: v for k in extra if (v := logged_value(run, k)) is not None}
                out.append(MlflowHit(value, ref, logged))
            token = data.get("next_page_token")
            if not token:
                return out, False
        return out, True

    async def delete_run(self, cfg: MlflowConfig, run_id: str) -> None:
        if await self._call(cfg, "POST", "runs/delete", json={"run_id": run_id}) is None:
            raise MlflowError(f"MLflow run not found: {run_id}")

    async def experiments(self, cfg: MlflowConfig) -> dict[str, str]:
        hit = self._names.get(cfg.tracking_uri)
        if hit and time.monotonic() - hit[0] < EXPERIMENTS_TTL_S:
            return hit[1]
        out: dict[str, str] = {}
        token: str | None = None
        while True:
            body: dict[str, Any] = {"max_results": 1000}
            if token:
                body["page_token"] = token
            data = await self._call(cfg, "POST", "experiments/search", json=body) or {}
            for e in data.get("experiments") or []:
                if e.get("experiment_id"):
                    out[e["experiment_id"]] = e.get("name") or e["experiment_id"]
            token = data.get("next_page_token")
            if not token:
                break
        self._names[cfg.tracking_uri] = (time.monotonic(), out)
        return out

    async def search_runs(
        self, cfg: MlflowConfig, experiment_ids: Sequence[str], filter_: str
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(SEARCH_MAX_PAGES):
            body: dict[str, Any] = {
                "experiment_ids": list(experiment_ids),
                "filter": filter_,
                "max_results": SEARCH_PAGE_SIZE,
                "order_by": ["attributes.start_time DESC"],
            }
            if token:
                body["page_token"] = token
            data = await self._call(cfg, "POST", "runs/search", json=body) or {}
            out += data.get("runs") or []
            token = data.get("next_page_token")
            if not token:
                break
        return out

    async def get_run(self, cfg: MlflowConfig, run_id: str) -> dict[str, Any] | None:
        data = await self._call(cfg, "GET", "runs/get", params={"run_id": run_id})
        run = (data or {}).get("run")
        return run if isinstance(run, dict) else None


@dataclass(frozen=True)
class _Claim:
    """An experiment named by a project: its runs not launched by TrainHub go there."""

    project_id: str
    cfg: MlflowConfig


class MlflowTracker:
    """Loop that refreshes the MLflow state of the runs of projects using MLflow, and finds
    the external runs (MLflow runs not launched by TrainHub) of the experiments projects
    name."""

    def __init__(self, service: RunService, source: MlflowSource) -> None:
        self.svc = service
        self.source = source
        self._task: asyncio.Task[None] | None = None
        #: Last successful round, per MLflow server.
        self._last_success: dict[str, datetime] = {}

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        await self.source.close()

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("error in the MLflow loop")
            await asyncio.sleep(self.svc.inventory.mlflow_poll_interval)

    def _wanted(self, run: Run) -> MlflowConfig | None:
        """MLflow settings of ``run`` if it must be refreshed now."""
        try:
            cfg = self.svc.inventory.mlflow_config(self.svc.inventory.project(run.project_id))
        except UnknownResourceError:
            return None
        if cfg is None:
            return None
        if run.status.is_active:
            return cfg
        if run.mlflow and run.mlflow.status in MLFLOW_TERMINAL:
            return None
        finished = run.finished_at or run.created_at
        return cfg if utcnow() - finished < FINISHED_GRACE else None

    def _claims(self) -> dict[str, dict[str, _Claim]]:
        """Experiments named by projects, per server: name → first project naming it."""
        inv = self.svc.inventory
        out: dict[str, dict[str, _Claim]] = {}
        for project in inv.projects:
            cfg = inv.mlflow_config(project)
            if cfg is None or not cfg.experiments:
                continue
            claims = out.setdefault(cfg.tracking_uri, {})
            for name in cfg.experiments:
                claims.setdefault(name, _Claim(project.id, cfg))
        return out

    async def poll(self) -> None:
        """One refresh round (usable directly in tests)."""
        runs = await self.svc.list_runs()
        known = {r.id for r in runs}
        wanted = [(r, c) for r in runs if (c := self._wanted(r)) is not None]
        claims = self._claims()
        for uri in sorted({c.tracking_uri for _, c in wanted} | set(claims)):
            await self._poll_server(
                [(r, c) for r, c in wanted if c.tracking_uri == uri],
                claims.get(uri, {}),
                known,
            )

    async def _poll_server(
        self,
        wanted: list[tuple[Run, MlflowConfig]],
        claims: dict[str, _Claim],
        known: set[str],
    ) -> None:
        """One round on one MLflow server: one search of the running runs and one of the
        runs started since the previous round, for every run concerned."""
        server = wanted[0][1] if wanted else next(iter(claims.values())).cfg
        uri = server.tracking_uri
        now = utcnow()
        first = uri not in self._last_success
        try:
            names = await self.source.experiments(server)
            by_name = {n: i for i, n in names.items()}
            discovered = {by_name[n]: c for n, c in claims.items() if n in by_name}
            ids = set(discovered)
            for _, cfg in wanted:
                if cfg.experiments is None:
                    ids |= set(names)
                else:
                    ids |= {by_name[n] for n in cfg.experiments if n in by_name}
            running: list[dict[str, Any]] = []
            recent: list[dict[str, Any]] = []
            if ids:
                exp = sorted(ids)
                running = await self.source.search_runs(server, exp, RUNNING_FILTER)
                since: datetime | None
                if first:
                    # Start-up: the failures that happened while the oldest running run went on.
                    starts = [t for r in running if (t := read_info(server, r).started_at)]
                    since = min(starts) if starts else None
                else:
                    since = max(self._last_success[uri] - RECENT_OVERLAP, now - MAX_CATCH_UP)
                if since is not None:
                    recent = await self.source.search_runs(
                        server,
                        exp,
                        f"attributes.start_time >= {_ms(since)} AND attributes.status != 'RUNNING'",
                    )
        except MlflowError as exc:
            for run, _ in wanted:
                await self._set_error(run, str(exc))
            return
        self._last_success[uri] = now
        found = running + recent
        await self._refresh_runs(wanted, found, now, first)
        if first:  # at start-up, only the failures are taken from the past
            recent = [r for r in recent if (r.get("info") or {}).get("status") in MLFLOW_FAILED]
        await self._refresh_external(uri, running + recent, discovered, names, known, now)

    # -- TrainHub runs ----------------------------------------------------------------

    async def _refresh_runs(
        self,
        wanted: list[tuple[Run, MlflowConfig]],
        found: list[dict[str, Any]],
        now: datetime,
        first: bool,
    ) -> None:
        fallback: list[tuple[Run, MlflowConfig]] = []
        for run, cfg in wanted:
            hit = _match(run, cfg, found)
            if hit is not None:
                await self._apply(run, read_info(cfg, hit, now))
            elif first or (run.mlflow and run.mlflow.run_id):
                # Not in this round's answers: its MLflow run stopped running some time ago
                # (or, at start-up, may have): read it on its own.
                fallback.append((run, cfg))
            # else: no MLflow run yet (it would be among the runs started since last round)
        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        async def one(run: Run, cfg: MlflowConfig) -> None:
            async with sem:
                await self.refresh(run, cfg)

        await asyncio.gather(*(one(r, c) for r, c in fallback))

    async def refresh(self, run: Run, cfg: MlflowConfig) -> None:
        """Read the MLflow run of ``run`` on its own."""
        try:
            info = await self.source.fetch(cfg, run)
        except MlflowError as exc:
            await self._set_error(run, str(exc))
            return
        await self._apply(run, info)

    async def _set_error(self, run: Run, error: str) -> None:
        # Keep what was read before: a temporary outage must not erase the progress.
        await self._apply(run, (run.mlflow or MlflowInfo()).model_copy(update={"error": error}))

    async def _apply(self, run: Run, info: MlflowInfo) -> None:
        if info != run.mlflow:
            await self.svc.set_mlflow(run.id, info, started_at=run.started_at)

    # -- external runs ----------------------------------------------------------------

    async def _refresh_external(
        self,
        uri: str,
        found: list[dict[str, Any]],
        discovered: dict[str, _Claim],
        names: dict[str, str],
        known: set[str],
        now: datetime,
    ) -> None:
        shown = {e.id: e for e in await self.svc.store.list_external()}
        hidden = await self.svc.store.external_ids() - set(shown)
        seen: set[str] = set()
        for raw in found:
            rid = _run_id(raw)
            claim = discovered.get((raw.get("info") or {}).get("experiment_id") or "")
            if rid is None or claim is None or rid in seen:
                continue
            tag = logged_value(raw, f"tags.{claim.cfg.run_tag}")
            if tag and tag in known:
                continue  # a TrainHub run
            seen.add(rid)
            if rid in hidden:
                continue  # deleted from TrainHub by the user
            await self.svc.save_external(
                _external(raw, claim, names, now, shown.get(rid)), previous=shown.get(rid)
            )
        # Shown runs that stopped running: read their final state once.
        inv = self.svc.inventory
        for ext in shown.values():
            if not ext.is_active or ext.id in seen:
                continue
            try:
                cfg = inv.mlflow_config(inv.project(ext.project_id))
            except UnknownResourceError:
                cfg = None
            if cfg is None or cfg.tracking_uri != uri:
                continue
            try:
                final = await self.source.get_run(cfg, ext.id)
            except MlflowError as exc:
                log.warning("MLflow run %s: %s", ext.id, exc)
                continue
            if final is None or is_deleted(final):
                await self.svc.forget_external(ext)
                continue
            claim = _Claim(ext.project_id, cfg)
            await self.svc.save_external(_external(final, claim, names, now, ext), previous=ext)


def _match(run: Run, cfg: MlflowConfig, found: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The most recent MLflow run of ``run`` among ``found`` (tagged with its id, started
    after it, minus the clock margin)."""
    since = run.started_at - START_MARGIN if run.started_at else None
    best: dict[str, Any] | None = None
    best_start = 0.0
    for raw in found:
        if logged_value(raw, f"tags.{cfg.run_tag}") != run.id:
            continue
        start = _number((raw.get("info") or {}).get("start_time")) or 0.0
        if since is not None and start < _ms(since):
            continue
        if best is None or start > best_start:
            best, best_start = raw, start
    return best


def _external(
    raw: dict[str, Any],
    claim: _Claim,
    names: dict[str, str],
    now: datetime,
    previous: ExternalRun | None,
) -> ExternalRun:
    info = read_info(claim.cfg, raw, now)
    failed = info.status in MLFLOW_FAILED
    # A new failure is to be seen; an already known one keeps its state.
    unseen = previous.unseen_failure if previous is not None and previous.is_failure else True
    return ExternalRun(
        id=info.run_id or "",
        project_id=claim.project_id,
        name=(raw.get("info") or {}).get("run_name") or None,
        user=logged_value(raw, "tags.mlflow.user") or (raw.get("info") or {}).get("user_id"),
        experiment=names.get(info.experiment_id or "") or info.experiment_id,
        mlflow=info,
        discovered_at=previous.discovered_at if previous else now,
        unseen_failure=failed and unseen,
        # Seen while stalled: until it logs again (or ends).
        stale_seen_at=previous.stale_seen_at if previous and info.stale else None,
    )
