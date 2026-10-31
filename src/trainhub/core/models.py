"""Data models: inventory (YAML, read-only) and runs (SQLite).

These models do no I/O; they are validated by Pydantic v2.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal, TypeVar

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .checkpoints import parse_layout

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"

#: Placeholders filled in by TrainHub. Any other ``{name}`` is a free run parameter,
#: entered before launch (text field of the draft).
ALLOWED_PLACEHOLDERS = frozenset(
    {"config", "config_name", "run_id", "machine", "project", "checkpoint"}
)
#: Identifier of the always-available venv: no activation (the image's Python).
SYSTEM_VENV_ID = "system"
#: A placeholder is ``{name}`` not preceded by ``$`` (to let ``${VAR}`` and Hydra/OmegaConf
#: interpolations ``${oc.env:X}`` through).
PLACEHOLDER_RE = re.compile(r"(?<!\$)\{([A-Za-z_][A-Za-z0-9_]*)\}")

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "local"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def parse_docker_command(v: object) -> object:
    """``"sudo -n docker"`` or ``["sudo", "-n", "docker"]`` → non-empty argument list."""
    import shlex

    if v is None:
        return v
    if isinstance(v, str):
        try:
            v = shlex.split(v)
        except ValueError as exc:
            raise ValueError(f"invalid docker_command: {exc}") from exc
    if isinstance(v, list | tuple):
        if not v or not all(isinstance(a, str) and a for a in v):
            raise ValueError("docker_command: empty command or empty arguments")
        return list(v)
    return v


def command_params(command: str) -> list[str]:
    """Free parameters of a command (non-reserved placeholders), in order of appearance."""
    names = PLACEHOLDER_RE.findall(command)
    return list(dict.fromkeys(n for n in names if n not in ALLOWED_PLACEHOLDERS))


def validate_command_template(command: str) -> str:
    """Validate a generic command; raise ``ValueError`` with a readable message."""
    if not command.strip():
        raise ValueError("the command is empty")
    names = PLACEHOLDER_RE.findall(command)
    if "config" not in names:
        raise ValueError("the command must contain the {config} placeholder")
    return command


# --------------------------------------------------------------------------- inventory


class Machine(_Strict):
    """Remote machine reachable over SSH (or ``localhost``)."""

    id: str = Field(pattern=ID_PATTERN)
    label: str | None = None
    host: str = Field(min_length=1)
    user: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    docker_command: list[str] | None = Field(
        default=None,
        description="Docker command on this machine (wrapper, sudo…); default: the inventory's",
    )

    @field_validator("docker_command", mode="before")
    @classmethod
    def _parse_docker(cls, v: object) -> object:
        return parse_docker_command(v)

    @property
    def display(self) -> str:
        return self.label or self.id

    @property
    def docker(self) -> list[str]:
        """argv of the effective Docker command on this machine."""
        return list(self.docker_command or ["docker"])

    @property
    def is_local(self) -> bool:
        """``True`` if commands run without SSH on the TrainHub host."""
        return self.host in LOCAL_HOSTS and self.user is None and self.port is None

    @property
    def ssh_target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host


class Image(_Strict):
    """Docker image available on the machines."""

    id: str = Field(pattern=ID_PATTERN)
    image: str = Field(min_length=1)
    label: str | None = None


class Venv(_Strict):
    """Python virtual environment present in the images."""

    id: str = Field(pattern=ID_PATTERN)
    path: str = Field(min_length=1, description="Path of the venv in the container")
    label: str | None = None
    images: list[str] | None = Field(
        default=None, description="Restrict the venv to these images (default: all)"
    )

    @property
    def is_system(self) -> bool:
        """No venv to activate (the image's Python)."""
        return self.path == ""


#: Implicit ``system`` venv: no activation.
SYSTEM_VENV = Venv.model_construct(
    id=SYSTEM_VENV_ID, path="", label="no venv (image Python)", images=None
)


class DockerOptions(_Strict):
    """Project-specific ``docker run`` options."""

    gpus: str | None = None
    shm_size: str | None = None
    volumes: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    extra_args: list[str] = Field(default_factory=list)
    tty: bool = True
    tty_size: tuple[int, int] = Field(
        default=(100, 40),
        description="Size (columns, rows) of the container's terminal. A docker run -d -t has "
        "a 0x0 pseudo-terminal, in which tqdm prints nothing.",
    )
    stop_timeout: int = Field(default=30, ge=0)
    keep_alive: bool = Field(
        default=True,
        description="Keep the container (shell available) when the command finishes",
    )

    @field_validator("tty_size")
    @classmethod
    def _check_tty_size(cls, v: tuple[int, int]) -> tuple[int, int]:
        cols, rows = v
        if not (20 <= cols <= 1000 and 5 <= rows <= 500):
            raise ValueError("tty_size: columns between 20 and 1000, rows between 5 and 500")
        return v

    @field_validator("env", mode="before")
    @classmethod
    def _env_to_str(cls, v: object) -> object:
        if isinstance(v, dict):
            return {str(k): ("" if val is None else str(val)) for k, val in v.items()}
        return v


class ConfigDiscover(_Strict):
    """Recursive discovery of config files (``find``) under a root, on the chosen machine.

    A config is identified by its path relative to the root, without extension
    (e.g. ``experiments/unet_large``). ``{config_name}`` gives the basename only.
    """

    path: str = Field(
        min_length=1,
        validation_alias=AliasChoices("path", "root"),
        description="Root of the configs on the host (absolute, or relative to home if ~/…)",
    )
    pattern: str = Field(default="*.yaml", description="File name pattern")
    exclude: list[str] = Field(
        default_factory=list,
        description="Glob patterns of relative paths to ignore, e.g. hydra/*, outputs/*",
    )
    max_depth: int = Field(default=10, ge=1, le=50)

    @field_validator("path")
    @classmethod
    def _check_path(cls, v: str) -> str:
        if v.startswith("-") or "\n" in v:
            raise ValueError("invalid configs root")
        return v


class ConfigSource(_Strict):
    """Static list of configs and/or discovery on the machine."""

    static: list[str] | None = None
    discover: ConfigDiscover | None = None

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, data: object) -> object:
        """Short form: ``configs: {root: …, pattern: …, exclude: […]}``."""
        if isinstance(data, dict) and "discover" not in data and ("root" in data or "path" in data):
            data = dict(data)
            keys = ("root", "path", "pattern", "exclude", "max_depth")
            data["discover"] = {k: data.pop(k) for k in keys if k in data}
        return data

    @model_validator(mode="after")
    def _one_source(self) -> ConfigSource:
        if self.static is None and self.discover is None:
            raise ValueError("configs: define 'static' and/or 'discover'")
        return self


