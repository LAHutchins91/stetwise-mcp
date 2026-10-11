"""Container HTTP API. Callers sign the raw body with ENGINE_HMAC_SECRET."""

from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, Request, Response

from .engine import dispatch

app = FastAPI(title="Stetwise engine", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "stetwise-engine", "version": "0.1.0"}


@app.post("/v1/dispatch")
async def post_dispatch(request: Request) -> Response:
    body = await request.body()
    secret = os.environ.get("ENGINE_HMAC_SECRET", "")
    if not secret:
        return Response(
            content='{"ok":false,"error":{"code":"engine_unconfigured","reason":"ENGINE_HMAC_SECRET is not set."}}',
            status_code=503,
            media_type="application/json",
        )
    given = request.headers.get("x-stetwise-signature", "")
    expected = hmac.new(secret.encode("utf-8"), body, "sha256").hexdigest()
    if not given or not hmac.compare_digest(given, expected):
        return Response(
            content='{"ok":false,"error":{"code":"unauthorized","reason":"The engine signature did not match."}}',
            status_code=401,
            media_type="application/json",
        )
    import json

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        result = {"ok": False, "error": {"code": "bad_request", "reason": "Engine input must be a JSON object."}}
    else:
        result = dispatch(payload)
    return Response(content=json.dumps(result), media_type="application/json")
