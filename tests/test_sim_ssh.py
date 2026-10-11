"""Full SSH → docker → venv path with the simulated machines (make sim-up).

Skipped automatically if the simulated machines are not reachable.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from trainhub.core.models import RunStatus
from trainhub.executors.ssh import SshExecutor
from trainhub.service import InventoryManager, RunService, Selection, Store

from .conftest import ROOT
from .helpers import wait_for


def _sim_ok() -> bool:
    if shutil.which("ssh") is None or shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "sim-gpu-01", "--",
             "docker", "image", "inspect", "trainhub-fake-train"],
            capture_output=True, check=True, timeout=15,
        )  # fmt: skip
    except (subprocess.SubprocessError, OSError):
        return False
    return True


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _sim_ok(), reason="simulated SSH machines unavailable (make sim-up)"),
]


def _compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "dev" / "docker-compose.yml"), *args],
        capture_output=True,
        check=True,
        timeout=60,
    )


async def test_ssh_full_path(tmp_path: Path) -> None:
    inv = InventoryManager(ROOT / "dev" / "inventory.sim.yaml")
    svc = RunService(inv, SshExecutor(), Store(tmp_path / "db.sqlite"), poll_interval=0.5)
    await svc.start()
    created: list[str] = []
    try:
        cfg = await svc.get_configs("fake", "sim-gpu-01")
        assert cfg.configs == ["baseline", "broken", "experiments/sweep/short", "long"]

        sel = Selection(
            project="fake",
            machine="sim-gpu-01",
            image="fake-train",
            venv="exp",
            config="long",
            overrides=['model.name="a b"', '+trainer.note="it\'s $HOME"'],
        )
        run = await svc.launch(sel, wait=True)
        created.append(run.container_name)
        assert run.status is RunStatus.RUNNING, run.error
        assert run.full_command[0] == "ssh"
        # ControlMaster: a shared connection socket exists
        assert glob.glob(os.path.expanduser("~/.ssh/th-*"))

        async def training() -> bool:
            return b"Epoch" in await svc.read_logs(run.id)

        await wait_for(training, timeout=30)
        logs = (await svc.read_logs(run.id)).decode()
        assert "venv=exp" in logs and "name: a b" in logs and "note: it's $HOME" in logs

        assert (await svc.pause(run.id)).status is RunStatus.PAUSED
        assert (await svc.resume(run.id)).status is RunStatus.RUNNING
        stopped = await svc.stop(run.id, wait=True)
        assert stopped.status is RunStatus.STOPPED and stopped.exit_code == 143

        # a machine goes down: unreachable, then back
        run2 = await svc.launch(sel.model_copy(update={"machine": "sim-gpu-02"}), wait=True)
        created.append(run2.container_name)
        _compose("stop", "sim-gpu-02")
        await svc.monitor.poll("sim-gpu-02")
        assert (await svc.get_run(run2.id)).status is RunStatus.UNREACHABLE
        assert svc.machine_state("sim-gpu-01").reachable is not False
        _compose("start", "sim-gpu-02")

        async def back() -> bool:
            await svc.monitor.poll("sim-gpu-02")
            return (await svc.get_run(run2.id)).status is RunStatus.RUNNING

        await wait_for(back, timeout=30, interval=0.5)
    finally:
        for name in created:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        await svc.close()
