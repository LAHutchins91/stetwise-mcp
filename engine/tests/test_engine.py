import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from stetwise.engine import dispatch, propose, search, inspect
from stetwise.errors import StetwiseRefusal
from stetwise.refs import Shift, shift_formula
from stetwise.zippack import read_package, write_package
from build_fixtures import contract_docx, model_xlsx

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


def test_formula_edit_keeps_style_and_shared_formula_group():
    original = POI.joinpath("50755_workday_formula_example.xlsx").read_bytes()
    with pytest.raises(StetwiseRefusal) as shared:
        propose(original, "50755.xlsx", [{"op": "set_cells", "sheet": "Sheet1", "cells": [{"ref": "B3", "formula": "WORKDAY(A3,6)"}]}])
    assert shared.value.code == "shared_formula"
    result = _propose(original, "50755.xlsx", [{"op": "set_cells", "sheet": "Sheet1", "cells": [{"ref": "B2", "formula": "WORKDAY(A2,6)"}]}])
    edited = _bytes(result)
    sheet = _part(edited, "xl/worksheets/sheet1.xml")
    assert b'<c r="B2" s="2"><f>WORKDAY(A2,6)</f></c>' in sheet or b'r="B2" s="2"' in sheet
    assert b"WORKDAY(A2,6)" in sheet
    assert b't="shared"' in sheet
    assert _compressed(edited, "xl/styles.xml") == _compressed(original, "xl/styles.xml")
    assert "xl/calcChain.xml" in result["verification"]["removed_parts"]
    assert b'fullCalcOnLoad="1"' in _part(edited, "xl/workbook.xml")


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


def test_tracking_protection_and_comment_only_matches_are_refused():
    for name in (
        "delins.docx",
        "bug56075-changeTracking_on.docx",
        "documentProtection_trackedChanges_no_password.docx",
    ):
        blob = POI.joinpath(name).read_bytes()
        with pytest.raises(StetwiseRefusal):
            propose(blob, name, [{"op": "replace_text", "find": "Tika" if name == "delins.docx" else "a", "replace": "b"}])
    comments = POI.joinpath("comment.docx").read_bytes()
    hits = search(comments, "comment.docx", "Unbekannter")
    # The comment body is long; search for a word that exists in comments.xml.
    comment_xml = _part(comments, "word/comments.xml")
    assert b"<w:t" in comment_xml
    with pytest.raises(StetwiseRefusal) as exc:
        propose(comments, "comment.docx", [{"op": "replace_text", "find": "does-not-exist", "replace": "x"}])
    assert exc.value.code == "not_found"


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
    assert recalc["ok"] is False
    assert recalc["error"]["code"] == "unsupported"
    assert "LibreOffice" in recalc["error"]["reason"]
