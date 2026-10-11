"""LibreOffice worker. Reads formula results from a copy. Does not import Stetwise.

Run with a Python that can `import uno` (the engine image's distro Python).
The parent patches those results into the original package.

Result kinds come from the calculated value, not the displayed text. A date
serial stays a number. A currency or percent stays a number. Booleans are
numbers whose number format is logical. Errors are formula-error cells.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

# com.sun.star.util.NumberFormat.LOGICAL
_LOGICAL = 1024
# com.sun.star.sheet.FormulaResult.ERROR
_FORMULA_ERROR = 4


def _port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# LibreOffice FormulaError values that are the Excel error of the same meaning.
# getError() is stable across locales. Err:502, Err:504, and the other Err: codes
# are LibreOffice-only and are not written into an xlsx.
_EXACT_ERROR = {
    503: "#NUM!",
    519: "#VALUE!",
    521: "#NULL!",
    524: "#REF!",
    525: "#NAME?",
    532: "#DIV/0!",
    32767: "#N/A",
}
_EXCEL_ERRORS = set(_EXACT_ERROR.values())


def _num_text(value: float) -> str:
    """Shortest round-trip spelling of the IEEE double, up to 17 significant digits."""
    if value != value or value in (float("inf"), float("-inf")) or value == 0.0:
        return "0"
    if abs(value) < 1e15 and value.is_integer():
        return str(int(value))
    return repr(value)


def _excel_error(code: int, shown: str) -> tuple[str, bool]:
    mapped = _EXACT_ERROR.get(code)
    if mapped:
        return mapped, False
    text = (shown or "").strip()
    if text in _EXCEL_ERRORS:
        return text, False
    if text.startswith("Err:"):
        try:
            mapped = _EXACT_ERROR.get(int(text[4:]))
        except ValueError:
            mapped = None
        if mapped:
            return mapped, False
        return text, True
    label = text or f"Err:{code}"
    return label, True


def _colname(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def _write_pid(proc: subprocess.Popen, profile: Path) -> None:
    path = os.environ.get("STETWISE_RECALC_PIDFILE")
    if not path:
        return
    Path(path).write_text(f"{proc.pid}\n{profile}\n", encoding="utf-8")


def _logical_boxes(doc, sheet) -> list[tuple[int, int, int, int]]:
    """Boxes whose number format is logical. Do not expand them into cells."""
    boxes: list[tuple[int, int, int, int]] = []
    try:
        unique = sheet.getUniqueCellFormatRanges()
        # getPropertyValue("NumberFormats") is empty. The attribute is the format table.
        formats = doc.NumberFormats
        count = int(unique.getCount())
    except Exception:
        return boxes
    for index in range(count):
        try:
            item = unique.getByIndex(index)
            key = int(item.getPropertyValue("NumberFormat"))
            info = formats.getByKey(key)
            flags = int(info.getPropertyValue("Type"))
            if flags & _LOGICAL == 0:
                continue
            for addr in item.getRangeAddresses():
                boxes.append((int(addr.StartColumn), int(addr.StartRow), int(addr.EndColumn), int(addr.EndRow)))
        except Exception:
            continue
    return boxes


def _in_boxes(boxes: list[tuple[int, int, int, int]], col: int, row: int) -> bool:
    for c1, r1, c2, r2 in boxes:
        if c1 <= col <= c2 and r1 <= row <= r2:
            return True
    return False


def _error_cells(rng) -> set[tuple[int, int]]:
    found: set[tuple[int, int]] = set()
    try:
        hits = rng.queryFormulaCells(_FORMULA_ERROR)
    except Exception:
        return found
    try:
        addresses = hits.getRangeAddresses()
    except Exception:
        return found
    for addr in addresses:
        rows = int(addr.EndRow) - int(addr.StartRow) + 1
        cols = int(addr.EndColumn) - int(addr.StartColumn) + 1
        # Error results are individual formula cells. A huge span is not expanded.
        if rows * cols > 8192:
            continue
        for row in range(int(addr.StartRow), int(addr.EndRow) + 1):
            for col in range(int(addr.StartColumn), int(addr.EndColumn) + 1):
                found.add((row, col))
    return found


def _sheet_cells(doc, sheet) -> dict[str, dict[str, str]]:
    cursor = sheet.createCursor()
    cursor.gotoEndOfUsedArea(True)
    rows = int(cursor.Rows.Count)
    cols = int(cursor.Columns.Count)
    if rows < 1 or cols < 1:
        return {}
    rng = sheet.getCellRangeByPosition(0, 0, cols - 1, rows - 1)
    formulas = rng.getFormulaArray()
    data = rng.getDataArray()
    errors = _error_cells(rng)
    logical = _logical_boxes(doc, sheet)
    cells: dict[str, dict[str, str]] = {}
    for row in range(rows):
        formula_row = formulas[row]
        data_row = data[row]
        for col in range(cols):
            formula = formula_row[col]
            if not isinstance(formula, str) or not formula.startswith("="):
                continue
            ref = f"{_colname(col)}{row + 1}"
            if (row, col) in errors:
                cell = sheet.getCellByPosition(col, row)
                code = int(cell.getError())
                shown = str(cell.getString() or "")
                raw, unmapped = _excel_error(code, shown)
                item = {"kind": "e", "raw": raw}
                if unmapped:
                    item["unmapped"] = True
                cells[ref] = item
                continue
            value = data_row[col]
            if isinstance(value, bool):
                cells[ref] = {"kind": "b", "raw": "1" if value else "0"}
            elif isinstance(value, float):
                if _in_boxes(logical, col, row):
                    cells[ref] = {"kind": "b", "raw": "1" if value != 0 else "0"}
                else:
                    cells[ref] = {"kind": "n", "raw": _num_text(value)}
            elif isinstance(value, str):
                if value.startswith("Err:"):
                    raw, unmapped = _excel_error(0, value)
                    item = {"kind": "e", "raw": raw}
                    if unmapped:
                        item["unmapped"] = True
                    cells[ref] = item
                else:
                    cells[ref] = {"kind": "str", "raw": value}
            elif value is None:
                cells[ref] = {"kind": "n", "raw": "0"}
            else:
                cells[ref] = {"kind": "str", "raw": str(value)}
    return cells


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: recalc_uno.py input.xlsx output.json", file=sys.stderr)
        sys.exit(2)
    source = Path(sys.argv[1]).resolve()
    dest = Path(sys.argv[2])
    import uno
    from com.sun.star.beans import PropertyValue

    port = _port()
    profile = Path(os.environ.get("TMPDIR") or "/tmp") / f"stetwise-lo-{port}"
    profile.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [
            os.environ.get("SOFFICE", "soffice"),
            "--headless",
            "--norestore",
            "--nologo",
            "--nofirststartwizard",
            f"-env:UserInstallation=file://{profile}",
            f"--accept=socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext",
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # The pid file exists before any calculation so a parent timeout can reap this group.
    _write_pid(proc, profile)
    hold = os.environ.get("STETWISE_RECALC_HOLD") or ""
    if hold:
        time.sleep(float(hold))
    try:
        local = uno.getComponentContext()
        resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
        ctx = None
        url = f"uno:socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext"
        for _ in range(80):
            try:
                ctx = resolver.resolve(url)
                break
            except Exception:
                time.sleep(0.1)
        if ctx is None:
            raise RuntimeError("LibreOffice did not accept a connection.")
        desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

        def prop(name: str, value):
            item = PropertyValue()
            item.Name = name
            item.Value = value
            return item

        doc = desktop.loadComponentFromURL(source.as_uri(), "_blank", 0, (prop("Hidden", True),))
        if doc is None:
            raise RuntimeError("LibreOffice could not open the workbook copy.")
        doc.calculateAll()
        sheets: dict[str, dict[str, dict[str, str]]] = {}
        for sheet in doc.Sheets:
            sheets[str(sheet.Name)] = _sheet_cells(doc, sheet)
        doc.close(False)
        dest.write_text(json.dumps({"sheets": sheets}), encoding="utf-8")
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
