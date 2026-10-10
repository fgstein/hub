"""Modèles de données : inventaire (YAML, lecture seule) et runs (SQLite).

Ces modèles ne font aucune I/O ; ils sont validés par Pydantic v2.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"

#: Placeholders autorisés dans la commande générique d'un projet.
ALLOWED_PLACEHOLDERS = frozenset({"config", "run_id", "machine", "project"})
#: Un placeholder est ``{nom}`` non précédé de ``$`` (pour laisser passer ``${VAR}`` et les
#: interpolations Hydra/OmegaConf ``${oc.env:X}``).
PLACEHOLDER_RE = re.compile(r"(?<!\$)\{([A-Za-z_][A-Za-z0-9_]*)\}")

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "local"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def validate_command_template(command: str) -> str:
    """Valide une commande générique ; lève ``ValueError`` avec un message lisible."""
    if not command.strip():
        raise ValueError("la commande est vide")
    names = PLACEHOLDER_RE.findall(command)
    unknown = sorted({n for n in names if n not in ALLOWED_PLACEHOLDERS})
    if unknown:
        allowed = ", ".join("{" + p + "}" for p in sorted(ALLOWED_PLACEHOLDERS))
        raise ValueError(
            f"placeholder(s) inconnu(s) : {', '.join('{' + u + '}' for u in unknown)} "
            f"(autorisés : {allowed})"
        )
    if "config" not in names:
        raise ValueError("la commande doit contenir le placeholder {config}")
    return command


# --------------------------------------------------------------------------- inventaire


class Machine(_Strict):
    """Machine distante joignable en SSH (ou ``localhost``)."""

    id: str = Field(pattern=ID_PATTERN)
    label: str | None = None
    host: str = Field(min_length=1)
    user: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)

    @property
    def display(self) -> str:
        return self.label or self.id

    @property
    def is_local(self) -> bool:
        """``True`` si les commandes s'exécutent sans SSH sur l'hôte de TrainHub."""
        return self.host in LOCAL_HOSTS and self.user is None and self.port is None

    @property
    def ssh_target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host


class Image(_Strict):
    """Image Docker disponible sur les machines."""

    id: str = Field(pattern=ID_PATTERN)
    image: str = Field(min_length=1)
    label: str | None = None


class Venv(_Strict):
    """Environnement virtuel Python présent dans les images."""

    id: str = Field(pattern=ID_PATTERN)
    path: str = Field(min_length=1, description="Chemin du venv dans le conteneur")
    label: str | None = None
    images: list[str] | None = Field(
        default=None, description="Restreint le venv à ces images (défaut : toutes)"
    )


class DockerOptions(_Strict):
    """Options ``docker run`` propres à un projet."""

    gpus: str | None = None
    shm_size: str | None = None
    volumes: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    extra_args: list[str] = Field(default_factory=list)
    tty: bool = True
    stop_timeout: int = Field(default=30, ge=0)

    @field_validator("env", mode="before")
    @classmethod
    def _env_to_str(cls, v: object) -> object:
        if isinstance(v, dict):
            return {str(k): ("" if val is None else str(val)) for k, val in v.items()}
        return v


class ConfigDiscover(_Strict):
    """Découverte des configs Hydra par ``ls`` sur la machine choisie."""

    path: str = Field(min_length=1, description="Répertoire côté hôte")
    pattern: str = "*.yaml"


class ConfigSource(_Strict):
    """Liste statique de configs et/ou découverte sur la machine."""

    static: list[str] | None = None
    discover: ConfigDiscover | None = None

    @model_validator(mode="after")
    def _one_source(self) -> ConfigSource:
        if self.static is None and self.discover is None:
            raise ValueError("configs : définir 'static' et/ou 'discover'")
        return self


class Project(_Strict):
    """Projet = un onglet de l'interface, avec sa commande générique."""

    id: str = Field(pattern=ID_PATTERN)
    label: str | None = None
    command: str
    workdir: str | None = None
    machines: list[str] | None = None
    images: list[str] | None = None
    venvs: list[str] | None = None
    docker: DockerOptions = Field(default_factory=DockerOptions)
    configs: ConfigSource = Field(default_factory=lambda: ConfigSource(static=[]))

    @field_validator("command")
    @classmethod
    def _check_command(cls, v: str) -> str:
        return validate_command_template(v)

    @property
    def display(self) -> str:
        return self.label or self.id


