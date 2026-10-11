"""Schémas Pydantic échangés par la couche service (réutilisés par l'API et le MCP)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..core.models import DockerOptions, Run, RunStatus
from ..core.status import elapsed_seconds, paused_seconds


class Selection(BaseModel):
    """Choix d'un run : projet, machine, image, venv, config (+ options)."""

    model_config = ConfigDict(extra="forbid")

    project: str
    machine: str
    image: str
    venv: str
    config: str
    overrides: list[str] = Field(default_factory=list, description="Overrides Hydra facultatifs")
    cuda_visible_devices: str | None = Field(
        default=None, description="Valeur de CUDA_VISIBLE_DEVICES (ex. '0,1')"
    )


class LastSelection(BaseModel):
    machine: str | None = None
    image: str | None = None
    venv: str | None = None
    config: str | None = None
    cuda_visible_devices: str | None = None


class UiState(BaseModel):
    """État d'interface persisté côté serveur."""

    open_tabs: list[str] = Field(default_factory=list)
    active_tab: str | None = None
    last_selection: dict[str, LastSelection] = Field(default_factory=dict)


class PreviewResult(BaseModel):
    text: str = Field(description="Commande lisible, sur plusieurs lignes")
    exact: str = Field(description="Commande exacte sur une ligne, copiable dans un terminal")
    argv: list[str]
    command: str = Field(description="Commande générique résolue")
    container_name: str


class ConfigList(BaseModel):
    project: str
    machine: str | None
    configs: list[str]
    static: list[str] = Field(default_factory=list)
    discovered: list[str] | None = None
    discover_error: str | None = None
    cached_at: datetime | None = None


class MachineStatus(BaseModel):
    id: str
    reachable: bool | None = Field(default=None, description="None : pas encore vérifié")
    last_check: datetime | None = None
    last_ok: datetime | None = None
    error: str | None = None
    latency_ms: float | None = None


class MachineView(BaseModel):
    id: str
    label: str | None
    host: str
    user: str | None
    port: int | None
    status: MachineStatus


class ImageView(BaseModel):
    id: str
    image: str
    label: str | None


class VenvView(BaseModel):
    id: str
    path: str
    label: str | None
    images: list[str] | None


class ProjectView(BaseModel):
    id: str
    label: str
    command: str = Field(description="Commande générique effective (surcharge ou YAML)")
    yaml_command: str
    command_overridden: bool
    workdir: str | None
    machines: list[str]
    images: list[str]
    venvs: list[str]
    venv_images: dict[str, list[str] | None] = Field(
        description="Pour chaque venv autorisé, images compatibles (None : toutes)"
    )
    static_configs: list[str] | None
    discover: bool
    docker: DockerOptions


class InventoryView(BaseModel):
    machines: list[MachineView]
    images: list[ImageView]
    venvs: list[VenvView]
    projects: list[ProjectView]
    error: str | None = Field(default=None, description="Erreur du dernier rechargement")
    executor: str


class RunView(Run):
    """Run enrichi des valeurs calculées."""

    active: bool
    elapsed_s: float
    paused_s: float

    @classmethod
    def of(cls, run: Run, now: datetime | None = None) -> RunView:
        return cls(
            **run.model_dump(),
            active=run.status.is_active,
            elapsed_s=round(elapsed_seconds(run, now), 3),
            paused_s=round(paused_seconds(run, now), 3),
        )


class Snapshot(BaseModel):
    type: Literal["snapshot"] = "snapshot"
    seq: int
    inventory: InventoryView
    runs: list[RunView]
    ui_state: UiState
    server_time: datetime


class Health(BaseModel):
    ok: bool
    executor: str
    machines: list[MachineStatus]
    inventory_error: str | None
    version: str


RUN_STATUSES = [s.value for s in RunStatus]
