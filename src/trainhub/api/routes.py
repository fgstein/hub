"""Routes REST (``/api``) : actions et lectures, toutes déléguées à la couche service."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from ..core.models import RunSource, RunStatus
from ..service import RunAction, RunService
from ..service.runs import InvalidRequestError
from ..service.schemas import (
    ConfigList,
    Health,
    InventoryView,
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
    """Mise à jour d'un projet : nouvelle commande, ou retour à la valeur du YAML."""

    command: str | None = Field(default=None, description="Nouvelle commande générique")
    reset: bool = Field(default=False, description="Revenir à la commande du YAML")


class LaunchRequest(Selection):
    source: RunSource = RunSource.UI


@router.get("/health", response_model=Health, tags=["système"])
async def health(svc: Svc) -> Health:
    """Santé du serveur et joignabilité des machines."""
    return svc.health()


@router.get("/inventory", response_model=InventoryView, tags=["inventaire"])
async def inventory(svc: Svc) -> InventoryView:
    """Machines, images, venvs, projets (vue fusionnée YAML + base)."""
    return svc.inventory_view()


@router.get("/projects/{project_id}/configs", response_model=ConfigList, tags=["inventaire"])
async def project_configs(
    project_id: str,
    svc: Svc,
    machine: str | None = None,
    refresh: bool = False,
) -> ConfigList:
    """Configs disponibles (statiques ou découvertes sur la machine, cache 60 s)."""
    return await svc.get_configs(project_id, machine, refresh=refresh)


@router.patch("/projects/{project_id}", response_model=ProjectView, tags=["inventaire"])
async def patch_project(project_id: str, patch: ProjectPatch, svc: Svc) -> ProjectView:
    """Met à jour la commande d'un projet, ou la remet à la valeur du YAML."""
    if patch.reset:
        return await svc.set_project_command(project_id, None)
    if patch.command is None:
        raise InvalidRequestError("indiquer 'command' ou 'reset: true'")
    return await svc.set_project_command(project_id, patch.command)


@router.get("/ui-state", response_model=UiState, tags=["interface"])
async def get_ui_state(svc: Svc) -> UiState:
    """Onglets ouverts, ordre, onglet actif, dernières sélections."""
    return await svc.get_ui_state()


@router.put("/ui-state", response_model=UiState, tags=["interface"])
async def put_ui_state(state: UiState, svc: Svc) -> UiState:
    return await svc.put_ui_state(state)


@router.post("/preview", response_model=PreviewResult, tags=["runs"])
async def preview(sel: Selection, svc: Svc) -> PreviewResult:
    """Commande complète pour une sélection (dry run, rien n'est exécuté)."""
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
                    f"statut inconnu : {part!r} "
                    f"(possibles : {', '.join(s.value for s in RunStatus)}, active, finished)"
                ) from exc
    return out


@router.get("/runs", response_model=list[RunView], tags=["runs"])
async def list_runs(
    svc: Svc,
    project: str | None = None,
    status: Annotated[
        str | None, Query(description="Statut(s) séparés par des virgules, ou active/finished")
    ] = None,
    limit: int | None = None,
) -> list[RunView]:
    """Liste des runs, du plus récent au plus ancien."""
    runs = await svc.list_runs(project=project, status=_parse_statuses(status), limit=limit)
    return [RunView.of(r) for r in runs]


@router.post("/runs", response_model=RunView, status_code=201, tags=["runs"])
async def launch(req: LaunchRequest, svc: Svc) -> RunView:
    """Lance un run ; renvoie immédiatement le run en statut ``launching``."""
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
    """Action sur un run : ``pause``, ``resume``, ``stop``, ``restart``, ``relaunch``."""
    run = await svc.action(run_id, action, source=RunSource.UI)
    return RunView.of(run)


@router.delete("/runs/{run_id}", status_code=204, tags=["runs"])
async def delete_run(run_id: str, svc: Svc) -> Response:
    """Supprime le run et son conteneur (``docker rm``)."""
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
    """Logs bruts (ANSI compris) en téléchargement."""
    data = await svc.read_logs(run_id, tail)
    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="trainhub-{run_id}.log"'
    return Response(content=data, media_type="text/plain; charset=utf-8", headers=headers)
