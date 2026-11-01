"""Link with Lookin through the MLflow run id: Lookin's index (cached) and TrainHub's own."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session

from trainhub.api.app import create_app
from trainhub.config import Settings
from trainhub.core.inventory import InventoryError, parse_inventory
from trainhub.mcp.server import build_mcp
from trainhub.service.lookin import LookinLink, mlflow_run_links, new_workspace_url, parse_lookin

from .helpers import make_service
from .test_checkpoints import INF_INVENTORY

LOOKIN = "lookin:\n  url: http://lookin.internal:8560/\n  public_url: https://lookin.lab\n"


def lookin_answer(run_id: str = "mlrun-1") -> dict[str, Any]:
    return {
        "app": "lookin",
        "version": "5.4",
        "runs": {
            run_id: [
                {
                    "workspace_id": "lkw-1",
                    "workspace": "Cls v3",
                    "group": "Review",
                    "experiment": "resnet",
                    "artifact": "results.csv",
                    "path": "/?workspace=lkw-1",
                    "url": "http://127.0.0.1:8560/?workspace=lkw-1",
                },
                {"workspace_id": "lkw-x", "workspace": "Evil", "path": "//evil.example/x"},
                "not an entry",
            ],
            "other": "not a list",
        },
    }


class Lookin:
    """Fake Lookin server (httpx transport) counting its requests."""

    def __init__(self, answer: Any = None, status: int = 200) -> None:
        self.answer = lookin_answer() if answer is None else answer
        self.status = status
        self.calls: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handle(req: httpx.Request) -> httpx.Response:
            self.calls.append(str(req.url))
            if isinstance(self.answer, Exception):
                raise self.answer
            return httpx.Response(self.status, json=self.answer)

        return httpx.MockTransport(handle)


def test_inventory_lookin_section() -> None:
    inv = parse_inventory(INF_INVENTORY + LOOKIN, "t.yaml")
    assert inv.lookin is not None
    assert inv.lookin.url == "http://lookin.internal:8560"
    assert inv.lookin.browser_url == "https://lookin.lab"
    no_public = parse_inventory(INF_INVENTORY + "lookin: {url: 'http://lk:8560'}\n", "t.yaml")
    assert no_public.lookin is not None and no_public.lookin.browser_url == "http://lk:8560"
    with pytest.raises(InventoryError, match="http"):
        parse_inventory(INF_INVENTORY + "lookin: {url: 'lookin:8560'}\n", "t.yaml")


def test_parse_lookin_keeps_only_pages_of_lookin() -> None:
    runs = parse_lookin(lookin_answer(), "https://lookin.lab")
    assert list(runs) == ["mlrun-1"]
    [w] = runs["mlrun-1"]
    assert w.url == "https://lookin.lab/?workspace=lkw-1"
    assert (w.workspace, w.group, w.experiment) == ("Cls v3", "Review", "resnet")
    assert parse_lookin(["not", "a", "dict"], "https://x") == {}


def test_new_workspace_url() -> None:
    url = new_workspace_url("https://lookin.lab", "abc", "http://mlflow:5000")
    parts = urlsplit(url)
    assert parts.netloc == "lookin.lab" and parts.path == "/"
    assert parse_qs(parts.query) == {"mlflow_run": ["abc"], "mlflow_server": ["http://mlflow:5000"]}
    assert new_workspace_url("https://lookin.lab", "abc") == "https://lookin.lab/?mlflow_run=abc"


async def test_lookin_index_cached_and_errors_kept(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=INF_INVENTORY + LOOKIN)
    fake = Lookin()
    svc.lookin = LookinLink(svc, transport=fake.transport())
    idx = await svc.lookin.index()
    assert idx.enabled and idx.url == "https://lookin.lab" and idx.error is None
    assert idx.version == "5.4" and idx.fetched_at is not None
    assert [w.workspace for w in idx.runs["mlrun-1"]] == ["Cls v3"]
    assert fake.calls == ["http://lookin.internal:8560/api/links/mlflow-runs"]
    await svc.lookin.index()
    assert len(fake.calls) == 1  # cached
    # Lookin down: the last answer stays, with the error.
    fake.answer = httpx.ConnectError("refused")
    idx = await svc.lookin.index(refresh=True)
    assert len(fake.calls) == 2
    assert idx.error and "refused" in idx.error
    assert [w.workspace for w in idx.runs["mlrun-1"]] == ["Cls v3"]


@pytest.mark.parametrize(
    ("answer", "status", "message"),
    [
        ({"app": "mlflow"}, 200, "does not answer like a Lookin"),
        ({"detail": "Not Found"}, 404, "older than 5.4"),
        ({}, 500, "answered 500"),
    ],
)
async def test_lookin_index_wrong_server(
    tmp_path: Path, answer: dict[str, Any], status: int, message: str
) -> None:
    svc = make_service(tmp_path, text=INF_INVENTORY + LOOKIN)
    svc.lookin = LookinLink(svc, transport=Lookin(answer, status).transport())
    idx = await svc.lookin.index()
    assert idx.enabled and idx.runs == {}
    assert idx.error and message in idx.error


async def test_lookin_not_configured(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=INF_INVENTORY)
    idx = await svc.lookin.index()
    assert not idx.enabled and idx.url is None and idx.runs == {}
    assert svc.inventory_view().lookin_url is None


async def test_mlflow_run_links_checkpoints_and_inferences(tmp_path: Path) -> None:
    """TrainHub's own index: run directories (training) and inferences, by MLflow run id."""
    svc = make_service(tmp_path, text=INF_INVENTORY)
    await svc.start()
    try:
        lst = await svc.checkpoints.listing("seg")
        links = await mlflow_run_links(svc)
        assert links.app == "trainhub" and links.version == svc.version
        a = next(r for r in lst.runs if r.name == "run_a")
        inf = next(i for i in a.inferences if i.exists)
        [entry] = [x for x in links.runs[inf.mlflow.run_id] if x.kind == "inference"]
        assert entry.source == "seg" and inf.checkpoint in entry.label
        parts = urlsplit(entry.path)
        assert parts.path == "/checkpoints/seg"
        assert parse_qs(parts.query) == {"run": ["run_a"], "inference": [inf.mlflow.run_id]}
        training = [m for m in a.mlflow_runs if not m.deleted]
        assert training
        [entry] = [x for x in links.runs[training[0].run_id] if x.kind == "checkpoint_run"]
        assert entry.path == "/checkpoints/seg?run=run_a"
    finally:
        await svc.close()


