"""JSON-in, JSON-out process used by the MCP server when ENGINE_URL is unset."""

import json
import sys

from .engine import dispatch


def main() -> None:
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        result = {"ok": False, "error": {"code": "bad_request", "reason": "Engine input must be a JSON object."}}
    else:
        result = dispatch(payload)
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
