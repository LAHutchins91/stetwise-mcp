"""Recalculate on a copy, then patch cached values into the original package.

LibreOffice rewrites a workbook when it saves. That save is discarded.
Only formula cached values in the original sheet XML change.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import xlsx
from .errors import StetwiseRefusal
from .xmlutil import parse_xml, xml_escape
from .zippack import Package

_F_TAG = None
_EXCEL_ERRORS = {"#DIV/0!", "#N/A", "#NAME?", "#NULL!", "#NUM!", "#REF!", "#VALUE!"}


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


def _patch_many(xml: bytes, updates: dict[str, tuple[str, str]]) -> bytes:
    """Splice every stale cache in one walk. Formula bytes inside each cell stay put."""
    if not updates:
        return xml
    pieces: list[bytes] = []
    cursor = 0
    seen: set[str] = set()
    for start, end, open_tag, inner in xlsx._iter_tag_spans(xml, xlsx._CELL_OPEN, b"c"):
        ref = dict(xlsx._attrs(open_tag)).get("r")
        found = updates.get(ref or "")
        if found is None:
            continue
        if b"<f" not in inner:
            raise StetwiseRefusal(
                f"Cell {ref} is not a formula cell in the original sheet, so its cached value was left alone.",
                "recalc_failed",
                started=True,
            )
        kind, raw = found
        t_value = {"e": "e", "str": "str", "b": "b"}.get(kind)
        attrs = xlsx._set_attr(xlsx._attrs(open_tag), "t", t_value)
        inner = _drop_value(inner)
        updated = xlsx._render_cell(attrs, inner + f"<v>{xml_escape(raw)}</v>".encode("utf-8"))
        pieces.append(xml[cursor:start])
        pieces.append(updated)
        cursor = end
        seen.add(ref or "")
    missing = next((ref for ref in updates if ref not in seen), None)
    if missing is not None:
        raise StetwiseRefusal(
            f"Cell {missing} was not found while writing a cached value.",
            "recalc_failed",
            started=True,
        )
    pieces.append(xml[cursor:])
    return b"".join(pieces)


def _remove_profile(path: str) -> None:
    """Delete one LibreOffice profile directory. A timeout SIGKILLs the worker before its own cleanup."""
    if not path:
        return
    folder = Path(path)
    name = folder.name
    if not name.startswith("stetwise-lo-") or name in {".", ".."} or "/" in name:
        return
    root = Path(os.environ.get("TMPDIR") or "/tmp")
    try:
        resolved = folder.resolve()
        allowed = root.resolve()
    except OSError:
        return
    if resolved.parent != allowed or not resolved.is_dir():
        return
    shutil.rmtree(resolved, ignore_errors=True)


def _reap(pid_path: Path) -> None:
    """Stop the soffice process group. The worker is SIGKILLed on timeout, so its finally does not run."""
    if not pid_path.exists():
        return
    lines = pid_path.read_text(encoding="utf-8").splitlines()
    pid = 0
    token = lines[1].strip() if len(lines) > 1 else ""
    try:
        pid = int(lines[0].strip()) if lines else 0
    except ValueError:
        pid = 0
    if pid > 1:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.15)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
    if token:
        _kill_cmdline(token)
    _remove_profile(token)


def _kill_cmdline(token: str) -> None:
    proc = Path("/proc")
    if not proc.is_dir():
        return
    needle = token.encode("utf-8")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if needle not in command:
            continue
        try:
            os.kill(int(entry.name), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _unmapped_errors(calculated: dict) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for sheet, cells in calculated.items():
        if not isinstance(cells, dict):
            continue
        for ref, item in cells.items():
            if not isinstance(item, dict):
                continue
            raw = str(item.get("raw") or "")
            if item.get("unmapped") or (item.get("kind") == "e" and raw not in _EXCEL_ERRORS):
                found.append((str(sheet), str(ref), raw or "Err"))
    return found


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
    timeout = float(os.environ.get("STETWISE_RECALC_TIMEOUT") or "60")
    with tempfile.TemporaryDirectory(prefix="stetwise-recalc-") as tmp:
        folder = Path(tmp)
        source = folder / "in.xlsx"
        report_path = folder / "out.json"
        pid_path = folder / "soffice.pid"
        source.write_bytes(blob)
        env = {**os.environ, "HOME": str(folder), "STETWISE_RECALC_PIDFILE": str(pid_path)}
        try:
            completed = subprocess.run(
                [python, str(worker), str(source), str(report_path)],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            _reap(pid_path)
            raise StetwiseRefusal(
                "LibreOffice did not finish recalculating within the time limit. The LibreOffice process was stopped.",
                "recalc_failed",
                started=True,
            )
        started = pid_path.exists()
        _reap(pid_path)
        if completed.returncode != 0 or not report_path.exists():
            detail = (completed.stderr or completed.stdout or "LibreOffice failed").strip().splitlines()
            tail = detail[-1][:240] if detail else "LibreOffice failed"
            raise StetwiseRefusal(
                f"LibreOffice could not recalculate this workbook. {tail}",
                "recalc_failed",
                started=started,
            )
        report = json.loads(report_path.read_text(encoding="utf-8"))
    calculated = report.get("sheets") or {}
    blocked = _unmapped_errors(calculated)
    if blocked:
        sheet, ref, raw = blocked[0]
        extra = f" {len(blocked)} formula cells had a LibreOffice error that is not an Excel error." if len(blocked) > 1 else ""
        raise StetwiseRefusal(
            f"LibreOffice returned {raw} for {sheet}!{ref}. That is not an Excel error value "
            f"(#DIV/0!, #N/A, #NAME?, #NULL!, #NUM!, #REF!, or #VALUE!).{extra} No cached values were written.",
            "recalc_failed",
            started=True,
        )
    original_names = {sheet.name for sheet in xlsx.sheet_map(package)}
    if not original_names.issubset(set(calculated)):
        missing = sorted(original_names - set(calculated))
        raise StetwiseRefusal(
            f"LibreOffice did not return sheet {missing[0]!r}, so no cached values were written.",
            "recalc_failed",
            started=True,
        )
    strings = xlsx._shared_strings(package)
    formats = xlsx._styles(package)
    changes = []
    replacements: dict[str, bytes] = {}
    for sheet in xlsx.sheet_map(package):
        xml = package.parts()[sheet.path]
        root = parse_xml(xml, sheet.path)
        updates: dict[str, tuple[str, str]] = {}
        for cell in xlsx._read_cells(root, strings, formats):
            if cell.get("formula") is None:
                continue
            found = calculated.get(sheet.name, {}).get(cell["ref"])
            if not found:
                continue
            kind = found["kind"]
            raw = found["raw"]
            if kind == "e" and raw not in _EXCEL_ERRORS:
                raise StetwiseRefusal(
                    f"LibreOffice returned {raw} for {sheet.name}!{cell['ref']}. That is not an Excel error value. No cached values were written.",
                    "recalc_failed",
                    started=True,
                )
            old_type = "e" if cell.get("type") == "e" else ("str" if cell.get("type") in {"str", "inlineStr", "s"} else "n")
            if cell.get("type") == "b":
                old_type = "b"
            if _same(None if cell.get("raw_value") is None else str(cell.get("raw_value")), old_type, kind, raw):
                continue
            updates[cell["ref"]] = (kind, raw)
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
        if updates:
            replacements[sheet.path] = _patch_many(xml, updates)
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
            "Cached values were calculated by LibreOffice's engine on a copy. Results may differ from Excel on edge cases. The original package was not replaced. Excel may recalculate again on open."
        ],
    )
    preview["changed"] = True
    preview["label"] = "LibreOffice-calculated"
    return preview
