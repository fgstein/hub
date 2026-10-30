"""MLflow: settings, environment of the container, REST client, tracking loop."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from trainhub.core import commands as cmd
from trainhub.core.inventory import InventoryError, parse_inventory
from trainhub.core.models import MlflowConfig, MlflowInfo, Run
from trainhub.core.selection import make_spec
from trainhub.core.status import utcnow
from trainhub.service.mlflow import HttpMlflow, MlflowError, MlflowSource, read_info
from trainhub.service.store import Store

from .conftest import BASIC_INVENTORY
from .helpers import SEL, make_service, sim_inventory_text, wait_for

MLFLOW = """
mlflow:
  tracking_uri: http://mlflow.lab:5000/
  max_epochs_param: trainer.max_epochs
  poll_interval_s: 5
"""


def _with_project_mlflow(text: str, block: str) -> str:
    """Add an ``mlflow`` block to the segmentation project."""
    anchor = "    configs:\n      static: [baseline, unet_large]\n"
    return text.replace(anchor, anchor + block, 1)


def test_settings_merge() -> None:
    inv = parse_inventory(
        _with_project_mlflow(
            BASIC_INVENTORY + MLFLOW,
            "    mlflow:\n      epoch_metric: trainer/epoch\n      epoch_offset: 1\n",
        ),
        "t.yaml",
    )
    seg = inv.mlflow_config(inv.project("segmentation"))
    assert seg == MlflowConfig(
        tracking_uri="http://mlflow.lab:5000",
        max_epochs_param="trainer.max_epochs",
        epoch_metric="trainer/epoch",
        epoch_offset=1,
    )
    det = inv.mlflow_config(inv.project("detection"))
    assert det is not None and det.epoch_metric == "epoch" and det.run_tag == "trainhub.run_id"
    assert inv.mlflow_poll_interval == 5


def test_settings_disabled_or_absent() -> None:
    inv = parse_inventory(
        _with_project_mlflow(BASIC_INVENTORY + MLFLOW, "    mlflow: {enabled: false}\n"), "t.yaml"
    )
    assert inv.mlflow_config(inv.project("segmentation")) is None
    assert inv.mlflow_config(inv.project("detection")) is not None
    plain = parse_inventory(BASIC_INVENTORY, "t.yaml")
    assert plain.mlflow_config(plain.project("segmentation")) is None
    assert plain.mlflow_poll_interval == 10
    # project only, without global section
    own = parse_inventory(
        _with_project_mlflow(BASIC_INVENTORY, "    mlflow: {tracking_uri: 'https://ml.x'}\n"),
        "t.yaml",
    )
    assert own.mlflow_config(own.project("segmentation")) is not None
    assert own.mlflow_config(own.project("detection")) is None


@pytest.mark.parametrize(
    "block", ["mlflow: {tracking_uri: 'mlflow.lab:5000'}", "mlflow: {run_tag: 'a`b'}"]
)
def test_settings_invalid(block: str) -> None:
    with pytest.raises(InventoryError):
        parse_inventory(BASIC_INVENTORY + block + "\n", "t.yaml")


def _env_of(argv: list[str]) -> dict[str, str]:
    return dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-e")


def test_container_environment() -> None:
    inv = parse_inventory(BASIC_INVENTORY + MLFLOW, "t.yaml")
    spec = make_spec(
        inv,
        run_id="r1a2b3",
        project="segmentation",
        machine="gpu-01",
        image="torch-2.4",
        venv="main",
        config="baseline",
    )
    env = _env_of(cmd.docker_run(spec))
    assert env["TRAINHUB_RUN_ID"] == "r1a2b3"
    assert json.loads(env["MLFLOW_RUN_CONTEXT"]) == {"trainhub.run_id": "r1a2b3"}
    # no MLflow: nothing added
    plain = parse_inventory(BASIC_INVENTORY, "t.yaml")
    spec = make_spec(
        plain,
        run_id="r1a2b3",
        project="segmentation",
        machine="gpu-01",
        image="torch-2.4",
        venv="main",
        config="baseline",
    )
    assert "MLFLOW_RUN_CONTEXT" not in _env_of(cmd.docker_run(spec))
    # inject_env: false
    off = parse_inventory(BASIC_INVENTORY + MLFLOW + "  inject_env: false\n", "t.yaml")
    spec = make_spec(
        off,
        run_id="r1a2b3",
        project="segmentation",
        machine="gpu-01",
        image="torch-2.4",
        venv="main",
        config="baseline",
    )
    assert "TRAINHUB_RUN_ID" not in _env_of(cmd.docker_run(spec))


CFG = MlflowConfig(tracking_uri="http://ml", max_epochs_param="max_epochs", epoch_metric="epoch")


def _mlrun(status: str = "RUNNING", epoch: float | None = 3, rid: str = "abc") -> dict[str, Any]:
    metrics = [{"key": "epoch", "value": epoch, "step": 3}] if epoch is not None else []
    return {
        "info": {"run_id": rid, "experiment_id": "7", "status": status},
        "data": {
            "params": [{"key": "max_epochs", "value": "10"}, {"key": "lr", "value": "x"}],
            "metrics": [*metrics, {"key": "loss", "value": 0.1}],
        },
    }


def test_read_info() -> None:
    info = read_info(CFG, _mlrun())
    assert info == MlflowInfo(
        run_id="abc",
        experiment_id="7",
        status="RUNNING",
        epoch=3,
        max_epochs=10,
        url="http://ml/#/experiments/7/runs/abc",
    )
    shifted = read_info(CFG.model_copy(update={"epoch_offset": 1}), _mlrun(epoch=0))
    assert shifted.epoch == 1
    bare = read_info(CFG, {"info": {"run_id": "x", "status": "FAILED"}})
    assert bare.epoch is None and bare.max_epochs is None and bare.url is None


def _run(**kw: Any) -> Run:
    base: dict[str, Any] = {
        "id": "r00001",
        "project_id": "segmentation",
        "machine_id": "gpu-01",
        "image_id": "torch-2.4",
        "venv_id": "main",
        "config": "baseline",
        "command": "python train.py",
        "container_name": "trainhub-segmentation-r00001",
        "status": "running",
        "created_at": utcnow(),
        "started_at": utcnow(),
    }
    return Run.model_validate(base | kw)


async def test_http_client_search_then_get() -> None:
    calls: list[tuple[str, str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body or dict(req.url.params)))
        path = req.url.path.removeprefix("/api/2.0/mlflow/")
        if path == "experiments/get-by-name":
            if req.url.params["experiment_name"] == "seg":
                return httpx.Response(200, json={"experiment": {"experiment_id": "7"}})
            return httpx.Response(404, json={"error_code": "RESOURCE_DOES_NOT_EXIST"})
        if path == "runs/search":
            return httpx.Response(200, json={"runs": [_mlrun()]})
        if path == "runs/get":
            return httpx.Response(200, json={"run": _mlrun("FINISHED", 10)})
        return httpx.Response(500, json={"message": "unexpected"})

    client = HttpMlflow(transport=httpx.MockTransport(handler))
    cfg = CFG.model_copy(update={"experiments": ["seg", "missing"]})
    try:
        run = _run()
        info = await client.fetch(cfg, run)
        assert info.status == "RUNNING" and info.epoch == 3 and info.max_epochs == 10
        search = next(c for c in calls if c[1].endswith("runs/search"))
        assert search[2]["experiment_ids"] == ["7"]
        assert search[2]["filter"].startswith("tags.`trainhub.run_id` = 'r00001' AND ")
        # once found, the run is read directly
        calls.clear()
        info = await client.fetch(cfg, run.model_copy(update={"mlflow": info}))
        assert info.status == "FINISHED" and [c[1] for c in calls] == ["/api/2.0/mlflow/runs/get"]
    finally:
        await client.close()


async def test_http_client_all_experiments_and_errors() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/2.0/mlflow/")
        if path == "experiments/search":
            token = json.loads(req.content).get("page_token")
            if token is None:
                return httpx.Response(
                    200, json={"experiments": [{"experiment_id": "0"}], "next_page_token": "p2"}
                )
            return httpx.Response(200, json={"experiments": [{"experiment_id": "3"}]})
        if path == "runs/search":
            assert json.loads(req.content)["experiment_ids"] == ["0", "3"]
            return httpx.Response(200, json={})
        return httpx.Response(500)

    client = HttpMlflow(transport=httpx.MockTransport(handler))
    try:
        assert await client.fetch(CFG, _run()) == MlflowInfo()
    finally:
        await client.close()

    def failing(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Permission denied"})

    client = HttpMlflow(transport=httpx.MockTransport(failing))
    try:
        with pytest.raises(MlflowError, match="403: Permission denied"):
            await client.fetch(CFG, _run())
    finally:
        await client.close()


async def test_store_roundtrip(tmp_path: Path) -> None:
    store = Store(tmp_path / "db.sqlite")
    await store.open()
    try:
        info = MlflowInfo(run_id="a", status="RUNNING", epoch=2, max_epochs=5)
        await store.save_run(_run(mlflow=info))
        await store.save_run(_run(id="r00002"))
        got = await store.get_run("r00001")
        assert got is not None and got.mlflow == info
        other = await store.get_run("r00002")
        assert other is not None and other.mlflow is None
    finally:
        await store.close()


def _sim_mlflow_text(**sim: float) -> str:
    return sim_inventory_text(**sim) + MLFLOW


async def test_tracker_with_fake(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=_sim_mlflow_text(duration=4, scale=4))
    await svc.start(monitor=True, watch_inventory=False)
    try:
        run = await svc.launch(SEL, wait=True)

        async def progressing() -> bool:
            await svc.mlflow.poll()
            r = await svc.get_run(run.id)
            return r.mlflow is not None and (r.mlflow.epoch or 0) >= 1

        await wait_for(progressing, timeout=20)
        r = await svc.get_run(run.id)
        assert r.mlflow is not None and r.mlflow.status == "RUNNING"
        assert r.mlflow.max_epochs and r.mlflow.url and r.mlflow.url.startswith("http://mlflow.lab")

        async def finished() -> bool:
            await svc.mlflow.poll()
            r = await svc.get_run(run.id)
            return r.mlflow is not None and r.mlflow.status == "FINISHED"

        await wait_for(finished, timeout=30)
        r = await svc.get_run(run.id)
        assert r.status == "succeeded" and r.mlflow is not None
        assert r.mlflow.epoch == r.mlflow.max_epochs
        # terminal: no longer polled
        assert svc.mlflow._wanted(r) is None
    finally:
        await svc.close()


async def test_tracker_stop_is_killed(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=_sim_mlflow_text(duration=60, scale=1))
    await svc.start(monitor=True, watch_inventory=False)
    try:
        run = await svc.launch(SEL, wait=True)
        await svc.stop(run.id, wait=True)
        await svc.mlflow.poll()
        r = await svc.get_run(run.id)
        assert r.status == "stopped" and r.mlflow is not None and r.mlflow.status == "KILLED"
    finally:
        await svc.close()


class _Flaky(MlflowSource):
    def __init__(self) -> None:
        self.fail = False

    async def fetch(self, cfg: MlflowConfig, run: Run) -> MlflowInfo:
        if self.fail:
            raise MlflowError("MLflow unreachable")
        return MlflowInfo(run_id="m", status="RUNNING", epoch=4, max_epochs=8)


async def test_tracker_keeps_state_on_error(tmp_path: Path) -> None:
    src = _Flaky()
    svc = make_service(tmp_path, text=_sim_mlflow_text(duration=60))
    svc.mlflow.source = src
    await svc.start(monitor=False, watch_inventory=False)
    try:
        run = await svc.launch(SEL, wait=True)
        await svc.mlflow.poll()
        src.fail = True
        await svc.mlflow.poll()
        r = await svc.get_run(run.id)
        assert r.mlflow is not None and r.mlflow.epoch == 4 and r.mlflow.error
        src.fail = False
        await svc.mlflow.poll()
        r = await svc.get_run(run.id)
        assert r.mlflow is not None and r.mlflow.error is None
    finally:
        await svc.close()


async def test_tracker_selection(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=_sim_mlflow_text())
    tr = svc.mlflow
    assert tr._wanted(_run()) is not None
    done = _run(status="succeeded", finished_at=utcnow())
    assert tr._wanted(done) is not None  # MLflow may lag behind the container
    assert tr._wanted(done.model_copy(update={"mlflow": MlflowInfo(status="FINISHED")})) is None
    old = done.model_copy(update={"finished_at": utcnow() - timedelta(hours=1)})
    assert tr._wanted(old) is None
    assert tr._wanted(_run(project_id="gone")) is None
    plain = make_service(tmp_path, text=sim_inventory_text())
    assert plain.mlflow._wanted(_run()) is None


async def test_restart_forgets_previous_mlflow_run(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=_sim_mlflow_text(duration=60))
    await svc.start(monitor=True, watch_inventory=False)
    try:
        run = await svc.launch(SEL, wait=True)
        await svc.stop(run.id, wait=True)
        await svc.mlflow.poll()
        first = (await svc.get_run(run.id)).mlflow
        assert first is not None and first.status == "KILLED"
        await svc.restart(run.id)
        assert (await svc.get_run(run.id)).mlflow is None
        # a stale answer about the previous execution is ignored
        await svc.set_mlflow(run.id, first, started_at=run.started_at)
        assert (await svc.get_run(run.id)).mlflow is None
        await svc.mlflow.poll()
        second = (await svc.get_run(run.id)).mlflow
        assert second is not None and second.status == "RUNNING"
        assert second.run_id != first.run_id
    finally:
        await svc.close()
