"""FastAPI application: REST API, WebSocket, MCP server (``/mcp``) and built front end.

A single process serves everything; the business logic lives in the service layer.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.staticfiles import StaticFiles

from .. import __version__
from ..config import Settings
from ..logging_setup import setup_logging
from ..service import RunService, ServiceError, create_service
from . import auth, routes, ws

log = logging.getLogger("trainhub.api")

_RESERVED_PREFIXES = ("/api", "/ws", "/mcp", "/login", "/docs", "/openapi.json", "/redoc")


def find_static_dir(settings: Settings) -> Path | None:
    """Directory of the built front end: ``TRAINHUB_STATIC_DIR``, package, or ``web/dist``."""
    candidates = [
        settings.static_dir,
        Path(__file__).resolve().parent.parent / "static",
        Path(__file__).resolve().parents[3] / "web" / "dist",
    ]
    for c in candidates:
        if c is not None and (c / "index.html").is_file():
            return c
    return None


def _format_validation(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path"))
        parts.append(f"{loc or 'request'}: {err.get('msg', 'invalid')}")
    return "invalid request — " + "; ".join(parts)


def create_app(
    settings: Settings | None = None,
    service: RunService | None = None,
    *,
    start_monitor: bool = True,
    enable_mcp: bool = True,
) -> FastAPI:
    """Build the application (service given, or created from the settings)."""
    settings = settings or Settings.from_env()
    svc = service or create_service(settings, version=__version__)
    mcp_app: Any = None
    mcp_server: Any = None
    if enable_mcp:
        from ..mcp.server import build_mcp

        mcp_server = build_mcp(svc, readonly=settings.mcp_readonly)
        mcp_app = mcp_server.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await svc.start(monitor=start_monitor)
        log.info(
            "TrainHub started",
            extra={
                "executor": svc.executor.kind,
                "inventory": str(settings.inventory),
                "machines": len(svc.inventory.machines),
                "projects": len(svc.inventory.projects),
            },
        )
        async with contextlib.AsyncExitStack() as stack:
            if mcp_server is not None:
                await stack.enter_async_context(mcp_server.session_manager.run())
            try:
                yield
            finally:
                await svc.close()

    app = FastAPI(
        title="TrainHub",
        version=__version__,
        description="Launch and monitor training commands on remote machines.",
        lifespan=lifespan,
    )
    app.state.service = svc
    app.state.settings = settings

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": _format_validation(exc)}, status_code=422)

    app.include_router(auth.router)
    app.include_router(routes.router)
    app.include_router(ws.router)
    if mcp_app is not None:
        # The MCP application exposes its route on “/mcp”: mount it at the root.
        for route in mcp_app.routes:
            app.router.routes.append(route)

    static = find_static_dir(settings)
    if static is not None:
        assets = static / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> Response:
            if ("/" + path).startswith(_RESERVED_PREFIXES):
                return JSONResponse({"detail": "not found"}, status_code=404)
            candidate = (static / path).resolve()
            if path and candidate.is_file() and static.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})
    else:

        @app.get("/", include_in_schema=False)
        async def no_front() -> JSONResponse:
            return JSONResponse(
                {
                    "detail": "front end not built (make build); API available at /api, "
                    "documentation at /docs"
                }
            )

    app.add_middleware(auth.TokenMiddleware, token=settings.token)
    return app


def app_factory() -> FastAPI:
    """Factory used by uvicorn (``--factory``)."""
    settings = Settings.from_env()
    setup_logging(settings.log_level)
    return create_app(settings)


def serve(host: str | None = None, port: int | None = None, *, reload: bool = False) -> None:
    """Start uvicorn with the environment settings."""
    import uvicorn

    settings = Settings.from_env()
    setup_logging(settings.log_level)
    uvicorn.run(
        "trainhub.api.app:app_factory",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        reload_dirs=[str(Path(__file__).resolve().parents[1])] if reload else None,
        log_config=None,
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )
