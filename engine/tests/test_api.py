"""HTTP engine: HMAC window, body cap, concurrency, and signed-URL transfer."""

from __future__ import annotations

import asyncio
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from stetwise.api import app, clear_nonces, listen_port, make_executor
from stetwise.errors import StetwiseRefusal
from stetwise.transfer import check_object_url

ROOT = Path(__file__).resolve().parents[2]
BLANK = ROOT / "engine" / "stetwise" / "blank" / "blank.xlsx"


@pytest.fixture(autouse=True)
def engine_env(monkeypatch):
    monkeypatch.setenv("ENGINE_HMAC_SECRET", "test-hmac-secret")
    monkeypatch.setenv("STETWISE_ENGINE_EXECUTOR", "thread")
    monkeypatch.setenv("STETWISE_ENGINE_CONCURRENCY", "2")
    monkeypatch.setenv("STETWISE_ENGINE_TEST_HOLD", "1")
    monkeypatch.setenv("STETWISE_ENGINE_ALLOW_LOOPBACK", "1")
    monkeypatch.setenv("STETWISE_ENGINE_MAX_BODY", "4096")
    clear_nonces()


def _sign(body: bytes, timestamp: int | None = None, nonce: str | None = None, secret: str = "test-hmac-secret") -> dict[str, str]:
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    token = nonce or f"{time.time_ns():x}"
    if len(token) < 16:
        token = token.rjust(16, "a")
    digest = hmac.new(secret.encode(), f"{stamp}.{token}.".encode() + body, "sha256").hexdigest()
    return {
        "content-type": "application/json",
        "x-stetwise-timestamp": stamp,
        "x-stetwise-nonce": token,
        "x-stetwise-signature": digest,
    }


class _Lifespan:
    def __init__(self) -> None:
        self._shutdown = asyncio.Event()
        self._started = asyncio.Event()
        self._stopped = asyncio.Event()
        self._startup_sent = False
        self._task: asyncio.Task | None = None

    async def _receive(self):
        if not self._startup_sent:
            self._startup_sent = True
            return {"type": "lifespan.startup"}
        await self._shutdown.wait()
        return {"type": "lifespan.shutdown"}

    async def _send(self, message):
        if message["type"] == "lifespan.startup.complete":
            self._started.set()
        elif message["type"] == "lifespan.startup.failed":
            self._started.set()
            raise RuntimeError(message.get("message") or "lifespan failed")
        elif message["type"] == "lifespan.shutdown.complete":
            self._stopped.set()

    async def __aenter__(self):
        scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}
        self._task = asyncio.create_task(app(scope, self._receive, self._send))
        await self._started.wait()
        self._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine")
        await self._client.__aenter__()
        return self._client

    async def __aexit__(self, exc_type, exc, tb):
        await self._client.__aexit__(exc_type, exc, tb)
        self._shutdown.set()
        await self._stopped.wait()
        if self._task is not None:
            await self._task
        return False


def _client():
    return _Lifespan()