class SourceFile(_Strict):
    """File sourced in the container before the command.

    Absolute path, ``~/…``, or relative to the project's ``workdir``. Short form: a string.
    """

    path: str = Field(min_length=1)
    optional: bool = Field(default=False, description="Ignored if it does not exist")

    @model_validator(mode="before")
    @classmethod
    def _from_str(cls, data: object) -> object:
        return {"path": data} if isinstance(data, str) else data

    @field_validator("path")
    @classmethod
    def _check_path(cls, v: str) -> str:
        if any(c in v for c in "\n\r\x00"):
            raise ValueError("invalid path (control character)")
        return v


class MlflowOptions(_Strict):
    """MLflow tracking of the runs (``mlflow`` section, global and/or per project).

    TrainHub finds the MLflow run of each of its runs by a tag (``run_tag``) whose value is
    the TrainHub run id, then reads its status, a parameter (maximum epoch) and a metric
    (current epoch). Fields left empty in a project take the global value.
    """

    enabled: bool = True
    tracking_uri: str | None = Field(
        default=None, description="MLflow server URL, as seen from TrainHub (http[s]://…)"
    )
    experiments: list[str] | None = Field(
        default=None,
        description="Experiment names searched (default: all). The running MLflow runs of "
        "the experiments a project names that TrainHub did not launch are shown in that "
        "project as external runs",
    )
    run_tag: str | None = Field(
        default=None, description="Tag of the MLflow run holding the TrainHub run id"
    )
    max_epochs_param: str | None = Field(
        default=None, description="MLflow parameter holding the maximum epoch"
    )
    epoch_metric: str | None = Field(
        default=None, description="MLflow metric holding the current epoch"
    )
    epoch_offset: int | None = Field(
        default=None, description="Added to the epoch metric (1 if it counts from 0)"
    )
    inject_env: bool | None = Field(
        default=None,
        description="Pass MLFLOW_RUN_CONTEXT (run_tag = run id) and TRAINHUB_RUN_ID to the "
        "container, so that MLflow tags the run without code change",
    )
    stale_after_min: float | None = Field(
        default=None,
        gt=0,
        description="A running MLflow run with no new metric for this long is shown as "
        "stalled (orange); default 30",
    )
    poll_interval_s: float | None = Field(
        default=None, ge=2, description="Polling period of MLflow (global section only)"
    )

    @field_validator("tracking_uri")
    @classmethod
    def _check_uri(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not re.match(r"^https?://[^\s]+$", v):
            raise ValueError("tracking_uri: an http:// or https:// URL is expected")
        return v.rstrip("/")

    @field_validator("run_tag", "max_epochs_param", "epoch_metric")
    @classmethod
    def _check_key(cls, v: str | None) -> str | None:
        if v is not None and (not v or any(c in v for c in "`'\"\n")):
            raise ValueError("invalid MLflow key (empty, or quote character)")
        return v


class MlflowConfig(_Strict):
    """Effective MLflow settings of a project (global section merged with the project's)."""

    tracking_uri: str
    experiments: list[str] | None = None
    run_tag: str = "trainhub.run_id"
    max_epochs_param: str = "max_epochs"
    epoch_metric: str = "epoch"
    epoch_offset: int = 0
    inject_env: bool = True
    stale_after_min: float = 30.0


class Project(_Strict):
    """Project = one tab of the interface, with its generic command."""

    id: str = Field(pattern=ID_PATTERN)
    label: str | None = None
    extends: str | None = Field(
        default=None,
        description="Project whose fields are inherited (resolved when the inventory is "
        "loaded); the fields given here replace them, docker options key by key",
    )
    command: str
    workdir: str | None = None
    machines: list[str] | None = None
    images: list[str] | None = None
    venvs: list[str] | None = None
    docker: DockerOptions = Field(default_factory=DockerOptions)
    configs: ConfigSource = Field(default_factory=lambda: ConfigSource(static=[]))
    env_files: list[SourceFile] = Field(
        default_factory=list,
        description="Variable files (KEY=value or export KEY=value) sourced and exported "
        "before the command, e.g. .env (relative to the workdir)",
    )
    source: list[SourceFile] = Field(
        default_factory=list,
        description="Scripts sourced before the command (after env_files), in order",
    )
    mlflow: MlflowOptions | None = Field(
        default=None, description="MLflow settings of the project (override the global ones)"
    )

    @field_validator("command")
    @classmethod
    def _check_command(cls, v: str) -> str:
        return validate_command_template(v)

    @property
    def display(self) -> str:
        return self.label or self.id

    @property
    def accepts_checkpoint(self) -> bool:
        return "checkpoint" in PLACEHOLDER_RE.findall(self.command)


#: Fields of a parent project that a project with ``extends`` does not inherit.
_NOT_INHERITED = frozenset({"id", "label", "extends"})


def resolve_extends(projects: list[object]) -> list[object]:
    """Raw projects (YAML) with ``extends`` resolved: the parent's fields, then the child's.

    ``docker`` is merged key by key; every other field given by the child replaces the
    parent's. Chains are allowed; an unknown parent or a cycle raises ``ValueError``.
    """
    by_id = {p["id"]: p for p in projects if isinstance(p, dict) and isinstance(p.get("id"), str)}
    resolved: dict[str, dict[str, object]] = {}

    def resolve(pid: str, chain: tuple[str, ...]) -> dict[str, object]:
        if pid in resolved:
            return resolved[pid]
        raw = by_id[pid]
        parent_id = raw.get("extends")
        if parent_id is None:
            resolved[pid] = raw
            return raw
        if not isinstance(parent_id, str) or parent_id not in by_id:
            known = ", ".join(sorted(by_id)) or "none"
            raise ValueError(
                f"project '{pid}': extends unknown project '{parent_id}'; projects: {known}"
            )
        if parent_id in chain:
            raise ValueError(f"projects: extends cycle ({' → '.join([*chain, pid, parent_id])})")
        parent = resolve(parent_id, (*chain, pid))
        merged = {k: v for k, v in parent.items() if k not in _NOT_INHERITED}
        for k, v in raw.items():
            pv = merged.get(k)
            if k == "docker" and isinstance(pv, dict) and isinstance(v, dict):
                merged[k] = {**pv, **v}
            else:
                merged[k] = v
        resolved[pid] = merged
        return merged

    return [
        resolve(p["id"], ())
        if isinstance(p, dict) and isinstance(p.get("id"), str) and p["id"] in by_id
        else p
        for p in projects
    ]


def split_mlflow_key(v: str) -> tuple[Literal["params", "tags"], str]:
    """``params.<key>`` / ``tags.<key>`` → (kind, key); raise ``ValueError`` otherwise."""
    kind, _, key = v.partition(".")
    if kind not in ("params", "tags") or not key:
        raise ValueError(f"{v!r}: params.<key> or tags.<key> expected")
    if any(c in key for c in "`'\"\n"):
        raise ValueError(f"{v!r}: invalid MLflow key (quote character)")
    return ("params" if kind == "params" else "tags"), key


def split_shown_key(v: str) -> tuple[Literal["params", "tags", "metrics"], str]:
    """Like :func:`split_mlflow_key`, ``metrics.<key>`` accepted too (values shown, not
    searched)."""
    kind, _, key = v.partition(".")
    if kind == "metrics" and key:
        if any(c in key for c in "`'\"\n"):
            raise ValueError(f"{v!r}: invalid MLflow key (quote character)")
        return "metrics", key
    try:
        return split_mlflow_key(v)
    except ValueError:
        raise ValueError(f"{v!r}: params.<key>, tags.<key> or metrics.<key> expected") from None


class CheckpointInferenceMlflow(_Strict):
    """MLflow runs of inferences made with the checkpoints of a source.

    An inference belongs to the checkpoint whose path is logged under ``match`` (e.g.
    ``params.ckpt_path``), possibly seen from the container: the run directory is the last
    component of the path that is a run of the source, the rest is the checkpoint.
    """

    match: str = Field(
        description="MLflow key holding the path of the checkpoint: params.<key> or "
        "tags.<key> (e.g. params.ckpt_path)",
    )
    experiments: list[str] | None = Field(
        default=None,
        description="Experiments of the inferences (default: those of the mlflow section)",
    )
    show: list[str] = Field(
        default_factory=list,
        description="Parameters, tags or metrics shown for each inference "
        "(params.<key> / tags.<key> / metrics.<key>: last value)",
    )
    dataset_name: str | None = Field(
        default=None,
        description="MLflow key holding the name of the dataset of the inference "
        "(params.<key> / tags.<key>): shown as a button that copies its path",
    )
    dataset_path: str | None = Field(
        default=None,
        description="MLflow key holding the path of the dataset (copied by the dataset button)",
    )
    mapping_name: str | None = Field(
        default=None, description="MLflow key holding the name of the mapping (badge)"
    )

    @field_validator("match")
    @classmethod
    def _check_match(cls, v: str) -> str:
        split_mlflow_key(v)
        return v

    @field_validator("dataset_name", "dataset_path", "mapping_name")
    @classmethod
    def _check_special(cls, v: str | None) -> str | None:
        if v is not None:
            split_mlflow_key(v)
        return v

    @property
    def keys(self) -> tuple[str, ...]:
        """MLflow keys read for each inference."""
        special = (self.dataset_name, self.dataset_path, self.mapping_name)
        return tuple(dict.fromkeys([*self.show, *(k for k in special if k)]))

    @field_validator("show")
    @classmethod
    def _check_show(cls, v: list[str]) -> list[str]:
        for key in v:
            split_shown_key(key)
        return v


class CheckpointMlflow(_Strict):
    """Link between the run directories of a checkpoint source and the MLflow runs.

    A run directory ``{name}`` belongs to the MLflow run whose parameter or tag ``match``
    holds a path with ``{name}`` as a component, e.g. the checkpoint directory logged by
    Lightning (``params.callbacks/model_checkpoint/dirpath`` =
    ``/workspace/logs/train/runs/2026-10-30_14-02-11/checkpoints``). Paths may be seen from
    the container: only the components are compared. The MLflow server is the global one.
    """

    enabled: bool = True
    match: str | None = Field(
        default=None,
        description="MLflow key holding the path of the run directory: params.<key> or "
        "tags.<key> (e.g. params.callbacks/model_checkpoint/dirpath)",
    )
    experiments: list[str] | None = Field(
        default=None, description="Experiment names searched (default: those of mlflow)"
    )
    show: list[str] = Field(
        default_factory=list,
        description="Parameters, tags or metrics of the training run shown for each run "
        "directory (params.<key> / tags.<key> / metrics.<key>: last value)",
    )
    inference: CheckpointInferenceMlflow | None = Field(
        default=None, description="MLflow runs of the inferences made with the checkpoints"
    )

    @field_validator("match")
    @classmethod
    def _check_match(cls, v: str | None) -> str | None:
        if v is not None:
            split_mlflow_key(v)
        return v

    @field_validator("show")
    @classmethod
    def _check_show(cls, v: list[str]) -> list[str]:
        for key in v:
            split_shown_key(key)
        return v

    @model_validator(mode="after")
    def _something(self) -> CheckpointMlflow:
        if self.match is None and self.inference is None:
            raise ValueError("mlflow: define 'match' and/or 'inference'")
        return self


class CheckpointSource(_Strict):
    """Directory of runs on a machine, whose checkpoints are listed in the Checkpoints tab.

    Independent of the projects: the runs may have been launched by TrainHub or not.
    ``layout`` describes the tree under ``root``: ``{name}`` (first level) is a run,
    ``{group}`` an optional grouping level, other directories are literal (globs allowed),
    the last component is the checkpoint file pattern.
    """

    id: str = Field(pattern=ID_PATTERN)
    label: str | None = None
    machine: str | None = Field(
        default=None,
        description="Machine where the files are listed and deleted; null (or omitted): the "
        "host running the TrainHub server, without SSH",
    )
    root: str = Field(min_length=1, description="Absolute directory of the runs, on the machine")
    layout: str = Field(
        default="{name}/checkpoints/{group}/*.ckpt",
        description="Tree under the root, e.g. {name}/checkpoints/{group}/*.ckpt",
    )
    mlflow: CheckpointMlflow | None = Field(
        default=None, description="Find the MLflow run of each run directory"
    )

    @field_validator("root")
    @classmethod
    def _check_root(cls, v: str) -> str:
        if not v.startswith("/") or any(c in v for c in "\n\r\x00"):
            raise ValueError("root must be an absolute path")
        v = v.rstrip("/")
        if not v:
            raise ValueError("root: / is not allowed")
        return v

    @field_validator("layout")
    @classmethod
    def _check_layout(cls, v: str) -> str:
        parse_layout(v)
        return v

    @property
    def display(self) -> str:
        return self.label or self.id


MachineState = Literal["ok", "flaky", "down"]


class SimulationSettings(_Strict):
    """Settings of the ``fake`` mode (``simulation`` section of ``inventory.dev.yaml``)."""

    duration_s: tuple[float, float] = (60.0, 300.0)
    log_rate: float = Field(default=8.0, gt=0, description="Log lines per second")
    failure_prob: float = Field(default=0.1, ge=0, le=1)
    time_scale: float = Field(default=1.0, gt=0, description="Time acceleration factor")
    latency_ms: tuple[float, float] = (50.0, 300.0)
    machines: dict[str, MachineState] = Field(default_factory=dict)
    configs: dict[str, list[str]] = Field(
        default_factory=dict, description="Simulated “discovered” configs, per project"
    )
    checkpoints: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Simulated run directories, per checkpoint source (default: a few)",
    )
    flaky_period_s: float = Field(default=40.0, gt=0)
    gpus: int = Field(default=4, ge=0, description="Simulated GPUs per machine (nvidia-smi)")
    cpus: int = Field(default=64, ge=1, description="Simulated CPU cores per machine")
    seed: int | None = None


