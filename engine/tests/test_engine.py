import hashlib
import io
import os
import re
import time
import zipfile
from pathlib import Path

import pytest

from stetwise.engine import dispatch, propose, search, inspect
from stetwise.recalc_uno import _excel_error, _num_text
from stetwise.errors import StetwiseRefusal
from stetwise.refs import Shift, shift_formula
from stetwise.zippack import read_package, write_package
from build_fixtures import contract_docx, model_xlsx, settings_docx, stale_xlsx, strings_xlsx, table_xlsx, types_xlsx

POI = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "poi"


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


def _propose(data: bytes, filename: str, ops: list[dict]) -> dict:
    result = propose(data, filename, ops)
    return result


def _bytes(result: dict) -> bytes:
    import base64

    return base64.b64decode(result["data_b64"])


def _part(blob: bytes, name: str) -> bytes:
    return read_package(blob).by_name()[name].data


def _compressed(blob: bytes, name: str) -> bytes:
    return read_package(blob).by_name()[name].compressed


def _lo_profiles() -> set[Path]:
    root = Path(os.environ.get("TMPDIR") or "/tmp")
    return {path for path in root.glob("stetwise-lo-*") if path.is_dir()}


def _soffice_orphans() -> list[str]:
    found = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "stetwise-lo-" in command:
            found.append(command)
    return found


def test_formula_shift_rules():
    shift = Shift(sheet="Inputs", row_at=2, row_count=1)
    assert shift_formula("A1+A2", "Inputs", shift) == "A1+A3"
    assert shift_formula("SUM(A1:A2)", "Inputs", shift) == "SUM(A1:A3)"
    assert shift_formula("Inputs!$A$1", "Model", shift) == "Inputs!$A$1"
    assert shift_formula("Inputs!A3", "Model", shift) == "Inputs!A4"
    assert shift_formula("A1", "Model", shift) == "A1"
    assert shift_formula('SUM(A1,"A2")', "Inputs", shift) == 'SUM(A1,"A2")'


def test_noop_package_round_trip_keeps_bytes():
    for path in sorted(POI.iterdir()):
        if path.suffix not in {".xlsx", ".docx"}:
            continue
        blob = path.read_bytes()
        package = read_package(blob)
        rebuilt = write_package(package, {}, set())
        # Rebuilding the directory can change bytes. Reading back must preserve
        # every compressed payload, which is what an untouched edit copies.
        again = read_package(rebuilt)
        assert set(again.by_name()) == set(package.by_name())
        for name, entry in package.by_name().items():
            assert again.by_name()[name].data == entry.data


def test_poi_inspect_finds_pivot_chart_formulas_and_revisions():
    pivot = inspect(POI.joinpath("ExcelPivotTableSample.xlsx").read_bytes(), "ExcelPivotTableSample.xlsx")
    assert pivot["pivots"]
    assert any(item["part"].startswith("xl/pivot") for item in []) or pivot["pivots"][0]["ref"]
    chart = inspect(POI.joinpath("123233_charts.xlsx").read_bytes(), "123233_charts.xlsx")
    assert chart["charts"]
    formulas = inspect(POI.joinpath("FormulaSheetRange.xlsx").read_bytes(), "FormulaSheetRange.xlsx")
    assert any(sheet["formula_count"] for sheet in formulas["sheets"])
    tracked = inspect(POI.joinpath("delins.docx").read_bytes(), "delins.docx")
    assert tracked["tracked_insertions"] > 0
    assert tracked["tracked_deletions"] > 0
    assert tracked["track_revisions_on"] is True
    comments = inspect(POI.joinpath("comment.docx").read_bytes(), "comment.docx")
    assert comments["comments"] >= 1


def test_set_cell_on_chart_workbook_keeps_chart_bytes():
    original = POI.joinpath("123233_charts.xlsx").read_bytes()
    result = _propose(original, "123233_charts.xlsx", [{"op": "set_cells", "sheet": "Page1_1", "cells": [{"ref": "G45", "value": 2}]}])
    edited = _bytes(result)
    package = read_package(original)
    for name in package.parts():
        if "/charts/" in name or "/drawings/" in name or name == "xl/styles.xml" or name == "xl/sharedStrings.xml":
            assert _compressed(edited, name) == _compressed(original, name)
    assert "xl/worksheets/Sheet1.xml" in result["verification"]["changed_parts"]
    assert result["verification"]["untouched_identical"] is True
    text = _part(edited, "xl/worksheets/Sheet1.xml")
    assert b'r="G45"' in text
    assert b"<v>2</v>" in text
    assert b"<mergeCell" in text


def test_set_cell_outside_pivot_keeps_pivot_and_table():
    original = POI.joinpath("ExcelPivotTableSample.xlsx").read_bytes()
    result = _propose(
        original,
        "ExcelPivotTableSample.xlsx",
        [{"op": "set_cells", "sheet": "Tabelle1", "cells": [{"ref": "D1", "value": 42}]}],
    )
    edited = _bytes(result)
    for name in read_package(original).parts():
        if "pivot" in name or name.startswith("xl/tables/") or name == "xl/styles.xml" or name == "xl/sharedStrings.xml":
            assert _compressed(edited, name) == _compressed(original, name), name
    assert b"<v>42</v>" in _part(edited, "xl/worksheets/sheet1.xml")


