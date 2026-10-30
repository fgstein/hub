"""Pydantic schemas exchanged by the service layer (reused by the API and MCP)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..core.models import DockerOptions, Run, RunStatus
from ..core.status import elapsed_seconds, paused_seconds


class Selection(BaseModel):
    """Choice of a run: project, machine, image, venv, config (+ options)."""

    model_config = ConfigDict(extra="forbid")

    project: str
    machine: str
    image: str
    venv: str
    config: str
    overrides: list[str] = Field(
        default_factory=list, description="Optional extra arguments appended to the command"
    )
    cuda_visible_devices: str | None = Field(
        default=None, description="Value of CUDA_VISIBLE_DEVICES (e.g. '0,1')"
    )
    params: dict[str, str] = Field(
        default_factory=dict,
        description="Values of the free command parameters (non-reserved {name}); "
        "each value is split as a shell would, empty = nothing inserted",
    )
    checkpoint: str | None = Field(
        default=None,
        description="{checkpoint}: absolute path of the checkpoint on the machine (as listed "
        "by a checkpoint source); translated to the container path through the volumes",
    )


class LastSelection(BaseModel):
    machine: str | None = None
    image: str | None = None
    venv: str | None = None
    config: str | None = None
    cuda_visible_devices: str | None = None
    params: dict[str, str] = Field(default_factory=dict)


class UiState(BaseModel):
    """UI state persisted on the server."""

    open_tabs: list[str] = Field(default_factory=list)
    active_tab: str | None = None
    last_selection: dict[str, LastSelection] = Field(default_factory=dict)


class PreviewResult(BaseModel):
    text: str = Field(description="Readable command, over several lines")
    exact: str = Field(description="Exact command on one line, ready to paste into a terminal")
    argv: list[str]
    command: str = Field(description="Resolved generic command")
    container_name: str


class RunCount(BaseModel):
    """Number of runs launched on a config (all statuses) and, among them, active ones."""

    total: int
    active: int


class ConfigList(BaseModel):
    project: str
    machine: str | None
    configs: list[str] = Field(
        description="Config identifiers: path relative to the root, without extension"
    )
    root: str | None = Field(default=None, description="Configs root (discovery)")
    run_counts: dict[str, RunCount] = Field(
        default_factory=dict, description="Existing runs per config (missing: none)"
    )
    static: list[str] = Field(default_factory=list)
    discovered: list[str] | None = None
    discover_error: str | None = None
    cached_at: datetime | None = None


class MachineStatus(BaseModel):
    id: str
    reachable: bool | None = Field(default=None, description="None: not checked yet")
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
    command: str = Field(description="Effective generic command (override or YAML)")
    yaml_command: str
    command_overridden: bool
    params: list[str] = Field(
        default_factory=list,
        description="Free parameters of the effective command, to fill in at launch",
    )
    workdir: str | None
    machines: list[str]
    images: list[str]
    venvs: list[str]
    venv_images: dict[str, list[str] | None] = Field(
        description="For each allowed venv, compatible images (None: all)"
    )
    static_configs: list[str] | None
    discover: bool
    docker: DockerOptions
    extends: str | None = Field(default=None, description="Parent project (inventory)")
    accepts_checkpoint: bool = Field(
        default=False, description="The effective command contains {checkpoint}"
    )


class CheckpointSourceView(BaseModel):
    """A checkpoint source of the inventory (directory of runs on a machine)."""

    id: str
    label: str
    machine: str
    root: str
    layout: str
    size: int | None = Field(
        default=None, description="Total size of the checkpoints, in bytes (None: not scanned)"
    )
    count: int | None = Field(default=None, description="Number of checkpoints (last scan)")
    scanned_at: datetime | None = None


class CheckpointFile(BaseModel):
    path: str = Field(description="Path relative to the source root")
    name: str
    size: int = Field(description="Bytes")
    mtime: datetime
    runs: RunCount | None = Field(
        default=None, description="TrainHub runs launched with this checkpoint (missing: none)"
    )


class CheckpointGroup(BaseModel):
    name: str = Field(description="Value of {group} (empty if the layout has no {group})")
    path: str | None = Field(description="Directory relative to the root (None: not unique)")
    size: int
    count: int
    files: list[CheckpointFile]


class CheckpointRun(BaseModel):
    """A run directory ({name}) of a source, launched by TrainHub or not."""

    name: str
    size: int
    count: int
    mtime: datetime | None = Field(description="Most recent checkpoint, else the directory")
    groups: list[CheckpointGroup]
    others: list[CheckpointFile] = Field(
        default_factory=list, description="Checkpoint files of the run outside the layout"
    )


class CheckpointListing(BaseModel):
    source: str
    machine: str
    root: str
    layout: str
    size: int = Field(description="Total size of the checkpoints of the source, in bytes")
    count: int
    runs: list[CheckpointRun] = Field(description="Most recent first")
    others: list[CheckpointFile] = Field(
        default_factory=list, description="Checkpoint files directly under the root"
    )
    error: str | None = None
    scanned_at: datetime | None = None


class CheckpointDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(
        min_length=1, description="Checkpoint files to delete, relative to the source root"
    )


class CheckpointDeleteResult(BaseModel):
    source: str
    deleted: list[str] = Field(description="Deleted (or already missing) files")
    failed: dict[str, str] = Field(default_factory=dict, description="File → error")
    removed_dirs: list[str] = Field(
        default_factory=list, description="Group directories removed because empty"
    )
    freed: int = Field(description="Bytes freed (sizes of the last scan)")
    size: int = Field(description="New total size of the source")
    count: int


class InventoryView(BaseModel):
    machines: list[MachineView]
    images: list[ImageView]
    venvs: list[VenvView]
    projects: list[ProjectView]
    checkpoint_sources: list[CheckpointSourceView] = Field(default_factory=list)
    error: str | None = Field(default=None, description="Error of the last reload")
    executor: str


class RunView(Run):
    """Run enriched with computed values."""

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
    version: str = Field(description="TrainHub version of the server")


class Health(BaseModel):
    ok: bool
    executor: str
    machines: list[MachineStatus]
    inventory_error: str | None
    version: str


class GpuView(BaseModel):
    """A GPU of a machine (``nvidia-smi``)."""

    index: int
    name: str
    memory_used_mib: int | None
    memory_total_mib: int | None
    utilization_pct: int | None
    temperature_c: int | None
    processes: int = Field(description="Compute processes on the GPU (all users)")
    runs: list[str] = Field(
        description="Active TrainHub runs pinned to this GPU by CUDA_VISIBLE_DEVICES"
    )


class GpuRunView(BaseModel):
    id: str
    project: str
    status: RunStatus
    cuda_visible_devices: str | None = Field(description="None: every GPU visible")


class MachineGpus(BaseModel):
    """GPU state of a machine, with the active TrainHub runs placed on it."""

    machine: str
    gpus: list[GpuView] = Field(default_factory=list)
    active_runs: list[GpuRunView] = Field(default_factory=list)
    error: str | None = None
    checked_at: datetime


RUN_STATUSES = [s.value for s in RunStatus]
