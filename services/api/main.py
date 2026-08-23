"""FastAPI adapter around the Creator Console.

WHY A SEPARATE LAYER
--------------------
`creator_console.api.ConsoleApi` is transport-agnostic and returns
`(status, payload)` tuples, so it can be driven by the built-in threaded HTTP
server, by tests, or by this FastAPI wrapper without duplicating any route
logic.

This module only:
  - builds a `ConsoleApp` from a configuration,
  - forwards HTTP requests to `ConsoleApi.handle`,
  - converts the returned status/payload to FastAPI `Response` objects.

No route logic lives here; no secret is read directly.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from creator_console.app import ConsoleApp, ConsoleConfig
from creator_console.api import ConsoleApi


def _config_from_env():
    import os

    workspace = os.environ.get("CONSOLE_WORKSPACE", "wsp_default")
    user = os.environ.get("CONSOLE_USER", "usr_api")
    root = os.environ.get("CONSOLE_ROOT", "./data/console")
    return {
        "workspace_id": workspace,
        "user_id": user,
        "storage_root": root,
        "max_upload_bytes": 4 * 1024 * 1024 * 1024,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    """One `ConsoleApp` per process."""
    app.state.console = ConsoleApp(
        ConsoleConfig(
            storage_root=app.state.console_config["storage_root"],
            user_id=app.state.console_config["user_id"],
            workspace_id=app.state.console_config["workspace_id"],
            max_upload_bytes=app.state.console_config["max_upload_bytes"],
            extra={"skip_startup_recovery": True},
        ),
    )
    app.state.console_api = ConsoleApi(app.state.console)
    yield
    app.state.console = None
    app.state.console_api = None


app = FastAPI(title="YouTube Otomation API", lifespan=lifespan)
app.state.console_config = _config_from_env()


@app.get("/health")
async def health():
    return {"ok": True, "service": "api"}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def catch_all(request: Request, path: str):
    """Forward all traffic to the canonical ConsoleApi."""
    api = request.app.state.console_api
    body = await request.body()
    raw_path = f"/{path}"
    query = str(request.url.query)
    if query:
        raw_path += f"?{query}"
    status, payload = api.handle(
        request.method,
        raw_path,
        body=body,
        headers=dict(request.headers),
    )

    if isinstance(payload, dict) and "__raw__" in payload:
        return Response(
            content=payload["__raw__"],
            status_code=status,
            media_type=payload.get("__content_type__", "application/octet-stream"),
            headers={
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            },
        )

    if isinstance(payload, dict) and "__file__" in payload:
        return FileResponse(
            path=payload["__file__"],
            status_code=status,
            media_type=payload.get("__content_type__", "application/octet-stream"),
            headers={
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
                "Accept-Ranges": "bytes",
            },
        )

    encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    return JSONResponse(
        content=json.loads(encoded.decode("utf-8")),
        status_code=status,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        },
    )
