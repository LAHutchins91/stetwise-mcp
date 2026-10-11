"""Fetch and write objects the MCP server names with signed URLs.

The engine does not hold the Supabase service role. It only opens the
https URL in the signed request, and it refuses private addresses unless
STETWISE_ENGINE_ALLOW_LOOPBACK=1 (tests).
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from .errors import StetwiseRefusal

_REDIRECTS = {301, 302, 303, 307, 308}
_BLOCKED_NAMES = {"localhost", "metadata.google.internal"}


def max_object_bytes() -> int:
    raw = os.environ.get("STETWISE_ENGINE_MAX_OBJECT", "26214400")
    try:
        value = int(raw)
    except ValueError:
        return 26214400
    return max(1, value)


def allow_loopback() -> bool:
    return os.environ.get("STETWISE_ENGINE_ALLOW_LOOPBACK") == "1"


def _octet_blocked(text: str) -> bool:
    if not text.isdigit() or len(text) > 3:
        return True
    if len(text) > 1 and text.startswith("0"):
        return True
    return int(text) > 255


def _private_ipv4(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4 or any(_octet_blocked(part) for part in parts):
        return True
    first, second = int(parts[0]), int(parts[1])
    if first in {0, 10, 127}:
        return True
    if first == 169 and second == 254:
        return True
    if first == 172 and 16 <= second <= 31:
        return True
    if first == 192 and second == 168:
        return True
    if first == 100 and 64 <= second <= 127:
        return True
    if first >= 224:
        return True
    return False


def _private_ipv6(ip: str) -> bool:
    lower = ip.lower().split("%", 1)[0]
    if lower in {"::1", "::"}:
        return True
    if lower.startswith("::ffff:"):
        mapped = lower.split("::ffff:", 1)[1]
        if "." in mapped:
            return _private_ipv4(mapped)
    first = lower.split(":", 1)[0] or "0"
    try:
        value = int(first, 16) if first else 0
    except ValueError:
        return True
    if (value & 0xFE00) == 0xFC00:
        return True
    if (value & 0xFFC0) == 0xFE80:
        return True
    return False


def _private_ip(ip: str) -> bool:
    if ":" in ip:
        return _private_ipv6(ip)
    return _private_ipv4(ip)


def _name_blocked(hostname: str) -> bool:
    host = hostname.strip("[]").lower().rstrip(".")
    if not host or host in _BLOCKED_NAMES or host.endswith(".local"):
        return True
    return False


def _address_blocked(ip: str) -> bool:
    if not _private_ip(ip):
        return False
    return not allow_loopback()


def _resolves_blocked(hostname: str) -> bool:
    host = hostname.strip("[]")
    try:
        socket.inet_pton(socket.AF_INET, host)
        return _address_blocked(host)
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return _address_blocked(host)
    except OSError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise StetwiseRefusal("The storage host could not be resolved.", "ssrf") from exc
    if not infos:
        raise StetwiseRefusal("The storage host could not be resolved.", "ssrf")
    for info in infos:
        ip = info[4][0]
        if _address_blocked(ip):
            return True
    return False


def check_object_url(raw: str) -> str:
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise StetwiseRefusal("The storage URL is not valid.", "ssrf") from exc
    if parsed.username or parsed.password:
        raise StetwiseRefusal("Storage URLs cannot include credentials.", "ssrf")
    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http" and allow_loopback():
        pass
    else:
        raise StetwiseRefusal("The engine only fetches https URLs on public addresses.", "ssrf")
    host = parsed.hostname or ""
    if _name_blocked(host) or _resolves_blocked(host):
        raise StetwiseRefusal("That storage host is not allowed.", "ssrf")
    if not parsed.path:
        raise StetwiseRefusal("The storage URL is not valid.", "ssrf")
    return raw


def _read_limited(response: httpx.Response) -> bytes:
    limit = max_object_bytes()
    declared = response.headers.get("content-length")
    if declared:
        try:
            if int(declared) > limit:
                raise StetwiseRefusal(f"The object is larger than {limit} bytes.", "file_too_large")
        except ValueError as exc:
            raise StetwiseRefusal("The object length was not a number.", "bad_request") from exc
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise StetwiseRefusal(f"The object is larger than {limit} bytes.", "file_too_large")
        chunks.append(chunk)
    if size == 0:
        raise StetwiseRefusal("The storage object was empty.", "empty_upload")
    return b"".join(chunks)


def fetch_object(url: str) -> bytes:
    current = check_object_url(url)
    with httpx.Client(timeout=60.0, follow_redirects=False) as client:
        for hop in range(4):
            response = client.get(current)
            if response.status_code in _REDIRECTS:
                if hop == 3:
                    raise StetwiseRefusal("The storage URL redirected too many times.", "ssrf")
                location = response.headers.get("location")
                if not location:
                    raise StetwiseRefusal("The storage URL redirected without a destination.", "ssrf")
                current = check_object_url(urljoin(current, location))
                continue
            if response.status_code != 200:
                raise StetwiseRefusal("The storage object could not be read.", "download_failed")
            return _read_limited(response)
    raise StetwiseRefusal("The storage URL redirected too many times.", "ssrf")


def put_object(url: str, data: bytes) -> None:
    limit = max_object_bytes()
    if len(data) > limit:
        raise StetwiseRefusal(f"The object is larger than {limit} bytes.", "file_too_large")
    current = check_object_url(url)
    headers = {"content-type": "application/octet-stream", "x-upsert": "true"}
    with httpx.Client(timeout=60.0, follow_redirects=False) as client:
        response = client.put(current, content=data, headers=headers)
    if response.status_code not in {200, 201}:
        raise StetwiseRefusal("The storage object could not be written.", "upload_failed")
