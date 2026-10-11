"""Recalculate on a copy, then patch cached values into the original package.

LibreOffice rewrites a workbook when it saves. That save is discarded.
Only formula cached values in the original sheet XML change.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import xlsx
from .errors import StetwiseRefusal
from .xmlutil import parse_xml, xml_escape
from .zippack import Package

_F_TAG = None


def _uno_python() -> str:
    candidates = [sys.executable, "/usr/bin/python3"]
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if shutil.which(candidate) is None and candidate != sys.executable:
            continue
        probe = subprocess.run(
            [candidate, "-c", "import uno"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    raise StetwiseRefusal(
        "LibreOffice's Python bridge is not installed in this engine, so recalculation cannot run.",
        "recalc_unavailable",
    )


def _has_formula(package: Package) -> bool:
    for sheet in xlsx.sheet_map(package):
        xml = package.parts()[sheet.path]
        if b"<f" in xml:
            return True
    return False


def _same(old_raw: str | None, old_type: str | None, kind: str, raw: str) -> bool:
    old = "" if old_raw is None else str(old_raw)
    if old == raw and (old_type or "n") == kind:
        return True
    if kind == "n" and (old_type in {None, "", "n"}):
        try:
            return float(old) == float(raw)
        except ValueError:
            return False
    return False


def _patch_cache(xml: bytes, ref: str, kind: str, raw: str) -> bytes:
    cells = xlsx._iter_tag_spans(xml, xlsx._CELL_OPEN, b"c")
    for start, end, open_tag, inner in cells:
        attrs = xlsx._attrs(open_tag)
        if dict(attrs).get("r") != ref:
            continue
        if b"<f" not in inner:
            raise StetwiseRefusal(
                f"Cell {ref} is not a formula cell in the original sheet, so its cached value was left alone.",
                "recalc_failed",
            )
        t_value = {"e": "e", "str": "str", "b": "b"}.get(kind)
        attrs = xlsx._set_attr(attrs, "t", t_value)
        inner = _drop_value(inner)
        escaped = xml_escape(raw)
        updated = open_tag if False else xlsx._render_cell(attrs, inner + f"<v>{escaped}</v>".encode("utf-8"))
        # _render_cell rebuilds the open tag. The formula bytes inside `inner` stay as they were.
        return xml[:start] + updated + xml[end:]
    raise StetwiseRefusal(f"Cell {ref} was not found while writing a cached value.", "recalc_failed")


def _drop_value(inner: bytes) -> bytes:
    import re

    inner = re.sub(br"<v\b[^>]*>.*?</v>", b"", inner, count=1, flags=re.DOTALL)
    inner = re.sub(br"<is\b[^>]*>.*?</is>", b"", inner, count=1, flags=re.DOTALL)
    return inner


def recalculate(blob: bytes, filename: str, package: Package) -> dict:
    lower = filename.lower()
    if lower.endswith(".docx") or "word/document.xml" in package.parts():
        raise StetwiseRefusal("recalculate is for workbooks. This file is a Word document.", "wrong_kind")
    if not _has_formula(package):
        raise StetwiseRefusal("This workbook has no formulas to recalculate.", "no_formulas")
    if shutil.which(os.environ.get("SOFFICE", "soffice")) is None:
        raise StetwiseRefusal(
            "LibreOffice is not installed in this engine, so recalculation cannot run.",
            "recalc_unavailable",
        )
    python = _uno_python()
    worker = Path(__file__).with_name("recalc_uno.py")
    with tempfile.TemporaryDirectory(prefix="stetwise-recalc-") as tmp:
        folder = Path(tmp)
        source = folder / "in.xlsx"
        report_path = folder / "out.json"
        source.write_bytes(blob)
        completed = subprocess.run(
            [python, str(worker), str(source), str(report_path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={**os.environ, "HOME": str(folder)},
        )
        if completed.returncode != 0 or not report_path.exists():
            detail = (completed.stderr or completed.stdout or "LibreOffice failed").strip().splitlines()
            tail = detail[-1][:240] if detail else "LibreOffice failed"
            raise StetwiseRefusal(
                f"LibreOffice could not recalculate this workbook. {tail}",
                "recalc_failed",
            )
        report = json.loads(report_path.read_text(encoding="utf-8"))
    calculated = report.get("sheets") or {}
    original_names = {sheet.name for sheet in xlsx.sheet_map(package)}
    if not original_names.issubset(set(calculated)):
        missing = sorted(original_names - set(calculated))
        raise StetwiseRefusal(
            f"LibreOffice did not return sheet {missing[0]!r}, so no cached values were written.",
            "recalc_failed",
        )
    strings = xlsx._shared_strings(package)
    formats = xlsx._styles(package)
    changes = []
    replacements: dict[str, bytes] = {}
    for sheet in xlsx.sheet_map(package):
        xml = package.parts()[sheet.path]
        root = parse_xml(xml, sheet.path)
        current = xml
        for cell in xlsx._read_cells(root, strings, formats):
            if cell.get("formula") is None:
                continue
            found = calculated.get(sheet.name, {}).get(cell["ref"])
            if not found:
                continue
            kind = found["kind"]
            raw = found["raw"]
            old_type = "e" if cell.get("type") == "e" else ("str" if cell.get("type") in {"str", "inlineStr", "s"} else "n")
            if cell.get("type") == "b":
                old_type = "b"
            if _same(None if cell.get("raw_value") is None else str(cell.get("raw_value")), old_type, kind, raw):
                continue
            current = _patch_cache(current, cell["ref"], kind, raw)
            changes.append(
                {
                    "sheet": sheet.name,
                    "ref": cell["ref"],
                    "formula": cell.get("formula"),
                    "before": cell.get("raw_value"),
                    "after": raw,
                    "kind": kind,
                    "source": "LibreOffice",
                }
            )
        if current != xml:
            replacements[sheet.path] = current
    from .engine import propose_parts

    if not changes:
        return {
            "changed": False,
            "diff": [],
            "warnings": ["LibreOffice recalculated this workbook. Every cached formula value already matched."],
            "label": "LibreOffice-calculated",
        }
    preview = propose_parts(
        blob,
        filename,
        replacements,
        set(),
        [{"op": "recalculate", "source": "LibreOffice", "changes": changes}],
        [
            "Cached values were calculated by LibreOffice on a copy. The original package was not replaced. Excel may recalculate again on open."
        ],
    )
    preview["changed"] = True
    preview["label"] = "LibreOffice-calculated"
    return preview
