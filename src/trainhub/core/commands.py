"""Construction des commandes (fonctions pures, aucune I/O).

Toutes les fonctions ``build_*`` renvoient un argv (``list[str]``) destiné à
``asyncio.create_subprocess_exec`` : jamais une chaîne passée à un shell local.

Niveaux d'interprétation pour un lancement via SSH :

1. ``ssh`` reçoit la commande distante comme **un seul argument**, construit avec
   :func:`shlex.join` ; le shell de connexion distant la découpe à nouveau en argv.
2. ``docker run … bash -lc SCRIPT`` : ``SCRIPT`` est interprété par ``bash`` dans le
   conteneur. Chaque valeur insérée dans ``SCRIPT`` (chemin du venv, workdir, placeholders,
   overrides Hydra) est échappée avec :func:`shlex.quote`.

Les fonctions ``docker_*`` renvoient l'argv « distant » (ce qui s'exécute sur la machine) ;
:func:`transport` l'enveloppe dans ``ssh`` sauf pour une machine locale.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field

from .models import PLACEHOLDER_RE, ConfigDiscover, Image, Machine, Project, Venv

SSH_CONTROL_OPTIONS: tuple[str, ...] = (
    "-o",
    "ControlMaster=auto",
    "-o",
    "ControlPersist=10m",
    "-o",
    "ControlPath=~/.ssh/th-%C",
)
#: Options non interactives ajoutées par l'exécuteur SSH (pas dans l'aperçu).
SSH_BATCH_OPTIONS: tuple[str, ...] = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=5")

LABEL_ROOT = "trainhub"
LABEL_RUN_ID = "trainhub.run_id"
LABEL_PROJECT = "trainhub.project"
LABEL_MACHINE = "trainhub.machine"
LABEL_IMAGE = "trainhub.image"
LABEL_VENV = "trainhub.venv"
LABEL_CONFIG = "trainhub.config"

PS_FORMAT = "{{json .}}"

_CUDA_RE = re.compile(r"^[0-9]+(,[0-9]+)*$|^(all|none|void|)$")


@dataclass(frozen=True)
class RunSpec:
    """Tout ce qu'il faut pour lancer un run."""

    run_id: str
    project: Project
    machine: Machine
    image: Image
    venv: Venv
    config: str
    command: str | None = None
    """Commande générique (surcharge de ``project.command``), placeholders non résolus."""
    overrides: tuple[str, ...] = ()
    """Overrides Hydra ajoutés à la fin de la commande, chacun échappé."""
    cuda_visible_devices: str | None = None

    @property
    def template(self) -> str:
        return self.command if self.command is not None else self.project.command

    @property
    def container_name(self) -> str:
        return container_name(self.project.id, self.run_id)


@dataclass(frozen=True)
class Target:
    """Un conteneur sur une machine."""

    machine: Machine
    container: str


@dataclass(frozen=True)
class _Groups:
    """argv découpé en groupes, pour l'affichage multi-ligne de :func:`preview`."""

    groups: list[list[str]] = field(default_factory=list)

    def add(self, *parts: str) -> None:
        self.groups.append(list(parts))

    def flat(self) -> list[str]:
        return [p for g in self.groups for p in g]


def container_name(project_id: str, run_id: str) -> str:
    """Nom du conteneur : ``th-<project>-<run_id>``."""
    return f"th-{project_id}-{run_id}"


def resolve_command(template: str, *, config: str, run_id: str, machine: str, project: str) -> str:
    """Remplace les placeholders par leurs valeurs échappées pour le shell.

    ``${VAR}`` et les interpolations Hydra ``${…}`` ne sont pas touchés.
    """
    values = {"config": config, "run_id": run_id, "machine": machine, "project": project}

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in values:
            raise ValueError(f"placeholder inconnu : {{{name}}}")
        return shlex.quote(values[name])

    return PLACEHOLDER_RE.sub(repl, template)


