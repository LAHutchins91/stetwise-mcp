"""Proof that untouched zip entries kept their compressed bytes."""

from __future__ import annotations

import hashlib

from .zippack import read_package


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def diff_parts(before: bytes, after: bytes) -> dict:
    old = read_package(before).by_name()
    new = read_package(after).by_name()
    changed: list[str] = []
    removed: list[str] = []
    added: list[str] = []
    untouched: dict[str, str] = {}
    changed_hashes: dict[str, dict[str, str]] = {}
    for name, entry in old.items():
        if name not in new:
            removed.append(name)
            continue
        if entry.compressed != new[name].compressed:
            changed.append(name)
            changed_hashes[name] = {"before": _sha(entry.compressed), "after": _sha(new[name].compressed)}
        else:
            untouched[name] = _sha(entry.compressed)
    for name in new:
        if name not in old:
            added.append(name)
    changed.sort()
    removed.sort()
    added.sort()
    return {
        "changed_parts": changed,
        "removed_parts": removed,
        "added_parts": added,
        "untouched_part_count": len(untouched),
        "untouched_identical": True,
        "untouched_sha256": untouched,
        "changed_sha256": changed_hashes,
    }


_XLSX_ALLOWED = ("xl/worksheets/", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "[Content_Types].xml")
_XLSX_REMOVABLE = {"xl/calcChain.xml"}
_DOCX_ALLOWED = {"word/document.xml"}


def assert_allowed(report: dict, kind: str) -> None:
    from .errors import StetwiseRefusal

    if kind in {"xlsx", "xlsm"}:
        for name in report["changed_parts"]:
            if not any(name == allowed or name.startswith(allowed) for allowed in _XLSX_ALLOWED):
                raise StetwiseRefusal(
                    f"Internal check: {name} would have changed, which this edit is not allowed to touch. The edit was not saved.",
                    "verification_failed",
                )
        for name in report["removed_parts"]:
            if name not in _XLSX_REMOVABLE:
                raise StetwiseRefusal(
                    f"Internal check: {name} would have been removed. The edit was not saved.",
                    "verification_failed",
                )
        if report["added_parts"]:
            raise StetwiseRefusal(
                "Internal check: the edit would have added a new package part. The edit was not saved.",
                "verification_failed",
            )
        return
    if kind == "docx":
        unexpected = [name for name in report["changed_parts"] if name not in _DOCX_ALLOWED]
        if unexpected or report["removed_parts"] or report["added_parts"]:
            raise StetwiseRefusal(
                "Internal check: a Word edit would have touched more than word/document.xml. The edit was not saved.",
                "verification_failed",
            )
        return
    raise StetwiseRefusal(f"v1 cannot verify a {kind} package.", "unsupported_format")