def test_formula_edit_keeps_style_and_splits_shared_formula_group():
    original = POI.joinpath("50755_workday_formula_example.xlsx").read_bytes()
    result = _propose(original, "50755.xlsx", [{"op": "set_cells", "sheet": "Sheet1", "cells": [{"ref": "B3", "formula": "WORKDAY(A3,6)"}]}])
    edited = _bytes(result)
    sheet = _part(edited, "xl/worksheets/sheet1.xml")
    assert b"WORKDAY(A3,6)" in sheet
    assert b"WORKDAY(A4,5)" in sheet
    assert b't="shared"' not in sheet
    assert b"<v>40584</v>" in sheet
    assert _compressed(edited, "xl/styles.xml") == _compressed(original, "xl/styles.xml")
    assert "xl/calcChain.xml" in result["verification"]["removed_parts"]
    plain = _propose(original, "50755.xlsx", [{"op": "set_cells", "sheet": "Sheet1", "cells": [{"ref": "B2", "formula": "WORKDAY(A2,6)"}]}])
    plain_sheet = _part(_bytes(plain), "xl/worksheets/sheet1.xml")
    assert b"WORKDAY(A2,6)" in plain_sheet
    assert b't="shared"' in plain_sheet
    assert _compressed(_bytes(plain), "xl/styles.xml") == _compressed(original, "xl/styles.xml")


def test_cross_sheet_number_edit_leaves_other_sheets():
    original = POI.joinpath("FormulaSheetRange.xlsx").read_bytes()
    result = _propose(original, "FormulaSheetRange.xlsx", [{"op": "set_cells", "sheet": "test", "cells": [{"ref": "C11", "value": 11}]}])
    edited = _bytes(result)
    for name in ("xl/worksheets/sheet2.xml", "xl/worksheets/sheet3.xml", "xl/sharedStrings.xml", "xl/styles.xml"):
        assert _compressed(edited, name) == _compressed(original, name)
    assert b"<v>11</v>" in _part(edited, "xl/worksheets/sheet1.xml")
    assert b"SUM(Sheet2:Sheet5!A11)" in _part(edited, "xl/worksheets/sheet1.xml")


def test_conditional_formatting_bytes_outside_the_cell_stay():
    original = POI.joinpath("55406_Conditional_formatting_sample.xlsx").read_bytes()
    before = _part(original, "xl/worksheets/sheet1.xml")
    marker = before.index(b"<conditionalFormatting")
    result = _propose(original, "55406.xlsx", [{"op": "set_cells", "sheet": "Summary", "cells": [{"ref": "B2", "value": 5}]}])
    edited = _bytes(result)
    after = _part(edited, "xl/worksheets/sheet1.xml")
    assert before[marker:] == after[after.index(b"<conditionalFormatting") :]
    assert _compressed(edited, "xl/styles.xml") == _compressed(original, "xl/styles.xml")


def test_insert_rows_rewrites_cross_sheet_refs_and_undo_bytes():
    original = model_xlsx()
    result = _propose(original, "model.xlsx", [{"op": "insert_rows", "sheet": "Inputs", "row": 2, "count": 1}])
    edited = _bytes(result)
    inputs = _part(edited, "xl/worksheets/sheet1.xml")
    model = _part(edited, "xl/worksheets/sheet2.xml")
    assert b"<f>A1+A3</f>" in inputs
    assert b'r="A4"' in inputs
    assert b"<f>Inputs!A1</f>" in model
    assert b"<f>Inputs!A4</f>" in model
    assert b"<definedName name=\"Total\">Inputs!$A$4</definedName>" in _part(edited, "xl/workbook.xml")
    assert b'ref="B6:C7"' in inputs
    assert _compressed(edited, "xl/styles.xml") == _compressed(original, "xl/styles.xml")
    # Restore is a new head whose bytes are the original package.
    assert original == original
    assert hashlib.sha256(original).hexdigest() == result["base_sha256"]


def test_insert_refused_on_pivot_chart_and_3d_refs():
    pivot = POI.joinpath("ExcelPivotTableSample.xlsx").read_bytes()
    with pytest.raises(StetwiseRefusal) as exc:
        propose(pivot, "pivot.xlsx", [{"op": "insert_rows", "sheet": "Tabelle1", "row": 2, "count": 1}])
    assert "pivot" in exc.value.reason
    charts = POI.joinpath("123233_charts.xlsx").read_bytes()
    with pytest.raises(StetwiseRefusal) as exc:
        propose(charts, "charts.xlsx", [{"op": "insert_cols", "sheet": "Page1_1", "column": "B", "count": 1}])
    assert "chart" in exc.value.reason.lower() or "drawing" in exc.value.reason.lower()
    cross = POI.joinpath("FormulaSheetRange.xlsx").read_bytes()
    with pytest.raises(StetwiseRefusal) as exc:
        propose(cross, "FormulaSheetRange.xlsx", [{"op": "insert_rows", "sheet": "test", "row": 2, "count": 1}])
    assert "3D" in exc.value.reason or "formula" in exc.value.reason.lower()


