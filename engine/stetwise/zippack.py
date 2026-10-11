"""OOXML zip reader and writer.

Untouched entries are copied as the original local-file bytes, compression included.
The central directory is rewritten only so offsets stay valid after a replaced entry
changes size. A no-op edit returns the original package bytes unchanged.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from .errors import StetwiseRefusal

LOCAL_SIG = 0x04034B50
CENTRAL_SIG = 0x02014B50
EOCD_SIG = 0x06054B50
ZIP64_EOCD_SIG = 0x06064B50

MAX_ENTRIES = 4000
MAX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_COMPRESSED = 50 * 1024 * 1024
MAX_RATIO = 80


@dataclass
class ZipEntry:
    name: str
    local_offset: int
    slice_end: int
    method: int
    flag: int
    crc: int
    comp_size: int
    uncomp_size: int
    compressed: bytes
    data: bytes
    raw_local: bytes
    version_made: int
    version_needed: int
    time: int
    date: int
    internal_attr: int
    external_attr: int
    extra: bytes
    comment: bytes
    disk_start: int


@dataclass
class Package:
    preamble: bytes
    entries: list[ZipEntry]
    comment: bytes

    def by_name(self) -> dict[str, ZipEntry]:
        out: dict[str, ZipEntry] = {}
        for entry in self.entries:
            if entry.name in out:
                raise StetwiseRefusal(
                    f"The package has more than one part named {entry.name!r}. v1 will not guess which one to edit.",
                    "malformed_package",
                )
            out[entry.name] = entry
        return out

    def parts(self) -> dict[str, bytes]:
        return {entry.name: entry.data for entry in self.entries}


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _find_eocd(data: bytes) -> int:
    if len(data) < 22:
        raise StetwiseRefusal("This file is not a zip package.", "not_ooxml")
    start = max(0, len(data) - (22 + 65535))
    idx = data.rfind(b"PK\x05\x06", start)
    if idx < 0:
        raise StetwiseRefusal("This file is not a zip package.", "not_ooxml")
    return idx


def _check_name(name: str) -> None:
    if not name or name.startswith("/") or name.startswith("\\"):
        raise StetwiseRefusal("The package contains an absolute part path.", "zip_path")
    parts = name.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise StetwiseRefusal(
            "The package contains a part path that escapes the zip. v1 rejects it.",
            "zip_path",
        )
    if "\x00" in name:
        raise StetwiseRefusal("The package contains a part path with a null byte.", "zip_path")


def _inflate_safe(compressed: bytes, expected: int) -> bytes:
    if expected > MAX_UNCOMPRESSED:
        raise StetwiseRefusal("A part is larger than v1 will unpack.", "zip_bomb")
    if compressed and expected / max(len(compressed), 1) > MAX_RATIO and expected > 1_000_000:
        raise StetwiseRefusal(
            "A part expands far beyond its compressed size. v1 rejects it as a zip bomb.",
            "zip_bomb",
        )
    decomp = zlib.decompressobj(-15)
    try:
        data = decomp.decompress(compressed, expected + 1)
        data += decomp.flush()
    except zlib.error as exc:
        raise StetwiseRefusal("A part could not be decompressed.", "malformed_package") from exc
    if len(data) != expected:
        raise StetwiseRefusal(
            "A part's uncompressed size does not match the zip directory.",
            "malformed_package",
        )
    return data


def read_package(blob: bytes) -> Package:
    if len(blob) > MAX_COMPRESSED:
        raise StetwiseRefusal("The file is larger than the 50 MB v1 limit.", "too_large")
    if b"PK\x06\x06" in blob[-128:]:
        raise StetwiseRefusal("ZIP64 packages are not supported in v1.", "zip64")
    eocd = _find_eocd(blob)
    if _u32(blob, eocd) != EOCD_SIG:
        raise StetwiseRefusal("This file is not a zip package.", "not_ooxml")
    total = _u16(blob, eocd + 10)
    cd_size = _u32(blob, eocd + 12)
    cd_offset = _u32(blob, eocd + 16)
    comment_len = _u16(blob, eocd + 20)
    if cd_offset == 0xFFFFFFFF or cd_size == 0xFFFFFFFF or total == 0xFFFF:
        raise StetwiseRefusal("ZIP64 packages are not supported in v1.", "zip64")
    if total > MAX_ENTRIES:
        raise StetwiseRefusal("The package has more parts than v1 will open.", "zip_bomb")
    if cd_offset + cd_size > len(blob):
        raise StetwiseRefusal("The zip directory is truncated.", "malformed_package")
    comment = blob[eocd + 22 : eocd + 22 + comment_len]
    cursor = cd_offset
    end = cd_offset + cd_size
    metas: list[dict] = []
    for _ in range(total):
        if cursor + 46 > end or _u32(blob, cursor) != CENTRAL_SIG:
            raise StetwiseRefusal("The zip directory is truncated.", "malformed_package")
        flag = _u16(blob, cursor + 8)
        method = _u16(blob, cursor + 10)
        crc = _u32(blob, cursor + 16)
        comp_size = _u32(blob, cursor + 20)
        uncomp_size = _u32(blob, cursor + 24)
        name_len = _u16(blob, cursor + 28)
        extra_len = _u16(blob, cursor + 30)
        comment_len_e = _u16(blob, cursor + 32)
        header_end = cursor + 46 + name_len + extra_len + comment_len_e
        if header_end > end:
            raise StetwiseRefusal("The zip directory is truncated.", "malformed_package")
        name_b = blob[cursor + 46 : cursor + 46 + name_len]
        try:
            name = name_b.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StetwiseRefusal("A part path is not UTF-8.", "malformed_package") from exc
        _check_name(name)
        if flag & 0x1:
            raise StetwiseRefusal(
                "This package is encrypted. v1 does not decrypt or edit it.",
                "encrypted",
            )
        if comp_size == 0xFFFFFFFF or uncomp_size == 0xFFFFFFFF:
            raise StetwiseRefusal("ZIP64 packages are not supported in v1.", "zip64")
        if method not in (0, 8):
            raise StetwiseRefusal(
                f"Part {name} uses a compression method v1 does not read.",
                "malformed_package",
            )
        metas.append(
            {
                "name": name,
                "flag": flag,
                "method": method,
                "crc": crc,
                "comp_size": comp_size,
                "uncomp_size": uncomp_size,
                "local_offset": _u32(blob, cursor + 42),
                "version_made": _u16(blob, cursor + 4),
                "version_needed": _u16(blob, cursor + 6),
                "time": _u16(blob, cursor + 12),
                "date": _u16(blob, cursor + 14),
                "disk_start": _u16(blob, cursor + 34),
                "internal_attr": _u16(blob, cursor + 36),
                "external_attr": _u32(blob, cursor + 38),
                "extra": blob[cursor + 46 + name_len : cursor + 46 + name_len + extra_len],
                "comment": blob[header_end - comment_len_e : header_end],
            }
        )
        cursor = header_end
    if not metas and total != 0:
        raise StetwiseRefusal("The zip directory is empty.", "malformed_package")
    ordered = sorted(metas, key=lambda item: item["local_offset"])
    if ordered and ordered[0]["local_offset"] > len(blob):
        raise StetwiseRefusal("A zip entry points outside the file.", "malformed_package")
    preamble = blob[: ordered[0]["local_offset"]] if ordered else blob[:cd_offset]
    entries: list[ZipEntry] = []
    uncompressed_total = 0
    for index, meta in enumerate(ordered):
        offset = meta["local_offset"]
        next_off = ordered[index + 1]["local_offset"] if index + 1 < len(ordered) else cd_offset
        if next_off < offset or next_off > len(blob) or offset + 30 > len(blob):
            raise StetwiseRefusal("Zip entries overlap or point outside the file.", "malformed_package")
        if _u32(blob, offset) != LOCAL_SIG:
            raise StetwiseRefusal("A zip entry is missing its local header.", "malformed_package")
        name_len = _u16(blob, offset + 26)
        extra_len = _u16(blob, offset + 28)
        data_start = offset + 30 + name_len + extra_len
        if data_start + meta["comp_size"] > next_off:
            raise StetwiseRefusal("A zip entry's compressed data is truncated.", "malformed_package")
        compressed = blob[data_start : data_start + meta["comp_size"]]
        if meta["method"] == 0:
            data = compressed
            if len(data) != meta["uncomp_size"]:
                raise StetwiseRefusal("A stored part has the wrong size.", "malformed_package")
        else:
            data = _inflate_safe(compressed, meta["uncomp_size"])
        if zlib.crc32(data) & 0xFFFFFFFF != meta["crc"]:
            raise StetwiseRefusal(f"Part {meta['name']} failed its CRC check.", "malformed_package")
        uncompressed_total += len(data)
        if uncompressed_total > MAX_UNCOMPRESSED:
            raise StetwiseRefusal("The package expands past the v1 size limit.", "zip_bomb")
        raw_local = blob[offset:next_off]
        entries.append(
            ZipEntry(
                name=meta["name"],
                local_offset=offset,
                slice_end=next_off,
                method=meta["method"],
                flag=meta["flag"],
                crc=meta["crc"],
                comp_size=meta["comp_size"],
                uncomp_size=meta["uncomp_size"],
                compressed=compressed,
                data=data,
                raw_local=raw_local,
                version_made=meta["version_made"],
                version_needed=meta["version_needed"],
                time=meta["time"],
                date=meta["date"],
                internal_attr=meta["internal_attr"],
                external_attr=meta["external_attr"],
                extra=meta["extra"],
                comment=meta["comment"],
                disk_start=meta["disk_start"],
            )
        )
    # Restore central-directory order for readers that expect it, while raw slices
    # stay attached to each entry.
    by_offset = {entry.local_offset: entry for entry in entries}
    in_cd_order = [by_offset[meta["local_offset"]] for meta in metas]
    return Package(preamble=preamble, entries=in_cd_order, comment=comment)


def _local_bytes(entry: ZipEntry, compressed: bytes, crc: int, uncomp_size: int) -> bytes:
    name = entry.name.encode("utf-8")
    flag = 0x800  # UTF-8 name, sizes present, no data descriptor
    header = struct.pack(
        "<IHHHHHIIIHH",
        LOCAL_SIG,
        20,
        flag,
        8,
        entry.time,
        entry.date,
        crc,
        len(compressed),
        uncomp_size,
        len(name),
        0,
    )
    return header + name + compressed


def _central_bytes(entry: ZipEntry, offset: int, replaced: bool) -> bytes:
    name = entry.name.encode("utf-8")
    if replaced:
        method = 8
        flag = 0x800
        extra = b""
        comment = b""
        comp_size = entry.comp_size
        uncomp = entry.uncomp_size
        crc = entry.crc
        version_needed = 20
    else:
        method = entry.method
        flag = entry.flag
        extra = entry.extra
        comment = entry.comment
        comp_size = entry.comp_size
        uncomp = entry.uncomp_size
        crc = entry.crc
        version_needed = entry.version_needed
    header = struct.pack(
        "<IHHHHHHIIIHHHHHII",
        CENTRAL_SIG,
        entry.version_made,
        version_needed,
        flag,
        method,
        entry.time,
        entry.date,
        crc,
        comp_size,
        uncomp,
        len(name),
        len(extra),
        len(comment),
        entry.disk_start,
        entry.internal_attr,
        entry.external_attr,
        offset,
    )
    return header + name + extra + comment


def write_package(package: Package, replacements: dict[str, bytes] | None = None, drop: set[str] | None = None) -> bytes:
    """Return a new package. Names omitted from replacements are copied raw."""
    replacements = replacements or {}
    drop = drop or set()
    unknown = (set(replacements) | drop) - {entry.name for entry in package.entries}
    if unknown:
        raise StetwiseRefusal(
            "The edit referred to a part that is not in the package: " + ", ".join(sorted(unknown)),
            "missing_part",
        )
    if not replacements and not drop:
        # Caller should pass the original blob for a true no-op. This rebuilds only
        # when asked to, and tests cover the original-bytes path in the engine.
        pass
    out = bytearray(package.preamble)
    written: list[tuple[ZipEntry, int, bool]] = []
    for entry in _local_order(package):
        if entry.name in drop:
            continue
        offset = len(out)
        if entry.name in replacements:
            data = replacements[entry.name]
            if not isinstance(data, bytes):
                raise StetwiseRefusal("A replacement part was not bytes.", "engine_error")
            if len(data) > MAX_UNCOMPRESSED:
                raise StetwiseRefusal("A replacement part is too large.", "too_large")
            compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
            compressed = compressor.compress(data) + compressor.flush()
            crc = zlib.crc32(data) & 0xFFFFFFFF
            local = _local_bytes(entry, compressed, crc, len(data))
            out += local
            clone = ZipEntry(**{**entry.__dict__})
            clone.crc = crc
            clone.comp_size = len(compressed)
            clone.uncomp_size = len(data)
            clone.method = 8
            clone.flag = 0x800
            clone.compressed = compressed
            clone.data = data
            written.append((clone, offset, True))
        else:
            out += entry.raw_local
            written.append((entry, offset, False))
    cd_offset = len(out)
    # Central directory follows the original central-directory order, skipping dropped parts.
    cd_index = {entry.name: (entry, offset, replaced) for entry, offset, replaced in written}
    for entry in package.entries:
        if entry.name not in cd_index:
            continue
        current, offset, replaced = cd_index[entry.name]
        out += _central_bytes(current, offset, replaced)
    cd_size = len(out) - cd_offset
    count = len(written)
    if count > 0xFFFF or cd_size > 0xFFFFFFFF or cd_offset > 0xFFFFFFFF:
        raise StetwiseRefusal("The edited package would require ZIP64, which v1 does not write.", "zip64")
    comment = package.comment[:65535]
    out += struct.pack("<IHHHHIIH", EOCD_SIG, 0, 0, count, count, cd_size, cd_offset, len(comment))
    out += comment
    return bytes(out)


def _local_order(package: Package) -> list[ZipEntry]:
    return sorted(package.entries, key=lambda entry: entry.local_offset)


def part_hashes(blob: bytes) -> dict[str, str]:
    import hashlib

    package = read_package(blob)
    return {entry.name: hashlib.sha256(entry.compressed).hexdigest() for entry in package.entries}