def resolved_command(spec: RunSpec) -> str:
    """Commande générique résolue, overrides Hydra compris."""
    cmd = resolve_command(
        spec.template,
        config=spec.config,
        run_id=spec.run_id,
        machine=spec.machine.id,
        project=spec.project.id,
    )
    if spec.overrides:
        cmd += " " + " ".join(shlex.quote(o) for o in spec.overrides)
    return cmd


def inner_script(spec: RunSpec) -> str:
    """Script exécuté par ``bash -lc`` dans le conteneur."""
    parts = [f"source {shlex.quote(spec.venv.path.rstrip('/') + '/bin/activate')}"]
    if spec.project.workdir:
        parts.append(f"cd {shlex.quote(spec.project.workdir)}")
    parts.append(f"exec {resolved_command(spec)}")
    return " && ".join(parts)


def _docker_run_groups(spec: RunSpec) -> _Groups:
    d = spec.project.docker
    g = _Groups()
    head = ["docker", "run", "-d"]
    if d.tty:
        head.append("-t")
    head.append("--init")
    g.add(*head)
    g.add("--name", spec.container_name)
    g.add(
        "--label",
        f"{LABEL_ROOT}=1",
        "--label",
        f"{LABEL_RUN_ID}={spec.run_id}",
        "--label",
        f"{LABEL_PROJECT}={spec.project.id}",
    )
    g.add(
        "--label",
        f"{LABEL_MACHINE}={spec.machine.id}",
        "--label",
        f"{LABEL_IMAGE}={spec.image.id}",
        "--label",
        f"{LABEL_VENV}={spec.venv.id}",
        "--label",
        f"{LABEL_CONFIG}={spec.config}",
    )
    opts: list[str] = []
    if d.gpus:
        opts += ["--gpus", d.gpus]
    if d.shm_size:
        opts += ["--shm-size", d.shm_size]
    for vol in d.volumes:
        opts += ["-v", vol]
    if spec.project.workdir:
        opts += ["-w", spec.project.workdir]
    for k, v in d.env.items():
        opts += ["-e", f"{k}={v}"]
    if spec.cuda_visible_devices is not None:
        opts += ["-e", f"CUDA_VISIBLE_DEVICES={spec.cuda_visible_devices}"]
    opts += list(d.extra_args)
    if opts:
        g.add(*opts)
    g.add(spec.image.image)
    g.add("bash", "-lc", inner_script(spec))
    return g


def validate_spec(spec: RunSpec) -> None:
    """Vérifie les valeurs libres d'un spec ; lève ``ValueError`` si invalide."""
    if not spec.config or spec.config.startswith("-"):
        raise ValueError(f"config invalide : {spec.config!r}")
    if "\n" in spec.config or "\x00" in spec.config:
        raise ValueError("config invalide : caractères de contrôle")
    if spec.cuda_visible_devices is not None and not _CUDA_RE.match(
        spec.cuda_visible_devices.replace(" ", "")
    ):
        raise ValueError(
            f"CUDA_VISIBLE_DEVICES invalide : {spec.cuda_visible_devices!r} "
            "(attendu : liste d'indices, ex. 0,1)"
        )


def docker_run(spec: RunSpec) -> list[str]:
    """argv distant de ``docker run -d …``."""
    validate_spec(spec)
    return _docker_run_groups(spec).flat()


def docker_logs(container: str, *, follow: bool = True, tail: int | None = 2000) -> list[str]:
    argv = ["docker", "logs"]
    if follow:
        argv.append("-f")
    if tail is not None:
        argv += ["--tail", str(int(tail))]
    argv.append(container)
    return argv


def docker_inspect(container: str) -> list[str]:
    return ["docker", "inspect", "--type", "container", container]


def docker_ps() -> list[str]:
    return [
        "docker",
        "ps",
        "-a",
        "--no-trunc",
        "--filter",
        f"label={LABEL_ROOT}=1",
        "--format",
        PS_FORMAT,
    ]


