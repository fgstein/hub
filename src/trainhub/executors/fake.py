"""``fake`` executor: machines and containers simulated in memory.

It interprets the docker argv actually built by :mod:`trainhub.core.commands`
(``docker run/ps/inspect/logs/pause/unpause/stop/start/restart/rm``, ``find`` and the
checkpoint deletion script), which also
tests command building. Each container produces realistic logs: Hydra banner, resolved
config, colored ANSI lines, tqdm bars rewritten with ``\\r``, per-epoch metrics, warnings,
and possibly a traceback with exit code 1.

A container launched with a healthcheck (``keep_alive``) stays started after the command,
like the real script: ``unhealthy`` healthcheck and the command's code in ``inspect``.
``open_shell`` simulates a minimal shell in that container.

Settings: ``simulation`` section of the inventory (see ``dev/inventory.dev.yaml``).
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import math
import random
import re
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..core import checkpoints as ck
from ..core.commands import (
    LABEL_RUN_ID,
    LABEL_SERIES,
    LABEL_SERIES_CONTINUE,
    discover_root,
    transport,
)
from ..core.gpus import visible_indices
from ..core.load import CPU_SCRIPT
from ..core.models import (
    CheckpointSource,
    Inventory,
    Machine,
    MlflowConfig,
    MlflowInfo,
    Run,
    SimulationSettings,
)
from ..core.series import end_marker, halt_message, start_marker
from ..service.mlflow import MlflowSource
from .base import (
    ExecResult,
    Executor,
    ExecutorError,
    LogStream,
    MachineUnreachableError,
    ShellSession,
)

# ``docker run`` options taking a separate value.
_RUN_VALUE_OPTS = frozenset(
    {
        "--name",
        "--label",
        "-l",
        "--gpus",
        "--shm-size",
        "-v",
        "--volume",
        "-w",
        "--workdir",
        "-e",
        "--env",
        "--network",
        "--user",
        "-u",
        "--entrypoint",
        "--ipc",
        "--memory",
        "-m",
        "--cpus",
        "--runtime",
        "--mount",
        "--device",
        "--ulimit",
        "--hostname",
        "-h",
        "--restart",
        "-p",
        "--publish",
        "--cap-add",
        "--security-opt",
        "--health-cmd",
        "--health-interval",
        "--health-retries",
        "--health-timeout",
        "--health-start-period",
    }
)

_LOG_LIMIT = 8 * 1024 * 1024  # bytes kept per simulated container

_DEFAULT_CKPT_RUNS = [
    "unet_large_lr1e-3",
    "unet_large_lr3e-4",
    "segformer_b2_bs16",
    "baseline_2026-10-02",
    "ablation_no_augment",
    "debug_tiny",
]


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str:
    return dt.isoformat().replace("+00:00", "Z") if dt else "0001-01-01T00:00:00Z"


@dataclass
class FakeContainer:
    """Simulated container."""

    name: str
    image: str
    labels: dict[str, str]
    command: list[str]
    tty: bool
    id: str
    env: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now)
    state: str = "created"
    exit_code: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    logs: bytearray = field(default_factory=bytearray)
    followers: list[asyncio.Queue[bytes | None]] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    running: asyncio.Event = field(default_factory=asyncio.Event)
    keep_alive: bool = False
    command_done: bool = False
    command_code: int = 0
    #: simulated MLflow run of the current execution
    ml_run: int = 0
    ml_status: str = "RUNNING"
    ml_epoch: int | None = None
    ml_epochs: int | None = None

    def write(self, data: bytes) -> None:
        self.logs += data
        if len(self.logs) > _LOG_LIMIT:
            del self.logs[: len(self.logs) - _LOG_LIMIT]
        for q in self.followers:
            q.put_nowait(data)

    def end_followers(self) -> None:
        for q in self.followers:
            q.put_nowait(None)
        self.followers.clear()

    @property
    def health(self) -> str | None:
        if not self.keep_alive or self.state not in ("running", "paused"):
            return None
        return "unhealthy" if self.command_done else "healthy"

    def status_text(self) -> str:
        if self.state == "running":
            return f"Up ({self.health})" if self.health else "Up"
        if self.state == "paused":
            return "Up (Paused)"
        if self.state == "exited":
            return f"Exited ({self.exit_code}) recently"
        return "Created"

    def ps_json(self) -> str:
        return json.dumps(
            {
                "ID": self.id[:12],
                "Image": self.image,
                "Names": self.name,
                "Labels": ",".join(f"{k}={v}" for k, v in self.labels.items()),
                "State": self.state,
                "Status": self.status_text(),
                "CreatedAt": self.created_at.strftime("%Y-%m-%d %H:%M:%S +0000 UTC"),
                "Command": '"' + " ".join(self.command)[:40] + '…"',
            }
        )

    def inspect_json(self) -> str:
        return json.dumps(
            [
                {
                    "Id": self.id,
                    "Name": "/" + self.name,
                    "Created": _iso(self.created_at),
                    "State": {
                        "Status": self.state,
                        "Running": self.state in ("running", "paused"),
                        "Paused": self.state == "paused",
                        "ExitCode": self.exit_code if self.state == "exited" else 0,
                        "StartedAt": _iso(self.started_at),
                        "FinishedAt": _iso(self.finished_at),
                        **(
                            {
                                "Health": {
                                    "Status": self.health,
                                    "Log": [
                                        {"Output": f"{self.command_code}\n"}
                                        if self.command_done
                                        else {"Output": ""}
                                    ],
                                }
                            }
                            if self.health
                            else {}
                        ),
                    },
                    "Config": {"Image": self.image, "Labels": self.labels, "Tty": self.tty},
                }
            ]
        )


class _FakeLogStream(LogStream):
    def __init__(self, queue: asyncio.Queue[bytes | None], c: FakeContainer | None) -> None:
        self._q = queue
        self._c = c
        self._closed = False

    async def _iter(self) -> AsyncIterator[bytes]:
        while not self._closed:
            item = await self._q.get()
            if item is None:
                break
            yield item

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iter()

    @property
    def returncode(self) -> int | None:
        return 0 if self._closed else None

    async def close(self) -> None:
        self._closed = True
        if self._c is not None and self._q in self._c.followers:
            self._c.followers.remove(self._q)
        self._q.put_nowait(None)


@dataclass
class FakeMachine:
    containers: dict[str, FakeContainer] = field(default_factory=dict)


class FakeExecutor(Executor):
    """Simulate Docker on in-memory machines."""

    kind = "fake"

    def __init__(
        self, settings: SimulationSettings | None = None, *, inventory: Inventory | None = None
    ) -> None:
        self.settings = settings or SimulationSettings()
        self.inventory = inventory
        self.machines: dict[str, FakeMachine] = {}
        self._rng = random.Random(self.settings.seed)
        self._t0 = time.monotonic()
        #: states forced by tests: machine -> "ok" | "down"
        self.forced_state: dict[str, str] = {}
        #: simulated checkpoint trees: root -> {relative path: (kind "d"/"f", mtime, size)}
        self.checkpoint_trees: dict[str, dict[str, tuple[str, float, int]]] = {}
        if inventory is not None:
            self.update_inventory(inventory)

    # -- configuration --------------------------------------------------------

    def update_inventory(self, inventory: Inventory) -> None:
        self.inventory = inventory
        if inventory.simulation is not None:
            self.settings = inventory.simulation

    def full_argv(self, machine: Machine, remote: Sequence[str]) -> list[str]:
        return transport(machine, remote)

    def machine_state(self, machine_id: str) -> str:
        """``ok``, ``flaky`` or ``down`` for a machine."""
        return self.forced_state.get(machine_id) or self.settings.machines.get(machine_id, "ok")

    def is_reachable(self, machine_id: str) -> bool:
        state = self.machine_state(machine_id)
        if state == "down":
            return False
        if state == "flaky":
            # Unreachable during ~30 % of each period, shifted per machine.
            period = self.settings.flaky_period_s
            h = int(hashlib.sha1(machine_id.encode()).hexdigest()[:6], 16)
            phase = ((time.monotonic() - self._t0) / period + (h % 1000) / 1000) % 1.0
            return phase < 0.7
        return True

    async def _latency(self) -> None:
        lo, hi = self.settings.latency_ms
        await asyncio.sleep(self._rng.uniform(lo, hi) / 1000.0)

    # -- execution -------------------------------------------------------------

    async def _exec(self, machine: Machine, remote: list[str], *, timeout: float) -> ExecResult:
        t0 = time.monotonic()
        await self._latency()
        if not self.is_reachable(machine.id):
            raise MachineUnreachableError(
                machine.id, f"ssh: connect to host {machine.host} port 22: Connection timed out"
            )
        m = self.machines.setdefault(machine.id, FakeMachine())
        code, out, err = self._dispatch(machine, m, remote)
        return ExecResult(
            argv=self.full_argv(machine, remote),
            returncode=code,
            stdout=out.encode(),
            stderr=err.encode(),
            duration_s=time.monotonic() - t0,
        )

    def _dispatch(self, machine: Machine, m: FakeMachine, argv: list[str]) -> tuple[int, str, str]:
        if not argv:
            return 127, "", "empty command"
        if argv[0] == "find" and "-printf" in argv:
            return self._find_checkpoints(argv)
        if argv[0] == "find":
            return self._find(argv)
        if argv[0] == "sh" and len(argv) > 4 and argv[3] == ck.RM_SCRIPT_NAME:
            return self._rm_checkpoints(argv[4], argv[5:])
        if argv[0] == "nvidia-smi":
            return self._nvidia_smi(machine, m, argv)
        if argv[:2] == ["sh", "-c"] and argv[2:] == [CPU_SCRIPT]:
            return self._proc(machine, m)
        if argv[0] != "docker" or len(argv) < 2:
            return 127, "", f"bash: {argv[0]}: command not found"
        sub, args = argv[1], argv[2:]
        handler = {
            "run": self._run,
            "ps": self._ps,
            "inspect": self._inspect,
            "logs": self._logs,
            "pause": self._pause,
            "unpause": self._unpause,
            "stop": self._stop,
            "start": self._start,
            "restart": self._restart,
            "rm": self._rm,
        }.get(sub)
        if handler is None:
            return 1, "", f"docker: '{sub}' is not a docker command."
        return handler(machine, m, args)

    def _find(self, argv: list[str]) -> tuple[int, str, str]:
        """Simulate ``find -L ROOT -maxdepth N -type f -name PATTERN`` on the configs roots."""
        root = next((a for a in argv[1:] if not a.startswith("-")), "")
        pattern = argv[argv.index("-name") + 1] if "-name" in argv else "*"
        inv = self.inventory
        if inv is not None:
            matching = [
                p
                for p in inv.projects
                if p.configs.discover is not None and discover_root(p.configs.discover) == root
            ]
            # Several projects may share a root (``extends``): the simulated tree is the one
            # declared for any of them.
            matching.sort(key=lambda p: p.id not in self.settings.configs)
            for p in matching[:1]:
                d = p.configs.discover
                assert d is not None
                names = self.settings.configs.get(p.id)
                if names is None:
                    names = list(p.configs.static or ["default"])
                ext = d.pattern.rsplit("*", 1)[-1] if "*" in d.pattern else ".yaml"
                # realistic noise: hidden and non-YAML files (filtered by TrainHub / -name)
                files = [f"{n}{ext}" for n in names] + [".hydra/config.yaml", "README.md"]
                prefix = "" if root == "/" else root
                out = [
                    f"{prefix}/{f}" for f in files if fnmatch.fnmatch(f.rsplit("/", 1)[-1], pattern)
                ]
                return 0, "\n".join(out) + "\n", ""
        return 1, "", f"find: '{root}': No such file or directory"

    # -- checkpoints -----------------------------------------------------------------

    def _checkpoint_tree(self, root: str) -> dict[str, tuple[str, float, int]] | None:
        tree = self.checkpoint_trees.get(root)
        if tree is not None:
            return tree
        src = next(
            (c for c in (self.inventory.checkpoints if self.inventory else []) if c.root == root),
            None,
        )
        if src is None:
            return None
        tree = self.checkpoint_trees[root] = self._generate_checkpoints(src)
        return tree

    def _generate_checkpoints(self, src: CheckpointSource) -> dict[str, tuple[str, float, int]]:
        """Realistic tree following the layout: groups topk/last, epochNNNNNNN files, one run
        without checkpoints, one file outside the layout."""
        layout = ck.parse_layout(src.layout)
        rng = random.Random(src.id)
        names = self.settings.checkpoints.get(src.id) or _DEFAULT_CKPT_RUNS
        now = time.time()
        tree: dict[str, tuple[str, float, int]] = {}

        def fname(stem: str) -> str:
            return layout.file.replace("*", stem, 1).replace("*", "").replace("?", "0")

        for i, name in enumerate(names):
            start = now - (i * 2.5 + rng.uniform(0.1, 1.0)) * 86400
            tree[name] = ("d", start, 0)
            if i == len(names) - 1 and len(names) > 2:
                continue  # a run that crashed before its first checkpoint
            size = rng.randrange(80, 1500) * 1024 * 1024
            epochs = sorted(rng.sample(range(1, 60), 4))
            groups = (
                {"topk": epochs[:3], "last": epochs[-1:]} if layout.group_index else {"": epochs}
            )
            for g, eps in groups.items():
                dirs = [
                    g if d == ck.GROUP else ("x" if any(c in d for c in "*?[") else d)
                    for d in layout.dirs
                ]
                for k in range(1, len(dirs) + 1):
                    tree["/".join([name, *dirs[:k]])] = ("d", start, 0)
                for ep in eps:
                    path = "/".join([name, *dirs, fname(f"epoch{ep:07d}")])
                    mtime = min(start + ep * 1800, now - 900 - ep)
                    tree[path] = ("f", mtime, size + rng.randrange(0, 4096))
            if i == 1:
                tree[f"{name}/{fname('model_final')}"] = ("f", min(start + 120000, now - 600), size)
        return tree

    def _find_checkpoints(self, argv: list[str]) -> tuple[int, str, str]:
        """Simulate the checkpoint ``find … -printf`` (see :func:`ck.list_checkpoints`)."""
        root = argv[2]
        tree = self._checkpoint_tree(root)
        if tree is None:
            return 1, "", f"find: '{root}': No such file or directory"
        lines = [
            f"{kind}\t{mtime:.10f}\t{size}\t{path}" for path, (kind, mtime, size) in tree.items()
        ]
        return 0, "\n".join(lines) + "\n", ""

    def _rm_checkpoints(self, root: str, args: list[str]) -> tuple[int, str, str]:
        tree = self._checkpoint_tree(root)
        if tree is None:
            return 2, f"root\t{root}\n", ""
        sep = args.index("") if "" in args else len(args)
        out: list[str] = []
        for rel in args[:sep]:
            entry = tree.get(rel)
            if entry is None:
                out.append(f"gone\t{rel}")
            elif entry[0] == "d":
                out.append(f"err\t{rel}\tis a directory")
            else:
                del tree[rel]
                out.append(f"ok\t{rel}")
        for rel in args[sep + 1 :]:
            if tree.get(rel, ("",))[0] == "d" and not any(p.startswith(rel + "/") for p in tree):
                del tree[rel]
                out.append(f"rmdir\t{rel}")
        return 0, "".join(line + "\n" for line in out), ""

    def _nvidia_smi(
        self, machine: Machine, m: FakeMachine, argv: list[str]
    ) -> tuple[int, str, str]:
        """Simulate the ``nvidia-smi --query-*`` of :mod:`trainhub.core.gpus`.

        A container whose command is running uses the GPUs of its ``CUDA_VISIBLE_DEVICES``
        (GPU 0 without restriction), with a memory footprint derived from its name.
        """
        n = self.settings.gpus
        if n == 0:
            return 127, "", "bash: nvidia-smi: command not found"
        tag = hashlib.sha1(machine.id.encode()).hexdigest()[:8]
        uuids = [f"GPU-{tag}-{i:04d}" for i in range(n)]
        apps: list[tuple[int, int, int]] = []  # (gpu, pid, MiB)
        for c in m.containers.values():
            if c.state not in ("running", "paused") or c.command_done:
                continue
            indices = visible_indices(c.env.get("CUDA_VISIBLE_DEVICES"))
            h = int(hashlib.sha1(c.name.encode()).hexdigest()[:6], 16)
            for i in [0] if indices is None else indices:
                if i < n:
                    apps.append((i, 1000 + h % 30000, 8000 + h % 50000))
        if any(a.startswith("--query-compute-apps") for a in argv):
            return 0, "".join(f"{uuids[g]}, {pid}, {mib}\n" for g, pid, mib in apps), ""
        lines = []
        for i in range(n):
            used = sum(mib for g, _, mib in apps if g == i)
            util = min(100, 85 * sum(1 for g, _, _ in apps if g == i))
            lines.append(
                f"{i}, {uuids[i]}, NVIDIA A100-SXM4-80GB, {used + 4 if used else 1}, 81920, "
                f"{util}, {34 + util // 3}\n"
            )
        return 0, "".join(lines), ""

    def _proc(self, machine: Machine, m: FakeMachine) -> tuple[int, str, str]:
        """Simulate the ``/proc`` reads of :mod:`trainhub.core.load`.

        Each running command keeps ~8 cores busy (cores chosen from its name), on top of a
        light background noise; memory follows the same rule.
        """
        n = self.settings.cpus
        busy = [self._rng.uniform(0.0, 0.08) for _ in range(n)]
        jobs = 0
        for c in m.containers.values():
            if c.state != "running" or c.command_done:
                continue
            jobs += 1
            h = int(hashlib.sha1(c.name.encode()).hexdigest()[:6], 16)
            for k in range(min(8, n)):
                i = (h + k * 7) % n
                busy[i] = min(1.0, busy[i] + self._rng.uniform(0.6, 0.95))
        tick = int((time.monotonic() - self._t0) * 100)
        before, after = [], []
        for i, b in enumerate([sum(busy) / n, *busy]):
            name = "cpu" if i == 0 else f"cpu{i - 1}"
            scale = n if i == 0 else 1
            used0 = 1000 * scale + tick * scale // 3
            idle0 = 5000 * scale + tick * scale
            dt = 50 * scale  # 0.5 s at 100 Hz
            before.append(f"{name} {used0} 0 0 {idle0} 0 0 0 0 0 0")
            used1 = used0 + round(dt * b)
            idle1 = idle0 + dt - round(dt * b)
            after.append(f"{name} {used1} 0 0 {idle1} 0 0 0 0 0 0")
        total_kb = 512 * 1024 * 1024
        avail_kb = max(total_kb // 10, total_kb - (12 + 48 * jobs) * 1024 * 1024)
        load1 = sum(busy)
        out = (
            f"{load1:.2f} {load1 * 0.9:.2f} {load1 * 0.8:.2f} 2/1234 5678\n---\n"
            + "\n".join(before)
            + "\n---\n"
            + "\n".join(after)
            + f"\n---\nMemTotal:       {total_kb} kB\nMemAvailable:   {avail_kb} kB\n"
        )
        return 0, out, ""

    def find_container(self, run_id: str) -> FakeContainer | None:
        """Container of a TrainHub run, on any machine."""
        for m in self.machines.values():
            for c in m.containers.values():
                if c.labels.get(LABEL_RUN_ID) == run_id:
                    return c
        return None

    def mlflow_source(self) -> MlflowSource:
        return FakeMlflow(self)

    # docker run ---------------------------------------------------------------

    def _run(self, machine: Machine, m: FakeMachine, args: list[str]) -> tuple[int, str, str]:
        name: str | None = None
        labels: dict[str, str] = {}
        env: dict[str, str] = {}
        tty = False
        keep_alive = "--health-cmd" in args
        i = 0
        while i < len(args):
            a = args[i]
            if not a.startswith("-"):
                break
            if a in _RUN_VALUE_OPTS:
                val = args[i + 1] if i + 1 < len(args) else ""
                if a == "--name":
                    name = val
                elif a in ("--label", "-l"):
                    k, _, v = val.partition("=")
                    labels[k] = v
                elif a in ("-e", "--env"):
                    k, _, v = val.partition("=")
                    env[k] = v
                i += 2
                continue
            if a == "--tty" or (not a.startswith("--") and "t" in a[1:]):
                tty = True
            i += 1
        if i >= len(args):
            return 125, "", "docker: 'docker run' requires at least 1 argument."
        image, command = args[i], args[i + 1 :]
        if "missing" in image or "notfound" in image:
            return (
                125,
                "",
                f"Unable to find image '{image}' locally\n"
                f"docker: Error response from daemon: pull access denied for {image}, "
                "repository does not exist or may require 'docker login'.",
            )
        if name is None:
            name = f"fake_{self._rng.randrange(16**6):06x}"
        if name in m.containers:
            existing = m.containers[name]
            return (
                125,
                "",
                f'docker: Error response from daemon: Conflict. The container name "/{name}" '
                f'is already in use by container "{existing.id}".',
            )
        cid = hashlib.sha256(f"{machine.id}/{name}/{time.time()}".encode()).hexdigest()
        c = FakeContainer(
            name=name,
            image=image,
            labels=labels,
            command=command,
            tty=tty,
            id=cid,
            env=env,
            keep_alive=keep_alive,
        )
        m.containers[name] = c
        self._start_container(c)
        return 0, cid + "\n", ""

    def _start_container(self, c: FakeContainer) -> None:
        c.ml_run += 1
        c.ml_status, c.ml_epoch, c.ml_epochs = "RUNNING", None, None
        c.state = "running"
        c.exit_code = 0
        c.command_done = False
        c.started_at = _now()
        c.finished_at = None
        c.running.set()
        c.task = asyncio.get_running_loop().create_task(self._simulate(c))

    def _finish(self, c: FakeContainer, code: int) -> None:
        c.state = "exited"
        c.exit_code = code
        c.finished_at = _now()
        c.running.set()
        c.end_followers()

    # other commands --------------------------------------------------------------

    def _get(self, m: FakeMachine, name: str) -> FakeContainer | None:
        return m.containers.get(name)

    @staticmethod
    def _no_such(name: str) -> tuple[int, str, str]:
        return 1, "", f"Error response from daemon: No such container: {name}"

    def _ps(self, machine: Machine, m: FakeMachine, args: list[str]) -> tuple[int, str, str]:
        lines = [c.ps_json() for c in m.containers.values() if c.labels.get("trainhub") == "1"]
        return 0, "".join(line + "\n" for line in lines), ""

    def _inspect(self, machine: Machine, m: FakeMachine, args: list[str]) -> tuple[int, str, str]:
        name = args[-1]
        c = self._get(m, name)
        if c is None:
            return 1, "[]\n", f"Error: No such container: {name}"
        return 0, c.inspect_json() + "\n", ""

    def _parse_logs_args(self, args: list[str]) -> tuple[bool, int | None, str]:
        follow, tail = False, None
        i = 0
        while i < len(args) - 1:
            if args[i] in ("-f", "--follow"):
                follow = True
            elif args[i] in ("--tail", "-n"):
                v = args[i + 1]
                tail = None if v == "all" else int(v)
                i += 1
            i += 1
        return follow, tail, args[-1]

    @staticmethod
    def _tail_bytes(data: bytes, tail: int | None) -> bytes:
        if tail is None:
            return data
        if tail <= 0:
            return b""
        idx = len(data)
        if data.endswith(b"\n"):
            idx -= 1
        for _ in range(tail):
            idx = data.rfind(b"\n", 0, idx)
            if idx < 0:
                return data
        return data[idx + 1 :]

    def _logs(self, machine: Machine, m: FakeMachine, args: list[str]) -> tuple[int, str, str]:
        _, tail, name = self._parse_logs_args(args)
        c = self._get(m, name)
        if c is None:
            return self._no_such(name)
        return 0, self._tail_bytes(bytes(c.logs), tail).decode("utf-8", "replace"), ""

    def _pause(self, machine: Machine, m: FakeMachine, args: list[str]) -> tuple[int, str, str]:
        c = self._get(m, args[-1])
        if c is None:
            return self._no_such(args[-1])
        if c.state != "running":
            return 1, "", f"Error response from daemon: Container {c.id} is not running"
        c.state = "paused"
        c.running.clear()
        return 0, c.name + "\n", ""

    def _unpause(self, machine: Machine, m: FakeMachine, args: list[str]) -> tuple[int, str, str]:
        c = self._get(m, args[-1])
        if c is None:
            return self._no_such(args[-1])
        if c.state != "paused":
            return 1, "", f"Error response from daemon: Container {c.id} is not paused"
        c.state = "running"
        c.running.set()
        return 0, c.name + "\n", ""

    def _stop(self, machine: Machine, m: FakeMachine, args: list[str]) -> tuple[int, str, str]:
        c = self._get(m, args[-1])
        if c is None:
            return self._no_such(args[-1])
        if c.state == "paused":
            return (
                1,
                "",
                "Error response from daemon: cannot stop a paused container; unpause it first",
            )
        if c.state == "running" and c.command_done:
            # Kept container: the script exits with the command's code.
            self._finish(c, c.command_code)
        elif c.state == "running":
            if c.task is not None:
                c.task.cancel()
            c.ml_status = "KILLED"
            reset = "\x1b[0m" if c.tty else ""
            c.write(
                f"\r\n{reset}[{_ts()}][__main__][WARNING] - Received SIGTERM, "
                "saving checkpoint and exiting…\r\n".encode()
            )
            self._finish(c, 143)
        return 0, c.name + "\n", ""

    def _start(self, machine: Machine, m: FakeMachine, args: list[str]) -> tuple[int, str, str]:
        c = self._get(m, args[-1])
        if c is None:
            return self._no_such(args[-1])
        if c.state in ("running", "paused"):
            return 0, c.name + "\n", ""
        self._start_container(c)
        return 0, c.name + "\n", ""

    def _restart(self, machine: Machine, m: FakeMachine, args: list[str]) -> tuple[int, str, str]:
        c = self._get(m, args[-1])
        if c is None:
            return self._no_such(args[-1])
        if c.state == "paused":
            c.state = "running"
            c.running.set()
        if c.state == "running":
            self._stop(machine, m, ["-t", "10", c.name])
        self._start_container(c)
        return 0, c.name + "\n", ""

    def _rm(self, machine: Machine, m: FakeMachine, args: list[str]) -> tuple[int, str, str]:
        force = "-f" in args or "--force" in args
        c = self._get(m, args[-1])
        if c is None:
            return self._no_such(args[-1])
        if c.state in ("running", "paused") and not force:
            return (
                1,
                "",
                f'Error response from daemon: cannot remove container "/{c.name}": '
                "container is running: stop the container before removing or force remove",
            )
        if c.task is not None:
            c.task.cancel()
        c.end_followers()
        del m.containers[c.name]
        return 0, c.name + "\n", ""

    # -- streams ---------------------------------------------------------------

    async def stream(self, machine: Machine, remote: Sequence[str]) -> LogStream:
        await self._latency()
        if not self.is_reachable(machine.id):
            raise MachineUnreachableError(machine.id, "Connection timed out")
        argv = list(remote)
        q: asyncio.Queue[bytes | None] = asyncio.Queue()
        if argv[:2] != ["docker", "logs"]:
            q.put_nowait(None)
            return _FakeLogStream(q, None)
        follow, tail, name = self._parse_logs_args(argv[2:])
        m = self.machines.setdefault(machine.id, FakeMachine())
        c = self._get(m, name)
        if c is None:
            q.put_nowait(f"Error response from daemon: No such container: {name}\n".encode())
            q.put_nowait(None)
            return _FakeLogStream(q, None)
        initial = self._tail_bytes(bytes(c.logs), tail)
        if initial:
            q.put_nowait(initial)
        if follow and c.state in ("running", "paused", "created"):
            c.followers.append(q)
        else:
            q.put_nowait(None)
        return _FakeLogStream(q, c)

    async def open_shell(self, machine: Machine, remote: Sequence[str]) -> ShellSession:
        await self._latency()
        if not self.is_reachable(machine.id):
            raise MachineUnreachableError(machine.id, "Connection timed out")
        argv = list(remote)
        if argv[:2] != ["docker", "exec"]:
            raise ExecutorError("simulated shell: only docker exec is supported")
        name = next((a for a in argv[2:] if not a.startswith("-") and "=" not in a), "")
        c = self._get(self.machines.setdefault(machine.id, FakeMachine()), name)
        if c is None or c.state not in ("running", "paused"):
            msg = f"Error response from daemon: container {name} is not running"
            return _FakeShell(name, error=msg)
        return _FakeShell(name)

    async def close(self) -> None:
        for m in self.machines.values():
            for c in m.containers.values():
                if c.task is not None:
                    c.task.cancel()
                c.end_followers()

    # -- training simulation ---------------------------------------------------

    async def _sleep(self, c: FakeContainer, seconds: float) -> None:
        """Wait ``seconds`` (simulated time), freezing during pauses."""
        remaining = seconds / self.settings.time_scale
        while remaining > 0:
            await c.running.wait()
            step = min(remaining, 0.25)
            t0 = time.monotonic()
            await asyncio.sleep(step)
            if c.running.is_set():
                remaining -= time.monotonic() - t0

    async def _simulate(self, c: FakeContainer) -> None:
        try:
            count = int(c.labels.get(LABEL_SERIES) or 0)
            code = await self._series(c, count) if count else await self._training(c)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # pragma: no cover - safety net
            c.write(f"simulation error: {exc}\n".encode())
            code = 1
        c.ml_status = "FINISHED" if code == 0 else "FAILED"
        if c.keep_alive:
            c.command_done = True
            c.command_code = code
            nl = "\r\n" if c.tty else "\n"
            c.write(
                f"{nl}[trainhub] command finished (code {code}): container kept, "
                f"shell available from TrainHub{nl}".encode()
            )
            return
        self._finish(c, code)

    async def _series(self, c: FakeContainer, count: int) -> int:
        """Repetitions of a series, with the markers of the real script."""
        nl = "\r\n" if c.tty else "\n"
        keep_going = c.labels.get(LABEL_SERIES_CONTINUE) == "1"
        # Start markers of the real script, with the values of each repetition.
        script = c.command[-1] if c.command else ""
        starts = re.findall(r"\[trainhub\] series run \d+/\d+ started[^'\n]*", script)
        last = 0
        for k in range(1, count + 1):
            if k > 1:  # each repetition creates its own MLflow run
                c.ml_run += 1
                c.ml_status, c.ml_epoch, c.ml_epochs = "RUNNING", None, None
            start = starts[k - 1] if len(starts) == count else start_marker(k, count)
            c.write(f"{start}{nl}".encode())
            code = await self._training(c, salt=str(k))
            c.ml_status = "FINISHED" if code == 0 else "FAILED"
            c.write(f"{nl}{end_marker(k, count, code)}{nl}".encode())
            if code != 0:
                last = code
                if not keep_going:
                    if k < count:
                        c.write(f"{halt_message(k, count)}{nl}".encode())
                    break
        return last

    async def _training(self, c: FakeContainer, salt: str = "") -> int:
        s = self.settings
        rng = random.Random(f"{c.id}{c.started_at}{salt}")
        nl = "\r\n" if c.tty else "\n"
        tty = c.tty

        def col(code: str, text: str) -> str:
            return f"\x1b[{code}m{text}\x1b[0m" if tty else text

        def w(text: str = "") -> None:
            c.write((text + nl).encode())

        config = c.labels.get("trainhub.config", "default")
        project = c.labels.get("trainhub.project", "project")
        duration = rng.uniform(*s.duration_s)
        epochs = rng.choice([5, 8, 10, 12, 20])
        epoch_s = duration / epochs
        bar_updates = max(4, min(60, int(epoch_s * s.log_rate)))
        steps = rng.choice([200, 250, 500, 1000])
        will_fail = rng.random() < s.failure_prob
        fail_epoch = rng.randrange(epochs) if will_fail else -1
        lr = rng.choice([1e-3, 3e-4, 1e-4])
        bs = rng.choice([16, 32, 64])
        seed = rng.randrange(10000)
        c.ml_epochs, c.ml_epoch = epochs, 0

        hydra = col("36", "[HYDRA]")
        w(f"{hydra} Launching 1 jobs locally")
        w(f"{hydra} \t#0 : --config-name {config}")
        await self._sleep(c, 0.3)
        info = col("32", "INFO")
        w(f"[{_ts()}][{project}][{info}] - Run directory: outputs/{_now():%Y-%m-%d/%H-%M-%S}")
        w(f"[{_ts()}][{project}][{info}] - Resolved configuration:")
        for line in (
            "model:",
            f"  name: {config}",
            f"  depth: {rng.choice([18, 34, 50])}",
            "optimizer:",
            "  _target_: torch.optim.AdamW",
            f"  lr: {lr}",
            "  weight_decay: 0.01",
            "trainer:",
            f"  max_epochs: {epochs}",
            "  precision: bf16-mixed",
            f"  devices: {rng.choice([1, 2, 4])}",
            "data:",
            f"  batch_size: {bs}",
            "  num_workers: 8",
            f"seed: {seed}",
        ):
            w("  " + line)
        await self._sleep(c, 0.5)
        warn = col("33", "WARNING")
        w(
            f"[{_ts()}][py.warnings][{warn}] - UserWarning: The dataloader does not have many "
            "workers which may be a bottleneck."
        )
        w(f"[{_ts()}][{project}][{info}] - GPU available: True (cuda), used: True")
        await self._sleep(c, 0.3)

        loss = rng.uniform(1.8, 2.6)
        best = math.inf
        for epoch in range(epochs):
            t_epoch = time.monotonic()
            for k in range(1, bar_updates + 1):
                await self._sleep(c, epoch_s / bar_updates)
                done = int(steps * k / bar_updates)
                frac = done / steps
                loss *= rng.uniform(0.985, 1.0)
                filled = int(frac * 10)
                partial = " ▏▎▍▌▋▊▉"[int((frac * 10 - filled) * 8)] if filled < 10 else ""
                bar = "█" * filled + partial
                elapsed = (time.monotonic() - t_epoch) * s.time_scale
                rate = done / elapsed if elapsed > 0 else 0.0
                eta = (steps - done) / rate if rate > 0 else 0.0
                c.write(
                    (
                        f"\rEpoch {epoch + 1}/{epochs} {int(frac * 100):3d}%|{bar:<10}| "
                        f"{done}/{steps} [{_mmss(elapsed)}<{_mmss(eta)}, {rate:.1f}it/s, "
                        f"loss={loss:.3f}]"
                    ).encode()
                )
                if epoch == fail_epoch and k == bar_updates // 2:
                    c.write(nl.encode())
                    self._traceback(c)
                    return 1
            c.write(nl.encode())
            c.ml_epoch = epoch + 1
            val = loss * rng.uniform(1.0, 1.15)
            acc = min(
                0.99, 0.5 + 0.45 * (1 - math.exp(-(epoch + 1) / 4)) + rng.uniform(-0.02, 0.02)
            )
            better = val < best
            best = min(best, val)
            mark = col("32", " ✓ best") if better else ""
            w(
                f"[{_ts()}][{project}][{info}] - epoch={epoch + 1} train/loss={loss:.4f} "
                f"val/loss={val:.4f} val/acc={acc:.4f} lr={lr:.1e}{mark}"
            )
            if better:
                w(f"[{_ts()}][{project}][{info}] - Saving checkpoint to checkpoints/best.ckpt")
            if rng.random() < 0.15:
                w(
                    f"[{_ts()}][py.warnings][{warn}] - Detected call of `lr_scheduler.step()` "
                    "before `optimizer.step()`."
                )
        w(
            f"[{_ts()}][{project}][{info}] - "
            + col("1;32", "Training finished.")
            + f" best val/loss={best:.4f}"
        )
        return 0

    def _traceback(self, c: FakeContainer) -> None:
        nl = "\r\n" if c.tty else "\n"
        red = "\x1b[31m" if c.tty else ""
        reset = "\x1b[0m" if c.tty else ""
        lines = [
            "Error executing job with overrides: []",
            "Traceback (most recent call last):",
            '  File "/workspace/train.py", line 88, in main',
            "    trainer.fit(model, datamodule=dm)",
            '  File "/opt/venvs/main/lib/python3.11/site-packages/lightning/trainer.py", '
            "line 543, in fit",
            "    call._call_and_handle_interrupt(",
            '  File "/workspace/model.py", line 41, in training_step',
            "    loss = self.criterion(logits, batch['mask'])",
            f"{red}RuntimeError: CUDA error: an illegal memory access was encountered{reset}",
            "",
            "Set the environment variable HYDRA_FULL_ERROR=1 for a complete stack trace.",
        ]
        c.write("".join(line + nl for line in lines).encode())


class FakeMlflow(MlflowSource):
    """MLflow server simulated from the containers of a :class:`FakeExecutor`."""

    def __init__(self, executor: FakeExecutor) -> None:
        self.executor = executor

    async def fetch(self, cfg: MlflowConfig, run: Run) -> MlflowInfo:
        c = self.executor.find_container(run.id)
        if c is None:
            return MlflowInfo()
        exp = str(int(hashlib.sha1(run.project_id.encode()).hexdigest()[:4], 16))
        ml_id = hashlib.sha1(f"{c.id}-{c.ml_run}".encode()).hexdigest()[:32]
        return MlflowInfo(
            run_id=ml_id,
            experiment_id=exp,
            status=c.ml_status,
            epoch=None if c.ml_epoch is None else c.ml_epoch + cfg.epoch_offset,
            max_epochs=c.ml_epochs,
            url=f"{cfg.tracking_uri}/#/experiments/{exp}/runs/{ml_id}",
        )


class _FakeShell(ShellSession):
    """Simulated shell: local echo, a few commands, ``exit``."""

    def __init__(self, container: str, *, error: str | None = None) -> None:
        self._q: asyncio.Queue[bytes] = asyncio.Queue()
        self._line = ""
        self._closed = False
        self.size = (120, 32)
        if error:
            self._q.put_nowait(f"{error}\r\n".encode())
            self._q.put_nowait(b"")
            self._closed = True
            return
        self._prompt = f"\x1b[1;32mroot@{container[:12]}\x1b[0m:\x1b[1;34m/workspace\x1b[0m# "
        self._q.put_nowait(
            b"\x1b[2m(simulated shell: fake mode, no command is executed)\x1b[0m\r\n"
            + self._prompt.encode()
        )

    async def read(self) -> bytes:
        return await self._q.get()

    def _run(self, line: str) -> str:
        cmd = line.strip()
        if not cmd:
            return ""
        if cmd in ("exit", "logout"):
            return "\x00exit"
        if cmd == "pwd":
            return "/workspace\r\n"
        if cmd == "ls":
            return "checkpoints  configs  outputs  train.py\r\n"
        if cmd.startswith("echo "):
            return cmd[5:] + "\r\n"
        if cmd == "nvidia-smi":
            return "GPU 0: NVIDIA A100-SXM4-80GB (simulated)  | 0MiB / 81920MiB\r\n"
        return f"{cmd.split()[0]}: simulated command\r\n"

    def write(self, data: bytes) -> None:
        if self._closed:
            return
        out = ""
        for ch in data.decode("utf-8", "replace"):
            if ch in ("\r", "\n"):
                res = self._run(self._line)
                self._line = ""
                if res == "\x00exit":
                    self._q.put_nowait(b"\r\nexit\r\n")
                    self._q.put_nowait(b"")
                    self._closed = True
                    return
                out += "\r\n" + res + self._prompt
            elif ch in ("\x7f", "\b"):
                if self._line:
                    self._line = self._line[:-1]
                    out += "\b \b"
            elif ch == "\x03":
                self._line = ""
                out += "^C\r\n" + self._prompt
            elif ch == "\x04" and not self._line:
                self._q.put_nowait(b"exit\r\n")
                self._q.put_nowait(b"")
                self._closed = True
                return
            elif ch >= " ":
                self._line += ch
                out += ch
        if out:
            self._q.put_nowait(out.encode())

    def resize(self, cols: int, rows: int) -> None:
        self.size = (cols, rows)

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._q.put_nowait(b"")


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]


def _mmss(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 60:02d}:{s % 60:02d}"
