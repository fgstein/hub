"""Link with Lookin, the explorer of classification results, through the MLflow run id.

TrainHub launches the trainings and the inferences, Lookin explores their results; both show
MLflow runs, and the MLflow run id is the whole link between them. Nothing is tagged: a run
started by hand links the same way as a run TrainHub launched.

Two directions, one route on each side:

- **TrainHub → Lookin**: ``GET <lookin.url>/api/links/mlflow-runs`` answers, for each MLflow run
  id, the Lookin workspaces with an experiment made from it. TrainHub keeps the answer
  :data:`CACHE_TTL_S` and serves it to its interface (``GET /api/lookin``), which draws the
  workspaces as badges next to the inferences and runs; the "Lookin" button opens
  ``<lookin>/?mlflow_run=<id>&mlflow_server=<tracking uri>``, where Lookin offers to put the
  run in a new or existing workspace.
- **Lookin → TrainHub**: ``GET /api/links/mlflow-runs`` here answers, for each MLflow run id
  TrainHub shows, where (:func:`mlflow_run_links`): a run it launched, an external run of a
  project, a run directory of a checkpoint source, an inference made with a checkpoint, with
  the path of the page. Lookin draws them as badges next to its experiments.

The answer shapes are a format shared by the two tools: fields are added, never renamed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

import httpx

from ..core.models import LookinOptions
from ..core.status import utcnow
from .schemas import LookinIndex, LookinWorkspace, MlflowRunLink, MlflowRunLinks

if TYPE_CHECKING:
    from .runs import RunService

log = logging.getLogger("trainhub.lookin")

#: Route of the run index, the same on both sides.
LINKS_PATH = "/api/links/mlflow-runs"
#: Lifetime of Lookin's answer (it reads its whole library on disk).
CACHE_TTL_S = 30.0
TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def new_workspace_url(lookin_url: str, run_id: str, tracking_uri: str | None = None) -> str:
    """Lookin page offering to put the MLflow run ``run_id`` in a workspace."""
    params = {"mlflow_run": run_id}
    if tracking_uri:
        params["mlflow_server"] = tracking_uri
    return f"{lookin_url}/?{urlencode(params)}"


def parse_lookin(payload: object, browser_url: str) -> dict[str, list[LookinWorkspace]]:
    """Workspaces per run id from Lookin's answer; entries of another shape are dropped."""
    out: dict[str, list[LookinWorkspace]] = {}
    runs = payload.get("runs") if isinstance(payload, dict) else None
    if not isinstance(runs, dict):
        return out
    for run_id, entries in runs.items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            path = str(e.get("path") or "")
            wid, name = e.get("workspace_id"), e.get("workspace")
            # Only a page of Lookin itself, behind the address the browser reaches it at.
            if not path.startswith("/") or path.startswith("//") or not wid or not name:
                continue
            out.setdefault(str(run_id), []).append(
                LookinWorkspace(
                    workspace_id=str(wid),
                    workspace=str(name),
                    group=str(e["group"]) if e.get("group") else None,
                    experiment=str(e.get("experiment") or ""),
                    artifact=str(e["artifact"]) if e.get("artifact") else None,
                    url=browser_url + path,
                )
            )
    return out