def test_api_routes(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=INF_INVENTORY + LOOKIN)
    fake = Lookin()
    svc.lookin = LookinLink(svc, transport=fake.transport())
    with TestClient(create_app(Settings(), svc)) as client:
        assert client.get("/api/inventory").json()["lookin_url"] == "https://lookin.lab"
        idx = client.get("/api/lookin").json()
        assert idx["runs"]["mlrun-1"][0]["url"] == "https://lookin.lab/?workspace=lkw-1"
        client.get("/api/lookin?refresh=true")
        assert len(fake.calls) == 2
        links = client.get("/api/links/mlflow-runs").json()
        assert links["app"] == "trainhub"
        kinds = {x["kind"] for entries in links["runs"].values() for x in entries}
        assert {"inference", "checkpoint_run"} <= kinds


def test_links_route_needs_the_token(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=INF_INVENTORY)
    with TestClient(create_app(Settings(token="s3cret"), svc)) as client:
        assert client.get("/api/links/mlflow-runs").status_code == 401
        ok = client.get("/api/links/mlflow-runs", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200 and ok.json()["app"] == "trainhub"


async def test_mcp_get_lookin_workspaces(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=INF_INVENTORY + LOOKIN)
    svc.lookin = LookinLink(svc, transport=Lookin().transport())
    async with create_connected_server_and_client_session(build_mcp(svc)._mcp_server) as c:
        res = await c.call_tool("get_lookin_workspaces", {"mlflow_run_ids": ["mlrun-1", "none"]})
        assert not res.isError, res.content[0].text
        out = res.structuredContent or json.loads(res.content[0].text)  # type: ignore[union-attr]
        out = out.get("result", out)
        assert out["lookin"] == "https://lookin.lab"
        assert out["runs"]["mlrun-1"]["workspaces"][0]["workspace"] == "Cls v3"
        assert out["runs"]["none"]["workspaces"] == []
        assert out["runs"]["none"]["new_workspace_url"].startswith(
            "https://lookin.lab/?mlflow_run=none"
        )


async def test_mcp_get_lookin_workspaces_not_configured(tmp_path: Path) -> None:
    svc = make_service(tmp_path, text=INF_INVENTORY)
    async with create_connected_server_and_client_session(build_mcp(svc)._mcp_server) as c:
        res = await c.call_tool("get_lookin_workspaces", {"mlflow_run_ids": ["x"]})
        assert res.isError and "not configured" in res.content[0].text  # type: ignore[union-attr]
