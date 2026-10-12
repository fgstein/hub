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

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"

#: Placeholders filled in by TrainHub. Any other ``{name}`` is a free run parameter,
#: entered before launch (text field of the draft).
ALLOWED_PLACEHOLDERS = frozenset({"config", "config_name", "run_id", "machine", "project"})
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


class Project(_Strict):
    """Project = one tab of the interface, with its generic command."""

    id: str = Field(pattern=ID_PATTERN)
    label: str | None = None
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

    @field_validator("command")
    @classmethod
    def _check_command(cls, v: str) -> str:
        return validate_command_template(v)

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
    flaky_period_s: float = Field(default=40.0, gt=0)
    gpus: int = Field(default=4, ge=0, description="Simulated GPUs per machine (nvidia-smi)")
    seed: int | None = None


class Inventory(_Strict):
    """Validated content of ``inventory.yaml``."""

    machines: list[Machine] = Field(default_factory=list)
    images: list[Image] = Field(default_factory=list)
    venvs: list[Venv] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    docker_command: list[str] = Field(
        default_factory=lambda: ["docker"],
        description="Default Docker command on the machines: binary, path or wrapper "
        "(e.g. 'sudo -n docker', '/opt/bin/mydocker'). Can be overridden per machine.",
    )
    simulation: SimulationSettings | None = None

    @field_validator("docker_command", mode="before")
    @classmethod
    def _parse_docker(cls, v: object) -> object:
        return ["docker"] if v is None else parse_docker_command(v)

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
        for kind in ("machines", "images", "venvs", "projects"):
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


_T = TypeVar("_T", Machine, Image, Venv, Project)


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
    stop_requested: bool = False
    container_alive: bool = Field(
        default=False, description="Container still started (shell available)"
    )