def test_listen_port_defaults_and_rejects_garbage(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    assert listen_port() == 8080
    monkeypatch.setenv("PORT", "9090")
    assert listen_port() == 9090
    monkeypatch.setenv("PORT", "nope")
    assert listen_port() == 8080
    monkeypatch.setenv("PORT", "0")
    assert listen_port() == 8080
    monkeypatch.setenv("PORT", "70000")
    assert listen_port() == 8080


def test_default_executor_is_a_process_pool(monkeypatch):
    monkeypatch.delenv("STETWISE_ENGINE_EXECUTOR", raising=False)
    pool = make_executor(1)
    try:
        assert type(pool).__name__ == "ProcessPoolExecutor"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def test_blocks_private_urls_unless_loopback_is_explicit(monkeypatch):
    monkeypatch.delenv("STETWISE_ENGINE_ALLOW_LOOPBACK", raising=False)
    with pytest.raises(StetwiseRefusal):
        check_object_url("http://127.0.0.1/file")
    with pytest.raises(StetwiseRefusal):
        check_object_url("https://metadata.google.internal/computeMetadata/v1/")
    with pytest.raises(StetwiseRefusal):
        check_object_url("https://127.0.0.1/file")


def test_hmac_window_replay_and_body_cap():
    async def scenario():
        body = json.dumps({"action": "health"}).encode()
        async with _client() as client:
            missing = await client.post("/v1/dispatch", content=body)
            assert missing.status_code == 401

            stale = await client.post("/v1/dispatch", content=body, headers=_sign(body, timestamp=int(time.time()) - 301))
            assert stale.status_code == 401
            assert stale.json()["error"]["reason"].endswith("5 minutes.")

            old = _sign(body)
            old["x-stetwise-signature"] = hmac.new(b"test-hmac-secret", body, "sha256").hexdigest()
            legacy = await client.post("/v1/dispatch", content=body, headers=old)
            assert legacy.status_code == 401

            headers = _sign(body, nonce="nonce-replay-0001")
            first = await client.post("/v1/dispatch", content=body, headers=headers)
            assert first.status_code == 200
            assert first.json()["ok"] is True
            again = await client.post("/v1/dispatch", content=body, headers=headers)
            assert again.status_code == 401
            reused = _sign(body, nonce="nonce-replay-0001")
            assert (await client.post("/v1/dispatch", content=body, headers=reused)).status_code == 401

            huge = b'{"action":"health","pad":"' + (b"x" * 5000) + b'"}'
            capped = await client.post("/v1/dispatch", content=huge)
            assert capped.status_code == 413

    asyncio.run(scenario())


def test_health_answers_while_a_job_holds_the_only_slot(monkeypatch):
    monkeypatch.setenv("STETWISE_ENGINE_CONCURRENCY", "1")

    async def scenario():
        hold = json.dumps({"action": "hold", "seconds": 1.5}).encode()
        probe_body = json.dumps({"action": "health"}).encode()
        async with _client() as client:
            started = asyncio.create_task(client.post("/v1/dispatch", content=hold, headers=_sign(hold, nonce="hold-nonce-000001")))
            deadline = time.time() + 5
            saw_busy = False
            while time.time() < deadline:
                health = await client.get("/health")
                assert health.status_code == 200
                assert health.json()["ok"] is True
                probe = await client.post("/v1/dispatch", content=probe_body, headers=_sign(probe_body))
                if probe.status_code == 429:
                    saw_busy = True
                    break
            assert saw_busy
            finished = await started
            assert finished.status_code == 200
            assert finished.json()["result"]["held"] is True

    asyncio.run(scenario())


def test_fetches_and_writes_storage_urls_without_returning_bytes(monkeypatch):
    blob = BLANK.read_bytes()
    stored: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/big"):
                self.send_response(200)
                self.send_header("Content-Length", "80")
                self.end_headers()
                self.wfile.write(b"x" * 80)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def do_PUT(self):
            length = int(self.headers.get("Content-Length") or "0")
            stored.append(self.rfile.read(length))
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        async def scenario():
            payload = {
                "action": "inspect",
                "filename": "blank.xlsx",
                "source_url": f"http://127.0.0.1:{port}/blank.xlsx",
                "source_key": "owner/incoming/abc",
                "dest_url": f"http://127.0.0.1:{port}/out",
                "dest_key": "owner/stage/1",
                "store_source": True,
            }
            body = json.dumps(payload).encode()
            async with _client() as client:
                response = await client.post("/v1/dispatch", content=body, headers=_sign(body))
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["ok"] is True
            assert "data_b64" not in result["result"]
            assert result["result"]["stored"] is True
            assert result["result"]["kind"] == "xlsx"
            assert stored == [blob]

            monkeypatch.delenv("STETWISE_ENGINE_ALLOW_LOOPBACK")
            denied_body = json.dumps({
                "action": "inspect",
                "filename": "blank.xlsx",
                "source_url": f"http://127.0.0.1:{port}/blank.xlsx",
            }).encode()
            async with _client() as client:
                denied = await client.post("/v1/dispatch", content=denied_body, headers=_sign(denied_body))
            assert denied.status_code == 200
            assert denied.json()["ok"] is False
            assert denied.json()["error"]["code"] == "ssrf"

            monkeypatch.setenv("STETWISE_ENGINE_ALLOW_LOOPBACK", "1")
            monkeypatch.setenv("STETWISE_ENGINE_MAX_OBJECT", "16")
            huge = json.dumps({
                "action": "inspect",
                "filename": "blank.xlsx",
                "source_url": f"http://127.0.0.1:{port}/big",
            }).encode()
            async with _client() as client:
                oversized = await client.post("/v1/dispatch", content=huge, headers=_sign(huge))
            assert oversized.json()["ok"] is False
            assert oversized.json()["error"]["code"] == "file_too_large"

        asyncio.run(scenario())
    finally:
        server.shutdown()
