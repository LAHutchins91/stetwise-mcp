"""Time inspect, propose, apply, and restore on xlsx and docx samples.

Writes BENCHMARKS.md. Samples are generated in a temp directory and are not committed.
Apply is the server's verify step (compressed-byte diff) on the proposed package.
Restore republishes stored bytes: the worker hashes and copies the original package
and does not parse the XML again.
"""

from __future__ import annotations

import json
import os
import platform
import resource
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = (
    ("small", 100 * 1024),
    ("medium", 2 * 1024 * 1024),
    ("large-5mb", 5 * 1024 * 1024),
    ("large-25mb", 25 * 1024 * 1024),
)
NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_OFF = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_X = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _rss_bytes() -> int:
    # ru_maxrss survives fork/exec and reports the parent that built the sample.
    # VmHWM starts over at exec and is the worker's own peak.
    status = Path("/proc/self/status")
    if status.exists():
        for line in status.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(rss)
    return int(rss) * 1024


def _zip(path: Path, parts: list[tuple[str, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in parts:
            archive.writestr(name, data)


def _xlsx(path: Path, target: int) -> None:
    prefix = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{NS_X}"><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>BENCHCELL</t></is></c></row>'
    )
    suffix = "</sheetData></worksheet>"
    row = '<row r="{n}"><c r="A{n}" t="inlineStr"><is><t>{text}</t></is></c></row>'
    sample = row.format(n=2, text="x" * 48).encode("utf-8")
    budget = max(target // 2, 1024)
    count = max(1, budget // max(len(sample), 1))
    chunks = [prefix.encode("utf-8")]
    for n in range(2, count + 2):
        chunks.append(row.format(n=n, text=f"{n:08d}-" + ("q" * 40)).encode("utf-8"))
    chunks.append(suffix.encode("utf-8"))
    sheet = b"".join(chunks)
    content_types = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{NS_CT}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="bin" ContentType="application/octet-stream"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>
""".encode()
    rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}"><Relationship Id="rId1" Type="{NS_OFF}/officeDocument" Target="xl/workbook.xml"/></Relationships>
""".encode()
    workbook_rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="{NS_OFF}/styles" Target="styles.xml"/>
</Relationships>
""".encode()
    workbook = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{NS_X}" xmlns:r="{NS_OFF}"><sheets><sheet name="Bench" sheetId="1" r:id="rId1"/></sheets></workbook>
""".encode()
    styles = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{NS_X}"><fonts count="1"><font><sz val="11"/></font></fonts><fills count="1"><fill><patternFill patternType="none"/></fill></fills><borders count="1"><border/></borders><cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellXfs></styleSheet>
""".encode()
    parts = [
        ("[Content_Types].xml", content_types),
        ("_rels/.rels", rels),
        ("xl/_rels/workbook.xml.rels", workbook_rels),
        ("xl/workbook.xml", workbook),
        ("xl/styles.xml", styles),
        ("xl/worksheets/sheet1.xml", sheet),
    ]
    _zip(path, parts)
    gap = target - path.stat().st_size
    if gap > 64:
        parts.append(("xl/pad.bin", b"P" * (gap - 64)))
        _zip(path, parts)


def _docx(path: Path, target: int) -> None:
    opening = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{NS_W}"><w:body><w:p><w:r><w:t>BENCHWORD</w:t></w:r></w:p>'
    ).encode()
    closing = b"</w:body></w:document>"
    sample = b'<w:p><w:r><w:t>00000002-' + (b"m" * 40) + b"</w:t></w:r></w:p>"
    count = max(1, (max(target // 2, 1024)) // max(len(sample), 1))
    chunks = [opening]
    for n in range(2, count + 2):
        token = f"{n:08d}-".encode() + b"m" * 40
        chunks.append(b"<w:p><w:r><w:t>" + token + b"</w:t></w:r></w:p>")
    chunks.append(closing)
    document = b"".join(chunks)
    content_types = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{NS_CT}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="bin" ContentType="application/octet-stream"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
""".encode()
    rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}"><Relationship Id="rId1" Type="{NS_OFF}/officeDocument" Target="word/document.xml"/></Relationships>
""".encode()
    parts = [("[Content_Types].xml", content_types), ("_rels/.rels", rels), ("word/document.xml", document)]
    _zip(path, parts)
    gap = target - path.stat().st_size
    if gap > 64:
        parts.append(("word/pad.bin", b"P" * (gap - 64)))
        _zip(path, parts)


def _run_worker(op: str, args: list[str]) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT / 'engine'}:{ROOT / 'engine' / 'tests'}"
    started = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker", op, *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    wall = time.perf_counter() - started
    line = (completed.stdout or "").strip().splitlines()
    payload = {}
    if line:
        try:
            payload = json.loads(line[-1])
        except json.JSONDecodeError:
            payload = {}
    if completed.returncode != 0 or not payload:
        payload = {
            "ok": False,
            "cpu_seconds": payload.get("cpu_seconds"),
            "peak_rss_bytes": payload.get("peak_rss_bytes"),
            "wall_seconds": round(wall, 4),
            "error": (completed.stderr or completed.stdout or "worker failed")[-500:],
        }
    payload.setdefault("wall_seconds", round(wall, 4))
    return payload


def _worker(op: str, paths: list[str]) -> None:
    os.environ.setdefault("PYTHONPATH", f"{ROOT / 'engine'}")
    sys.path.insert(0, str(ROOT / "engine"))
    from stetwise.engine import inspect, propose
    from stetwise.verify import assert_allowed, diff_parts

    def propose_file(blob: bytes, name: str) -> dict:
        if name.endswith(".docx"):
            ops = [{"op": "replace_text", "find": "BENCHWORD", "replace": "BENCHED"}]
        else:
            ops = [{"op": "set_cells", "sheet": "Bench", "cells": [{"ref": "A1", "value": "BENCHED"}]}]
        return propose(blob, name, ops)

    cpu_start = time.process_time()
    error = ""
    ok = True
    try:
        if op == "baseline":
            pass
        elif op == "inspect":
            blob = Path(paths[0]).read_bytes()
            inspect(blob, Path(paths[0]).name)
        elif op == "propose":
            blob = Path(paths[0]).read_bytes()
            result = propose_file(blob, Path(paths[0]).name)
            import base64

            Path(paths[1]).write_bytes(base64.b64decode(result["data_b64"]))
        elif op == "apply":
            before = Path(paths[0]).read_bytes()
            after = Path(paths[1]).read_bytes()
            kind = "docx" if paths[0].endswith(".docx") else "xlsx"
            report = diff_parts(before, after)
            assert_allowed(report, kind)
        elif op == "restore":
            blob = Path(paths[0]).read_bytes()
            copy = bytes(blob)
            if len(copy) != len(blob):
                raise RuntimeError("restore copy length changed")
            import hashlib

            hashlib.sha256(copy).hexdigest()
        elif op == "recalc":
            from stetwise.recalc import recalculate
            from stetwise.zippack import read_package

            blob = Path(paths[0]).read_bytes()
            name = Path(paths[0]).name
            recalculate(blob, name, read_package(blob))
        else:
            raise RuntimeError(f"unknown op {op}")
    except Exception as exc:
        ok = False
        error = f"{type(exc).__name__}: {exc}"
    print(json.dumps({
        "ok": ok,
        "cpu_seconds": round(time.process_time() - cpu_start, 4),
        "peak_rss_bytes": _rss_bytes(),
        "error": error,
    }))
    if not ok:
        sys.exit(1)


def _fmt_mib(value: int | None) -> str:
    if value is None:
        return ""
    return f"{value / (1024 * 1024):.1f}"


def _fmt_cpu(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.4f}"


_FORMULA_COLS = "ABCDEFGHIJ"
# Ten columns of this formula land near 25 MB at about 250 thousand cells.
_FORMULA = "ROW()+0*12+0*13+0*14+0*15+0*16+0*17+0*18+0*19+0*20"


def _formula_row(number: int) -> bytes:
    cells = "".join(f'<c r="{col}{number}"><f>{_FORMULA}</f></c>' for col in _FORMULA_COLS)
    return f'<row r="{number}">{cells}</row>'.encode("utf-8")


def _formula_xlsx(path: Path, target: int) -> int:
    """ZIP_STORED workbook of formula cells, sized to at least `target` bytes. Returns the formula count."""
    prefix = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{NS_X}"><sheetData>'
    ).encode("utf-8")
    suffix = b"</sheetData></worksheet>"
    content_types = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{NS_CT}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>
""".encode()
    rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}"><Relationship Id="rId1" Type="{NS_OFF}/officeDocument" Target="xl/workbook.xml"/></Relationships>
""".encode()
    workbook_rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{NS_REL}">
  <Relationship Id="rId1" Type="{NS_OFF}/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="{NS_OFF}/styles" Target="styles.xml"/>
</Relationships>
""".encode()
    workbook = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{NS_X}" xmlns:r="{NS_OFF}"><sheets><sheet name="Bench" sheetId="1" r:id="rId1"/></sheets></workbook>
""".encode()
    styles = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{NS_X}"><fonts count="1"><font><sz val="11"/></font></fonts><fills count="1"><fill><patternFill patternType="none"/></fill></fills><borders count="1"><border/></borders><cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellXfs></styleSheet>
""".encode()
    overhead = len(content_types) + len(rels) + len(workbook_rels) + len(workbook) + len(styles) + len(prefix) + len(suffix) + 512
    row = _formula_row(100000)
    per_row = max(len(row), 1)
    rows = max(1, (target - overhead + per_row - 1) // per_row)
    chunks = [prefix]
    for number in range(1, rows + 1):
        chunks.append(_formula_row(number))
    chunks.append(suffix)
    parts = [
        ("[Content_Types].xml", content_types),
        ("_rels/.rels", rels),
        ("xl/_rels/workbook.xml.rels", workbook_rels),
        ("xl/workbook.xml", workbook),
        ("xl/styles.xml", styles),
        ("xl/worksheets/sheet1.xml", b"".join(chunks)),
    ]
    _zip(path, parts)
    return rows * len(_FORMULA_COLS)


def _proc_rss_cpu(pid: int) -> tuple[int, int]:
    """Resident bytes and cumulative CPU ticks for one process. (0, 0) if it has exited."""
    status = Path(f"/proc/{pid}/status")
    stat = Path(f"/proc/{pid}/stat")
    try:
        rss = 0
        for line in status.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * 1024
                break
        text = stat.read_text(encoding="utf-8")
        fields = text[text.rfind(")") + 2 :].split()
        ticks = int(fields[11]) + int(fields[12])
    except (OSError, IndexError, ValueError):
        return 0, 0
    return rss, ticks


def _children_of() -> dict[int, list[int]]:
    groups: dict[int, list[int]] = {}
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text(encoding="utf-8")
            fields = text[text.rfind(")") + 2 :].split()
            ppid = int(fields[1])
        except (OSError, IndexError, ValueError):
            continue
        groups.setdefault(ppid, []).append(int(entry.name))
    return groups


def _tree(root: int, groups: dict[int, list[int]]) -> list[int]:
    found = [root]
    stack = [root]
    while stack:
        current = stack.pop()
        for child in groups.get(current, []):
            found.append(child)
            stack.append(child)
    return found


def _run_recalc(sample: Path) -> dict:
    """Time recalculate(), including LibreOffice. The product timeout stays at its default."""
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT / 'engine'}:{ROOT / 'engine' / 'tests'}"
    env.pop("STETWISE_RECALC_TIMEOUT", None)
    env.pop("STETWISE_RECALC_HOLD", None)
    started = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--worker", "recalc", str(sample)],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    cpu_ticks: dict[int, int] = {}
    peak = 0
    while proc.poll() is None:
        groups = _children_of()
        resident = 0
        for pid in _tree(proc.pid, groups):
            rss, ticks = _proc_rss_cpu(pid)
            resident += rss
            if ticks:
                cpu_ticks[pid] = max(cpu_ticks.get(pid, 0), ticks)
        peak = max(peak, resident)
        time.sleep(0.1)
    stdout, stderr = proc.communicate()
    wall = time.perf_counter() - started
    payload: dict = {}
    line = (stdout or "").strip().splitlines()
    if line:
        try:
            payload = json.loads(line[-1])
        except json.JSONDecodeError:
            payload = {}
    if proc.returncode != 0 or not payload:
        payload = {
            "ok": False,
            "error": (stderr or stdout or "worker failed")[-500:],
        }
    clk = os.sysconf("SC_CLK_TCK") or 100
    payload["cpu_seconds"] = round(sum(cpu_ticks.values()) / clk, 4)
    payload["peak_rss_bytes"] = peak
    payload["wall_seconds"] = round(wall, 4)
    return payload


def _recalc_lines(rows: list[tuple[str, int, int, dict]]) -> list[str]:
    lines = [
        "",
        "## Recalculate",
        "",
        "Formula workbooks are ZIP_STORED. Each cell is `ROW()` plus a few zero terms, ten columns wide, with no cached value. `recalculate` starts LibreOffice, reads formula results in bulk, and patches cached values in one pass. The product time limit stays 60 seconds.",
        "",
        "CPU seconds are the process tree (the worker plus LibreOffice) sampled while the call runs. Peak MiB is the highest resident set of that tree at one sample. Wall seconds include LibreOffice startup, calculation, and the XML patch.",
        "",
        "| Sample | Bytes | Formulas | OK | CPU seconds | Peak MiB | Wall seconds |",
        "|---|---:|---:|---|---:|---:|---:|",
    ]
    failures = []
    for label, size, formulas, result in rows:
        lines.append(
            "| {label} | {size} | {formulas} | {ok} | {cpu} | {rss} | {wall} |".format(
                label=label,
                size=size,
                formulas=formulas,
                ok="yes" if result.get("ok") else "no",
                cpu=_fmt_cpu(result.get("cpu_seconds")),
                rss=_fmt_mib(result.get("peak_rss_bytes")),
                wall=_fmt_cpu(result.get("wall_seconds")),
            )
        )
        if not result.get("ok"):
            failures.append(f"- {label} recalc: {result.get('error', 'failed')}")
    if failures:
        lines.extend(["", "### Recalculate failures", "", *failures])
    else:
        lines.extend(["", "Every recalculation finished inside the product time limit."])
    return lines


def _measure_recalc() -> list[tuple[str, int, int, dict]]:
    rows = []
    with tempfile.TemporaryDirectory(prefix="stetwise-recalc-bench-") as tmp:
        folder = Path(tmp)
        for label, target in TARGETS:
            sample = folder / f"{label}.xlsx"
            formulas = _formula_xlsx(sample, target)
            size = sample.stat().st_size
            result = _run_recalc(sample)
            rows.append((label, size, formulas, result))
            print(f"recalc {label} {size} formulas={formulas} ok={result.get('ok')} wall={result.get('wall_seconds')}", file=sys.stderr)
    return rows


def _write_recalc_section(rows: list[tuple[str, int, int, dict]]) -> None:
    path = ROOT / "BENCHMARKS.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else "# Stetwise benchmarks\n"
    marker = "\n## Recalculate\n"
    if marker in existing:
        existing = existing.split(marker)[0].rstrip() + "\n"
    section = "\n".join(_recalc_lines(rows)).rstrip() + "\n"
    path.write_text(existing.rstrip() + "\n" + section, encoding="utf-8")
    print(path)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        _worker(sys.argv[2], sys.argv[3:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--recalc-only":
        _write_recalc_section(_measure_recalc())
        return
    rows = []
    with tempfile.TemporaryDirectory(prefix="stetwise-bench-") as tmp:
        folder = Path(tmp)
        for label, target in TARGETS:
            for kind, builder in (("xlsx", _xlsx), ("docx", _docx)):
                sample = folder / f"{label}.{kind}"
                builder(sample, target)
                size = sample.stat().st_size
                after = folder / f"{label}.{kind}.after"
                propose = _run_worker("propose", [str(sample), str(after)])
                measured = {
                    "inspect": _run_worker("inspect", [str(sample)]),
                    "propose": propose,
                    "apply": _run_worker("apply", [str(sample), str(after)]) if propose.get("ok") and after.exists() else {"ok": False, "error": "propose did not write a package"},
                    "restore": _run_worker("restore", [str(sample)]),
                }
                rows.append((label, kind, size, measured))
                print(f"{label} {kind} {size} done", file=sys.stderr)
        baseline = _run_worker("baseline", [])
    mem = ""
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem = line.split()[1] + " kB"
                break
    lines = [
        "# Stetwise benchmarks",
        "",
        "One run of `python3 scripts/benchmark.py` on this machine. These are measurements, not a capacity promise.",
        "No prices are recorded here. The numbers are the CPU time and peak memory of one file operation, so a monthly edit cap can be compared with CPU time.",
        "",
        f"- Python {platform.python_version()} on {platform.platform()}",
        f"- CPUs: {os.cpu_count()}",
        f"- MemTotal: {mem or 'unavailable'}",
        f"- Engine import baseline peak RSS: {_fmt_mib(baseline.get('peak_rss_bytes'))} MiB",
        "",
        "Samples are ZIP_STORED packages. Most of the file is worksheet or document XML, padded so the package lands on the target size. The edit is one cell (`A1` on sheet `Bench`) or one Word token (`BENCHWORD`).",
        "",
        "- `inspect` parses the package and lists sheets or paragraphs.",
        "- `propose` builds the edited package.",
        "- `apply` is the verify step the server runs before publishing: it compares compressed part bytes and refuses an edit that touched a part it should have left alone. It does not repeat propose.",
        "- `restore` copies the stored original bytes and hashes them. It does not parse XML. Publishing the restored version is a metadata write.",
        "",
        "CPU seconds are `time.process_time` inside the worker, after imports. Peak memory is the worker's VmHWM after the operation, including the interpreter. That counter starts when the worker process starts, so it is not the parent that built the sample.",
        "",
        "| Sample | Kind | Bytes | Op | OK | CPU seconds | Peak MiB | Wall seconds |",
        "|---|---|---:|---|---|---:|---:|---:|",
    ]
    for label, kind, size, measured in rows:
        for op, result in measured.items():
            lines.append(
                "| {label} | {kind} | {size} | {op} | {ok} | {cpu} | {rss} | {wall} |".format(
                    label=label,
                    kind=kind,
                    size=size,
                    op=op,
                    ok="yes" if result.get("ok") else "no",
                    cpu=_fmt_cpu(result.get("cpu_seconds")),
                    rss=_fmt_mib(result.get("peak_rss_bytes")),
                    wall=_fmt_cpu(result.get("wall_seconds")),
                )
            )
    failures = []
    for label, kind, _size, measured in rows:
        for op, result in measured.items():
            if not result.get("ok"):
                failures.append(f"- {label} {kind} {op}: {result.get('error', 'failed')}")
    if failures:
        lines.extend(["", "## Failures", "", *failures])
    else:
        lines.extend(["", "Every measured operation completed."])
    lines.extend(_recalc_lines(_measure_recalc()))
    lines.append("")
    out = ROOT / "BENCHMARKS.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
