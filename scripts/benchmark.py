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


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        _worker(sys.argv[2], sys.argv[3:])
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
    lines.append("")
    out = ROOT / "BENCHMARKS.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