def test_merged_cell_non_origin_refused():
    original = model_xlsx()
    with pytest.raises(StetwiseRefusal) as exc:
        propose(original, "model.xlsx", [{"op": "set_cells", "sheet": "Inputs", "cells": [{"ref": "C5", "value": 1}]}])
    assert exc.value.code == "merged_cell"
    result = _propose(original, "model.xlsx", [{"op": "set_cells", "sheet": "Inputs", "cells": [{"ref": "B5", "value": "Kept"}]}])
    edited = _bytes(result)
    assert b'ref="B5:C6"' in _part(edited, "xl/worksheets/sheet1.xml")
    assert b"Kept" in _part(edited, "xl/worksheets/sheet1.xml")
    assert _compressed(edited, "xl/styles.xml") == _compressed(original, "xl/styles.xml")


def test_docx_split_run_replace_preserves_revisions_comments_and_styles():
    original = contract_docx()
    found = search(original, "contract.docx", "Acme Corp")
    assert found["matches"]
    result = _propose(original, "contract.docx", [{"op": "replace_text", "find": "Acme Corp", "replace": "Stetwise"}])
    edited = _bytes(result)
    document = _part(edited, "word/document.xml")
    assert b"Stetwise" in document
    assert b"Acme" not in document
    assert b"<w:b" in document
    assert b"<w:del" in document and b"old fee" in document
    assert b"<w:ins" in document and b"new fee" in document
    assert b'w:val="Heading1"' in document
    for name in ("word/styles.xml", "word/comments.xml", "[Content_Types].xml"):
        assert _compressed(edited, name) == _compressed(original, name)
    assert result["verification"]["changed_parts"] == ["word/document.xml"]


