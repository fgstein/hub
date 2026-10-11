"""Transport stdio : ``trainhub mcp``.

Ce processus ne crée pas de seconde instance de TrainHub avec son propre état : il relaie
les requêtes MCP (liste et appel des outils) vers le serveur en cours d'exécution, via son
point d'accès Streamable HTTP ``/mcp``.
"""

from __future__ import annotations

import os
from typing import Any

import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from ..config import Settings


def default_url() -> str:
    return Settings.from_env().base_url + "/mcp"


async def run_stdio_proxy(url: str | None = None, *, token: str | None = None) -> None:
    """Sert MCP sur stdin/stdout en relayant vers ``url`` (``http://…/mcp``)."""
    url = url or default_url()
    if not url.rstrip("/").endswith("/mcp"):
        url = url.rstrip("/") + "/mcp"
    token = token or os.environ.get("TRAINHUB_TOKEN") or None
    headers = {"Authorization": f"Bearer {token}"} if token else None

    async with (
        streamablehttp_client(url, headers=headers) as (read, write, _),
        ClientSession(read, write) as upstream,
    ):
        init = await upstream.initialize()
        server: Server[Any, Any] = Server(
            "trainhub", version="0.1.0", instructions=init.instructions
        )

        @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
        async def list_tools() -> list[types.Tool]:
            return (await upstream.list_tools()).tools

        @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
        async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            return await upstream.call_tool(name, arguments)

        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())