class LookinLink:
    """Lookin's index, read on demand and cached :data:`CACHE_TTL_S` (one read at a time)."""

    def __init__(self, svc: RunService, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.svc = svc
        self._transport = transport
        self._lock = asyncio.Lock()
        self._key: tuple[str, str] | None = None
        self._at = 0.0
        self._runs: dict[str, list[LookinWorkspace]] = {}
        self._version: str | None = None
        self._fetched_at: datetime | None = None
        self._error: str | None = None

    def _options(self) -> LookinOptions | None:
        opts = self.svc.inventory.lookin
        return opts if opts is not None and opts.enabled else None

    async def index(self, *, refresh: bool = False) -> LookinIndex:
        opts = self._options()
        if opts is None:
            return LookinIndex(enabled=False)
        key = (opts.url, opts.browser_url)
        async with self._lock:
            if key != self._key:
                self._key, self._at, self._runs = key, 0.0, {}
                self._version = self._fetched_at = self._error = None
            if refresh or time.monotonic() - self._at >= CACHE_TTL_S:
                await self._read(opts)
            return LookinIndex(
                enabled=True,
                url=opts.browser_url,
                runs=self._runs,
                version=self._version,
                fetched_at=self._fetched_at,
                error=self._error,
            )

    async def _read(self, opts: LookinOptions) -> None:
        self._at = time.monotonic()
        try:
            async with httpx.AsyncClient(
                base_url=opts.url, timeout=TIMEOUT, transport=self._transport
            ) as client:
                resp = await client.get(LINKS_PATH, headers={"Accept": "application/json"})
            if resp.status_code == 404:
                raise ValueError("this Lookin has no run index (404): it is older than 5.4")
            if resp.status_code != 200:
                raise ValueError(f"Lookin answered {resp.status_code}")
            payload = resp.json()
            if not isinstance(payload, dict) or payload.get("app") != "lookin":
                raise ValueError("the address does not answer like a Lookin server")
        except (httpx.HTTPError, ValueError) as exc:
            # The last good answer stays shown, with the error.
            self._error = f"Lookin at {opts.url}: {exc}"
            log.warning("Lookin unreachable", extra={"url": opts.url, "error": str(exc)})
            return
        self._runs = parse_lookin(payload, opts.browser_url)
        self._version = str(payload.get("version") or "") or None
        self._fetched_at = utcnow()
        self._error = None


def _p(project: str, run_id: str) -> str:
    return f"/p/{quote(project, safe='')}?{urlencode({'run': run_id})}"


async def mlflow_run_links(svc: RunService) -> MlflowRunLinks:
    """Every MLflow run TrainHub shows, by run id, with the page that shows it.

    The checkpoint sources are listed as the Checkpoints tab lists them (scans and MLflow
    searches cached 5 min): the first call after a start scans every source.
    """
    inv = svc.inventory
    projects = {p.id: p.display for p in inv.projects}
    runs: dict[str, list[MlflowRunLink]] = {}
    errors: list[str] = []

    def add(run_id: str | None, link: MlflowRunLink) -> None:
        if run_id:
            runs.setdefault(run_id, []).append(link)

    for r in await svc.store.list_runs():
        if r.mlflow is None or not r.mlflow.run_id:
            continue
        add(
            r.mlflow.run_id,
            MlflowRunLink(
                kind="run",
                label=f"{projects.get(r.project_id, r.project_id)} · {r.id}",
                detail=f"TrainHub run {r.id} of project {r.project_id}, config {r.config}",
                path=_p(r.project_id, r.id),
                project=r.project_id,
                status=r.status.value,
            ),
        )
    for e in await svc.store.list_external():
        add(
            e.id,
            MlflowRunLink(
                kind="external",
                label=f"{projects.get(e.project_id, e.project_id)} · {e.name or e.id[:8]}",
                detail=f"MLflow run of experiment {e.experiment or '?'}, shown in project "
                f"{e.project_id} (not launched by TrainHub)",
                path=_p(e.project_id, e.id),
                project=e.project_id,
                status=e.mlflow.status,
            ),
        )

    async def listing(src_id: str) -> Any:
        try:
            return await svc.checkpoints.listing(src_id)
        except Exception as exc:
            errors.append(f"checkpoint source {src_id}: {exc}")
            return None

    sources = inv.checkpoints
    listings = await asyncio.gather(*(listing(s.id) for s in sources))
    for src, lst in zip(sources, listings, strict=True):
        if lst is None:
            continue
        for err in (lst.error, lst.mlflow and lst.mlflow.error):
            if err:
                errors.append(f"checkpoint source {src.id}: {err}")
        if lst.mlflow_inference and lst.mlflow_inference.error:
            errors.append(f"checkpoint source {src.id} (inferences): {lst.mlflow_inference.error}")
        base = f"/checkpoints/{quote(src.id, safe='')}"
        for cr in lst.runs:
            for m in cr.mlflow_runs:
                if m.deleted:
                    continue
                add(
                    m.run_id,
                    MlflowRunLink(
                        kind="checkpoint_run",
                        label=f"{src.display} · {cr.name}",
                        detail=f"Run directory {cr.name} of checkpoint source {src.id} "
                        f"({cr.count} checkpoint(s))",
                        path=f"{base}?{urlencode({'run': cr.name})}",
                        source=src.id,
                        status=m.status,
                    ),
                )
            for inf in cr.inferences:
                query = {"run": cr.name, "inference": inf.mlflow.run_id}
                add(
                    inf.mlflow.run_id,
                    MlflowRunLink(
                        kind="inference",
                        label=f"{src.display} · {inf.checkpoint}",
                        detail=f"Inference made with checkpoint {inf.checkpoint}"
                        + (f" on {inf.dataset_name}" if inf.dataset_name else ""),
                        path=f"{base}?{urlencode(query)}",
                        source=src.id,
                        status=inf.mlflow.status,
                    ),
                )
    return MlflowRunLinks(version=svc.version, runs=runs, errors=errors)
