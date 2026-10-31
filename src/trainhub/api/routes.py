"""REST routes (``/api``): actions and reads, all delegated to the service layer."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from ..core.models import RunSource, RunStatus
from ..service import RunAction, RunService
from ..service.runs import InvalidRequestError
from ..service.schemas import (
    CheckpointDeleteRequest,
    CheckpointDeleteResult,
    CheckpointListing,
    CheckpointRunDeleteRequest,
    CheckpointRunDeleteResult,
    CheckpointSourceView,
    ConfigList,
    Health,
    InventoryView,
    MachineGpus,
    MachineLoad,
    MlflowDeleteRequest,
    MlflowDeleteResult,
    MlflowOrphanListing,
    PreviewResult,
    ProjectView,
    RunView,
    Selection,
    UiState,
)

router = APIRouter(prefix="/api")


def get_service(request: Request) -> RunService:
    svc: RunService = request.app.state.service
    return svc


Svc = Annotated[RunService, Depends(get_service)]


class ProjectPatch(BaseModel):
    """Project update: new command, or back to the YAML value."""

    command: str | None = Field(default=None, description="New generic command")
    reset: bool = Field(default=False, description="Go back to the YAML command")


class LaunchRequest(Selection):
    source: RunSource = RunSource.UI


@router.get("/health", response_model=Health, tags=["system"])
async def health(svc: Svc) -> Health:
    """Server health and machine reachability."""
    return svc.health()


@router.get("/inventory", response_model=InventoryView, tags=["inventory"])
async def inventory(svc: Svc) -> InventoryView:
    """Machines, images, venvs, projects (merged view of YAML + database)."""
    return svc.inventory_view()


@router.get("/machines/{machine_id}/gpus", response_model=MachineGpus, tags=["inventory"])
async def machine_gpus(machine_id: str, svc: Svc) -> MachineGpus:
    """GPUs of a machine (``nvidia-smi`` on the host) and the active runs placed on it."""
    return await svc.machine_gpus(machine_id)


@router.get("/machines/{machine_id}/load", response_model=MachineLoad, tags=["inventory"])
async def machine_load(machine_id: str, svc: Svc) -> MachineLoad:
    """CPU (``/proc``) and GPU (``nvidia-smi``) load of a machine; an unreachable machine
    is reported (``reachable: false``), not an error."""
    return await svc.machine_load(machine_id)


@router.get("/projects/{project_id}/configs", response_model=ConfigList, tags=["inventory"])
async def project_configs(
    project_id: str,
    svc: Svc,
    machine: str | None = None,
    refresh: bool = False,
) -> ConfigList:
    """Available configs (static or discovered on the machine, 60 s cache)."""
    return await svc.get_configs(project_id, machine, refresh=refresh)


@router.patch("/projects/{project_id}", response_model=ProjectView, tags=["inventory"])
async def patch_project(project_id: str, patch: ProjectPatch, svc: Svc) -> ProjectView:
    """Update a project's command, or reset it to the YAML value."""
    if patch.reset:
        return await svc.set_project_command(project_id, None)
    if patch.command is None:
        raise InvalidRequestError("provide 'command' or 'reset: true'")
    return await svc.set_project_command(project_id, patch.command)


@router.get("/checkpoints", response_model=list[CheckpointSourceView], tags=["checkpoints"])
async def checkpoint_sources(svc: Svc) -> list[CheckpointSourceView]:
    """Checkpoint sources, with the total size and count of their last scan."""
    return svc.checkpoints.sources()


@router.get("/mlflow-orphans", response_model=MlflowOrphanListing, tags=["checkpoints"])
async def mlflow_orphans(svc: Svc, refresh: bool = False) -> MlflowOrphanListing:
    """MLflow runs whose logged directory is in no checkpoint source (scans and searches
    cached 5 min, ``refresh`` forces them)."""
    return await svc.checkpoints.mlflow_orphans(refresh=refresh)


@router.post("/mlflow-orphans/delete", response_model=MlflowDeleteResult, tags=["checkpoints"])
async def delete_mlflow_orphans(req: MlflowDeleteRequest, svc: Svc) -> MlflowDeleteResult:
    """Delete MLflow runs without directory (MLflow trash, until ``mlflow gc``); a run that
    is not an orphan is refused."""
    return await svc.checkpoints.delete_mlflow_orphans(req.run_ids)


@router.get("/checkpoints/{source_id}", response_model=CheckpointListing, tags=["checkpoints"])
async def checkpoint_listing(source_id: str, svc: Svc, refresh: bool = False) -> CheckpointListing:
    """Runs and checkpoints of a source (``find`` on its machine, 5 min cache)."""
    return await svc.checkpoints.listing(source_id, refresh=refresh)


