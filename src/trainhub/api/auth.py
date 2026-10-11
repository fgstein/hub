"""Authentification optionnelle par jeton (``TRAINHUB_TOKEN``).

Si un jeton est défini, toutes les routes, le WebSocket et ``/mcp`` l'exigent : en-tête
``Authorization: Bearer <jeton>``, ou cookie ``trainhub_token`` posé par la page ``/login``.
"""

from __future__ import annotations

import hmac
import html
from urllib.parse import quote

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

COOKIE = "trainhub_token"
PUBLIC_PATHS = ("/login", "/favicon.svg", "/favicon.ico")

router = APIRouter()


def _check(expected: str | None, provided: str | None) -> bool:
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(expected.encode(), provided.encode())


def _token_from(headers: dict[str, str] | object, cookies: dict[str, str]) -> str | None:
    auth = getattr(headers, "get", lambda _k: None)("authorization")
    if auth and auth.lower().startswith("bearer "):
        return str(auth[7:].strip())
    return cookies.get(COOKIE)


def websocket_authorized(ws: WebSocket) -> bool:
    expected: str | None = ws.app.state.settings.token
    token = _token_from(ws.headers, dict(ws.cookies)) or ws.query_params.get("token")
    return _check(expected, token)


class TokenMiddleware:
    """Middleware ASGI : refuse les requêtes HTTP sans jeton valide.

    Le WebSocket est vérifié dans son handler (fermeture avec le code 4401).
    """

    def __init__(self, app: ASGIApp, token: str | None) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.token:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path
        if path.startswith(PUBLIC_PATHS) or _check(
            self.token, _token_from(request.headers, dict(request.cookies))
        ):
            await self.app(scope, receive, send)
            return
        response: Response
        if path.startswith(("/api", "/mcp")) or "text/html" not in request.headers.get(
            "accept", ""
        ):
            response = JSONResponse({"detail": "jeton requis"}, status_code=401)
        else:
            response = RedirectResponse(f"/login?next={quote(path)}", status_code=303)
        await response(scope, receive, send)


_LOGIN = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TrainHub</title>
<style>
:root{{color-scheme:dark light}}body{{margin:0;min-height:100vh;display:grid;place-items:center;
background:#0a0a0a;color:#eee;font:14px ui-monospace,SFMono-Regular,Menlo,monospace}}
@media (prefers-color-scheme:light){{body{{background:#fafafa;color:#111}}
input{{background:#fff!important;color:#111!important}}}}
form{{display:flex;flex-direction:column;gap:12px;width:280px}}
h1{{font-size:14px;font-weight:600;margin:0 0 8px}}
input{{padding:8px 10px;border:1px solid #333;border-radius:6px;background:#141414;color:#eee;
font:inherit}}button{{padding:8px;border:0;border-radius:6px;background:#eee;color:#111;
font:inherit;cursor:pointer}}.err{{color:#f87171}}</style></head><body>
<form method="post" action="/login"><h1>TrainHub</h1>{error}
<input type="password" name="token" placeholder="Jeton" autofocus required>
<input type="hidden" name="next" value="{next}"><button type="submit">Se connecter</button>
</form></body></html>"""


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(next: str = "/", error: int = 0) -> HTMLResponse:
    err = '<div class="err">Jeton invalide.</div>' if error else ""
    return HTMLResponse(_LOGIN.format(error=err, next=html.escape(next)))


@router.post("/login", include_in_schema=False)
async def login(request: Request, token: str = Form(...), next: str = Form("/")) -> Response:
    expected: str | None = request.app.state.settings.token
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    if not _check(expected, token):
        return RedirectResponse(f"/login?error=1&next={quote(target)}", status_code=303)
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=30 * 86400)
    return resp