class Inventory(_Strict):
    """Validated content of ``inventory.yaml``."""

    machines: list[Machine] = Field(default_factory=list)
    images: list[Image] = Field(default_factory=list)
    venvs: list[Venv] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    checkpoints: list[CheckpointSource] = Field(
        default_factory=list, description="Checkpoint sources (Checkpoints tab)"
    )
    docker_command: list[str] = Field(
        default_factory=lambda: ["docker"],
        description="Default Docker command on the machines: binary, path or wrapper "
        "(e.g. 'sudo -n docker', '/opt/bin/mydocker'). Can be overridden per machine.",
    )
    mlflow: MlflowOptions | None = Field(
        default=None, description="MLflow tracking (progress bar and training status)"
    )
    simulation: SimulationSettings | None = None

    @model_validator(mode="before")
    @classmethod
    def _extends(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("projects"), list):
            data = {**data, "projects": resolve_extends(data["projects"])}
        return data

    @field_validator("docker_command", mode="before")
    @classmethod
    def _parse_docker(cls, v: object) -> object:
        return ["docker"] if v is None else parse_docker_command(v)

    def mlflow_config(self, project: Project) -> MlflowConfig | None:
        """Effective MLflow settings of ``project`` (``None``: MLflow not used)."""
        if project.mlflow is not None and not project.mlflow.enabled:
            return None
        merged: dict[str, object] = {}
        for layer in (self.mlflow, project.mlflow):
            if layer is None or not layer.enabled:
                continue
            for k, v in layer.model_dump(exclude={"enabled", "poll_interval_s"}).items():
                if v is not None:
                    merged[k] = v
        if not merged.get("tracking_uri"):
            return None
        return MlflowConfig.model_validate(merged)

    def checkpoint_mlflow(
        self, src: CheckpointSource, *, inference: bool = False
    ) -> MlflowConfig | None:
        """MLflow server and experiments to search for the training runs of ``src``, or its
        inferences (``None``: not configured)."""
        ml = src.mlflow
        if ml is None or not ml.enabled or (ml.inference is None if inference else not ml.match):
            return None
        if self.mlflow is None or not self.mlflow.enabled or not self.mlflow.tracking_uri:
            return None
        experiments = ml.inference.experiments if inference and ml.inference else ml.experiments
        return MlflowConfig(
            tracking_uri=self.mlflow.tracking_uri,
            experiments=experiments or self.mlflow.experiments,
            run_tag=self.mlflow.run_tag or MlflowConfig.model_fields["run_tag"].default,
        )

    @property
    def mlflow_poll_interval(self) -> float:
        return (self.mlflow.poll_interval_s if self.mlflow else None) or 30.0

    @model_validator(mode="after")
    def _apply_docker_command(self) -> Inventory:
        # Each machine carries its effective command (its own, else the inventory's).
        machines = [
            m if m.docker_command else m.model_copy(update={"docker_command": self.docker_command})
            for m in self.machines
        ]
        object.__setattr__(self, "machines", machines)
        return self

    @model_validator(mode="after")
    def _check_refs(self) -> Inventory:
        for kind in ("machines", "images", "venvs", "projects", "checkpoints"):
            seen: set[str] = set()
            for item in getattr(self, kind):
                if item.id in seen:
                    raise ValueError(f"{kind}: duplicate id '{item.id}'")
                seen.add(item.id)
        machines = {m.id for m in self.machines}
        images = {i.id for i in self.images}
        venvs = {v.id for v in self.venvs} | {SYSTEM_VENV_ID}
        for v in self.venvs:
            for ref in v.images or []:
                if ref not in images:
                    raise ValueError(f"venv '{v.id}': unknown image '{ref}'")
        for p in self.projects:
            for field, known in (("machines", machines), ("images", images), ("venvs", venvs)):
                for ref in getattr(p, field) or []:
                    if ref not in known:
                        raise ValueError(f"project '{p.id}': unknown {field[:-1]} '{ref}'")
        for c in self.checkpoints:
            if c.machine is not None and c.machine not in machines:
                raise ValueError(f"checkpoints '{c.id}': unknown machine '{c.machine}'")
            if (
                c.mlflow is not None
                and c.mlflow.enabled
                and not (self.mlflow and self.mlflow.tracking_uri)
            ):
                raise ValueError(
                    f"checkpoints '{c.id}': mlflow needs the global mlflow.tracking_uri"
                )
        return self

    # -- access --------------------------------------------------------------

    def machine(self, mid: str) -> Machine:
        return _find(self.machines, mid, "machine")

    def image(self, iid: str) -> Image:
        return _find(self.images, iid, "image")

    def venv(self, vid: str) -> Venv:
        if vid == SYSTEM_VENV_ID and all(v.id != vid for v in self.venvs):
            return SYSTEM_VENV
        return _find(self.venvs, vid, "venv")

    def project(self, pid: str) -> Project:
        return _find(self.projects, pid, "project")

    def checkpoint_source(self, sid: str) -> CheckpointSource:
        return _find(self.checkpoints, sid, "checkpoint source")

    def allowed_machines(self, project: Project) -> list[Machine]:
        if project.machines is None:
            return list(self.machines)
        return [self.machine(m) for m in project.machines]

    def allowed_images(self, project: Project) -> list[Image]:
        if project.images is None:
            return list(self.images)
        return [self.image(i) for i in project.images]

    def allowed_venvs(self, project: Project, image: str | None = None) -> list[Venv]:
        """Usable venvs; ``system`` (no venv) is always offered, last."""
        venvs = list(self.venvs) if project.venvs is None else [self.venv(v) for v in project.venvs]
        if image is not None:
            venvs = [v for v in venvs if v.images is None or image in v.images]
        if not any(v.id == SYSTEM_VENV_ID for v in venvs):
            venvs.append(SYSTEM_VENV)
        return venvs


class UnknownResourceError(KeyError):
    """Resource missing from the inventory."""

    def __init__(self, kind: str, rid: str, available: list[str]) -> None:
        self.kind, self.rid, self.available = kind, rid, available
        super().__init__(rid)

    def __str__(self) -> str:
        avail = ", ".join(self.available) or "none"
        return f"unknown {self.kind} '{self.rid}'; available: {avail}"


_T = TypeVar("_T", Machine, Image, Venv, Project, CheckpointSource)


def _find(items: list[_T], rid: str, kind: str) -> _T:
    for it in items:
        if it.id == rid:
            return it
    raise UnknownResourceError(kind, rid, [it.id for it in items])


# --------------------------------------------------------------------------- runs


class RunStatus(StrEnum):
    """Status of a run (section 4.3)."""

    LAUNCHING = "launching"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"
    UNREACHABLE = "unreachable"

    @property
    def is_active(self) -> bool:
        return self in ACTIVE_STATUSES

    @property
    def is_finished(self) -> bool:
        return self in FINISHED_STATUSES


ACTIVE_STATUSES = frozenset(
    {
        RunStatus.LAUNCHING,
        RunStatus.RUNNING,
        RunStatus.PAUSED,
        RunStatus.STOPPING,
        RunStatus.UNREACHABLE,
    }
)
FINISHED_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.STOPPED})


