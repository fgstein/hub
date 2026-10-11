"""Serveur MCP (client MCP en mémoire) avec l'exécuteur fake."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from trainhub.mcp.server import READ_TOOLS, build_mcp
from trainhub.service import RunService

from .helpers import make_service


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncIterator[RunService]:
    s = make_service(tmp_path)
    await s.start()
    yield s
    await s.close()


def payload(result: Any) -> Any:
    assert not result.isError, result.content[0].text
    if result.structuredContent is not None:
        sc = result.structuredContent
        return sc.get("result", sc)
    return result.content[0].text


SEL = {
    "project": "segmentation",
    "machine": "gpu-01",
    "image": "torch-2.4",
    "venv": "main",
    "config": "baseline",
}


async def test_tools_and_annotations(svc: RunService) -> None:
    async with create_connected_server_and_client_session(build_mcp(svc)._mcp_server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert set(READ_TOOLS) <= set(tools)
        assert {"launch_run", "stop_run", "delete_run", "set_project_command"} <= set(tools)
        assert tools["get_logs"].annotations.readOnlyHint is True
        assert tools["stop_run"].annotations.destructiveHint is True
        assert tools["delete_run"].annotations.destructiveHint is True
        assert tools["pause_run"].annotations.destructiveHint is False
        assert all(t.description and len(t.description) > 40 for t in tools.values())


async def test_readonly(svc: RunService) -> None:
    server = build_mcp(svc, readonly=True)._mcp_server
    async with create_connected_server_and_client_session(server) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert names == set(READ_TOOLS)


async def _call(c: ClientSession, name: str, **args: Any) -> Any:
    return payload(await c.call_tool(name, args))


async def test_scenario(svc: RunService) -> None:
    events = svc.bus.subscribe()
    async with create_connected_server_and_client_session(build_mcp(svc)._mcp_server) as c:
        projects = await _call(c, "list_projects")
        assert projects[0]["id"] == "segmentation"
        res = await _call(c, "list_resources", project="segmentation")
        assert [m["id"] for m in res["machines"]] == ["gpu-01", "gpu-02"]
        cfg = await _call(c, "list_configs", project="detection", machine="gpu-02")
        assert cfg["configs"] == ["yolo_m", "yolo_s"]
        prev = await _call(c, "preview_command", **SEL)
        assert "docker run" in prev["full_command"]

        run = await _call(c, "launch_run", **SEL)
        assert run["status"] == "running" and run["source"] == "mcp"
        rid = run["id"]
        await asyncio.sleep(0.5)
        logs = await _call(c, "get_logs", run_id=rid, tail=20)
        assert logs.startswith(f"[run {rid} · running")
        assert "\x1b" not in logs and "\r" not in logs
        epochs = await _call(c, "get_logs", run_id=rid, grep="epoch=")
        assert all("epoch=" in ln for ln in epochs.splitlines()[1:])

        assert (await _call(c, "pause_run", run_id=rid))["status"] == "paused"
        assert (await _call(c, "resume_run", run_id=rid))["status"] == "running"
        stopped = await _call(c, "stop_run", run_id=rid)
        assert stopped["status"] == "stopped" and stopped["exit_code"] == 143
        detail = await _call(c, "get_run", run_id=rid)
        assert detail["container_name"] == f"th-segmentation-{rid}"
        assert "restart" in detail["allowed_actions"]
        runs = await _call(c, "list_runs", status="finished")
        assert [r["id"] for r in runs] == [rid]
        new = await _call(c, "relaunch_run", run_id=rid)
        assert new["id"] != rid
        assert (await _call(c, "delete_run", run_id=rid)) == {"deleted": rid}
        cmd = await _call(
            c, "set_project_command", project="segmentation", command="python x.py {config}"
        )
        assert cmd["command_overridden"]

    types = []
    while not events.queue.empty():
        ev = events.queue.get_nowait()
        assert ev is not None
        types.append(ev.type)
    assert "run.created" in types and "run.deleted" in types


async def test_actionable_errors(svc: RunService) -> None:
    async with create_connected_server_and_client_session(build_mcp(svc)._mcp_server) as c:
        bad = await c.call_tool("launch_run", {**SEL, "config": "foo"})
        assert bad.isError
        text = bad.content[0].text
        assert "config 'foo' inconnue" in text and "baseline, unet_large" in text
        missing = await c.call_tool("get_run", {"run_id": "rzzzzz"})
        assert missing.isError and "introuvable" in missing.content[0].text
        st = await c.call_tool("list_runs", {"status": "bogus"})
        assert st.isError and "possibles" in st.content[0].text


def test_http_transport(tmp_path: Path) -> None:
    """Streamable HTTP monté sur /mcp dans l'application principale."""
    from fastapi.testclient import TestClient

    from trainhub.api.app import create_app
    from trainhub.config import Settings

    app = create_app(Settings(), make_service(tmp_path))
    with TestClient(app) as client:
        headers = {"accept": "application/json, text/event-stream"}
        init = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
        assert init.status_code == 200, init.text
        sid = init.headers.get("mcp-session-id")
        h2 = {**headers, "mcp-session-id": sid} if sid else headers
        client.post(
            "/mcp", headers=h2, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        r = client.post(
            "/mcp",
            headers=h2,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "list_projects", "arguments": {}}},
        )  # fmt: skip
        body = r.text
        data = json.loads(body.split("data: ", 1)[1]) if "data: " in body else r.json()
        assert data["result"]["isError"] is False
        assert "segmentation" in json.dumps(data["result"])
