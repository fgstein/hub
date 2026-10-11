"""REST API and WebSocket (FastAPI test client) with the fake executor."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from trainhub.api.app import create_app
from trainhub.api.ws import parse_log_frame
from trainhub.config import Settings

from .helpers import SEL, make_service


def _client(tmp_path: Path, **settings: Any) -> TestClient:
    svc = make_service(tmp_path)
    app = create_app(Settings(**settings), svc)
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    with _client(tmp_path) as c:
        yield c


def wait_status(client: TestClient, run_id: str, *statuses: str, timeout: float = 10) -> dict:  # type: ignore[type-arg]
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in statuses:
            return run  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError(f"status {statuses} not reached: {run['status']}")


def recv_until(ws: WebSocketTestSession, pred: Any, limit: int = 2000) -> list[Any]:
    """Read messages (JSON or binary) until ``pred`` is true."""
    seen = []
    for _ in range(limit):
        msg = ws.receive()
        if msg.get("bytes") is not None:
            item: Any = parse_log_frame(msg["bytes"])
        else:
            import json

            item = json.loads(msg["text"])
        seen.append(item)
        if pred(item):
            return seen
    raise AssertionError("expected message not received")


def test_inventory_health_openapi(client: TestClient) -> None:
    inv = client.get("/api/inventory").json()
    assert [p["id"] for p in inv["projects"]] == ["segmentation", "detection"]
    assert inv["executor"] == "fake"
    seg = inv["projects"][0]
    assert seg["venv_images"] == {"main": None, "exp": ["torch-nightly"], "system": None}
    assert seg["params"] == []
    health = client.get("/api/health").json()
    assert health["ok"] and health["executor"] == "fake"
    schema = client.get("/openapi.json").json()
    assert "/api/runs/{run_id}/{action}" in schema["paths"]


def test_configs_and_preview(client: TestClient) -> None:
    r = client.get("/api/projects/detection/configs", params={"machine": "gpu-01"})
    assert r.json()["configs"] == ["exp/sweep/yolo_l", "yolo_m", "yolo_s"]
    p = client.post("/api/preview", json=SEL.model_dump()).json()
    assert p["argv"][0] == "ssh" and "docker run -d -t --init" in p["text"]
    bad = client.post("/api/preview", json={**SEL.model_dump(), "venv": "exp"})
    assert bad.status_code == 422 and "available: main" in bad.json()["detail"]
    missing = client.post("/api/preview", json={"project": "segmentation"})
    assert missing.status_code == 422 and "machine" in missing.json()["detail"]


def test_project_command_patch(client: TestClient) -> None:
    r = client.patch("/api/projects/segmentation", json={"command": "python t.py {config}"})
    assert r.status_code == 200 and r.json()["command_overridden"]
    bad = client.patch("/api/projects/segmentation", json={"command": "python t.py"})
    assert bad.status_code == 422 and "{config}" in bad.json()["detail"]
    reset = client.patch("/api/projects/segmentation", json={"reset": True}).json()
    assert reset["command"] == reset["yaml_command"]
    assert client.patch("/api/projects/nope", json={"reset": True}).status_code == 404


def test_ui_state_roundtrip(client: TestClient) -> None:
    state = client.get("/api/ui-state").json()
    assert state["open_tabs"] == ["segmentation", "detection"]
    state["open_tabs"] = ["detection", "segmentation"]
    state["active_tab"] = "segmentation"
    saved = client.put("/api/ui-state", json=state).json()
    assert saved["open_tabs"] == ["detection", "segmentation"]
    assert client.get("/api/ui-state").json()["active_tab"] == "segmentation"


def test_scenario_launch_logs_pause_resume_stop(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and snap["runs"] == []
        r = client.post("/api/runs", json=SEL.model_dump())
        assert r.status_code == 201
        run = r.json()
        assert run["status"] == "launching" and run["source"] == "ui"
        rid = run["id"]
        recv_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "run.created")
        recv_until(
            ws,
            lambda m: (
                isinstance(m, dict)
                and m.get("type") == "run.updated"
                and m["run"]["status"] == "running"
            ),
        )
        ws.send_json({"type": "logs.subscribe", "run_id": rid})
        frames = recv_until(ws, lambda m: isinstance(m, tuple) and b"Epoch" in m[1])
        logs = b"".join(m[1] for m in frames if isinstance(m, tuple) and m[0] == rid)
        assert b"[HYDRA]" in logs

        paused = client.post(f"/api/runs/{rid}/pause")
        assert paused.status_code == 200 and paused.json()["status"] == "paused"
        again = client.post(f"/api/runs/{rid}/pause")
        assert again.status_code == 409 and "status 'paused'" in again.json()["detail"]
        assert client.post(f"/api/runs/{rid}/resume").json()["status"] == "running"
        stop = client.post(f"/api/runs/{rid}/stop").json()
        assert stop["status"] == "stopping"
        recv_until(
            ws,
            lambda m: (
                isinstance(m, dict)
                and m.get("type") == "run.updated"
                and m["run"]["status"] == "stopped"
            ),
        )
        ws.send_json({"type": "logs.unsubscribe", "run_id": rid})
        ws.send_json({"type": "ping", "t": 1})
        recv_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "pong")

    final = client.get(f"/api/runs/{rid}").json()
    assert final["exit_code"] == 143 and final["active"] is False
    dl = client.get(f"/api/runs/{rid}/logs")
    assert dl.status_code == 200 and b"SIGTERM" in dl.content
    assert "attachment" in dl.headers["content-disposition"]
    listed = client.get("/api/runs", params={"status": "finished"}).json()
    assert [x["id"] for x in listed] == [rid]
    assert client.get("/api/runs", params={"status": "nope"}).status_code == 422
    relaunched = client.post(f"/api/runs/{rid}/relaunch").json()
    assert relaunched["id"] != rid
    assert client.delete(f"/api/runs/{rid}").status_code == 204
    assert client.get(f"/api/runs/{rid}").status_code == 404
    assert client.post(f"/api/runs/{relaunched['id']}/explode").status_code == 422


def test_ws_snapshot_on_request(client: TestClient) -> None:
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json({"type": "snapshot.request"})
        snap = recv_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "snapshot")[-1]
        assert "inventory" in snap and "seq" in snap
        ws.send_json({"type": "logs.subscribe", "run_id": "nope"})
        err = recv_until(ws, lambda m: isinstance(m, dict) and m.get("type") == "error")[-1]
        assert "not found" in err["message"]


def test_token_auth(tmp_path: Path) -> None:
    with _client(tmp_path, token="s3cret") as c:
        assert c.get("/api/inventory").status_code == 401
        ok = c.get("/api/inventory", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert c.post("/mcp", json={}).status_code == 401
        page = c.get("/", headers={"accept": "text/html"}, follow_redirects=False)
        assert page.status_code == 303 and page.headers["location"].startswith("/login")
        assert "Token" in c.get("/login").text
        bad = c.post("/login", data={"token": "x", "next": "/"}, follow_redirects=False)
        assert "error=1" in bad.headers["location"]
        good = c.post("/login", data={"token": "s3cret", "next": "/p/x"}, follow_redirects=False)
        assert good.headers["location"] == "/p/x"
        assert c.get("/api/inventory").status_code == 200  # cookie set
        c.cookies.clear()
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect), c.websocket_connect("/ws") as ws:
            ws.receive_json()
        with c.websocket_connect("/ws?token=s3cret") as ws:
            assert ws.receive_json()["type"] == "snapshot"


def test_shell_websocket(client: TestClient) -> None:
    rid = client.post("/api/runs", json=SEL.model_dump()).json()["id"]
    wait_status(client, rid, "running")
    with client.websocket_connect(f"/ws/shell/{rid}?cols=100&rows=30") as ws:
        assert b"simul" in ws.receive_bytes()
        ws.send_bytes(b"echo bonjour\r")
        out = b""
        while b"bonjour\r\n" not in out:
            out += ws.receive_bytes()
        ws.send_text('{"type": "resize", "cols": 80, "rows": 20}')
        ws.send_bytes(b"exit\r")
        msgs = []
        while True:
            msg = ws.receive()
            if msg.get("text"):
                msgs.append(msg["text"])
                break
        assert '"exit"' in msgs[0]


def test_shell_websocket_refused_for_unknown_run(client: TestClient) -> None:
    with client.websocket_connect("/ws/shell/nope") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error" and "not found" in msg["message"]