class MlflowInfo(BaseModel):
    """What TrainHub read of the MLflow run matching a TrainHub run."""

    model_config = ConfigDict(extra="ignore")

    run_id: str | None = Field(default=None, description="None: no MLflow run found yet")
    experiment_id: str | None = None
    status: str | None = Field(
        default=None, description="MLflow status: RUNNING, SCHEDULED, FINISHED, FAILED, KILLED"
    )
    epoch: float | None = Field(default=None, description="Current epoch (metric + offset)")
    max_epochs: float | None = Field(default=None, description="Maximum epoch (parameter)")
    url: str | None = Field(default=None, description="Page of the run in the MLflow UI")
    started_at: datetime | None = Field(default=None, description="Start of the MLflow run")
    finished_at: datetime | None = Field(default=None, description="End of the MLflow run")
    last_activity: datetime | None = Field(
        default=None, description="Time of the most recent metric logged"
    )
    stale: bool = Field(
        default=False,
        description="Running, but no new metric for stale_after_min (training stalled?)",
    )
    error: str | None = Field(default=None, description="Last error querying MLflow")


class MlflowRunRef(BaseModel):
    """An MLflow run found for a run directory of a checkpoint source."""

    run_id: str
    run_name: str | None = None
    experiment_id: str | None = None
    status: str | None = Field(
        default=None, description="MLflow status: RUNNING, SCHEDULED, FINISHED, FAILED, KILLED"
    )
    started_at: datetime | None = None
    url: str | None = Field(default=None, description="Page of the run in the MLflow UI")
    deleted: bool = Field(
        default=False, description="In the MLflow trash (lifecycle_stage deleted, until mlflow gc)"
    )


