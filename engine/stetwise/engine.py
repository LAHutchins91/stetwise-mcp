"""Stateless Stetwise engine. Bytes in, bytes and a verification report out."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

from . import docx, xlsx
from .errors import StetwiseError, StetwiseRefusal
from .verify import assert_allowed, diff_parts
from .zippack import read_package, write_package

FUTURE = {
    "pptx": "PowerPoint edits are not in v1.",
    "cloud": "Google Drive and OneDrive are not in v1.",
    "batch": "Editing several files in one call is not in v1.",
    "legacy": "Legacy .xls, .doc, and .ppt files are not in v1.",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect(blob: bytes, filename: str) -> str:
    lower = (filename or "").lower()
    if lower.endswith(".pptx"):
        raise StetwiseRefusal(FUTURE["pptx"], "unsupported_format")
    if lower.endswith((".ppt", ".xls", ".doc")) and not lower.endswith((".docx", ".xlsx", ".xlsm")):
        raise StetwiseRefusal(FUTURE["legacy"], "unsupported_format")
    if not blob.startswith(b"PK"):
        raise StetwiseRefusal(
            "This is not an OOXML zip. Encrypted files and legacy .xls/.doc files are refused in v1.",
            "not_ooxml",
        )
    package = read_package(blob)
    names = set(package.parts())
    if "ppt/presentation.xml" in names:
        raise StetwiseRefusal(FUTURE["pptx"], "unsupported_format")
    if "word/document.xml" in names:
        if lower.endswith(".xlsx") or lower.endswith(".xlsm"):
            raise StetwiseRefusal("The file name says spreadsheet but the package is a Word document.", "kind_mismatch")
        return "docx"
    if "xl/workbook.xml" in names:
        if lower.endswith(".docx"):
            raise StetwiseRefusal("The file name says Word but the package is a workbook.", "kind_mismatch")
        if any(name == "xl/vbaProject.bin" for name in names):
            return "xlsm"
        return "xlsx"
    raise StetwiseRefusal(
        "v1 edits .xlsx and .docx packages only. This zip does not contain a workbook or a Word document.",
        "unsupported_format",
    )


def inspect(blob: bytes, filename: str) -> dict:
    kind = detect(blob, filename)
    package = read_package(blob)
    body = docx.inspect_docx(package) if kind == "docx" else xlsx.inspect_xlsx(package)
    body["sha256"] = sha256(blob)
    body["size"] = len(blob)
    body["filename"] = filename
    return body


def read(blob: bytes, filename: str, args: dict) -> dict:
    kind = detect(blob, filename)
    package = read_package(blob)
    if kind == "docx":
        return docx.read_section(package, args.get("anchor"), int(args.get("limit") or 80))
    sheet = args.get("sheet")
    if not sheet:
        sheets = xlsx.sheet_map(package)
        if not sheets:
            raise StetwiseRefusal("This workbook has no sheets.", "unknown_sheet")
        sheet = sheets[0].name
    return xlsx.read_range(package, sheet, args.get("range"), int(args.get("limit") or 500))


def search(blob: bytes, filename: str, query: str, limit: int = 50) -> dict:
    kind = detect(blob, filename)
    package = read_package(blob)
    if kind == "docx":
        return docx.search_docx(package, query, limit)
    return xlsx.search_xlsx(package, query, limit)


def _proposal_cells(ops: list[dict]) -> int:
    from .refs import split_ref

    total = 0
    for op in ops:
        name = op.get("op")
        if name == "set_cells":
            total += len(op.get("cells") or [])
        elif name == "format_cells":
            total += len(op.get("refs") or [])
        elif name in {"clear_range", "fill_range"}:
            c1, r1, c2, r2 = split_ref(str(op.get("range") or ""))
            total += (c2 - c1 + 1) * (r2 - r1 + 1)
    return total


def propose(blob: bytes, filename: str, ops: list[dict]) -> dict:
    if not ops:
        raise StetwiseRefusal("propose_edit needs at least one operation.", "empty_edit")
    if len(ops) > 20:
        raise StetwiseRefusal("v1 accepts at most 20 operations in one proposal.", "too_many")
    if _proposal_cells(ops) > 5000:
        raise StetwiseRefusal("v1 accepts at most 5000 cells in one proposal.", "too_many")
    kind = detect(blob, filename)
    current = blob
    diffs: list[dict] = []
    warnings: list[str] = []
    touched: set[str] = set()
    dropped: set[str] = set()
    for op in ops:
        package = read_package(current)
        step_kind = detect(current, filename)
        if step_kind != kind:
            raise StetwiseRefusal("The edit changed the package kind. v1 stopped.", "verification_failed")
        result = _one(package, step_kind, op)
        if not result.replacements and not result.drop:
            raise StetwiseRefusal("An operation made no change.", "empty_edit")
        current = write_package(package, result.replacements, result.drop)
        diffs.extend(result.diff)
        warnings.extend(result.warnings)
        touched.update(result.replacements)
        dropped.update(result.drop)
    report = diff_parts(blob, current)
    if set(report["changed_parts"]) - touched:
        raise StetwiseRefusal(
            "Internal check: a part changed that this edit did not mean to touch. The edit was not saved.",
            "verification_failed",
        )
    if set(report["removed_parts"]) - dropped:
        raise StetwiseRefusal(
            "Internal check: a part was removed that this edit did not mean to remove. The edit was not saved.",
            "verification_failed",
        )
    extra: set[str] = set()
    for op in ops:
        if op.get("op") == "format_cells" and kind in {"xlsx", "xlsm"}:
            extra.add("xl/styles.xml")
        if kind == "docx" and op.get("track_changes"):
            extra.add("word/settings.xml")
    assert_allowed(report, kind, extra)
    # Hashes of untouched parts are the proof. Omit the full map when it is large
    # but keep the count; callers can request every hash because files stay small.
    return {
        "sha256": sha256(current),
        "size": len(current),
        "base_sha256": sha256(blob),
        "kind": kind,
        "diff": diffs,
        "warnings": warnings,
        "verification": {
            "changed_parts": report["changed_parts"],
            "removed_parts": report["removed_parts"],
            "added_parts": report["added_parts"],
            "untouched_part_count": report["untouched_part_count"],
            "untouched_identical": report["untouched_identical"],
            "untouched_sha256": report["untouched_sha256"],
            "changed_sha256": report["changed_sha256"],
        },
        "data_b64": base64.b64encode(current).decode("ascii"),
    }


def _track(op: dict) -> bool:
    return bool(op.get("track_changes"))


def _one(package, kind: str, op: dict):
    name = op.get("op")
    if kind == "docx":
        if name == "replace_text":
            return docx.replace_text(
                package, op.get("find") or "", op.get("replace") or "", op.get("occurrence"), track_changes=_track(op)
            )
        if name == "replace_paragraph":
            return docx.replace_paragraph(package, op.get("anchor") or "", op.get("text") or "", track_changes=_track(op))
        if name == "update_table_cells":
            return docx.update_table_cells(
                package, int(op.get("table") or 0), op.get("cells") or [], track_changes=_track(op)
            )
        if name == "insert_paragraph":
            return docx.insert_paragraph(
                package, op.get("anchor") or "", op.get("text") or "", track_changes=_track(op), style=op.get("style")
            )
        if name == "delete_paragraph":
            return docx.delete_paragraph(package, op.get("anchor") or "", track_changes=_track(op))
        if name == "format_runs":
            color = op.get("font_color")
            colour = op.get("font_colour")
            if color is not None and colour is not None and str(color).lower() != str(colour).lower():
                raise StetwiseRefusal("font_color and font_colour do not match.", "bad_format")
            return docx.format_runs(
                package,
                op.get("find") or "",
                bold=op.get("bold"),
                italic=op.get("italic"),
                underline=op.get("underline"),
                occurrence=op.get("occurrence"),
                font_size=op.get("font_size"),
                font_color=None if color is None and colour is None else (color if color is not None else colour),
            )
        if name in {"set_cells", "insert_rows", "insert_cols", "format_cells", "clear_range", "fill_range"}:
            raise StetwiseRefusal(f"{name} is an Excel operation and this file is a Word document.", "wrong_kind")
        raise StetwiseRefusal(f"Unknown operation {name!r}.", "unknown_op")
    if name == "set_cells":
        return xlsx.set_cells(package, op.get("sheet") or "", op.get("cells") or [])
    if name == "insert_rows":
        return xlsx.insert_rows(package, op.get("sheet") or "", int(op["row"]), int(op.get("count") or 1))
    if name == "insert_cols":
        return xlsx.insert_cols(package, op.get("sheet") or "", op.get("column") or "", int(op.get("count") or 1))
    if name == "replace_text":
        return xlsx.replace_text(package, op.get("find") or "", op.get("replace") or "", op.get("occurrence"))
    if name == "format_cells":
        return xlsx.format_cells(package, op.get("sheet") or "", op.get("refs") or [], op)
    if name == "clear_range":
        return xlsx.clear_range(package, op.get("sheet") or "", op.get("range") or "")
    if name == "fill_range":
        return xlsx.fill_range(
            package,
            op.get("sheet") or "",
            op.get("range") or "",
            value=op.get("value") if "value" in op else None,
            formula=op.get("formula") if "formula" in op else None,
        )
    if name in {"replace_paragraph", "update_table_cells", "insert_paragraph", "delete_paragraph", "format_runs"}:
        raise StetwiseRefusal(f"{name} is a Word operation and this file is a workbook.", "wrong_kind")
    raise StetwiseRefusal(f"Unknown operation {name!r}.", "unknown_op")


def propose_parts(blob: bytes, filename: str, replacements: dict[str, bytes], drop: set[str], diffs: list[dict], warnings: list[str]) -> dict:
    kind = detect(blob, filename)
    package = read_package(blob)
    current = write_package(package, replacements, drop)
    report = diff_parts(blob, current)
    if set(report["changed_parts"]) - set(replacements):
        raise StetwiseRefusal(
            "Internal check: a part changed that this edit did not mean to touch. The edit was not saved.",
            "verification_failed",
        )
    if set(report["removed_parts"]) - drop:
        raise StetwiseRefusal(
            "Internal check: a part was removed that this edit did not mean to remove. The edit was not saved.",
            "verification_failed",
        )
    assert_allowed(report, kind)
    return {
        "sha256": sha256(current),
        "size": len(current),
        "base_sha256": sha256(blob),
        "kind": kind,
        "diff": diffs,
        "warnings": warnings,
        "verification": {
            "changed_parts": report["changed_parts"],
            "removed_parts": report["removed_parts"],
            "added_parts": report["added_parts"],
            "untouched_part_count": report["untouched_part_count"],
            "untouched_identical": report["untouched_identical"],
            "untouched_sha256": report["untouched_sha256"],
            "changed_sha256": report["changed_sha256"],
        },
        "data_b64": base64.b64encode(current).decode("ascii"),
    }


def blank_file(kind: str) -> dict:
    if kind not in {"xlsx", "docx"}:
        raise StetwiseRefusal("create_file accepts xlsx or docx.", "unsupported_format")
    path = Path(__file__).resolve().parent / "blank" / f"blank.{kind}"
    if not path.is_file():
        raise StetwiseRefusal("The blank template is missing from the engine.", "engine_error")
    blob = path.read_bytes()
    filename = f"blank.{kind}"
    detected = detect(blob, filename)
    return {
        "kind": detected,
        "filename": filename,
        "sha256": sha256(blob),
        "size": len(blob),
        "data_b64": base64.b64encode(blob).decode("ascii"),
        "source": "LibreOffice",
    }


_VERIFY_EXTRA = {"xl/styles.xml", "word/settings.xml"}


def verify_pair(before: bytes, after: bytes, filename: str) -> dict:
    kind = detect(after, filename)
    report = diff_parts(before, after)
    extra = {name for name in report["changed_parts"] if name in _VERIFY_EXTRA}
    assert_allowed(report, kind, extra)
    return {
        "kind": kind,
        "changed_parts": report["changed_parts"],
        "removed_parts": report["removed_parts"],
        "added_parts": report["added_parts"],
        "untouched_part_count": report["untouched_part_count"],
        "untouched_identical": report["untouched_identical"],
    }


def dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        action = payload.get("action")
        filename = str(payload.get("filename") or "file")
        if action == "health":
            return {"ok": True, "service": "stetwise-engine", "version": "0.1.0", "future": FUTURE}
        if action == "verify":
            before_raw = payload.get("before_b64")
            after_raw = payload.get("after_b64")
            if not isinstance(before_raw, str) or not isinstance(after_raw, str):
                raise StetwiseRefusal("before_b64 and after_b64 are required.", "bad_request")
            return {
                "ok": True,
                "result": verify_pair(base64.b64decode(before_raw), base64.b64decode(after_raw), filename),
            }
        if action == "recalculate":
            raw = payload.get("data_b64")
            if not isinstance(raw, str):
                raise StetwiseRefusal("data_b64 is required.", "bad_request")
            blob = base64.b64decode(raw)
            detect(blob, filename)
            from .recalc import recalculate

            return {"ok": True, "result": recalculate(blob, filename, read_package(blob))}
        if action == "list_comments":
            raw = payload.get("data_b64")
            if not isinstance(raw, str):
                raise StetwiseRefusal("data_b64 is required.", "bad_request")
            blob = base64.b64decode(raw)
            kind = detect(blob, filename)
            if kind != "docx":
                raise StetwiseRefusal("list_comments reads Word comments. This file is a workbook.", "wrong_kind")
            return {"ok": True, "result": docx.list_comments(read_package(blob))}
        if action == "blank":
            return {"ok": True, "result": blank_file(str(payload.get("kind") or ""))}
        raw = payload.get("data_b64")
        if not isinstance(raw, str):
            raise StetwiseRefusal("data_b64 is required.", "bad_request")
        blob = base64.b64decode(raw)
        if action == "inspect":
            return {"ok": True, "result": inspect(blob, filename)}
        if action == "read":
            return {"ok": True, "result": read(blob, filename, payload)}
        if action == "search":
            return {"ok": True, "result": search(blob, filename, str(payload.get("query") or ""), int(payload.get("limit") or 50))}
        if action == "propose":
            return {"ok": True, "result": propose(blob, filename, payload.get("ops") or [])}
        raise StetwiseRefusal(f"Unknown action {action!r}.", "unknown_action")
    except StetwiseError as exc:
        return {"ok": False, "error": {"code": exc.code, "reason": exc.reason, "started": bool(exc.started)}}
    except Exception:
        return {
            "ok": False,
            "error": {
                "code": "engine_error",
                "reason": "The file engine hit an unexpected error and did not change the file.",
            },
        }
