"""``trainhub mcp`` (stdio) relayé vers un serveur TrainHub réel (uvicorn, mode fake)."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from trainhub.api.app import create_app
from trainhub.config import Settings

from .helpers import make_service


@pytest.fixture
def server_url(tmp_path: Path) -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(Settings(port=port), make_service(tmp_path))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


async def test_stdio_proxy(server_url: str) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "trainhub.cli", "mcp", "--url", server_url],
        env={**os.environ, "TRAINHUB_LOG_LEVEL": "ERROR"},
    )
    async with stdio_client(params) as (r, w), ClientSession(r, w) as c:
        init = await c.initialize()
        assert init.instructions and "TrainHub" in init.instructions
        tools = {t.name for t in (await c.list_tools()).tools}
        assert "launch_run" in tools
        res = await c.call_tool(
            "launch_run",
            {
                "project": "segmentation",
                "machine": "gpu-01",
                "image": "torch-2.4",
                "venv": "main",
                "config": "baseline",
            },
        )
        assert not res.isError, res.content
        assert "running" in res.content[0].text