class CheckpointInference(BaseModel):
    """An inference made with a checkpoint: an MLflow run of ``mlflow.inference``."""

    checkpoint: str = Field(description="Checkpoint, relative to the source root")
    exists: bool = Field(description="The checkpoint is still on disk (last scan)")
    started_at: datetime | None = None
    mlflow: MlflowRunRef
    dataset_name: str | None = Field(default=None, description="Value of inference.dataset_name")
    dataset_path: str | None = Field(default=None, description="Value of inference.dataset_path")
    mapping_name: str | None = Field(default=None, description="Value of inference.mapping_name")
    logged: dict[str, str] = Field(
        default_factory=dict, description="Values of the keys of mlflow.inference.show"
    )


#: MLflow statuses after which the MLflow run no longer changes.
MLFLOW_TERMINAL = frozenset({"FINISHED", "FAILED", "KILLED"})
#: MLflow statuses of a failed training.
MLFLOW_FAILED = frozenset({"FAILED", "KILLED"})


class Series(BaseModel):
    """A run repeated ``count`` times in a row in its container (see :mod:`.series`)."""

    model_config = ConfigDict(extra="ignore")

    count: int = Field(description="Number of repetitions")
    continue_on_failure: bool = Field(
        default=False,
        description="Go on with the next repetitions after a failure (default: stop the series)",
    )
    random: dict[str, tuple[int, int]] = Field(
        default_factory=dict, description="Parameters drawn at random: name → [min, max]"
    )
    values: list[dict[str, str]] = Field(
        description="For each repetition, the values of the parameters that change "
        "(override the run's params)"
    )
    current: int = Field(
        default=0, description="Repetition in progress or last started, from 1 (0: none yet)"
    )
    exit_codes: list[int] = Field(
        default_factory=list, description="Exit codes of the finished repetitions, in order"
    )