def docker_pause(container: str) -> list[str]:
    return ["docker", "pause", container]


def docker_unpause(container: str) -> list[str]:
    return ["docker", "unpause", container]


def docker_stop(container: str, timeout: int = 30) -> list[str]:
    return ["docker", "stop", "-t", str(int(timeout)), container]


def docker_start(container: str) -> list[str]:
    return ["docker", "start", container]


def docker_rm(container: str, *, force: bool = False) -> list[str]:
    return ["docker", "rm", *(["-f"] if force else []), container]


def list_configs(discover: ConfigDiscover) -> list[str]:
    """argv distant listant le répertoire de configs (filtrage par motif côté Python)."""
    return ["ls", "-1A", "--", discover.path]


def ssh_argv(machine: Machine, remote: Sequence[str], *, extra: Sequence[str] = ()) -> list[str]:
    """Enveloppe un argv distant dans ``ssh`` (la commande distante est un seul argument)."""
    argv = ["ssh", *SSH_CONTROL_OPTIONS, *extra]
    if machine.port is not None:
        argv += ["-p", str(machine.port)]
    argv += [machine.ssh_target, "--", shlex.join(remote)]
    return argv


def transport(machine: Machine, remote: Sequence[str], *, extra: Sequence[str] = ()) -> list[str]:
    """argv local complet : ``ssh … -- CMD`` ou ``CMD`` tel quel pour une machine locale."""
    if machine.is_local:
        return list(remote)
    return ssh_argv(machine, remote, extra=extra)


# -- API de construction « complète » (section 4.1) ---------------------------


def build_run(spec: RunSpec) -> list[str]:
    """argv complet de lancement du conteneur."""
    return transport(spec.machine, docker_run(spec))


def build_logs(target: Target, follow: bool = True, tail: int | None = 2000) -> list[str]:
    return transport(target.machine, docker_logs(target.container, follow=follow, tail=tail))


def build_inspect(target: Target) -> list[str]:
    return transport(target.machine, docker_inspect(target.container))


def build_ps(machine: Machine) -> list[str]:
    return transport(machine, docker_ps())


def build_pause(target: Target) -> list[str]:
    return transport(target.machine, docker_pause(target.container))


def build_unpause(target: Target) -> list[str]:
    return transport(target.machine, docker_unpause(target.container))


def build_stop(target: Target, timeout: int = 30) -> list[str]:
    return transport(target.machine, docker_stop(target.container, timeout))


def build_start(target: Target) -> list[str]:
    return transport(target.machine, docker_start(target.container))


def build_rm(target: Target) -> list[str]:
    return transport(target.machine, docker_rm(target.container))


def build_list_configs(machine: Machine, discover: ConfigDiscover) -> list[str]:
    return transport(machine, list_configs(discover))


def preview(spec: RunSpec) -> str:
    """Commande de lancement lisible, sur plusieurs lignes (affichage, dry run).

    La partie distante est montrée avec un seul niveau d'échappement (celui que voit le
    shell distant) ; :func:`preview_exact` donne la version copiable telle quelle.
    """
    validate_spec(spec)
    groups = _docker_run_groups(spec).groups
    q = shlex.join
    lines: list[str] = []
    if not spec.machine.is_local:
        head = ["ssh", *SSH_CONTROL_OPTIONS]
        if spec.machine.port is not None:
            head += ["-p", str(spec.machine.port)]
        lines.append(q(head))
        lines.append("    " + q([spec.machine.ssh_target, "--"]))
        indent_first, indent = "  ", "    "
    else:
        indent_first, indent = "", "  "
    for i, g in enumerate(groups):
        lines.append((indent_first if i == 0 else indent) + q(g))
    return " \\\n".join(lines)


def preview_exact(spec: RunSpec) -> str:
    """Commande de lancement exacte, sur une ligne, copiable dans un terminal local."""
    return shlex.join(build_run(spec))
