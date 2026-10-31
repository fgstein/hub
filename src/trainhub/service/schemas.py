"""Pydantic schemas exchanged by the service layer (reused by the API and MCP)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.models import (
    CheckpointInference,
    DockerOptions,
    ExternalRun,
    MlflowRunRef,
    Run,
    RunStatus,
    Series,
)
from ..core.series import DEFAULT_RANGE, MAX_SERIES, draw_values
from ..core.status import elapsed_seconds, paused_seconds


class RandomRange(BaseModel):
    """Integer range a parameter is drawn from (bounds included)."""

    model_config = ConfigDict(extra="forbid")

    min: int = DEFAULT_RANGE[0]
    max: int = DEFAULT_RANGE[1]

    @model_validator(mode="after")
    def _check(self) -> RandomRange:
        if self.min > self.max:
            raise ValueError(f"empty range: min ({self.min}) > max ({self.max})")
        return self


class SeriesRequest(BaseModel):
    """Repeat the run several times in a row, in the same container."""

    model_config = ConfigDict(extra="forbid")

    count: int = Field(ge=1, le=MAX_SERIES, description="Number of repetitions")
    random: dict[str, RandomRange] = Field(
        default_factory=dict,
        description="Command parameters drawn at random for each repetition (distinct "
        "values when the range allows it); they override params",
    )
    values: list[dict[str, str]] | None = Field(
        default=None,
        description="Explicit values of the parameters that change, one dict per repetition "
        "(instead of random; e.g. to reproduce a series)",
    )
    continue_on_failure: bool = Field(
        default=False,
        description="Go on with the next repetitions after a failure (default: the series "
        "stops at the first failure)",
    )

    @model_validator(mode="after")
    def _check(self) -> SeriesRequest:
        if self.values is not None:
            if self.random:
                raise ValueError("series: give either random or values, not both")
            if len(self.values) != self.count:
                raise ValueError(
                    f"series: {len(self.values)} set(s) of values for {self.count} repetition(s)"
                )
        return self

    def plan(self) -> Series:
        """The repetitions, random values drawn now."""
        ranges = {k: (r.min, r.max) for k, r in self.random.items()}
        values = self.values if self.values is not None else draw_values(ranges, self.count)
        return Series(
            count=self.count,
            continue_on_failure=self.continue_on_failure,
            random=ranges,
            values=[dict(v) for v in values],
        )


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
        "by a checkpoint source); translated to the container path through the volumes "
        "(unchanged if none covers it)",
    )
    series: SeriesRequest | None = Field(
        default=None, description="Repeat the run several times in a row (same container)"
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
    machine: str | None = Field(description="null: the host running the TrainHub server")
    root: str
    layout: str
    size: int | None = Field(
        default=None, description="Total size of the checkpoints, in bytes (None: not scanned)"
    )
    count: int | None = Field(default=None, description="Number of checkpoints (last scan)")
    scanned_at: datetime | None = None
    mlflow_match: str | None = Field(
        default=None, description="MLflow key linking its run directories (mlflow.match)"
    )


class CheckpointFile(BaseModel):
    path: str = Field(description="Path relative to the source root")
    name: str
    size: int = Field(description="Bytes")
    mtime: datetime


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
    inferences: list[CheckpointInference] = Field(
        default_factory=list,
        description="Inferences made with the checkpoints of this directory (MLflow runs of "
        "mlflow.inference), most recent first",
    )
    mlflow_runs: list[MlflowRunRef] = Field(
        default_factory=list,
        description="MLflow runs logged from this directory, most recent first (empty: none "
        "found, or the source has no mlflow section)",
    )
    mlflow_logged: dict[str, str] = Field(
        default_factory=dict,
        description="Values of the keys of mlflow.show for the latest MLflow run (not deleted)",
    )


class CheckpointMlflowStatus(BaseModel):
    """Search of the MLflow runs of a checkpoint source."""

    match: str = Field(description="MLflow key holding the path (params.<key> / tags.<key>)")
    tracking_uri: str
    searched: int = Field(description="MLflow runs having the key")
    truncated: bool = Field(default=False, description="Search stopped before the end")
    searched_at: datetime | None = None
    error: str | None = None


class CheckpointListing(BaseModel):
    source: str
    machine: str | None = Field(description="null: the host running the TrainHub server")
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
    mlflow: CheckpointMlflowStatus | None = Field(
        default=None,
        description="Link of the run directories with MLflow (None: no mlflow.match)",
    )
    mlflow_inference: CheckpointMlflowStatus | None = Field(
        default=None, description="Search of the MLflow inferences (None: no mlflow.inference)"
    )


class MlflowOrphan(BaseModel):
    """MLflow run whose logged directory is in no checkpoint source (deleted, or elsewhere)."""

    run: MlflowRunRef
    path: str = Field(description="Directory logged by the run (value of match)")
    match: str = Field(description="MLflow key holding it")
    sources: list[str] = Field(description="Sources whose MLflow search found the run")


class MlflowOrphanListing(BaseModel):
    """MLflow runs without a directory on disk, over every checkpoint source."""

    enabled: bool = Field(description="At least one source has mlflow.match")
    runs: list[MlflowOrphan] = Field(description="Most recent first")
    searched: int = Field(description="MLflow runs read (all sources)")
    linked: int = Field(description="Of which with a directory in a source")
    truncated: bool = Field(default=False, description="An MLflow search stopped early")
    searched_at: datetime | None = Field(default=None, description="Oldest search used")
    mlflow_errors: dict[str, str] = Field(
        default_factory=dict, description="Source → MLflow error (its runs may be missing)"
    )
    scan_errors: dict[str, str] = Field(
        default_factory=dict,
        description="Source → scan error without any listing: runs of its directories may be "
        "shown as orphans by mistake",
    )


class MlflowDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_ids: list[str] = Field(
        min_length=1, max_length=1000, description="MLflow runs to delete (orphans only)"
    )


class MlflowDeleteResult(BaseModel):
    deleted: list[str] = Field(description="Deleted MLflow runs (MLflow trash)")
    failed: dict[str, str] = Field(default_factory=dict, description="Run id → error")
    remaining: int = Field(description="Orphans left")


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


class CheckpointRunDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    names: list[str] = Field(
        min_length=1,
        max_length=1000,
        description="Run directories ({name} of the layout) to delete entirely",
    )


class CheckpointRunDeleteResult(BaseModel):
    source: str
    deleted: list[str] = Field(description="Deleted (or already missing) run directories")
    failed: dict[str, str] = Field(default_factory=dict, description="Directory → error")
    freed: int = Field(
        description="Bytes of the checkpoints they held (last scan; other files not counted)"
    )
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
    failed: bool = Field(
        description="Its command failed, or its training (MLflow FAILED/KILLED) while it ran"
    )
    elapsed_s: float
    paused_s: float

    @classmethod
    def of(cls, run: Run, now: datetime | None = None) -> RunView:
        return cls(
            **run.model_dump(),
            active=run.status.is_active,
            failed=run.is_failure,
            elapsed_s=round(elapsed_seconds(run, now), 3),
            paused_s=round(paused_seconds(run, now), 3),
        )


class ExternalRunView(ExternalRun):
    """External run (found in MLflow, not launched by TrainHub), with computed values."""

    active: bool = Field(description="MLflow status RUNNING or SCHEDULED")
    failed: bool = Field(description="MLflow status FAILED or KILLED")

    @classmethod
    def of(cls, run: ExternalRun) -> ExternalRunView:
        return cls(**run.model_dump(), active=run.is_active, failed=run.is_failure)


class Snapshot(BaseModel):
    type: Literal["snapshot"] = "snapshot"
    seq: int
    inventory: InventoryView
    runs: list[RunView]
    external_runs: list[ExternalRunView] = Field(
        default_factory=list, description="MLflow runs not launched by TrainHub"
    )
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


class CpuView(BaseModel):
    """CPU and memory of a machine (``/proc``, on the host)."""

    cores: int
    usage_pct: float = Field(description="Usage of all the cores, over 0.5 s")
    per_core_pct: list[float] = Field(description="Usage of each core, over 0.5 s")
    load_avg: tuple[float, float, float] | None = Field(description="1, 5 and 15 min load")
    memory_used_mib: int | None
    memory_total_mib: int | None


class MachineLoad(BaseModel):
    """CPU and GPU load of a machine, with the active TrainHub runs placed on it."""

    machine: str
    reachable: bool
    cpu: CpuView | None = None
    cpu_error: str | None = None
    gpus: list[GpuView] = Field(default_factory=list)
    gpu_error: str | None = None
    active_runs: list[GpuRunView] = Field(default_factory=list)
    error: str | None = Field(default=None, description="Machine unreachable")
    checked_at: datetime


RUN_STATUSES = [s.value for s in RunStatus]
