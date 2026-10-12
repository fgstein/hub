"""Local mode integration: real docker commands (skipped if Docker is unavailable)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from trainhub.core.models import RunStatus
from trainhub.executors.local import LocalExecutor
from trainhub.service import InventoryManager, RunService, Selection, Store

from .conftest import ROOT
from .helpers import wait_for

IMAGE = "trainhub-fake-train"


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=10)
        subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True, check=True, timeout=10
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return True


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_ok(), reason=f"Docker or the {IMAGE} image unavailable"),
]


async def test_local_lifecycle(tmp_path: Path) -> None:
    inv = InventoryManager(ROOT / "dev" / "inventory.local.yaml")
    svc = RunService(inv, LocalExecutor(), Store(tmp_path / "db.sqlite"), poll_interval=0.5)
    await svc.start()
    run_ids: list[str] = []
    try:
        sel = Selection(
            project="fake",
            machine="localhost",
            image="fake-train",
            venv="exp",
            config="baseline",
            overrides=["trainer.max_epochs=1", 'model.name="a b"'],
        )
        run = await svc.launch(sel, wait=True)
        run_ids.append(run.id)
        assert run.status is RunStatus.RUNNING, run.error
        assert run.full_command[0] == "docker"
        text = b"".join([c async for c in svc.stream_logs(run.id)]).decode()
        assert "venv=exp" in text and "name: a b" in text and "Epoch 1/1" in text
        # tqdm prints its bar (TTY sized by stty, otherwise 0x0 and nothing is shown)
        assert "it/s" in text and "100%|" in text

        async def finished() -> bool:
            return (await svc.get_run(run.id)).status is RunStatus.SUCCEEDED

        await wait_for(finished, timeout=30)
        assert (await svc.get_run(run.id)).exit_code == 0

        # pause / resume / stop (SIGTERM handled by train.py → 143)
        long = await svc.launch(
            sel.model_copy(update={"config": "long", "overrides": []}), wait=True
        )
        run_ids.append(long.id)

        async def training() -> bool:
            return b"Epoch" in await svc.read_logs(long.id)

        await wait_for(training, timeout=30)
        assert (await svc.pause(long.id)).status is RunStatus.PAUSED
        assert (await svc.resume(long.id)).status is RunStatus.RUNNING
        stopped = await svc.stop(long.id, wait=True)
        assert stopped.status is RunStatus.STOPPED and stopped.exit_code == 143
        assert b"SIGTERM" in await svc.read_logs(long.id)

        # failure at epoch 2
        broken = await svc.launch(
            sel.model_copy(update={"config": "broken", "overrides": []}), wait=True
        )
        run_ids.append(broken.id)

        async def failed() -> bool:
            return (await svc.get_run(broken.id)).status is RunStatus.FAILED

        await wait_for(failed, timeout=30)
        assert (await svc.get_run(broken.id)).exit_code == 1

        # config in a subdirectory, discovered recursively
        cfg = await svc.get_configs("fake", "localhost")
        assert "experiments/sweep/short" in cfg.configs
        nested = await svc.launch(
            sel.model_copy(update={"config": "experiments/sweep/short", "overrides": []}),
            wait=True,
        )
        run_ids.append(nested.id)

        async def nested_ok() -> bool:
            return (await svc.get_run(nested.id)).status is RunStatus.SUCCEEDED

        await wait_for(nested_ok, timeout=30)
        assert b"name: short" in await svc.read_logs(nested.id)

        # image absente
        missing = await svc.launch(sel.model_copy(update={"image": "missing"}), wait=True)
        run_ids.append(missing.id)
        assert missing.status is RunStatus.FAILED and missing.error
    finally:
        for rid in run_ids:
            r = await svc.get_run(rid)
            subprocess.run(["docker", "rm", "-f", r.container_name], capture_output=True)
        await svc.close()