MachineState = Literal["ok", "flaky", "down"]


class SimulationSettings(_Strict):
    """Paramètres du mode ``fake`` (section ``simulation`` de ``inventory.dev.yaml``)."""

    duration_s: tuple[float, float] = (60.0, 300.0)
    log_rate: float = Field(default=8.0, gt=0, description="Lignes de log par seconde")
    failure_prob: float = Field(default=0.1, ge=0, le=1)
    time_scale: float = Field(default=1.0, gt=0, description="Facteur d'accélération du temps")
    latency_ms: tuple[float, float] = (50.0, 300.0)
    machines: dict[str, MachineState] = Field(default_factory=dict)
    configs: dict[str, list[str]] = Field(
        default_factory=dict, description="Configs « découvertes » simulées, par projet"
    )
    flaky_period_s: float = Field(default=40.0, gt=0)
    seed: int | None = None


class Inventory(_Strict):
    """Contenu validé de ``inventory.yaml``."""

    machines: list[Machine] = Field(default_factory=list)
    images: list[Image] = Field(default_factory=list)
    venvs: list[Venv] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    simulation: SimulationSettings | None = None

    @model_validator(mode="after")
    def _check_refs(self) -> Inventory:
        for kind in ("machines", "images", "venvs", "projects"):
            seen: set[str] = set()
            for item in getattr(self, kind):
                if item.id in seen:
                    raise ValueError(f"{kind} : id '{item.id}' dupliqué")
                seen.add(item.id)
        machines = {m.id for m in self.machines}
        images = {i.id for i in self.images}
        venvs = {v.id for v in self.venvs}
        for v in self.venvs:
            for ref in v.images or []:
                if ref not in images:
                    raise ValueError(f"venv '{v.id}' : image '{ref}' inconnue")
        for p in self.projects:
            for field, known in (("machines", machines), ("images", images), ("venvs", venvs)):
                for ref in getattr(p, field) or []:
                    if ref not in known:
                        raise ValueError(f"projet '{p.id}' : {field[:-1]} '{ref}' inconnue")
        return self

    # -- accès ---------------------------------------------------------------

    def machine(self, mid: str) -> Machine:
        return _find(self.machines, mid, "machine")

    def image(self, iid: str) -> Image:
        return _find(self.images, iid, "image")

    def venv(self, vid: str) -> Venv:
        return _find(self.venvs, vid, "venv")

    def project(self, pid: str) -> Project:
        return _find(self.projects, pid, "projet")

    def allowed_machines(self, project: Project) -> list[Machine]:
        if project.machines is None:
            return list(self.machines)
        return [self.machine(m) for m in project.machines]

    def allowed_images(self, project: Project) -> list[Image]:
        if project.images is None:
            return list(self.images)
        return [self.image(i) for i in project.images]

    def allowed_venvs(self, project: Project, image: str | None = None) -> list[Venv]:
        venvs = list(self.venvs) if project.venvs is None else [self.venv(v) for v in project.venvs]
        if image is None:
            return venvs
        return [v for v in venvs if v.images is None or image in v.images]


class UnknownResourceError(KeyError):
    """Ressource absente de l'inventaire."""

    def __init__(self, kind: str, rid: str, available: list[str]) -> None:
        self.kind, self.rid, self.available = kind, rid, available
        super().__init__(rid)

    def __str__(self) -> str:
        avail = ", ".join(self.available) or "aucun"
        return f"{self.kind} '{self.rid}' inconnu(e) ; disponibles : {avail}"


_T = TypeVar("_T", Machine, Image, Venv, Project)


def _find(items: list[_T], rid: str, kind: str) -> _T:
    for it in items:
        if it.id == rid:
            return it
    raise UnknownResourceError(kind, rid, [it.id for it in items])


# --------------------------------------------------------------------------- runs


class RunStatus(StrEnum):
    """Statut d'un run (section 4.3)."""

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
    """Un run persistant (section 3.3)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    project_id: str
    machine_id: str
    image_id: str
    venv_id: str
    config: str
    command: str = Field(description="Commande générique résolue (placeholders remplacés)")
    full_command: list[str] = Field(default_factory=list, description="argv complet exécuté")
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
    stop_requested: bool = False