@router.post(
    "/checkpoints/{source_id}/delete",
    response_model=CheckpointDeleteResult,
    tags=["checkpoints"],
)
async def delete_checkpoints(
    source_id: str, req: CheckpointDeleteRequest, svc: Svc
) -> CheckpointDeleteResult:
    """Delete checkpoint files (paths relative to the root); empty group directories are
    removed. Partial failures are listed in ``failed``."""
    return await svc.checkpoints.delete(source_id, req.paths)


@router.post(
    "/checkpoints/{source_id}/delete-runs",
    response_model=CheckpointRunDeleteResult,
    tags=["checkpoints"],
)
async def delete_checkpoint_runs(
    source_id: str, req: CheckpointRunDeleteRequest, svc: Svc
) -> CheckpointRunDeleteResult:
    """Delete whole run directories of a source (everything in them, not only the
    checkpoints)."""
    return await svc.checkpoints.delete_runs(source_id, req.names)


@router.get("/ui-state", response_model=UiState, tags=["ui"])
async def get_ui_state(svc: Svc) -> UiState:
    """Open tabs, order, active tab, last selections."""
    return await svc.get_ui_state()


@router.put("/ui-state", response_model=UiState, tags=["ui"])
async def put_ui_state(state: UiState, svc: Svc) -> UiState:
    return await svc.put_ui_state(state)


@router.post("/preview", response_model=PreviewResult, tags=["runs"])
async def preview(sel: Selection, svc: Svc) -> PreviewResult:
    """Full command for a selection (dry run, nothing is executed)."""
    return await svc.preview(sel)


def _parse_statuses(status: str | None) -> list[RunStatus] | None:
    if not status:
        return None
    out: list[RunStatus] = []
    for part in re.split(r"[,\s]+", status.strip()):
        if part == "active":
            out += [s for s in RunStatus if s.is_active]
        elif part == "finished":
            out += [s for s in RunStatus if s.is_finished]
        else:
            try:
                out.append(RunStatus(part))
            except ValueError as exc:
                raise InvalidRequestError(
                    f"unknown status: {part!r} "
                    f"(possible: {', '.join(s.value for s in RunStatus)}, active, finished)"
                ) from exc
    return out


@router.get("/runs", response_model=list[RunView], tags=["runs"])
async def list_runs(
    svc: Svc,
    project: str | None = None,
    status: Annotated[
        str | None, Query(description="Comma-separated status(es), or active/finished")
    ] = None,
    limit: int | None = None,
) -> list[RunView]:
    """List of runs, most recent first."""
    runs = await svc.list_runs(project=project, status=_parse_statuses(status), limit=limit)
    return [RunView.of(r) for r in runs]


@router.post("/runs", response_model=RunView, status_code=201, tags=["runs"])
async def launch(req: LaunchRequest, svc: Svc) -> RunView:
    """Launch a run; return the run immediately with status ``launching``."""
    sel = Selection.model_validate(req.model_dump(exclude={"source"}))
    run = await svc.launch(sel, source=req.source)
    return RunView.of(run)


@router.get("/runs/{run_id}", response_model=RunView, tags=["runs"])
async def get_run(run_id: str, svc: Svc) -> RunView:
    return RunView.of(await svc.get_run(run_id))


@router.post("/runs/{run_id}/{action}", response_model=RunView, tags=["runs"])
async def run_action(
    run_id: str,
    action: RunAction,
    svc: Svc,
) -> RunView:
    """Action on a run: ``pause``, ``resume``, ``stop``, ``restart``, ``relaunch``."""
    run = await svc.action(run_id, action, source=RunSource.UI)
    return RunView.of(run)


@router.delete("/runs/{run_id}", status_code=204, tags=["runs"])
async def delete_run(run_id: str, svc: Svc) -> Response:
    """Delete the run and its container (``docker rm``)."""
    await svc.delete(run_id)
    return Response(status_code=204)


@router.get(
    "/runs/{run_id}/logs",
    tags=["runs"],
    response_class=Response,
    responses={200: {"content": {"text/plain": {}}}},
)
async def run_logs(
    run_id: str, svc: Svc, tail: int | None = None, download: bool = True
) -> Response:
    """Raw logs (ANSI included) as a download."""
    data = await svc.read_logs(run_id, tail)
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="trainhub-{run_id}.log"'
    return Response(content=data, media_type="text/plain; charset=utf-8", headers=headers)
