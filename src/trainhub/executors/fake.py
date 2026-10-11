"""Exécuteur ``fake`` : machines et conteneurs simulés en mémoire.

Il interprète l'argv docker réellement construit par :mod:`trainhub.core.commands`
(``docker run/ps/inspect/logs/pause/unpause/stop/start/rm`` et ``ls``), ce qui teste aussi
la construction des commandes. Chaque conteneur produit des logs réalistes : bannière Hydra,
config résolue, lignes ANSI colorées, barres tqdm réécrites avec ``\\r``, métriques par époque,
warnings, et éventuellement un traceback avec code de sortie 1.

Paramètres : section ``simulation`` de l'inventaire (voir ``dev/inventory.dev.yaml``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..core.commands import transport
from ..core.models import Inventory, Machine, SimulationSettings
from .base import ExecResult, Executor, LogStream, MachineUnreachableError

# Options ``docker run`` prenant une valeur séparée.
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
    }
)

_LOG_LIMIT = 8 * 1024 * 1024  # octets conservés par conteneur simulé


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str:
    return dt.isoformat().replace("+00:00", "Z") if dt else "0001-01-01T00:00:00Z"


@dataclass
class FakeContainer:
    """Conteneur simulé."""

    name: str
    image: str
    labels: dict[str, str]
    command: list[str]
    tty: bool
    id: str
    created_at: datetime = field(default_factory=_now)
    state: str = "created"
    exit_code: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    logs: bytearray = field(default_factory=bytearray)
    followers: list[asyncio.Queue[bytes | None]] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    running: asyncio.Event = field(default_factory=asyncio.Event)

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

    def status_text(self) -> str:
        if self.state == "running":
            return "Up"
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
    """Simule Docker sur des machines en mémoire."""

    kind = "fake"

    def __init__(
        self, settings: SimulationSettings | None = None, *, inventory: Inventory | None = None
    ) -> None:
        self.settings = settings or SimulationSettings()
        self.inventory = inventory
        self.machines: dict[str, FakeMachine] = {}
        self._rng = random.Random(self.settings.seed)
        self._t0 = time.monotonic()
        #: états forcés par les tests : machine -> "ok" | "down"
        self.forced_state: dict[str, str] = {}
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
        """``ok``, ``flaky`` ou ``down`` pour une machine."""
        return self.forced_state.get(machine_id) or self.settings.machines.get(machine_id, "ok")

    def is_reachable(self, machine_id: str) -> bool:
        state = self.machine_state(machine_id)
        if state == "down":
            return False
        if state == "flaky":
            # Injoignable pendant ~30 % de chaque période, décalé selon la machine.
            period = self.settings.flaky_period_s
            h = int(hashlib.sha1(machine_id.encode()).hexdigest()[:6], 16)
            phase = ((time.monotonic() - self._t0) / period + (h % 1000) / 1000) % 1.0
            return phase < 0.7
        return True

    async def _latency(self) -> None:
        lo, hi = self.settings.latency_ms
        await asyncio.sleep(self._rng.uniform(lo, hi) / 1000.0)

    # -- exécution -------------------------------------------------------------

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
            return 127, "", "commande vide"
        if argv[0] == "ls":
            return self._ls(argv)
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
            "rm": self._rm,
        }.get(sub)
        if handler is None:
            return 1, "", f"docker: '{sub}' is not a docker command."
        return handler(machine, m, args)

    def _ls(self, argv: list[str]) -> tuple[int, str, str]:
        path = argv[-1]
        inv = self.inventory
        if inv is not None:
            for p in inv.projects:
                d = p.configs.discover
                if d is not None and d.path.rstrip("/") == path.rstrip("/"):
                    names = self.settings.configs.get(p.id)
                    if names is None:
                        names = list(p.configs.static or ["default"])
                    ext = d.pattern.rsplit("*", 1)[-1] if "*" in d.pattern else ".yaml"
                    files = [n + ext for n in names] + ["README.md", "_base"]
                    return 0, "\n".join(files) + "\n", ""
        return 2, "", f"ls: cannot access '{path}': No such file or directory"

    # docker run ---------------------------------------------------------------

    def _run(self, machine: Machine, m: FakeMachine, args: list[str]) -> tuple[int, str, str]:
        name: str | None = None
        labels: dict[str, str] = {}
        tty = False
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
        c = FakeContainer(name=name, image=image, labels=labels, command=command, tty=tty, id=cid)
        m.containers[name] = c
        self._start_container(c)
        return 0, cid + "\n", ""

    def _start_container(self, c: FakeContainer) -> None:
        c.state = "running"
        c.exit_code = 0
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

    # autres commandes ------------------------------------------------------------

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
        if c.state == "running":
            if c.task is not None:
                c.task.cancel()
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

    # -- flux ------------------------------------------------------------------

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

    async def close(self) -> None:
        for m in self.machines.values():
            for c in m.containers.values():
                if c.task is not None:
                    c.task.cancel()
                c.end_followers()

    # -- simulation d'un entraînement -----------------------------------------

    async def _sleep(self, c: FakeContainer, seconds: float) -> None:
        """Attend ``seconds`` (temps simulé), en gelant pendant les pauses."""
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
            code = await self._training(c)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # pragma: no cover - filet de sécurité
            c.write(f"simulation error: {exc}\n".encode())
            code = 1
        self._finish(c, code)

    async def _training(self, c: FakeContainer) -> int:
        s = self.settings
        rng = random.Random(f"{c.id}{c.started_at}")
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


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]


def _mmss(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 60:02d}:{s % 60:02d}"
