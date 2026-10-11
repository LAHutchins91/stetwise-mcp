"""LibreOffice worker. Reads formula results from a copy. Does not import Stetwise.

Run with a Python that can `import uno` (the engine image's distro Python).
The parent patches those results into the original package.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def _port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _num_text(value: float) -> str:
    if value != value or value in (float("inf"), float("-inf")):
        return "0"
    if abs(value) < 1e15 and float(value).is_integer():
        return str(int(value))
    return format(value, ".15g")


def _kind(cell) -> tuple[str, str]:
    error = int(cell.getError())
    text = str(cell.getString() or "")
    if error or (text.startswith("#") and text.endswith("!")):
        return "e", text or "#VALUE!"
    if text in {"TRUE", "FALSE"}:
        return "b", "1" if text == "TRUE" else "0"
    value = float(cell.getValue())
    rendered = _num_text(value)
    if text == rendered or text == str(value):
        return "n", rendered
    try:
        if abs(float(text) - value) < 1e-9:
            return "n", rendered
    except ValueError:
        pass
    return "str", text


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
            cells: dict[str, dict[str, str]] = {}
            cursor = sheet.createCursor()
            cursor.gotoEndOfUsedArea(True)
            rows = int(cursor.Rows.Count)
            cols = int(cursor.Columns.Count)
            for row in range(rows):
                for col in range(cols):
                    cell = sheet.getCellByPosition(col, row)
                    formula = str(cell.getFormula() or "")
                    if not formula.startswith("="):
                        continue
                    kind, raw = _kind(cell)
                    ref = cell.AbsoluteName.split(".")[-1].replace("$", "")
                    cells[ref] = {"kind": kind, "raw": raw}
            sheets[str(sheet.Name)] = cells
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
            os.killpg(proc.pid, signal.SIGKILL)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
