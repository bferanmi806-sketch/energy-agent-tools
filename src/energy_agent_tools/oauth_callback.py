"""Loopback callback for an operator-started OAuth authorization transaction."""

from __future__ import annotations

import hmac

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .auth import AuthorizationRequest, AuthStore
from .models import EnergyError


def callback_app(
    store: AuthStore, user_id: str, authorization: AuthorizationRequest, redirect_uri: str
) -> Starlette:
    from urllib.parse import urlparse

    parsed = urlparse(redirect_uri)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("CLI callback requires an exact http://127.0.0.1:PORT/path redirect URI")

    async def callback(request: Request) -> JSONResponse:
        states = request.query_params.getlist("state")
        codes = request.query_params.getlist("code")
        if (
            len(states) != 1
            or len(codes) != 1
            or not hmac.compare_digest(states[0], authorization.state)
        ):
            return JSONResponse({"ok": False, "error": "invalid_oauth_callback"}, status_code=400)
        try:
            account = await store.complete_oauth(user_id, states[0], codes[0], redirect_uri)
            return JSONResponse(
                {
                    "ok": True,
                    "connection": account.public(),
                    "message": "Authorization completed. You can close this window.",
                }
            )
        except EnergyError as exc:
            return JSONResponse({"ok": False, "error": exc.code}, status_code=400)

    return Starlette(routes=[Route(parsed.path or "/", callback, methods=["GET"])])
