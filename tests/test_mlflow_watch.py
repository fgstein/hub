"""MLflow watch: one search per round, external runs, stalled runs, failures to be seen."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from trainhub.core.models import MlflowConfig
from trainhub.core.status import utcnow
from trainhub.service.mlflow import HttpMlflow, read_info
from trainhub.service.schemas import RunView

from .helpers import SEL, make_service, sim_inventory_text, wait_for

MLFLOW = """
mlflow:
  tracking_uri: http://mlflow.lab:5000
  max_epochs_param: trainer.max_epochs
"""
#: The segmentation project names its experiment: its external runs are shown there.
CLAIM = "    mlflow: {experiments: [seg-exp]}\n"
CFG = MlflowConfig(tracking_uri="http://ml", stale_after_min=30)


def _claiming(text: str) -> str:
    anchor = "    configs:\n      static: [baseline, unet_large]\n"
    return text.replace(anchor, anchor + CLAIM, 1)


def _ms(minutes_ago: float) -> int:
    return int((utcnow() - timedelta(minutes=minutes_ago)).timestamp() * 1000)


def test_read_info_times_and_stale() -> None:
    raw = {
        "info": {"run_id": "a", "experiment_id": "1", "status": "RUNNING"},
        "data": {"metrics": [{"key": "epoch", "value": 2, "timestamp": _ms(45)}]},
    }
    raw["info"]["start_time"] = _ms(60)
    info = read_info(CFG, raw)
    assert info.started_at is not None and info.last_activity is not None
    assert info.stale  # no metric for 45 min
    raw["data"]["metrics"].append({"key": "loss", "value": 0.1, "timestamp": _ms(1)})
    assert not read_info(CFG, raw).stale
    # without any metric, the start counts as the last sign of life
    fresh = {"info": {"run_id": "b", "status": "RUNNING", "start_time": _ms(5)}}
    assert not read_info(CFG, fresh).stale
    # only a running run can be stalled
    raw["data"]["metrics"].pop()
    raw["info"].update(status="FINISHED", end_time=_ms(40))
    done = read_info(CFG, raw)
    assert not done.stale and done.finished_at is not None


async def test_one_search_per_round() -> None:
    """The TrainHub runs read their state from the search of the running runs: two
    requests per round whatever the number of runs."""
    calls: list[str] = []
    svc = make_service(text=sim_inventory_text(duration=600) + MLFLOW)
    await svc.start(monitor=True, watch_inventory=False)
    runs = [await svc.launch(SEL, wait=True) for _ in range(3)]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/api/2.0/mlflow/")
        calls.append(path)
        if path == "experiments/search":
            return httpx.Response(200, json={"experiments": [{"experiment_id": "1", "name": "x"}]})
        body = json.loads(req.content)
        if path == "runs/search" and body["filter"] == "attributes.status = 'RUNNING'":
            return httpx.Response(
                200,
                json={
                    "runs": [
                        {
                            "info": {
                                "run_id": f"m{i}",
                                "experiment_id": "1",
                                "status": "RUNNING",
                                "start_time": _ms(0),
                            },
                            "data": {
                                "metrics": [{"key": "epoch", "value": i, "timestamp": _ms(0)}],
                                "tags": [{"key": "trainhub.run_id", "value": r.id}],
                            },
                        }
                        for i, r in enumerate(runs)
                    ]
                },
            )
        if path == "runs/search":
            return httpx.Response(200, json={})
        return httpx.Response(500, json={"message": f"unexpected {path}"})

    svc.mlflow.source = HttpMlflow(transport=httpx.MockTransport(handler))
    try:
        await svc.mlflow.poll()
        # start-up: running runs, then the failures since the oldest running one
        assert calls == ["experiments/search", "runs/search", "runs/search"]
        for i, r in enumerate(runs):
            ml = (await svc.get_run(r.id)).mlflow
            assert ml is not None and ml.run_id == f"m{i}" and ml.epoch == i
        calls.clear()
        await svc.mlflow.poll()
        # experiments cached; running runs + runs started since the previous round
        assert calls == ["runs/search", "runs/search"]
    finally:
        await svc.close()


async def test_external_runs(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=_claiming(sim_inventory_text(duration=600) + MLFLOW))
    await svc.start(monitor=True, watch_inventory=False)
    try:
        own = await svc.launch(SEL, wait=True)
        await svc.mlflow.poll()
        ext = {e.name.split("-")[0]: e for e in await svc.list_external_runs() if e.name}
        assert set(ext) == {"notebook", "sweep", "debug"}
        assert all(e.project_id == "segmentation" for e in ext.values())
        assert ext["notebook"].is_active and not ext["notebook"].mlflow.stale
        assert ext["notebook"].user == "bob" and ext["notebook"].experiment == "seg-exp"
        assert ext["sweep"].mlflow.stale  # no metric for 45 min
        # a failure found at start-up (since the oldest running run) is to be seen
        assert ext["debug"].is_failure and ext["debug"].unseen_failure
        # the TrainHub run is not an external run
        assert own.id not in {e.id for e in ext.values()}
        assert (await svc.get_run(own.id)).mlflow is not None

        snap = await svc.snapshot()
        assert {e.id for e in snap.external_runs} == {e.id for e in ext.values()}

        seen = await svc.mark_external_seen(ext["debug"].id)
        assert not seen.unseen_failure
        await svc.mlflow.poll()
        assert not (await svc.get_external_run(ext["debug"].id)).unseen_failure

        # removed from TrainHub: it does not come back, MLflow is not touched
        await svc.delete_external(ext["notebook"].id)
        await svc.mlflow.poll()
        assert ext["notebook"].id not in {e.id for e in await svc.list_external_runs()}

        # a running external run that ends: read once, kept, failure to be seen
        source = svc.mlflow.source
        source.deleted.add(ext["sweep"].id)  # type: ignore[attr-defined]
        await svc.mlflow.poll()
        assert ext["sweep"].id not in {e.id for e in await svc.list_external_runs()}
    finally:
        await svc.close()


async def test_no_claim_no_external_run(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=sim_inventory_text(duration=600) + MLFLOW)
    await svc.start(monitor=True, watch_inventory=False)
    try:
        await svc.launch(SEL, wait=True)
        await svc.mlflow.poll()
        assert await svc.list_external_runs() == []
    finally:
        await svc.close()


async def test_failure_stays_until_seen(tmp_path: Path) -> None:
    svc = make_service(tmp_path, duration=1, scale=4, failure=1.0)
    await svc.start(monitor=True, watch_inventory=False)
    try:
        run = await svc.launch(SEL, wait=True)

        async def failed() -> bool:
            return (await svc.get_run(run.id)).status == "failed"

        await wait_for(failed, timeout=20)
        r = await svc.get_run(run.id)
        assert r.unseen_failure and RunView.of(r).failed
        r = await svc.mark_seen(run.id)
        assert not r.unseen_failure
        # a new failure after a restart is to be seen again
        await svc.restart(run.id)
        assert not (await svc.get_run(run.id)).unseen_failure
        await wait_for(failed, timeout=20)
        assert (await svc.get_run(run.id)).unseen_failure
    finally:
        await svc.close()


async def test_stopped_or_finished_by_hand_is_not_a_failure(tmp_path: Path) -> None:
    svc = make_service(tmp_path, duration=600)
    await svc.start(monitor=True, watch_inventory=False)
    try:
        run = await svc.launch(SEL, wait=True)
        stopped = await svc.stop(run.id, wait=True)
        assert stopped.status == "stopped" and not stopped.unseen_failure
        other = await svc.launch(SEL, wait=True)
        done = await svc.force_finish(other.id)
        assert done.status == "failed" and not done.unseen_failure
    finally:
        await svc.close()


async def test_http_search_runs_pages() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        if body.get("page_token") is None:
            return httpx.Response(
                200, json={"runs": [{"info": {"run_id": "a"}}], "next_page_token": "p"}
            )
        return httpx.Response(200, json={"runs": [{"info": {"run_id": "b"}}]})

    client = HttpMlflow(transport=httpx.MockTransport(handler))
    try:
        got = await client.search_runs(CFG, ["1", "2"], "attributes.status = 'RUNNING'")
        assert [r["info"]["run_id"] for r in got] == ["a", "b"]
        assert seen[0]["experiment_ids"] == ["1", "2"] and seen[1]["page_token"] == "p"
    finally:
        await client.close()


def test_api_external_runs(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from trainhub.api.app import create_app
    from trainhub.config import Settings

    svc = make_service(tmp_path, text=_claiming(sim_inventory_text(duration=600) + MLFLOW))
    with TestClient(create_app(Settings(), svc)) as client:
        client.portal.call(svc.mlflow.poll)  # type: ignore[union-attr]
        runs = client.get("/api/external-runs", params={"project": "segmentation"}).json()
        assert len(runs) == 3 and all("active" in r and "failed" in r for r in runs)
        failed = next(r for r in runs if r["failed"])
        assert failed["unseen_failure"]
        res = client.post(f"/api/external-runs/{failed['id']}/seen")
        assert res.status_code == 200 and not res.json()["unseen_failure"]
        assert client.delete(f"/api/external-runs/{failed['id']}").status_code == 204
        assert client.post(f"/api/external-runs/{failed['id']}/seen").status_code == 404
        assert len(client.get("/api/external-runs").json()) == 2
        assert client.post("/api/runs/nope/seen").status_code == 404


async def test_mcp_external_runs(tmp_path: Path) -> None:
    from mcp.shared.memory import create_connected_server_and_client_session

    from trainhub.mcp.server import build_mcp

    svc = make_service(tmp_path, text=_claiming(sim_inventory_text(duration=600) + MLFLOW))
    await svc.start(monitor=True, watch_inventory=False)
    try:
        await svc.mlflow.poll()
        async with create_connected_server_and_client_session(build_mcp(svc)._mcp_server) as c:
            res = await c.call_tool("list_external_runs", {"project": "segmentation"})
            runs = (res.structuredContent or {})["result"]
            assert {r["user"] for r in runs} == {"bob", "carol"}
            failed = next(r for r in runs if r["failed"])
            res = await c.call_tool("mark_run_seen", {"run_id": failed["id"]})
            assert not res.isError and not (res.structuredContent or {})["unseen_failure"]
            res = await c.call_tool("delete_external_run", {"run_id": failed["id"]})
            assert not res.isError
            res = await c.call_tool("mark_run_seen", {"run_id": failed["id"]})
            assert res.isError
    finally:
        await svc.close()


async def test_stalled_external_run_seen(tmp_path: Path) -> None:
    """A stalled external run (often a killed process MLflow was never told about) can be
    marked as seen: finished runs until it logs again."""
    from trainhub.service.mlflow import _Claim, _external

    svc = make_service(tmp_path, text=_claiming(sim_inventory_text(duration=600) + MLFLOW))
    await svc.start(monitor=True, watch_inventory=False)
    try:
        await svc.mlflow.poll()
        ext = {e.name.split("-")[0]: e for e in await svc.list_external_runs() if e.name}
        # running and logging: nothing to see
        assert (await svc.mark_external_seen(ext["notebook"].id)).stale_seen_at is None
        seen = await svc.mark_external_seen(ext["sweep"].id)
        assert seen.stale_seen_at is not None and seen.is_active
        await svc.mlflow.poll()  # still stalled: stays seen
        assert (await svc.get_external_run(ext["sweep"].id)).stale_seen_at is not None
        # a running run can be removed from TrainHub too
        await svc.delete_external(ext["sweep"].id)
        await svc.mlflow.poll()
        assert ext["sweep"].id not in {e.id for e in await svc.list_external_runs()}
    finally:
        await svc.close()

    # logs again: back among the active runs
    claim = _Claim("segmentation", CFG)
    raw = {
        "info": {"run_id": "a", "experiment_id": "1", "status": "RUNNING", "start_time": _ms(90)},
        "data": {"metrics": [{"key": "epoch", "value": 2, "timestamp": _ms(60)}]},
    }
    stalled = _external(raw, claim, {}, utcnow(), None)
    assert stalled.mlflow.stale
    seen_run = stalled.model_copy(update={"stale_seen_at": utcnow()})
    assert _external(raw, claim, {}, utcnow(), seen_run).stale_seen_at is not None
    raw["data"]["metrics"][0]["timestamp"] = _ms(0)
    assert _external(raw, claim, {}, utcnow(), seen_run).stale_seen_at is None
