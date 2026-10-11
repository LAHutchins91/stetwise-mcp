"""Container HTTP API.

Callers sign timestamp + "." + nonce + "." + body with ENGINE_HMAC_SECRET.
CPU work runs in a process pool so /health keeps answering.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import multiprocessing
import os
import time
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from .jobs import run_job

_pool: Executor | None = None
_slots: asyncio.Semaphore | None = None
_nonces: dict[str, float] = {}
_NONCE_TTL = 300.0
_NONCE_CAP = 10000
_MAX_SKEW = 300


def concurrency() -> int:
    raw = os.environ.get("STETWISE_ENGINE_CONCURRENCY", "2")
    try:
        value = int(raw)
    except ValueError:
        value = 2
    return max(1, min(value, 8))


def max_body_bytes() -> int:
    raw = os.environ.get("STETWISE_ENGINE_MAX_BODY", "4194304")
    try:
        value = int(raw)
    except ValueError:
        return 4194304
    return max(1024, min(value, 8 * 1024 * 1024))


def listen_port() -> int:
    raw = os.environ.get("PORT", "8080").strip()
    try:
        port = int(raw)
    except ValueError:
        return 8080
    if port < 1 or port > 65535:
        return 8080
    return port


def make_executor(workers: int) -> Executor:
    if os.environ.get("STETWISE_ENGINE_EXECUTOR") == "thread":
        return ThreadPoolExecutor(max_workers=workers)
    return ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))


def clear_nonces() -> None:
    _nonces.clear()


def _prune_nonces(now: float) -> None:
    expired = [key for key, exp in _nonces.items() if exp <= now]
    for key in expired:
        del _nonces[key]
    overflow = len(_nonces) - _NONCE_CAP
    if overflow > 0:
        oldest = sorted(_nonces.items(), key=lambda item: item[1])[:overflow]
        for key, _exp in oldest:
            del _nonces[key]


def _claim_nonce(nonce: str, now: float) -> bool:
    _prune_nonces(now)
    if nonce in _nonces:
        return False
    _nonces[nonce] = now + _NONCE_TTL
    return True


def _json(payload: dict, status: int) -> Response:
    return Response(content=json.dumps(payload), status_code=status, media_type="application/json")


def _error(code: str, reason: str, status: int) -> Response:
    return _json({"ok": False, "error": {"code": code, "reason": reason}}, status)


def _authorize(body: bytes, headers) -> Response | None:
    secret = os.environ.get("ENGINE_HMAC_SECRET", "")
    if not secret:
        return _error("engine_unconfigured", "ENGINE_HMAC_SECRET is not set.", 503)
    timestamp = headers.get("x-stetwise-timestamp", "")
    nonce = headers.get("x-stetwise-nonce", "")
    given = headers.get("x-stetwise-signature", "")
    if not timestamp.isdigit():
        return _error("unauthorized", "The engine timestamp is missing.", 401)
    if abs(time.time() - int(timestamp)) > _MAX_SKEW:
        return _error("unauthorized", "The engine request is older than 5 minutes.", 401)
    if not nonce.isascii() or not (16 <= len(nonce) <= 128) or any(char in nonce for char in ".\r\n "):
        return _error("unauthorized", "The engine nonce is not valid.", 401)
    signed = f"{timestamp}.{nonce}.".encode("utf-8") + body
    expected = hmac.new(secret.encode("utf-8"), signed, "sha256").hexdigest()
    if not given or len(given) != len(expected) or not hmac.compare_digest(given, expected):
        return _error("unauthorized", "The engine signature did not match.", 401)
    if not _claim_nonce(nonce, time.time()):
        return _error("unauthorized", "This engine request was already used.", 401)
    return None


async def _read_body(request: Request) -> bytes | Response:
    limit = max_body_bytes()
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > limit:
                return _error("body_too_large", f"Engine requests are limited to {limit} bytes.", 413)
        except ValueError:
            return _error("bad_request", "Content-Length is not a number.", 400)
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return _error("body_too_large", f"Engine requests are limited to {limit} bytes.", 413)
        chunks.append(chunk)
    return b"".join(chunks)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _pool, _slots
    workers = concurrency()
    _pool = make_executor(workers)
    _slots = asyncio.Semaphore(workers)
    try:
        yield
    finally:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None
        _slots = None


app = FastAPI(title="Stetwise engine", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "service": "stetwise-engine", "version": "0.1.0"}


@app.post("/v1/dispatch")
async def post_dispatch(request: Request) -> Response:
    body = await _read_body(request)
    if isinstance(body, Response):
        return body
    denied = _authorize(body, request.headers)
    if denied is not None:
        return denied
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        return _error("bad_request", "Engine input must be a JSON object.", 400)
    if _slots is None or _pool is None:
        return _error("engine_unavailable", "The file engine is not ready.", 503)
    try:
        await asyncio.wait_for(_slots.acquire(), timeout=0.05)
    except TimeoutError:
        return _error("engine_busy", "The engine is at its concurrency limit. Retry shortly.", 429)
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(_pool, run_job, payload)
    finally:
        _slots.release()
    return _json(result, 200)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=listen_port(), log_level="info")


if __name__ == "__main__":
    main()
