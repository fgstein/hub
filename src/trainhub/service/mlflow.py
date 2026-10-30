"""MLflow tracking of the runs: progress (current epoch / maximum epoch) and training status.

TrainHub finds the MLflow run of a TrainHub run by a tag whose value is the TrainHub run id
(``MlflowConfig.run_tag``, ``trainhub.run_id`` by default). With ``inject_env`` (default),
TrainHub passes ``MLFLOW_RUN_CONTEXT={"<run_tag>": "<run id>"}`` to the container, and MLflow
adds that tag to every run it creates: no change is needed in the training code.

The REST API of MLflow is used directly (``httpx``), so that TrainHub does not depend on the
``mlflow`` package. Credentials come from the usual MLflow variables of the TrainHub process:
``MLFLOW_TRACKING_TOKEN`` (bearer) or ``MLFLOW_TRACKING_USERNAME`` / ``MLFLOW_TRACKING_PASSWORD``.
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import logging
import os
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import httpx

from ..core.models import MLFLOW_TERMINAL, MlflowConfig, MlflowInfo, Run, UnknownResourceError
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


class MlflowError(Exception):
    """MLflow unreachable or error answer."""


def read_info(cfg: MlflowConfig, run: dict[str, Any]) -> MlflowInfo:
    """:class:`MlflowInfo` of a run of the REST API (``{"info": …, "data": …}``)."""
    info = run.get("info") or {}
    data = run.get("data") or {}
    params = {p.get("key"): p.get("value") for p in data.get("params") or []}
    metrics = {m.get("key"): m.get("value") for m in data.get("metrics") or []}
    epoch = _number(metrics.get(cfg.epoch_metric))
    run_id = info.get("run_id") or info.get("run_uuid")
    exp = info.get("experiment_id")
    return MlflowInfo(
        run_id=run_id,
        experiment_id=exp,
        status=info.get("status"),
        epoch=None if epoch is None else epoch + cfg.epoch_offset,
        max_epochs=_number(params.get(cfg.max_epochs_param)),
        url=f"{cfg.tracking_uri}/#/experiments/{exp}/runs/{run_id}" if run_id and exp else None,
    )


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


class MlflowTracker:
    """Loop that refreshes the MLflow state of the runs of projects using MLflow."""

    def __init__(self, service: RunService, source: MlflowSource) -> None:
        self.svc = service
        self.source = source
        self._task: asyncio.Task[None] | None = None

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

    async def poll(self) -> None:
        """One refresh round (usable directly in tests)."""
        runs = [(r, c) for r in await self.svc.list_runs() if (c := self._wanted(r)) is not None]
        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        async def one(run: Run, cfg: MlflowConfig) -> None:
            async with sem:
                await self.refresh(run, cfg)

        await asyncio.gather(*(one(r, c) for r, c in runs))

    async def refresh(self, run: Run, cfg: MlflowConfig) -> None:
        try:
            info = await self.source.fetch(cfg, run)
        except MlflowError as exc:
            # Keep what was read before: a temporary outage must not erase the progress.
            info = (run.mlflow or MlflowInfo()).model_copy(update={"error": str(exc)})
        if info != run.mlflow:
            await self.svc.set_mlflow(run.id, info, started_at=run.started_at)
