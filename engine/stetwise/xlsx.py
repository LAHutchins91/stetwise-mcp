"""Excel .xlsx / .xlsm reads and surgical edits. Nothing here saves through openpyxl."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape

from lxml import etree

from .errors import StetwiseRefusal
from .refs import (
    Shift,
    cell_in_range,
    col_to_num,
    formula_blockers,
    num_to_col,
    shift_cell_ref,
    shift_formula,
    shift_ref_text,
    split_ref,
)
from .xmlutil import all_named, children, local, parse_xml, xml_escape
from .zippack import Package

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL_ATTR = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"

BUILTIN_FMT = {
    0: "General",
    1: "0",
    2: "0.00",
    3: "#,##0",
    4: "#,##0.00",
    9: "0%",
    10: "0.00%",
    11: "0.00E+00",
    14: "m/d/yyyy",
    15: "d-mmm-yy",
    16: "d-mmm",
    17: "mmm-yy",
    18: "h:mm AM/PM",
    19: "h:mm:ss AM/PM",
    20: "h:mm",
    21: "h:mm:ss",
    22: "m/d/yyyy h:mm",
    37: "#,##0 ;(#,##0)",
    38: "#,##0 ;[Red](#,##0)",
    39: "#,##0.00;(#,##0.00)",
    40: "#,##0.00;[Red](#,##0.00)",
    45: "mm:ss",
    46: "[h]:mm:ss",
    47: "mmss.0",
    48: "##0.0E+0",
    49: "@",
}

_CELL_OPEN = re.compile(br"<c(?=[\s>/])")
_ROW_OPEN = re.compile(br"<row(?=[\s>/])")
_FORMULA = re.compile(br"<f(\s[^>]*)?>(.*?)</f>", re.DOTALL)
_DEFINED = re.compile(br"<definedName\b([^>]*)>(.*?)</definedName>", re.DOTALL)
_REL = re.compile(br'<Relationship\b[^>]*Type="[^"]*?/calcChain"[^>]*/>')
_CT_CALC = re.compile(br'<Override\b[^>]*PartName="/xl/calcChain\.xml"[^>]*/>')


@dataclass
class SheetInfo:
    name: str
    path: str
    sheet_id: str


@dataclass
class CellChange:
    ref: str
    before: dict
    after: dict


@dataclass
class EditResult:
    replacements: dict[str, bytes] = field(default_factory=dict)
    drop: set[str] = field(default_factory=set)
    diff: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _q(name: str) -> str:
    return f"{{{NS_MAIN}}}{name}"


def sheet_map(package: Package) -> list[SheetInfo]:
    parts = package.parts()
    if "xl/workbook.xml" not in parts or "xl/_rels/workbook.xml.rels" not in parts:
        raise StetwiseRefusal("This workbook is missing xl/workbook.xml.", "not_xlsx")
    wb = parse_xml(parts["xl/workbook.xml"], "xl/workbook.xml")
    rels = parse_xml(parts["xl/_rels/workbook.xml.rels"], "xl/_rels/workbook.xml.rels")
    targets: dict[str, str] = {}
    for rel in rels:
        if local(rel.tag) != "Relationship":
            continue
        typ = rel.get("Type") or ""
        if typ.endswith("/worksheet"):
            target = rel.get("Target") or ""
            targets[rel.get("Id") or ""] = _resolve("xl/", target)
    sheets: list[SheetInfo] = []
    root_sheets = first_child(wb, "sheets")
    if root_sheets is None:
        return sheets
    for sheet in children(root_sheets, "sheet"):
        name = sheet.get("name") or ""
        rid = sheet.get(NS_REL_ATTR) or ""
        path = targets.get(rid)
        if not path or path not in parts:
            raise StetwiseRefusal(
                f"Sheet {name!r} points at a missing worksheet part.",
                "broken_relationship",
            )
        sheets.append(SheetInfo(name=name, path=path, sheet_id=sheet.get("sheetId") or ""))
    return sheets


def _resolve(base: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    if target.startswith("../"):
        raise StetwiseRefusal("A relationship target escapes the package.", "zip_path")
    return base + target


def first_child(el: etree._Element, name: str) -> etree._Element | None:
    for child in el:
        if local(child.tag) == name:
            return child
    return None


def _shared_strings(package: Package) -> list[str]:
    parts = package.parts()
    raw = parts.get("xl/sharedStrings.xml")
    if raw is None:
        return []
    root = parse_xml(raw, "xl/sharedStrings.xml")
    values: list[str] = []
    for si in children(root, "si"):
        texts = [node.text or "" for node in si.iter() if local(node.tag) == "t"]
        values.append("".join(texts))
    return values


def _styles(package: Package) -> dict[int, str]:
    parts = package.parts()
    raw = parts.get("xl/styles.xml")
    if raw is None:
        return {}
    root = parse_xml(raw, "xl/styles.xml")
    custom: dict[int, str] = {}
    for node in all_named(root, "numFmt"):
        if node.get("numFmtId") and node.get("formatCode"):
            custom[int(node.get("numFmtId"))] = node.get("formatCode") or ""
    xfs = first_child(root, "cellXfs")
    formats: dict[int, str] = {}
    if xfs is None:
        return formats
    for index, xf in enumerate(children(xfs, "xf")):
        fmt_id = int(xf.get("numFmtId") or 0)
        formats[index] = custom.get(fmt_id, BUILTIN_FMT.get(fmt_id, "General"))
    return formats


def _sheet_by_name(package: Package, name: str) -> SheetInfo:
    for sheet in sheet_map(package):
        if sheet.name == name:
            return sheet
    known = ", ".join(sheet.name for sheet in sheet_map(package)) or "(none)"
    raise StetwiseRefusal(f"No sheet named {name!r}. Sheets: {known}.", "unknown_sheet")


def _protected(xml: bytes) -> bool:
    if b"<sheetProtection" in xml:
        return True
    # LibreOffice writes an empty workbookProtection element on a new workbook.
    # That does not lock structure or cells. A real lock carries attributes.
    match = re.search(br"<workbookProtection\b([^>/]*)/?>", xml)
    return bool(match and match.group(1).strip())


def _pivot_boxes(package: Package) -> list[dict]:
    boxes = []
    parts = package.parts()
    for sheet in sheet_map(package):
        rel_path = _rels_for(sheet.path)
        rel_xml = parts.get(rel_path)
        if not rel_xml:
            continue
        rels = parse_xml(rel_xml, rel_path)
        for rel in rels:
            if local(rel.tag) != "Relationship":
                continue
            if not (rel.get("Type") or "").endswith("/pivotTable"):
                continue
            target = _normalize(sheet.path, rel.get("Target") or "")
            raw = parts.get(target)
            if raw is None:
                continue
            root = parse_xml(raw, target)
            loc = first_child(root, "location")
            boxes.append(
                {
                    "sheet": sheet.name,
                    "name": root.get("name") or "pivot",
                    "ref": (loc.get("ref") if loc is not None else None) or "",
                    "part": target,
                }
            )
    return boxes


def _normalize(from_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    base = from_part.rsplit("/", 1)[0]
    parts = base.split("/")
    for piece in target.split("/"):
        if piece == "..":
            if not parts:
                raise StetwiseRefusal("A relationship target escapes the package.", "zip_path")
            parts.pop()
        elif piece not in ("", "."):
            parts.append(piece)
    return "/".join(parts)


def _rels_for(part: str) -> str:
    folder, name = part.rsplit("/", 1)
    return f"{folder}/_rels/{name}.rels"


def _table_boxes(package: Package) -> list[dict]:
    boxes = []
    parts = package.parts()
    for name, data in parts.items():
        if not name.startswith("xl/tables/") or not name.endswith(".xml"):
            continue
        root = parse_xml(data, name)
        if local(root.tag) != "table":
            continue
        header = root.get("headerRowCount")
        totals = root.get("totalsRowCount")
        boxes.append(
            {
                "name": root.get("displayName") or root.get("name") or name,
                "ref": root.get("ref") or "",
                "part": name,
                "header_rows": int(header) if header and header.isdigit() else 1,
                "total_rows": int(totals) if totals and totals.isdigit() else 0,
            }
        )
    # Attribute tables to sheets via relationships when possible.
    sheet_of: dict[str, str] = {}
    for sheet in sheet_map(package):
        rel_xml = parts.get(_rels_for(sheet.path))
        if not rel_xml:
            continue
        rels = parse_xml(rel_xml, _rels_for(sheet.path))
        for rel in rels:
            if local(rel.tag) == "Relationship" and (rel.get("Type") or "").endswith("/table"):
                sheet_of[_normalize(sheet.path, rel.get("Target") or "")] = sheet.name
    for box in boxes:
        box["sheet"] = sheet_of.get(box["part"])
    return boxes


def _merge_refs(sheet_xml: bytes) -> list[str]:
    root = parse_xml(sheet_xml, "worksheet")
    return [node.get("ref") or "" for node in all_named(root, "mergeCell") if node.get("ref")]


def _chart_parts(package: Package) -> list[str]:
    return [entry.name for entry in package.entries if "/charts/" in entry.name or entry.name.startswith("xl/drawings/")]


def inspect_xlsx(package: Package) -> dict:
    parts = package.parts()
    formats = _styles(package)
    strings = _shared_strings(package)
    sheets_out = []
    for sheet in sheet_map(package):
        xml = parts[sheet.path]
        root = parse_xml(xml, sheet.path)
        dim = first_child(root, "dimension")
        cells = _read_cells(root, strings, formats)
        refs = [cell["ref"] for cell in cells]
        sheets_out.append(
            {
                "name": sheet.name,
                "part": sheet.path,
                "dimension": dim.get("ref") if dim is not None else None,
                "used_range": _used_range(refs),
                "cell_count": len(cells),
                "formula_count": sum(1 for cell in cells if cell.get("formula")),
                "merged_cells": _merge_refs(xml),
                "conditional_formatting": len(all_named(root, "conditionalFormatting")),
                "data_validations": len(all_named(root, "dataValidation")),
                "protected": b"<sheetProtection" in xml,
                "shared_formulas": b't="shared"' in xml,
            }
        )
    names = []
    wb = parse_xml(parts["xl/workbook.xml"], "xl/workbook.xml")
    for node in all_named(wb, "definedName"):
        names.append({"name": node.get("name"), "formula": (node.text or "").strip()})
    features = []
    if any("pivot" in entry.name for entry in package.entries):
        features.append("pivot tables and pivot caches (kept, not edited)")
    if _chart_parts(package):
        features.append("charts and drawings (kept, not edited)")
    if any(entry.name.startswith("xl/tables/") for entry in package.entries):
        features.append("Excel tables (body cells can be edited; header, totals, and table XML stay)")
    if any(entry.name == "xl/vbaProject.bin" for entry in package.entries):
        features.append("vba project (kept byte-for-byte, never executed)")
    if any("externalLink" in entry.name for entry in package.entries):
        features.append("external links (not refreshed)")
    return {
        "kind": "xlsm" if any(entry.name == "xl/vbaProject.bin" for entry in package.entries) else "xlsx",
        "sheets": sheets_out,
        "named_ranges": names,
        "pivots": _pivot_boxes(package),
        "tables": _table_boxes(package),
        "charts": _chart_parts(package),
        "has_vba": any(entry.name == "xl/vbaProject.bin" for entry in package.entries),
        "workbook_protected": b"<workbookProtection" in parts["xl/workbook.xml"],
        "features_left_standing": features,
        "recalc": "libreoffice_on_copy",
    }


def _used_range(refs: list[str]) -> str | None:
    if not refs:
        return None
    boxes = [split_ref(ref) for ref in refs]
    c1 = min(box[0] for box in boxes)
    r1 = min(box[1] for box in boxes)
    c2 = max(box[2] for box in boxes)
    r2 = max(box[3] for box in boxes)
    start = f"{num_to_col(c1)}{r1}"
    end = f"{num_to_col(c2)}{r2}"
    return start if start == end else f"{start}:{end}"


def _read_cells(root: etree._Element, strings: list[str], formats: dict[int, str]) -> list[dict]:
    cells = []
    for node in all_named(root, "c"):
        ref = node.get("r")
        if not ref:
            continue
        kind = node.get("t") or "n"
        formula = None
        formula_type = None
        for child in node:
            if local(child.tag) == "f":
                formula = child.text or ""
                formula_type = child.get("t")
        value = None
        for child in node:
            if local(child.tag) == "v":
                value = child.text
            elif local(child.tag) == "is":
                value = "".join(t.text or "" for t in all_named(child, "t"))
                kind = "inlineStr"
        display = value
        if kind == "s" and value is not None and value.isdigit():
            index = int(value)
            display = strings[index] if index < len(strings) else None
        style = node.get("s")
        style_index = int(style) if style and style.isdigit() else None
        cached = "missing"
        if formula is not None and value is not None:
            cached = "present_unchecked"
        elif formula is not None:
            cached = "missing"
        elif value is not None:
            cached = "value"
        cells.append(
            {
                "ref": ref,
                "type": kind,
                "formula": formula,
                "formula_type": formula_type,
                "value": display,
                "raw_value": value,
                "number_format": formats.get(style_index, "General") if style_index is not None else "General",
                "style_index": style_index,
                "cached_value_status": cached,
            }
        )
    return cells


def read_range(package: Package, sheet_name: str, ref: str | None, limit: int = 500) -> dict:
    sheet = _sheet_by_name(package, sheet_name)
    root = parse_xml(package.parts()[sheet.path], sheet.path)
    cells = _read_cells(root, _shared_strings(package), _styles(package))
    if ref:
        selected = [cell for cell in cells if _ref_overlaps(cell["ref"], ref)]
    else:
        selected = cells
    truncated = len(selected) > limit
    return {
        "sheet": sheet.name,
        "range": ref or _used_range([cell["ref"] for cell in cells]),
        "cells": selected[:limit],
        "truncated": truncated,
        "recalc": "Cached values are not refreshed by read. recalculate previews LibreOffice results on a copy.",
    }


def _ref_overlaps(cell: str, ref: str) -> bool:
    try:
        return cell_in_range(cell, ref)
    except StetwiseRefusal:
        return cell.replace("$", "") == ref.replace("$", "")


def search_xlsx(package: Package, query: str, limit: int = 50) -> dict:
    if not query:
        raise StetwiseRefusal("Search text is empty.", "empty_query")
    hits = []
    formats = _styles(package)
    strings = _shared_strings(package)
    for sheet in sheet_map(package):
        root = parse_xml(package.parts()[sheet.path], sheet.path)
        for cell in _read_cells(root, strings, formats):
            hay = " ".join(str(cell.get(key) or "") for key in ("value", "formula"))
            if query in hay:
                hits.append({"sheet": sheet.name, "ref": cell["ref"], "value": cell.get("value"), "formula": cell.get("formula")})
                if len(hits) >= limit:
                    return {"matches": hits, "truncated": True}
    return {"matches": hits, "truncated": False}


def _iter_tag_spans(xml: bytes, pattern: re.Pattern[bytes], tag: bytes) -> list[tuple[int, int, bytes, bytes]]:
    spans = []
    for match in pattern.finditer(xml):
        start = match.start()
        gt = xml.find(b">", start)
        if gt < 0:
            raise StetwiseRefusal("A worksheet tag is truncated.", "malformed_xml")
        open_tag = xml[start : gt + 1]
        if open_tag.endswith(b"/>"):
            spans.append((start, gt + 1, open_tag, b""))
            continue
        close = xml.find(b"</" + tag + b">", gt)
        if close < 0:
            raise StetwiseRefusal("A worksheet tag is not closed.", "malformed_xml")
        spans.append((start, close + len(tag) + 3, open_tag, xml[gt + 1 : close]))
    return spans


def _attrs(open_tag: bytes) -> list[tuple[str, str]]:
    body = open_tag.decode("utf-8")
    body = re.sub(r"^<[A-Za-z0-9]+", "", body)
    body = body.removesuffix("/>").removesuffix(">")
    return [(key, unescape(value)) for key, value in re.findall(r'([A-Za-z_:][-A-Za-z0-9_:.]*)="([^"]*)"', body)]


def _render_cell(attrs: list[tuple[str, str]], inner: bytes) -> bytes:
    pieces = ["<c"]
    for key, value in attrs:
        pieces.append(f' {key}="{xml_escape(value)}"')
    if not inner:
        pieces.append("/>")
        return "".join(pieces).encode("utf-8")
    pieces.append(">")
    return "".join(pieces).encode("utf-8") + inner + b"</c>"


def _set_attr(attrs: list[tuple[str, str]], key: str, value: str | None) -> list[tuple[str, str]]:
    out = []
    found = False
    for attr, current in attrs:
        if attr == key:
            found = True
            if value is not None:
                out.append((attr, value))
        else:
            out.append((attr, current))
    if value is not None and not found:
        out.append((key, value))
    return out


def _cell_inner(value: str | int | float | bool | None, formula: str | None) -> tuple[bytes, str | None]:
    if formula is not None:
        text = formula[1:] if formula.startswith("=") else formula
        if formula_blockers(text):
            # The user is setting this formula explicitly. Structured refs and
            # 3D refs are stored as-is; we only refuse them for row/column inserts.
            pass
        return f"<f>{xml_escape(text)}</f>".encode("utf-8"), None
    if value is None:
        return b"", None
    if isinstance(value, bool):
        return f"<v>{1 if value else 0}</v>".encode("utf-8"), "b"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"<v>{value}</v>".encode("utf-8"), None
    text = str(value)
    from .xmlutil import reject_controls

    reject_controls(text, "Cell value")
    space = ' xml:space="preserve"' if text[:1].isspace() or text[-1:].isspace() else ""
    return f"<is><t{space}>{xml_escape(text)}</t></is>".encode("utf-8"), "inlineStr"


def set_cells(package: Package, sheet_name: str, cells: list[dict]) -> EditResult:
    if not cells:
        raise StetwiseRefusal("set_cells needs at least one cell.", "empty_edit")
    if len(cells) > 500:
        raise StetwiseRefusal("v1 accepts at most 500 cells in one set_cells edit.", "too_many")
    sheet = _sheet_by_name(package, sheet_name)
    parts = package.parts()
    if _protected(parts[sheet.path]) or _protected(parts["xl/workbook.xml"]):
        raise StetwiseRefusal(
            f"Sheet {sheet.name!r} or the workbook is protected. v1 will not bypass protection.",
            "protected",
        )
    xml = parts[sheet.path]
    pivots = [box for box in _pivot_boxes(package) if box["sheet"] == sheet.name and box["ref"]]
    tables = [box for box in _table_boxes(package) if box.get("sheet") == sheet.name and box["ref"]]
    merges = _merge_refs(xml)
    root = parse_xml(xml, sheet.path)
    existing = {cell["ref"]: cell for cell in _read_cells(root, _shared_strings(package), _styles(package))}
    warnings: list[str] = []
    if _pivot_boxes(package):
        warnings.append("Pivot tables and caches were not modified and will not be refreshed.")
    if _chart_parts(package):
        warnings.append("Chart parts were not modified. Chart caches can stay stale until Excel refreshes them.")
    if any(entry.name == "xl/vbaProject.bin" for entry in package.entries):
        warnings.append("The VBA project was copied byte-for-byte and was not executed.")
    rendered: dict[str, bytes] = {}
    changes = []
    formula_touched = False
    saw_table_body = False
    for spec in cells:
        ref = str(spec.get("ref") or "").replace("$", "")
        if not re.fullmatch(r"[A-Za-z]{1,3}\d+", ref):
            raise StetwiseRefusal(f"Cell reference {ref!r} is not a single cell.", "bad_ref")
        ref = f"{num_to_col(col_to_num(''.join(ch for ch in ref if ch.isalpha())))}{''.join(ch for ch in ref if ch.isdigit())}"
        if "formula" in spec and spec.get("formula") is not None and "value" in spec and spec.get("value") is not None:
            raise StetwiseRefusal(
                f"Cell {ref} sets both a formula and a value. Send one of them.",
                "ambiguous_cell",
            )
        for box in pivots:
            if cell_in_range(ref, box["ref"]):
                raise StetwiseRefusal(
                    f"Cell {ref} is inside pivot table {box['name']} ({box['ref']}). v1 will not rewrite a pivot layout.",
                    "pivot_cell",
                )
        from .xlsx_ops import release_shared, table_role

        for box in tables:
            role = table_role(box, ref)
            if role == "header":
                raise StetwiseRefusal(
                    f"Cell {ref} is in the header of Excel table {box['name']} ({box['ref']}). Header edits would rewrite table XML, so v1 leaves that row alone.",
                    "table_header",
                )
            if role == "totals":
                raise StetwiseRefusal(
                    f"Cell {ref} is in the totals row of Excel table {box['name']} ({box['ref']}). Totals edits would rewrite table XML, so v1 leaves that row alone.",
                    "table_totals",
                )
            if role == "body" and not saw_table_body:
                warnings.append(
                    f"Excel table {box['name']} body cells can change. table XML, the header, and the totals row were not rewritten."
                )
                saw_table_body = True
        for merge in merges:
            if cell_in_range(ref, merge):
                origin = merge.split(":")[0].replace("$", "")
                if origin != ref:
                    raise StetwiseRefusal(
                        f"Cell {ref} is inside merged range {merge} but is not its top-left cell.",
                        "merged_cell",
                    )
        before = existing.get(ref)
        if before and before.get("formula_type") == "shared":
            xml, shared_notes = release_shared(xml, ref)
            for note in shared_notes:
                changes.append({"ref": note["ref"], "before": {"shared": True}, "after": note})
            if shared_notes and "Shared formula" not in " ".join(warnings):
                warnings.append(
                    "A shared formula group was split or had its master moved so this cell could change. Other cells keep an equivalent formula."
                )
        formula = spec.get("formula") if "formula" in spec else None
        value = spec.get("value") if "value" in spec else None
        if formula is None and "formula" not in spec and "value" not in spec:
            raise StetwiseRefusal(f"Cell {ref} has no value or formula to set.", "empty_edit")
        inner, t_value = _cell_inner(value, formula)
        if formula is not None:
            formula_touched = True
        rendered[ref] = (inner, t_value)  # type: ignore[assignment]
        changes.append(
            {
                "ref": ref,
                "before": {"formula": None if not before else before.get("formula"), "value": None if not before else before.get("value"), "style_index": None if not before else before.get("style_index")},
                "after": {"formula": None if formula is None else (formula[1:] if str(formula).startswith("=") else formula), "value": value},
            }
        )
    new_xml = xml
    for ref, payload in rendered.items():
        inner, t_value = payload  # type: ignore[misc]
        new_xml = _write_cell(new_xml, ref, inner, t_value)
    new_xml = _expand_dimension(new_xml, list(rendered))
    result = EditResult(warnings=warnings, diff=[{"op": "set_cells", "sheet": sheet.name, "changes": changes}])
    if new_xml != parts[sheet.path]:
        result.replacements[sheet.path] = new_xml
    if formula_touched:
        workbook = _enable_full_calc(parts["xl/workbook.xml"])
        if workbook != parts["xl/workbook.xml"]:
            result.replacements["xl/workbook.xml"] = workbook
        if "xl/calcChain.xml" in parts:
            result.drop.add("xl/calcChain.xml")
            rels = parts["xl/_rels/workbook.xml.rels"]
            content_types = parts["[Content_Types].xml"]
            new_rels = _REL.sub(b"", rels)
            new_types = _CT_CALC.sub(b"", content_types)
            if new_rels == rels or new_types == content_types:
                raise StetwiseRefusal(
                    "This workbook's calc chain is not in a form v1 can remove safely, so the formula edit was refused.",
                    "calc_chain",
                )
            result.replacements["xl/_rels/workbook.xml.rels"] = new_rels
            result.replacements["[Content_Types].xml"] = new_types
        result.warnings.append(
            "Formula cached values were cleared and fullCalcOnLoad was set. Use recalculate to preview LibreOffice values before Excel opens the file."
        )
    if not result.replacements and not result.drop:
        raise StetwiseRefusal("The edit did not change any cell.", "empty_edit")
    return result


def _write_cell(xml: bytes, ref: str, inner: bytes, t_value: str | None) -> bytes:
    cells = _iter_tag_spans(xml, _CELL_OPEN, b"c")
    for start, end, open_tag, _inner in cells:
        attrs = _attrs(open_tag)
        current = dict(attrs).get("r")
        if current != ref:
            continue
        attrs = _set_attr(attrs, "t", t_value)
        # Drop cached formula children by replacing the whole inner XML.
        return xml[:start] + _render_cell(attrs, inner) + xml[end:]
    return _insert_missing_cell(xml, ref, _render_cell([("r", ref)] + ([("t", t_value)] if t_value else []), inner))


def _insert_missing_cell(xml: bytes, ref: str, cell_xml: bytes) -> bytes:
    col, row = split_ref(ref)[:2]
    rows = _iter_tag_spans(xml, _ROW_OPEN, b"row")
    for start, end, open_tag, inner in rows:
        attrs = _attrs(open_tag)
        raw_row = dict(attrs).get("r")
        if raw_row != str(row):
            continue
        cells = _iter_tag_spans(inner, _CELL_OPEN, b"c")
        insert_at = len(inner)
        for c_start, c_end, c_open, _c_inner in cells:
            c_ref = dict(_attrs(c_open)).get("r")
            if not c_ref:
                continue
            c_col, c_row = split_ref(c_ref)[:2]
            if c_row == row and c_col > col:
                insert_at = c_start
                break
            insert_at = c_end
        new_inner = inner[:insert_at] + cell_xml + inner[insert_at:]
        if open_tag.endswith(b"/>"):
            rebuilt = open_tag[:-2].rstrip() + b">" + new_inner + b"</row>"
        else:
            rebuilt = open_tag + new_inner + b"</row>"
        return xml[:start] + rebuilt + xml[end:]
    new_row = f'<row r="{row}">'.encode("utf-8") + cell_xml + b"</row>"
    for start, _end, open_tag, _inner in rows:
        raw_row = dict(_attrs(open_tag)).get("r")
        if raw_row and raw_row.isdigit() and int(raw_row) > row:
            return xml[:start] + new_row + xml[start:]
    marker = xml.find(b"</sheetData>")
    if marker < 0:
        empty = re.search(br"<sheetData\b[^>]*/>", xml)
        if not empty:
            raise StetwiseRefusal("The worksheet has no sheetData element, so v1 will not invent one.", "malformed_xml")
        return xml[: empty.start()] + b"<sheetData>" + new_row + b"</sheetData>" + xml[empty.end() :]
    return xml[:marker] + new_row + xml[marker:]


def _expand_dimension(xml: bytes, refs: list[str]) -> bytes:
    match = re.search(br'<dimension\b([^>]*?)\bref="([^"]+)"', xml)
    if not match or not refs:
        return xml
    try:
        c1, r1, c2, r2 = split_ref(match.group(2).decode())
    except StetwiseRefusal:
        return xml
    for ref in refs:
        cc1, rr1, cc2, rr2 = split_ref(ref)
        c1, r1, c2, r2 = min(c1, cc1), min(r1, rr1), max(c2, cc2), max(r2, rr2)
    start = f"{num_to_col(c1)}{r1}"
    end = f"{num_to_col(c2)}{r2}"
    updated = start if start == end else f"{start}:{end}"
    if updated == match.group(2).decode():
        return xml
    return xml[: match.start(2)] + updated.encode("utf-8") + xml[match.end(2) :]


def _enable_full_calc(xml: bytes) -> bytes:
    if b'fullCalcOnLoad="1"' in xml:
        return xml
    match = re.search(br"<calcPr\b([^>]*)/>", xml)
    if match:
        return xml[: match.start()] + b"<calcPr" + match.group(1) + b' fullCalcOnLoad="1"/>' + xml[match.end() :]
    match = re.search(br"<calcPr\b([^>]*)>", xml)
    if match:
        return xml[: match.start()] + b"<calcPr" + match.group(1) + b' fullCalcOnLoad="1">' + xml[match.end() :]
    if b"</workbook>" not in xml:
        raise StetwiseRefusal("workbook.xml has no workbook element.", "malformed_xml")
    return xml.replace(b"</workbook>", b'<calcPr fullCalcOnLoad="1"/></workbook>', 1)


def _insert_blockers(package: Package) -> list[str]:
    reasons = []
    names = [entry.name for entry in package.entries]
    if any("pivot" in name for name in names):
        reasons.append("pivot tables")
    if any("/charts/" in name or name.startswith("xl/drawings/") or name.endswith(".vml") for name in names):
        reasons.append("charts, drawings, or legacy drawing parts")
    if any(name.startswith("xl/tables/") for name in names):
        reasons.append("Excel tables")
    if any("slicer" in name for name in names):
        reasons.append("slicers")
    if any(name == "xl/vbaProject.bin" for name in names):
        reasons.append("macros")
    if any("externalLink" in name or name == "xl/comments" or "comments" in name for name in names):
        reasons.append("comments or external links")
    parts = package.parts()
    if _protected(parts.get("xl/workbook.xml", b"")) or any(_protected(parts[sheet.path]) for sheet in sheet_map(package)):
        reasons.append("sheet or workbook protection")
    for sheet in sheet_map(package):
        xml = parts[sheet.path]
        if b't="shared"' in xml:
            reasons.append("shared formulas")
            break
    try:
        _scan_formulas(package)
    except StetwiseRefusal as exc:
        reasons.append(exc.reason)
    # de-duplicate while keeping order
    seen = set()
    out = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            out.append(reason)
    return out


def _scan_formulas(package: Package) -> None:
    parts = package.parts()
    for sheet in sheet_map(package):
        xml = parts[sheet.path]
        for match in _FORMULA.finditer(xml):
            text = unescape(match.group(2).decode("utf-8"))
            blocker = formula_blockers(text)
            if blocker:
                raise StetwiseRefusal(
                    f"a formula on {sheet.name} contains {blocker}",
                    "unsafe_formula",
                )
        root = parse_xml(xml, sheet.path)
        for node in all_named(root, "formula"):
            text = node.text or ""
            blocker = formula_blockers(text)
            if blocker:
                raise StetwiseRefusal(f"a conditional-formatting or validation formula contains {blocker}", "unsafe_formula")
    wb = parts["xl/workbook.xml"]
    for match in _DEFINED.finditer(wb):
        text = unescape(match.group(2).decode("utf-8"))
        blocker = formula_blockers(text)
        if blocker:
            raise StetwiseRefusal(f"defined name contains {blocker}", "unsafe_formula")
        qualified = re.sub(
            r"(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_.]*)!\$?[A-Za-z]{1,3}\$?\d+(?::\$?[A-Za-z]{1,3}\$?\d+)?",
            "",
            text,
        )
        qualified = re.sub(
            r"(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_.]*)!(?:\$?[A-Za-z]{1,3}:\$?[A-Za-z]{1,3}|\$?\d+:\$?\d+)",
            "",
            qualified,
        )
        if re.search(r"\$?[A-Za-z]{1,3}\$?\d+", qualified):
            raise StetwiseRefusal(
                "a defined name uses a cell reference without a sheet name",
                "unsafe_formula",
            )


def insert_rows(package: Package, sheet_name: str, row: int, count: int) -> EditResult:
    return _insert(package, sheet_name, Shift(sheet=sheet_name, row_at=row, row_count=count), "insert_rows")


def insert_cols(package: Package, sheet_name: str, column: str, count: int) -> EditResult:
    return _insert(
        package,
        sheet_name,
        Shift(sheet=sheet_name, col_at=col_to_num(column), col_count=count),
        "insert_cols",
    )


def _insert(package: Package, sheet_name: str, shift: Shift, op: str) -> EditResult:
    if shift.row_count < 1 and shift.col_count < 1:
        raise StetwiseRefusal("Insert count must be at least 1.", "bad_ref")
    _sheet_by_name(package, sheet_name)
    blockers = _insert_blockers(package)
    if blockers:
        raise StetwiseRefusal(
            "v1 will not insert rows or columns in this workbook because it has "
            + "; ".join(blockers)
            + ". Cell edits that do not touch those features are still available. Nothing was changed.",
            "unsafe_structure",
        )
    parts = package.parts()
    result = EditResult()
    for sheet in sheet_map(package):
        xml = parts[sheet.path]
        if sheet.name == sheet_name:
            updated = _rewrite_sheet_structure(xml, sheet.name, shift)
        else:
            updated = _rewrite_sheet_formulas_only(xml, sheet.name, shift)
        if updated is not None and updated != xml:
            result.replacements[sheet.path] = updated
    workbook = _rewrite_defined_names(parts["xl/workbook.xml"], shift)
    workbook = _enable_full_calc(workbook)
    if workbook != parts["xl/workbook.xml"]:
        result.replacements["xl/workbook.xml"] = workbook
    if "xl/calcChain.xml" in parts:
        result.drop.add("xl/calcChain.xml")
        new_rels = _REL.sub(b"", parts["xl/_rels/workbook.xml.rels"])
        new_types = _CT_CALC.sub(b"", parts["[Content_Types].xml"])
        if new_rels == parts["xl/_rels/workbook.xml.rels"] or new_types == parts["[Content_Types].xml"]:
            raise StetwiseRefusal(
                "This workbook's calc chain is not in a form v1 can remove safely.",
                "calc_chain",
            )
        result.replacements["xl/_rels/workbook.xml.rels"] = new_rels
        result.replacements["[Content_Types].xml"] = new_types
    result.warnings.append(
        "Cached formula values were not recalculated. fullCalcOnLoad was set so Excel recalculates on open."
    )
    where = f"row {shift.row_at}" if shift.row_at else f"column {num_to_col(shift.col_at or 1)}"
    result.diff.append(
        {
            "op": op,
            "sheet": sheet_name,
            "at": where,
            "count": shift.row_count or shift.col_count,
            "parts": sorted(result.replacements),
        }
    )
    return result


def replace_text(package: Package, find: str, replace: str, occurrence: int | None = None) -> EditResult:
    from .xlsx_ops import replace_text as _replace_text

    return _replace_text(package, find, replace, occurrence)


def format_cells(package: Package, sheet_name: str, refs: list, options: dict) -> EditResult:
    from .xlsx_ops import format_cells as _format_cells

    return _format_cells(package, sheet_name, refs, options)


def _rewrite_sheet_formulas_only(xml: bytes, home: str, shift: Shift) -> bytes | None:
    changed = False

    def sub(match: re.Match[bytes]) -> bytes:
        nonlocal changed
        text = unescape(match.group(2).decode("utf-8"))
        updated = shift_formula(text, home, shift)
        if updated == text:
            return match.group(0)
        changed = True
        attr = match.group(1) or b""
        return b"<f" + attr + b">" + xml_escape(updated).encode("utf-8") + b"</f>"

    new = _FORMULA.sub(sub, xml)
    root = parse_xml(xml, home)
    # Validation and conditional formatting formulas are <formula> not <f>.
    for node in all_named(root, "formula"):
        text = node.text or ""
        updated = shift_formula(text, home, shift)
        if updated != text:
            changed = True
            node.text = updated
    if any(local(node.tag) == "formula" and (node.text or "") for node in all_named(parse_xml(new, home), "formula")):
        # If <f> replacements were the only change, `new` is enough. If formula
        # elements changed, serialize just those by a second surgical pass.
        pass
    formula_nodes_changed = False
    pattern = re.compile(br"<formula(\s[^>]*)?>(.*?)</formula>", re.DOTALL)

    def sub_formula(match: re.Match[bytes]) -> bytes:
        nonlocal formula_nodes_changed
        text = unescape(match.group(2).decode("utf-8"))
        updated = shift_formula(text, home, shift)
        if updated == text:
            return match.group(0)
        formula_nodes_changed = True
        attr = match.group(1) or b""
        return b"<formula" + attr + b">" + xml_escape(updated).encode("utf-8") + b"</formula>"

    new = pattern.sub(sub_formula, new)
    if not changed and not formula_nodes_changed:
        return None
    return new


def _rewrite_defined_names(xml: bytes, shift: Shift) -> bytes:
    def sub(match: re.Match[bytes]) -> bytes:
        text = unescape(match.group(2).decode("utf-8"))
        updated = shift_formula(text, shift.sheet, shift)
        if updated == text:
            return match.group(0)
        return b"<definedName" + match.group(1) + b">" + xml_escape(updated).encode("utf-8") + b"</definedName>"

    return _DEFINED.sub(sub, xml)


def _rewrite_sheet_structure(xml: bytes, home: str, shift: Shift) -> bytes:
    root = parse_xml(xml, home)
    sheet_data = first_child(root, "sheetData")
    if sheet_data is None:
        raise StetwiseRefusal("The worksheet has no sheetData element.", "malformed_xml")
    for row in children(sheet_data, "row"):
        raw = row.get("r")
        if not raw or not raw.isdigit():
            continue
        new_row = int(shift_cell_ref(f"A{raw}", shift)[1:])
        if shift.col_at:
            # Row numbers stay; columns on cells move below.
            new_row = int(raw)
        row.set("r", str(new_row))
        for cell in children(row, "c"):
            ref = cell.get("r")
            if not ref:
                continue
            cell.set("r", shift_cell_ref(ref, shift))
            for child in cell:
                if local(child.tag) == "f" and child.text:
                    child.text = shift_formula(child.text, home, shift)
    for node in all_named(root, "mergeCell"):
        ref = node.get("ref")
        if ref:
            node.set("ref", shift_ref_text(ref, home, shift))
    for node in all_named(root, "conditionalFormatting"):
        sqref = node.get("sqref")
        if sqref:
            node.set("sqref", shift_ref_text(sqref, home, shift))
    for node in all_named(root, "dataValidation"):
        sqref = node.get("sqref")
        if sqref:
            node.set("sqref", shift_ref_text(sqref, home, shift))
    for node in all_named(root, "formula"):
        if node.text:
            node.text = shift_formula(node.text, home, shift)
    dim = first_child(root, "dimension")
    if dim is not None and dim.get("ref"):
        dim.set("ref", shift_ref_text(dim.get("ref") or "", home, shift))
    # Keep the original declaration when present.
    body = etree.tostring(root, xml_declaration=False, encoding="UTF-8")
    decl = b""
    if xml.startswith(b"<?xml"):
        decl = xml.split(b"?>", 1)[0] + b"?>"
        rest = xml.split(b"?>", 1)[1]
        if rest.startswith(b"\r\n"):
            decl += b"\r\n"
        elif rest.startswith(b"\n"):
            decl += b"\n"
    else:
        decl = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    return decl + body