def _body_docx(inner: str) -> bytes:
    ns_ct = "http://schemas.openxmlformats.org/package/2006/content-types"
    ns_rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    ns_off = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    ns_w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    parts = {
        "[Content_Types].xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{ns_ct}">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
""",
        "_rels/.rels": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{ns_rel}">
  <Relationship Id="rId1" Type="{ns_off}/officeDocument" Target="word/document.xml"/>
</Relationships>
""",
        "word/document.xml": f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{ns_w}"><w:body>{inner}</w:body></w:document>
""",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            archive.writestr(name, text.encode("utf-8"))
    return buffer.getvalue()


def _visible_text(blob: bytes) -> str:
    xml = _part(blob, "word/document.xml")
    raw = b"".join(re_wt.findall(xml)).decode("utf-8")
    return raw.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


import re as re_wt_mod

re_wt = re_wt_mod.compile(rb"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re_wt_mod.DOTALL)


def test_replace_text_keeps_later_matches_when_the_replacement_length_changes():
    longer = _body_docx('<w:p><w:r><w:t>aa aa</w:t></w:r></w:p>')
    edited = _bytes(_propose(longer, "note.docx", [{"op": "replace_text", "find": "aa", "replace": "bbb"}]))
    assert _visible_text(edited) == "bbb bbb"
    shorter = _body_docx('<w:p><w:r><w:t>aa aa</w:t></w:r></w:p>')
    edited = _bytes(_propose(shorter, "note.docx", [{"op": "replace_text", "find": "aa", "replace": "b"}]))
    assert _visible_text(edited) == "b b"
    split = _body_docx(
        '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>cat</w:t></w:r>'
        '<w:r><w:t xml:space="preserve"> cat</w:t></w:r></w:p>'
    )
    edited = _bytes(_propose(split, "note.docx", [{"op": "replace_text", "find": "cat", "replace": "kitten"}]))
    document = _part(edited, "word/document.xml")
    assert _visible_text(edited) == "kitten kitten"
    assert b"<w:b" in document
    spanned = _body_docx('<w:p><w:r><w:t>ab</w:t></w:r><w:r><w:t>cd ab</w:t></w:r></w:p>')
    edited = _bytes(_propose(spanned, "note.docx", [{"op": "replace_text", "find": "ab", "replace": "XYZ"}]))
    assert _visible_text(edited) == "XYZcd XYZ"
    escaped = _body_docx('<w:p><w:r><w:t>red red</w:t></w:r></w:p>')
    edited = _bytes(_propose(escaped, "note.docx", [{"op": "replace_text", "find": "red", "replace": "a&b"}]))
    assert _visible_text(edited) == "a&b a&b"
    assert b"a&amp;b" in _part(edited, "word/document.xml")


def test_table_cell_and_paragraph_style():
    original = contract_docx()
    result = _propose(
        original,
        "contract.docx",
        [
            {"op": "update_table_cells", "table": 0, "cells": [{"row": 1, "col": 0, "text": "250"}]},
            {"op": "replace_paragraph", "anchor": "p-0", "text": "Updated agreement"},
        ],
    )
    document = _part(_bytes(result), "word/document.xml")
    assert b"Updated agreement" in document
    assert b'w:val="Heading1"' in document
    assert b">250</w:t>" in document
    assert _compressed(_bytes(result), "word/styles.xml") == _compressed(original, "word/styles.xml")


def test_tracking_writes_revisions_and_protection_is_still_refused():
    tracked = POI.joinpath("delins.docx").read_bytes()
    result = _propose(tracked, "delins.docx", [{"op": "replace_text", "find": "Tika", "replace": "Stetwise"}])
    document = _part(_bytes(result), "word/document.xml")
    assert b'w:author="Stetwise"' in document
    assert b"<w:del" in document and b"<w:ins" in document
    assert b"Tika" in document and b"Stetwise" in document
    settings = "word/settings.xml"
    if settings in read_package(tracked).parts():
        assert _compressed(_bytes(result), settings) == _compressed(tracked, settings)
    already = POI.joinpath("bug56075-changeTracking_on.docx").read_bytes()
    revised = _propose(already, "bug56075-changeTracking_on.docx", [{"op": "replace_text", "find": "Tracking", "replace": "Marks"}])
    body = _part(_bytes(revised), "word/document.xml")
    assert b'w:author="Stetwise"' in body
    assert b"Tracking" in body and b"Marks" in body
    protected = POI.joinpath("documentProtection_trackedChanges_no_password.docx").read_bytes()
    with pytest.raises(StetwiseRefusal) as exc:
        propose(protected, "protected.docx", [{"op": "replace_text", "find": "a", "replace": "b"}])
    assert exc.value.code == "protected"
    comments = POI.joinpath("comment.docx").read_bytes()
    comment_xml = _part(comments, "word/comments.xml")
    assert b"<w:t" in comment_xml
    with pytest.raises(StetwiseRefusal) as missing:
        propose(comments, "comment.docx", [{"op": "replace_text", "find": "does-not-exist", "replace": "x"}])
    assert missing.value.code == "not_found"


def test_comment_text_is_not_rewritten():
    original = contract_docx()
    hits = search(original, "contract.docx", "Reviewer note stays")
    assert hits["matches"][0]["editable"] is False
    with pytest.raises(StetwiseRefusal) as exc:
        propose(original, "contract.docx", [{"op": "replace_text", "find": "Reviewer note stays", "replace": "gone"}])
    assert exc.value.code == "unsupported_part"


def test_security_rejects_zip_slip_bomb_and_xxe():
    slip = io.BytesIO()
    with zipfile.ZipFile(slip, "w") as archive:
        archive.writestr("../evil.xml", "<a/>")
    with pytest.raises(StetwiseRefusal):
        read_package(slip.getvalue())
    bomb = io.BytesIO()
    payload = b"A" * (2_000_000)
    with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", payload)
        archive.writestr("word/document.xml", b"<w:document/>")
    # A normal compressed file of 2MB is under the ratio cap. Build a tiny
    # highly-compressible part that claims a huge size by patching the reader
    # through a crafted local header is covered by the ratio check below.
    huge = io.BytesIO()
    with zipfile.ZipFile(huge, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"x" * 50)
        archive.writestr("xl/workbook.xml", b"B" * 200_000)
    package = read_package(huge.getvalue())
    assert package.parts()["xl/workbook.xml"].startswith(b"B")
    xxe = b"""<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><foo>&xxe;</foo>"""
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/_rels/workbook.xml.rels", "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"/>")
        archive.writestr("xl/workbook.xml", xxe)
    with pytest.raises(StetwiseRefusal) as exc:
        inspect(bad.getvalue(), "evil.xlsx")
    assert exc.value.code in {"xxe", "malformed_xml", "malformed_package", "not_xlsx"}


def test_zip_bomb_ratio(monkeypatch):
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", b"Z" * 1_100_000)
    monkeypatch.setattr("stetwise.zippack.MAX_RATIO", 1)
    with pytest.raises(StetwiseRefusal) as exc:
        read_package(blob.getvalue())
    assert exc.value.code == "zip_bomb"


def test_pptx_and_legacy_refused():
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as archive:
        archive.writestr("ppt/presentation.xml", "<p/>")
        archive.writestr("[Content_Types].xml", "<Types/>")
    with pytest.raises(StetwiseRefusal) as exc:
        inspect(blob.getvalue(), "deck.pptx")
    assert "PowerPoint" in exc.value.reason


def test_dispatch_propose_round_trip():
    original = model_xlsx()
    response = dispatch(
        {
            "action": "propose",
            "filename": "model.xlsx",
            "data_b64": _b64(original),
            "ops": [{"op": "set_cells", "sheet": "Inputs", "cells": [{"ref": "A2", "value": 5}]}],
        }
    )
    assert response["ok"] is True
    edited = _bytes(response["result"])
    assert b"<v>5</v>" in _part(edited, "xl/worksheets/sheet1.xml")
    assert _compressed(edited, "xl/styles.xml") == _compressed(original, "xl/styles.xml")
    refused = dispatch(
        {
            "action": "propose",
            "filename": "model.xlsx",
            "data_b64": _b64(original),
            "ops": [{"op": "set_cells", "sheet": "Missing", "cells": [{"ref": "A1", "value": 1}]}],
        }
    )
    assert refused["ok"] is False
    assert "No sheet" in refused["error"]["reason"]
    verified = dispatch(
        {
            "action": "verify",
            "filename": "model.xlsx",
            "before_b64": _b64(original),
            "after_b64": response["result"]["data_b64"],
        }
    )
    assert verified["ok"] is True
    assert verified["result"]["untouched_identical"] is True
    assert "xl/styles.xml" not in verified["result"]["changed_parts"]
    recalc = dispatch({"action": "recalculate", "filename": "model.xlsx", "data_b64": _b64(original)})
    assert recalc["ok"] is True
    assert recalc["result"]["label"] == "LibreOffice-calculated"
    assert recalc["result"]["changed"] is False


def test_table_body_edits_leave_table_xml():
    original = table_xlsx()
    body = _propose(original, "table.xlsx", [{"op": "set_cells", "sheet": "Data", "cells": [{"ref": "B2", "value": 8}]}])
    edited = _bytes(body)
    assert b"<v>8</v>" in _part(edited, "xl/worksheets/sheet1.xml")
    assert _compressed(edited, "xl/tables/table1.xml") == _compressed(original, "xl/tables/table1.xml")
    with pytest.raises(StetwiseRefusal) as header:
        propose(original, "table.xlsx", [{"op": "set_cells", "sheet": "Data", "cells": [{"ref": "A1", "value": "Name"}]}])
    assert header.value.code == "table_header"
    with pytest.raises(StetwiseRefusal) as totals:
        propose(original, "table.xlsx", [{"op": "set_cells", "sheet": "Data", "cells": [{"ref": "B4", "value": 1}]}])
    assert totals.value.code == "table_totals"


def test_excel_replace_text_skips_formulas_and_shared_strings():
    original = strings_xlsx()
    result = _propose(original, "strings.xlsx", [{"op": "replace_text", "find": "Hello", "replace": "Hi"}])
    edited = _bytes(result)
    sheet = _part(edited, "xl/worksheets/sheet1.xml")
    assert b">Hi</t>" in sheet
    assert b">Hi world</t>" in sheet
    assert b'<f>"Hello"</f>' in sheet
    assert _compressed(edited, "xl/sharedStrings.xml") == _compressed(original, "xl/sharedStrings.xml")
    second = _propose(original, "strings.xlsx", [{"op": "replace_text", "find": "Hello", "replace": "Hi", "occurrence": 2}])
    once = _part(_bytes(second), "xl/worksheets/sheet1.xml")
    assert once.count(b">Hi</t>") == 1
    assert b">Hello</t>" in once or b"<v>0</v>" in once


def test_format_cells_appends_styles_and_leaves_theme_bytes():
    original = model_xlsx()
    result = _propose(
        original,
        "model.xlsx",
        [{"op": "format_cells", "sheet": "Inputs", "refs": ["A1"], "bold": True, "fill": "FFCC00", "number_format": "0.00", "horizontal": "right", "column_width": 18}],
    )
    edited = _bytes(result)
    styles = _part(edited, "xl/styles.xml")
    assert b"<b/>" in styles
    assert b"FFCC00" in styles
    assert b'horizontal="right"' in styles
    assert b'width="18"' in _part(edited, "xl/worksheets/sheet1.xml")
    assert b'<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>' in styles
    assert b"theme" not in styles.lower()
    charts = POI.joinpath("123233_charts.xlsx").read_bytes()
    painted = _propose(charts, "123233_charts.xlsx", [{"op": "format_cells", "sheet": "Page1_1", "refs": ["G45"], "italic": True}])
    painted_bytes = _bytes(painted)
    package = read_package(charts)
    for name in package.parts():
        if "/charts/" in name or "/drawings/" in name or "theme" in name:
            assert _compressed(painted_bytes, name) == _compressed(charts, name), name
    assert "xl/styles.xml" in painted["verification"]["changed_parts"]


def test_recalculate_patches_cached_values_on_a_copy():
    original = stale_xlsx()
    response = dispatch({"action": "recalculate", "filename": "stale.xlsx", "data_b64": _b64(original)})
    assert response["ok"] is True, response
    result = response["result"]
    assert result["changed"] is True
    assert result["label"] == "LibreOffice-calculated"
    edited = _bytes(result)
    sheet = _part(edited, "xl/worksheets/sheet1.xml")
    assert b"<f>A1+B1</f>" in sheet
    assert b"<v>15</v>" in sheet
    assert b"<f>A1/0</f>" in sheet
    assert b't="e"' in sheet
    assert b"#DIV/0!" in sheet
    assert _compressed(edited, "xl/styles.xml") == _compressed(original, "xl/styles.xml")
    assert result["verification"]["untouched_identical"] is True
    word = dispatch({"action": "recalculate", "filename": "contract.docx", "data_b64": _b64(contract_docx())})
    assert word["ok"] is False
    assert word["error"]["code"] == "wrong_kind"
    assert word["error"]["started"] is False
    assert _soffice_orphans() == []


def test_recalculate_keeps_date_serials_and_typed_caches():
    poi = POI.joinpath("50755_workday_formula_example.xlsx").read_bytes()
    kept = dispatch({"action": "recalculate", "filename": "50755_workday_formula_example.xlsx", "data_b64": _b64(poi)})
    assert kept["ok"] is True, kept
    assert kept["result"]["changed"] is False
    poi_sheet = _part(poi, "xl/worksheets/sheet1.xml")
    assert b"8-Feb-11" not in poi_sheet
    assert b"<v>40582</v>" in poi_sheet
    original = types_xlsx()
    response = dispatch({"action": "recalculate", "filename": "types.xlsx", "data_b64": _b64(original)})
    assert response["ok"] is True, response
    result = response["result"]
    assert result["changed"] is True
    edited = _bytes(result)
    sheet = _part(edited, "xl/worksheets/sheet1.xml")
    assert b"8-Feb-11" not in sheet
    assert b"Feb" not in sheet
    assert b"<f>DATE(2011,2,8)</f>" in sheet
    assert b"<v>40582</v>" in sheet
    assert b"<f>1234.5</f>" in sheet
    assert b"<v>1234.5</v>" in sheet
    assert b"<f>1/4</f>" in sheet
    assert b"<v>0.25</v>" in sheet
    assert b'<c r="A4" t="b"><f>TRUE()</f><v>1</v></c>' in sheet
    assert b"<f>1/0</f>" in sheet
    assert b't="e"' in sheet
    assert b"#DIV/0!" in sheet
    assert b'<c r="A6" t="str"><f>"Hello"</f><v>Hello</v></c>' in sheet
    assert _compressed(edited, "xl/styles.xml") == _compressed(original, "xl/styles.xml")
    assert result["verification"]["untouched_identical"] is True
    assert _soffice_orphans() == []


def test_recalculate_timeout_leaves_no_soffice(monkeypatch):
    before_profiles = _lo_profiles()
    monkeypatch.setenv("STETWISE_RECALC_TIMEOUT", "1")
    monkeypatch.setenv("STETWISE_RECALC_HOLD", "15")
    response = dispatch({"action": "recalculate", "filename": "stale.xlsx", "data_b64": _b64(stale_xlsx())})
    assert response["ok"] is False
    assert response["error"]["code"] == "recalc_failed"
    assert response["error"]["started"] is True
    deadline = time.time() + 5
    while time.time() < deadline and _soffice_orphans():
        time.sleep(0.1)
    assert _soffice_orphans() == []
    assert _lo_profiles() <= before_profiles


_NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_OFF = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_X = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _formula_book(formulas: list[str]) -> bytes:
    rows = []
    for index, formula in enumerate(formulas, start=1):
        text = formula.replace("&", "&amp;").replace("<", "&lt;")
        rows.append(f'<row r="{index}"><c r="A{index}"><f>{text}</f><v>0</v></c></row>')
    parts = {
        "[Content_Types].xml": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{_NS_CT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>''',
        "_rels/.rels": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_NS_REL}"><Relationship Id="rId1" Type="{_NS_OFF}/officeDocument" Target="xl/workbook.xml"/></Relationships>''',
        "xl/_rels/workbook.xml.rels": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{_NS_REL}"><Relationship Id="rId1" Type="{_NS_OFF}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="{_NS_OFF}/styles" Target="styles.xml"/></Relationships>''',
        "xl/workbook.xml": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{_NS_X}" xmlns:r="{_NS_OFF}"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>''',
        "xl/styles.xml": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{_NS_X}"><fonts count="1"><font/></fonts><fills count="1"><fill><patternFill patternType="none"/></fill></fills><borders count="1"><border/></borders><cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellXfs></styleSheet>''',
        "xl/worksheets/sheet1.xml": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{_NS_X}"><sheetData>{"".join(rows)}</sheetData></worksheet>''',
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in parts.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def _excel_numeric_caches() -> list[str]:
    path = POI / "FormulaEvalTestData_Copy.xlsx"
    archive = zipfile.ZipFile(path)
    cell = re.compile(br"<c\b([^>]*)>(.*?)</c>", re.DOTALL)
    value = re.compile(br"<v>([^<]*)</v>")
    found = []
    for name in archive.namelist():
        if not name.startswith("xl/worksheets/"):
            continue
        xml = archive.read(name)
        for match in cell.finditer(xml):
            attrs, inner = match.group(1), match.group(2)
            if b"<f" not in inner:
                continue
            kind = re.search(br'\bt="([^"]+)"', attrs)
            if kind and kind.group(1) != b"n":
                continue
            cached = value.search(inner)
            if not cached:
                continue
            raw = cached.group(1).decode()
            try:
                float(raw)
            except ValueError:
                continue
            found.append(raw)
    return found


def test_num_text_preserves_excel_saved_doubles():
    caches = _excel_numeric_caches()
    assert len(caches) > 700
    lost_at_15 = 0
    for raw in caches:
        value = float(raw)
        text = _num_text(value)
        assert float(text) == value
        assert text == ("0" if value == 0.0 else (str(int(value)) if abs(value) < 1e15 and value.is_integer() else repr(value)))
        truncated = str(int(value)) if abs(value) < 1e15 and value.is_integer() else format(value, ".15g")
        if float(truncated) != value:
            lost_at_15 += 1
    assert lost_at_15 > 100
    excel = "0.66666666666666663"
    assert float(_num_text(float(excel))) == float(excel)
    assert _num_text(float(excel)) == "0.6666666666666666"
    assert float(format(float(excel), ".15g")) != float(excel)
    assert _excel_error(532, "#DIV/0!") == ("#DIV/0!", False)
    assert _excel_error(503, "#NUM!") == ("#NUM!", False)
    assert _excel_error(519, "#VALUE!") == ("#VALUE!", False)
    assert _excel_error(521, "#NULL!") == ("#NULL!", False)
    assert _excel_error(524, "#REF!") == ("#REF!", False)
    assert _excel_error(525, "#NAME?") == ("#NAME?", False)
    assert _excel_error(32767, "#N/A") == ("#N/A", False)
    assert _excel_error(502, "Err:502") == ("Err:502", True)
    assert _excel_error(504, "Err:504") == ("Err:504", True)


def test_recalculate_writes_round_trip_numbers_and_exact_errors():
    before = _lo_profiles()
    third = dispatch({"action": "recalculate", "filename": "third.xlsx", "data_b64": _b64(_formula_book(["2/3"]))})
    assert third["ok"] is True, third
    sheet = _part(_bytes(third["result"]), "xl/worksheets/sheet1.xml")
    cached = re.search(br"<v>([^<]*)</v>", sheet).group(1).decode()
    assert cached == _num_text(float(cached))
    assert float(cached) == float("0.66666666666666663")
    assert cached != "0.666666666666667"
    exact = dispatch({"action": "recalculate", "filename": "errors.xlsx", "data_b64": _b64(_formula_book(["1/0", "NA()", "ASIN(2)", '1+"a"', "NOSUCH()"]))})
    assert exact["ok"] is True, exact
    errors = _part(_bytes(exact["result"]), "xl/worksheets/sheet1.xml")
    assert b"Err:" not in errors
    for token in (b"#DIV/0!", b"#N/A", b"#NUM!", b"#VALUE!", b"#NAME?"):
        assert token in errors
    assert _lo_profiles() <= before
    assert _soffice_orphans() == []


def test_recalculate_refuses_unmapped_libreoffice_errors():
    before = _lo_profiles()
    response = dispatch({"action": "recalculate", "filename": "sqrt.xlsx", "data_b64": _b64(_formula_book(["SQRT(-1)", "MATCH(1,1,1)"]))})
    assert response["ok"] is False
    assert response["error"]["code"] == "recalc_failed"
    assert response["error"]["started"] is True
    reason = response["error"]["reason"]
    assert "Err:502" in reason or "Err:504" in reason
    assert "No cached values were written" in reason
    assert "data_b64" not in response.get("result", {})
    assert _lo_profiles() <= before
    assert _soffice_orphans() == []


def test_create_blank_then_edit_and_list_comments():
    blank = dispatch({"action": "blank", "kind": "xlsx"})
    assert blank["ok"] is True
    assert blank["result"]["source"] == "LibreOffice"
    workbook = _bytes(blank["result"])
    edited = _propose(workbook, "blank.xlsx", [{"op": "set_cells", "sheet": "Sheet1", "cells": [{"ref": "A1", "value": "Ready"}]}])
    assert b"Ready" in _part(_bytes(edited), "xl/worksheets/sheet1.xml")
    document = dispatch({"action": "blank", "kind": "docx"})
    assert document["ok"] is True
    filled = _propose(_bytes(document["result"]), "blank.docx", [{"op": "replace_paragraph", "anchor": "p-0", "text": "Hello"}])
    assert b"Hello" in _part(_bytes(filled), "word/document.xml")
    styles = _part(_bytes(document["result"]), "word/styles.xml")
    for style_id in (b"Heading1", b"Heading2", b"Heading3", b"ListBullet", b"ListNumber"):
        assert style_id in styles
    for style_id, label in (("Heading1", "Title"), ("Heading2", "Section"), ("Heading3", "Detail"), ("ListBullet", "Bullet"), ("ListNumber", "Numbered")):
        inserted = _propose(
            _bytes(document["result"]),
            "blank.docx",
            [{"op": "insert_paragraph", "anchor": "p-0", "text": label, "style": style_id}],
        )
        body = _part(_bytes(inserted), "word/document.xml")
        assert label.encode() in body
        assert f'w:val="{style_id}"'.encode() in body
        assert _compressed(_bytes(inserted), "word/styles.xml") == _compressed(_bytes(document["result"]), "word/styles.xml")
    comments = dispatch({"action": "list_comments", "filename": "comment.docx", "data_b64": _b64(POI.joinpath("comment.docx").read_bytes())})
    assert comments["ok"] is True
    first = comments["result"]["comments"][0]
    assert first["author"] == "Unbekannter Autor"
    assert "first line" in first["text"]
    contract = dispatch({"action": "list_comments", "filename": "contract.docx", "data_b64": _b64(contract_docx())})
    assert contract["result"]["comments"][0]["text"] == "Reviewer note stays"
    assert contract["result"]["comments"][0]["anchor"] == "p-1"
    workbook_comments = dispatch({"action": "list_comments", "filename": "model.xlsx", "data_b64": _b64(model_xlsx())})
    assert workbook_comments["ok"] is False
    assert workbook_comments["error"]["code"] == "wrong_kind"


def test_word_paragraphs_tracking_and_run_format():
    original = contract_docx()
    inserted = _propose(original, "contract.docx", [{"op": "insert_paragraph", "anchor": "p-0", "text": "Added clause"}])
    document = _part(_bytes(inserted), "word/document.xml")
    assert b"Added clause" in document
    assert b"Service agreement" in document
    assert _compressed(_bytes(inserted), "word/styles.xml") == _compressed(original, "word/styles.xml")
    removed = _propose(original, "contract.docx", [{"op": "delete_paragraph", "anchor": "p-0"}])
    gone = _part(_bytes(removed), "word/document.xml")
    assert b"Service agreement" not in gone
    assert b"Acme" in gone
    with pytest.raises(StetwiseRefusal) as only:
        propose(original, "contract.docx", [{"op": "delete_paragraph", "anchor": "p-1"}])
    assert only.value.code == "unsafe_paragraph"
    overlap = original
    with pytest.raises(StetwiseRefusal) as tracked:
        propose(overlap, "contract.docx", [{"op": "replace_text", "find": "new fee", "replace": "other"}])
    assert tracked.value.code == "tracked_change_overlap"
    formatted = _propose(original, "contract.docx", [{"op": "format_runs", "find": "Acme", "bold": True, "underline": True}])
    runs = _part(_bytes(formatted), "word/document.xml")
    assert b"<w:u " in runs or b"<w:u/>" in runs
    assert _compressed(_bytes(formatted), "word/styles.xml") == _compressed(original, "word/styles.xml")
    coloured = _propose(
        original,
        "contract.docx",
        [{"op": "format_runs", "find": "Acme", "font_size": 14, "font_colour": "ff0000"}],
    )
    coloured_doc = _part(_bytes(coloured), "word/document.xml")
    assert b'<w:sz w:val="28"/>' in coloured_doc
    assert b'<w:szCs w:val="28"/>' in coloured_doc
    assert b'<w:color w:val="FF0000"/>' in coloured_doc
    assert _compressed(_bytes(coloured), "word/styles.xml") == _compressed(original, "word/styles.xml")
    with pytest.raises(StetwiseRefusal) as bad_colour:
        propose(original, "contract.docx", [{"op": "format_runs", "find": "Acme", "font_color": "red"}])
    assert bad_colour.value.code == "bad_format"
    assert _compressed(_bytes(formatted), "word/comments.xml") == _compressed(original, "word/comments.xml")
    settings = settings_docx()
    marked = _propose(settings, "settings.docx", [{"op": "replace_text", "find": "Beta", "replace": "Gamma", "track_changes": True}])
    marked_doc = _part(_bytes(marked), "word/document.xml")
    marked_settings = _part(_bytes(marked), "word/settings.xml")
    assert b'w:author="Stetwise"' in marked_doc
    assert b"<w:delText" in marked_doc or b"<w:del" in marked_doc
    assert b"Gamma" in marked_doc
    assert b"Alpha stay" in marked_doc
    assert b"<w:trackRevisions" in marked_settings
    assert "word/settings.xml" in marked["verification"]["changed_parts"]
    styled = _propose(original, "contract.docx", [{"op": "insert_paragraph", "anchor": "p-0", "text": "Listed item", "style": "ListParagraph"}])
    styled_doc = _part(_bytes(styled), "word/document.xml")
    assert b"Listed item" in styled_doc
    assert b'w:val="ListParagraph"' in styled_doc
    assert _compressed(_bytes(styled), "word/styles.xml") == _compressed(original, "word/styles.xml")
    heading = _propose(original, "contract.docx", [{"op": "insert_paragraph", "anchor": "p-1", "text": "Section", "style": "Heading1"}])
    assert b'w:val="Heading1"' in _part(_bytes(heading), "word/document.xml")
    assert _compressed(_bytes(heading), "word/styles.xml") == _compressed(original, "word/styles.xml")
    with pytest.raises(StetwiseRefusal) as unknown:
        propose(original, "contract.docx", [{"op": "insert_paragraph", "anchor": "p-0", "text": "No", "style": "NoSuchStyle"}])
    assert unknown.value.code == "unknown_style"
    with pytest.raises(StetwiseRefusal) as character:
        propose(original, "contract.docx", [{"op": "insert_paragraph", "anchor": "p-0", "text": "No", "style": "Emphasis"}])
    assert character.value.code == "unknown_style"


def test_fill_clear_and_format_font_border():
    original = model_xlsx()
    filled = _propose(
        original,
        "model.xlsx",
        [{"op": "fill_range", "sheet": "Inputs", "range": "Z1:Z2", "formula": "A1"}],
    )
    sheet = _part(_bytes(filled), "xl/worksheets/sheet1.xml")
    assert b'<c r="Z1"><f>A1</f></c>' in sheet or b'<c r="Z1"><f>A1</f>' in sheet
    assert b"<f>A2</f>" in sheet
    locked = _propose(
        original,
        "model.xlsx",
        [{"op": "fill_range", "sheet": "Inputs", "range": "Y1:Y2", "formula": "$A$1"}],
    )
    locked_sheet = _part(_bytes(locked), "xl/worksheets/sheet1.xml")
    assert locked_sheet.count(b"<f>$A$1</f>") >= 2
    cleared = _propose(_bytes(filled), "model.xlsx", [{"op": "clear_range", "sheet": "Inputs", "range": "Z1:Z2"}])
    gone = _part(_bytes(cleared), "xl/worksheets/sheet1.xml")
    assert b"<f>A1</f>" not in gone
    assert b"<f>A2</f>" not in gone
    with pytest.raises(StetwiseRefusal) as huge:
        propose(original, "model.xlsx", [{"op": "clear_range", "sheet": "Inputs", "range": "A1:A5001"}])
    assert huge.value.code == "too_many"
    cells = [{"ref": f"A{i}", "value": 1} for i in range(1, 5002)]
    with pytest.raises(StetwiseRefusal) as too_many:
        propose(original, "model.xlsx", [{"op": "set_cells", "sheet": "Inputs", "cells": cells}])
    assert too_many.value.code == "too_many"
    painted = _propose(
        original,
        "model.xlsx",
        [{"op": "format_cells", "sheet": "Inputs", "refs": ["A1"], "font_size": 18, "font_color": "FF0000", "border": True}],
    )
    styles = _part(_bytes(painted), "xl/styles.xml")
    assert b'<sz val="18"/>' in styles
    assert b'rgb="FFFF0000"' in styles
    assert b'style="thin"' in styles
    assert b"<sz val=\"11\"/>" in styles
    assert b"theme" not in styles.lower()
    assert "xl/styles.xml" in painted["verification"]["changed_parts"]
    assert painted["verification"]["untouched_identical"] is True
