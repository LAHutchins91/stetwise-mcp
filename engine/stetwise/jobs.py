"""Work that must not run on the API event loop."""

from __future__ import annotations

import base64
import hashlib
import os
import time
from typing import Any

from .engine import dispatch
from .errors import StetwiseError
from .transfer import fetch_object, put_object


def run_job(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        action = payload.get("action")
        if action == "hold":
            if os.environ.get("STETWISE_ENGINE_TEST_HOLD") != "1":
                return {
                    "ok": False,
                    "error": {"code": "unknown_action", "reason": "Unknown action 'hold'.", "started": False},
                }
            time.sleep(min(float(payload.get("seconds") or 0.5), 5.0))
            return {"ok": True, "result": {"held": True}}
        prepared, source = _prepare(payload)
        result = dispatch(prepared)
        return _store(payload, result, source)
    except StetwiseError as exc:
        return {"ok": False, "error": {"code": exc.code, "reason": exc.reason, "started": bool(exc.started)}}
    except Exception:
        return {
            "ok": False,
            "error": {
                "code": "engine_error",
                "reason": "The file engine hit an unexpected error and did not change the file.",
                "started": False,
            },
        }


def _prepare(payload: dict[str, Any]) -> tuple[dict[str, Any], bytes | None]:
    prepared = dict(payload)
    source: bytes | None = None
    source_url = prepared.get("source_url")
    if isinstance(source_url, str) and source_url:
        source = fetch_object(source_url)
        prepared["data_b64"] = base64.b64encode(source).decode("ascii")
    for url_key, b64_key in (("before_url", "before_b64"), ("after_url", "after_b64")):
        raw = prepared.get(url_key)
        if isinstance(raw, str) and raw:
            prepared[b64_key] = base64.b64encode(fetch_object(raw)).decode("ascii")
    return prepared, source


def _store(payload: dict[str, Any], result: dict[str, Any], source: bytes | None) -> dict[str, Any]:
    if not result.get("ok"):
        return result
    dest = payload.get("dest_url")
    if not isinstance(dest, str) or not dest:
        return result
    inner = result.get("result")
    if not isinstance(inner, dict):
        return result
    raw = inner.get("data_b64")
    data: bytes | None = None
    if isinstance(raw, str) and raw:
        data = base64.b64decode(raw)
    elif payload.get("store_source") and source is not None:
        data = source
    if data is None:
        return result
    put_object(dest, data)
    inner.pop("data_b64", None)
    inner["stored"] = True
    if isinstance(payload.get("dest_key"), str):
        inner["dest_key"] = payload["dest_key"]
    inner.setdefault("sha256", hashlib.sha256(data).hexdigest())
    inner.setdefault("size", len(data))
    return result