class RunSource(StrEnum):
    UI = "ui"
    MCP = "mcp"
    CLI = "cli"
    IMPORT = "import"


class Run(BaseModel):
    """A persisted run (section 3.3)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    project_id: str
    machine_id: str
    image_id: str
    venv_id: str
    config: str
    command: str = Field(description="Resolved generic command (placeholders replaced)")
    full_command: list[str] = Field(default_factory=list, description="Full executed argv")
    container_name: str
    status: RunStatus
    exit_code: int | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    paused_total_s: float = 0.0
    paused_at: datetime | None = None
    error: str | None = None
    source: RunSource = RunSource.UI
    overrides: list[str] = Field(default_factory=list)
    cuda_visible_devices: str | None = None
    params: dict[str, str] = Field(
        default_factory=dict, description="Values of the free command parameters"
    )
    checkpoint: str | None = Field(
        default=None, description="{checkpoint}: path of the checkpoint on the machine"
    )
    stop_requested: bool = False
    container_alive: bool = Field(
        default=False, description="Container still started (shell available)"
    )
    mlflow: MlflowInfo | None = Field(
        default=None, description="Matching MLflow run (projects with MLflow only)"
    )
    series: Series | None = Field(
        default=None, description="Repetitions of the command in the container (None: run once)"
    )
    unseen_failure: bool = Field(
        default=False,
        description="The run failed and nobody marked it as seen yet: it stays among the "
        "active runs until then",
    )

    @property
    def is_failure(self) -> bool:
        """The run failed: its command (status ``failed``), or its training (MLflow
        ``FAILED``/``KILLED``) while it ran. A deliberate stop is not a failure."""
        if self.status is RunStatus.FAILED:
            return True
        ml = self.mlflow.status if self.mlflow else None
        return self.status in (RunStatus.RUNNING, RunStatus.SUCCEEDED) and ml in MLFLOW_FAILED


class ExternalRun(BaseModel):
    """A run found in MLflow that TrainHub did not launch.

    It is shown in the project whose ``mlflow.experiments`` names its experiment, with what
    MLflow knows of it: TrainHub has no machine, container nor logs for it.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(description="MLflow run id")
    project_id: str
    name: str | None = Field(default=None, description="MLflow run name")
    user: str | None = Field(default=None, description="mlflow.user tag")
    experiment: str | None = Field(default=None, description="MLflow experiment name")
    mlflow: MlflowInfo
    discovered_at: datetime = Field(description="When TrainHub found it")
    unseen_failure: bool = Field(
        default=False,
        description="Failed and not marked as seen yet: stays among the active runs until then",
    )
    stale_seen_at: datetime | None = Field(
        default=None,
        description="Stalled (running, no new metric: often a killed process MLflow was never "
        "told about) and marked as seen at that time: shown among the finished runs until a "
        "new metric arrives",
    )

    @property
    def is_active(self) -> bool:
        return self.mlflow.status in ("RUNNING", "SCHEDULED")

    @property
    def is_failure(self) -> bool:
        return self.mlflow.status in MLFLOW_FAILED
