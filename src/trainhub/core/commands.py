"""Command building (pure functions, no I/O).

Every ``build_*`` function returns an argv (``list[str]``) meant for
``asyncio.create_subprocess_exec``: never a string handed to a local shell.

Interpretation levels for a launch over SSH:

1. ``ssh`` receives the remote command as **a single argument**, built with
   :func:`shlex.join`; the remote login shell splits it back into an argv.
2. ``docker run … bash -lc SCRIPT``: ``SCRIPT`` is interpreted by ``bash`` inside the
   container. Every value inserted in ``SCRIPT`` (venv path, workdir, placeholders,
   extra arguments) is escaped with :func:`shlex.quote`.

The ``docker_*`` functions return the “remote” argv (what runs on the machine);
:func:`transport` wraps it in ``ssh`` except for a local machine.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .models import (
    ALLOWED_PLACEHOLDERS,
    PLACEHOLDER_RE,
    ConfigDiscover,
    Image,
    Machine,
    Project,
    Venv,
)

SSH_CONTROL_OPTIONS: tuple[str, ...] = (
    "-o",
    "ControlMaster=auto",
    "-o",
    "ControlPersist=10m",
    "-o",
    "ControlPath=~/.ssh/th-%C",
)
#: Non-interactive options added by the SSH executor (not in the preview).
SSH_BATCH_OPTIONS: tuple[str, ...] = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=5")

LABEL_ROOT = "trainhub"
LABEL_RUN_ID = "trainhub.run_id"
LABEL_PROJECT = "trainhub.project"
LABEL_MACHINE = "trainhub.machine"
LABEL_IMAGE = "trainhub.image"
LABEL_VENV = "trainhub.venv"
LABEL_CONFIG = "trainhub.config"

PS_FORMAT = "{{json .}}"

#: File (in the container) where the script writes the exit code of the command.
EXIT_FILE = "/tmp/.trainhub-exit"
#: Healthcheck: ``unhealthy`` (and prints the code) once the command has finished.
HEALTH_CMD = f"test ! -e {EXIT_FILE} || {{ cat {EXIT_FILE}; exit 1; }}"
HEALTH_INTERVAL = "3s"

_CUDA_RE = re.compile(r"^[0-9]+(,[0-9]+)*$|^(all|none|void|)$")


@dataclass(frozen=True)
class RunSpec:
    """Everything needed to launch a run."""

    run_id: str
    project: Project
    machine: Machine
    image: Image
    venv: Venv
    config: str
    command: str | None = None
    """Generic command (overrides ``project.command``), placeholders unresolved."""
    overrides: tuple[str, ...] = ()
    """Extra arguments (overrides) appended to the command, each one escaped."""
    cuda_visible_devices: str | None = None
    params: Mapping[str, str] = field(default_factory=dict)
    """Values of the free parameters (non-reserved ``{name}``) of the command."""

    @property
    def template(self) -> str:
        return self.command if self.command is not None else self.project.command

    @property
    def container_name(self) -> str:
        return container_name(self.project.id, self.run_id)


@dataclass(frozen=True)
class Target:
    """A container on a machine."""

    machine: Machine
    container: str


@dataclass(frozen=True)
class _Groups:
    """argv split into groups, for the multi-line display of :func:`preview`."""

    groups: list[list[str]] = field(default_factory=list)

    def add(self, *parts: str) -> None:
        self.groups.append(list(parts))

    def flat(self) -> list[str]:
        return [p for g in self.groups for p in g]


def container_name(project_id: str, run_id: str) -> str:
    """Container name: ``th-<project>-<run_id>``."""
    return f"th-{project_id}-{run_id}"


def param_words(name: str, value: str) -> list[str]:
    """Value of a free parameter split as a shell would (``a=1 'b c'`` → 2 words)."""
    if "\x00" in value:
        raise ValueError(f"parameter {{{name}}}: null character not allowed")
    try:
        return shlex.split(value)
    except ValueError as exc:
        raise ValueError(f"invalid parameter {{{name}}}: {exc}") from exc


def resolve_command(
    template: str,
    *,
    config: str,
    run_id: str,
    machine: str,
    project: str,
    params: Mapping[str, str] | None = None,
) -> str:
    """Replace placeholders with their shell-escaped values.

    ``{config}``: relative path without extension (e.g. ``exp/unet``); ``{config_name}``:
    basename (``unet``). ``${VAR}`` and Hydra interpolations ``${…}`` are left untouched.
    Any other ``{name}`` is a free parameter: its value (``params``) is split as a shell
    would and each word is escaped; if missing or empty, nothing is inserted.
    """
    free = params or {}
    values = {
        "config": config,
        "config_name": config.rsplit("/", 1)[-1],
        "run_id": run_id,
        "machine": machine,
        "project": project,
    }

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in ALLOWED_PLACEHOLDERS:
            return " ".join(shlex.quote(w) for w in param_words(name, free.get(name, "")))
        return shlex.quote(values[name])

    return PLACEHOLDER_RE.sub(repl, template)


def resolved_command(spec: RunSpec) -> str:
    """Resolved generic command, including extra arguments (overrides)."""
    cmd = resolve_command(
        spec.template,
        config=spec.config,
        run_id=spec.run_id,
        machine=spec.machine.id,
        project=spec.project.id,
        params=spec.params,
    )
    if spec.overrides:
        cmd += " " + " ".join(shlex.quote(o) for o in spec.overrides)
    return cmd


def _activate_path(venv_path: str) -> str:
    return venv_path.rstrip("/") + "/bin/activate"


def inner_script(spec: RunSpec) -> str:
    """Script run by ``bash -lc`` in the container.

    Without ``keep_alive``: ``source venv && cd workdir && exec CMD``.

    With ``keep_alive`` (default), the command runs in the background under ``bash``:

    - ``SIGTERM`` (``docker stop``) is forwarded to the command, then the container exits with
      its exit code;
    - when the command finishes on its own, its code is written to :data:`EXIT_FILE` (the
      healthcheck then becomes ``unhealthy``, which signals the end to TrainHub) and the
      container stays started so that a shell can be opened in it; when it is closed, it exits
      with the command's code.
    """
    setup: list[str] = []
    if spec.venv.path:  # “system” venv: nothing to activate
        setup.append(f"source {shlex.quote(_activate_path(spec.venv.path))}")
    if spec.project.workdir:
        setup.append(f"cd {shlex.quote(spec.project.workdir)}")
    if not spec.project.docker.keep_alive:
        return " && ".join([*setup, f"exec {resolved_command(spec)}"])
    f = EXIT_FILE
    msg = (
        r"\n[trainhub] command finished (code %s): container kept, "
        r"shell available from TrainHub\n"
    )
    return "; ".join(
        [
            f"rm -f {f}",
            *(f"{s} || exit 1" for s in setup),
            f"{resolved_command(spec)} & pid=$!",
            "trap 'stop=1; kill -TERM $pid 2>/dev/null' TERM INT HUP",
            "wait $pid",
            "code=$?",
            "while kill -0 $pid 2>/dev/null; do wait $pid; code=$?; done",
            'if [ -n "$stop" ]; then exit $code; fi',
            f"echo $code > {f}",
            f"printf {shlex.quote(msg)} $code",
            "trap 'exit $code' TERM INT HUP",
            "while :; do sleep 3600 & wait $!; done",
        ]
    )


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
    if d.keep_alive:
        opts += [
            "--health-cmd",
            HEALTH_CMD,
            "--health-interval",
            HEALTH_INTERVAL,
            "--health-retries",
            "1",
            "--health-timeout",
            "5s",
        ]
    opts += list(d.extra_args)
    if opts:
        g.add(*opts)
    g.add(spec.image.image)
    g.add("bash", "-lc", inner_script(spec))
    return g


def validate_spec(spec: RunSpec) -> None:
    """Check the free values of a spec; raise ``ValueError`` if invalid."""
    if not spec.config or spec.config.startswith("-"):
        raise ValueError(f"invalid config: {spec.config!r}")
    if "\n" in spec.config or "\x00" in spec.config:
        raise ValueError("invalid config: control characters")
    parts = spec.config.split("/")
    if spec.config.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"invalid config: {spec.config!r} (relative path expected)")
    if spec.cuda_visible_devices is not None and not _CUDA_RE.match(
        spec.cuda_visible_devices.replace(" ", "")
    ):
        raise ValueError(
            f"invalid CUDA_VISIBLE_DEVICES: {spec.cuda_visible_devices!r} "
            "(expected: list of indices, e.g. 0,1)"
        )
    for name, value in spec.params.items():
        param_words(name, value)


def docker_run(spec: RunSpec) -> list[str]:
    """Remote argv of ``docker run -d …``."""
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


def docker_restart(container: str, timeout: int = 30) -> list[str]:
    return ["docker", "restart", "-t", str(int(timeout)), container]


def docker_exec_shell(
    container: str, *, venv_path: str | None = None, workdir: str | None = None
) -> list[str]:
    """Remote argv of an interactive shell in the container (venv activated, in the workdir).

    ``bash`` if available, else ``sh``.
    """
    parts: list[str] = []
    if workdir:
        parts.append(f"cd {shlex.quote(workdir)} 2>/dev/null")
    if venv_path:
        act = shlex.quote(_activate_path(venv_path))
        parts.append(f"if [ -f {act} ]; then . {act}; fi")
    parts.append("if command -v bash >/dev/null 2>&1; then exec bash -i; else exec sh -i; fi")
    return [
        "docker",
        "exec",
        "-it",
        "-e",
        "TERM=xterm-256color",
        container,
        "sh",
        "-c",
        "; ".join(parts),
    ]


def discover_root(discover: ConfigDiscover) -> str:
    """Root passed to ``find``: ``~/x`` becomes ``x`` (relative to the SSH session's home)."""
    root = discover.path
    if root == "~":
        return "."
    if root.startswith("~/"):
        root = root[2:] or "."
    return root.rstrip("/") or "/"


def list_configs(discover: ConfigDiscover) -> list[str]:
    """Remote argv recursively listing the config files under the root."""
    return [
        "find",
        "-L",
        discover_root(discover),
        "-maxdepth",
        str(discover.max_depth),
        "-type",
        "f",
        "-name",
        discover.pattern,
    ]


def parse_config_listing(output: str, discover: ConfigDiscover) -> list[str]:
    """``find`` output → sorted config identifiers (relative path without extension).

    Paths containing a hidden component (``.git``, ``.hydra``…) and those matching
    ``exclude`` are ignored.
    """
    import fnmatch

    root = discover_root(discover)
    prefix = "" if root == "/" else root
    suffix = discover.pattern.rsplit("*", 1)[-1] if "*" in discover.pattern else ""
    found: set[str] = set()
    for line in output.splitlines():
        path = line.rstrip("\r")
        if not path:
            continue
        if prefix and path.startswith(prefix + "/"):
            rel = path[len(prefix) + 1 :]
        elif root == "/" and path.startswith("/"):
            rel = path[1:]
        else:
            continue
        parts = rel.split("/")
        if any(p.startswith(".") or not p for p in parts):
            continue
        if not fnmatch.fnmatch(parts[-1], discover.pattern):
            continue
        if any(fnmatch.fnmatch(rel, pat) for pat in discover.exclude):
            continue
        name = rel[: -len(suffix)] if suffix and rel.endswith(suffix) else rel
        if name:
            found.add(name)
    return sorted(found)


def ssh_argv(machine: Machine, remote: Sequence[str], *, extra: Sequence[str] = ()) -> list[str]:
    """Wrap a remote argv in ``ssh`` (the remote command is a single argument)."""
    argv = ["ssh", *SSH_CONTROL_OPTIONS, *extra]
    if machine.port is not None:
        argv += ["-p", str(machine.port)]
    argv += [machine.ssh_target, "--", shlex.join(remote)]
    return argv


def transport(machine: Machine, remote: Sequence[str], *, extra: Sequence[str] = ()) -> list[str]:
    """Full local argv: ``ssh … -- CMD``, or ``CMD`` as is for a local machine."""
    if machine.is_local:
        return list(remote)
    return ssh_argv(machine, remote, extra=extra)


# -- “full” building API (section 4.1) ----------------------------------------


def build_run(spec: RunSpec) -> list[str]:
    """Full argv launching the container."""
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
    """Readable launch command, over several lines (display, dry run).

    The remote part is shown with a single escaping level (the one the remote shell
    sees); :func:`preview_exact` gives the version that can be copied as is.
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
    """Exact launch command, on one line, ready to paste into a local terminal."""
    return shlex.join(build_run(spec))
