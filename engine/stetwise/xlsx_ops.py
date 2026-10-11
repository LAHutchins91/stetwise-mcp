"""Excel edits that stay inside the sheet, plus style records appended for format_cells."""

from __future__ import annotations

import html
import re

from .errors import StetwiseRefusal
from .refs import cell_in_range, col_to_num, num_to_col, offset_formula, split_ref
from .xmlutil import reject_controls, xml_escape
from .zippack import Package

_F_SELF = re.compile(br"<f\b[^>]*/>")
_F_OPEN = re.compile(br"<f\b[^>]*>.*?</f>", re.DOTALL)


def _formula_span(inner: bytes) -> tuple[int, int] | None:
    match = _F_SELF.search(inner) or _F_OPEN.search(inner)
    if not match:
        return None
    return match.start(), match.end()


def _pos(ref: str) -> tuple[int, int]:
    col, row, _, _ = split_ref(ref)
    return col, row


def _range_text(c1: int, r1: int, c2: int, r2: int) -> str:
    start = f"{num_to_col(c1)}{r1}"
    end = f"{num_to_col(c2)}{r2}"
    return start if start == end else f"{start}:{end}"


def _groups(xml: bytes) -> dict[str, dict]:
    from .xlsx import _CELL_OPEN, _attrs, _iter_tag_spans

    groups: dict[str, dict] = {}
    for _start, _end, open_tag, inner in _iter_tag_spans(xml, _CELL_OPEN, b"c"):
        ref = dict(_attrs(open_tag)).get("r")
        span = _formula_span(inner)
        if not ref or not span:
            continue
        tag = inner[span[0] : span[1]]
        gt = tag.find(b">")
        attrs = dict(_attrs(tag[: gt + 1] if not tag.endswith(b"/>") else tag[:-2] + b">"))
        if attrs.get("t") != "shared":
            continue
        si = attrs.get("si") or ""
        text = b""
        if tag.endswith(b"</f>") and b">" in tag:
            text = tag[tag.find(b">") + 1 : -4]
        group = groups.setdefault(si, {"si": si, "cells": [], "master": None, "formula": "", "ref": attrs.get("ref") or ""})
        group["cells"].append(ref)
        if text or attrs.get("ref"):
            group["master"] = ref
            group["formula"] = html.unescape(text.decode("utf-8"))
            group["ref"] = attrs.get("ref") or group["ref"]
    return groups


def _set_formula_xml(xml: bytes, ref: str, formula_xml: bytes) -> bytes:
    from .xlsx import _CELL_OPEN, _attrs, _iter_tag_spans

    for start, end, open_tag, inner in _iter_tag_spans(xml, _CELL_OPEN, b"c"):
        if dict(_attrs(open_tag)).get("r") != ref:
            continue
        span = _formula_span(inner)
        if not span:
            raise StetwiseRefusal(f"Cell {ref} has no formula element to rewrite.", "shared_formula")
        new_inner = inner[: span[0]] + formula_xml + inner[span[1] :]
        close = b"" if open_tag.endswith(b"/>") else b"</c>"
        if open_tag.endswith(b"/>"):
            raise StetwiseRefusal(f"Cell {ref} is empty, so its shared formula cannot be split.", "shared_formula")
        return xml[:start] + open_tag + new_inner + close + xml[end:]
    raise StetwiseRefusal(f"Cell {ref} was not found while splitting a shared formula.", "shared_formula")


def _plain_formula(text: str) -> bytes:
    return f"<f>{xml_escape(text)}</f>".encode("utf-8")


def _shared_formula(text: str, ref: str, si: str) -> bytes:
    return f'<f t="shared" ref="{xml_escape(ref)}" si="{xml_escape(si)}">{xml_escape(text)}</f>'.encode("utf-8")


def _solid_line(cells: list[str]) -> tuple[str, list[str]] | None:
    positions = {ref: _pos(ref) for ref in cells}
    cols = {col for col, _row in positions.values()}
    rows = {row for _col, row in positions.values()}
    ordered = sorted(cells, key=lambda ref: positions[ref])
    if len(cols) == 1:
        rmin = min(rows)
        rmax = max(rows)
        if len(rows) == rmax - rmin + 1:
            return "col", ordered
    if len(rows) == 1:
        cmin = min(cols)
        cmax = max(cols)
        if len(cols) == cmax - cmin + 1:
            return "row", ordered
    return None


def release_shared(xml: bytes, ref: str) -> tuple[bytes, list[dict]]:
    """Take one cell out of its shared-formula group without changing the others' results."""
    groups = _groups(xml)
    group = next((item for item in groups.values() if ref in item["cells"]), None)
    if group is None:
        return xml, []
    master = group["master"]
    formula = group["formula"]
    if not master or not formula:
        raise StetwiseRefusal(
            f"Cell {ref} is in a shared formula group whose master formula is missing. v1 will not guess it.",
            "shared_formula",
        )
    notes = []
    cells = list(dict.fromkeys(group["cells"]))
    line = _solid_line(cells)
    master_col, master_row = _pos(master)

    def shifted(target: str) -> str:
        col, row = _pos(target)
        return offset_formula(formula, col - master_col, row - master_row)

    endpoint = False
    ordered: list[str] = []
    if line is not None:
        _axis, ordered = line
        endpoint = ref in {ordered[0], ordered[-1]}
    if line is not None and endpoint and len(ordered) > 1:
        remaining = [cell for cell in ordered if cell != ref]
        if len(remaining) == 1:
            only = remaining[0]
            xml = _set_formula_xml(xml, only, _plain_formula(shifted(only)))
            notes.append({"ref": only, "action": "unshared", "formula": shifted(only)})
            return xml, notes
        new_master = remaining[0]
        c1, r1 = _pos(remaining[0])
        c2, r2 = _pos(remaining[-1])
        ref_text = _range_text(c1, r1, c2, r2)
        if new_master == master:
            # Shrink the ref on the existing master. Children stay shared.
            xml = _set_formula_xml(xml, master, _shared_formula(formula, ref_text, group["si"]))
            notes.append({"ref": master, "action": "shrunk", "formula": formula, "ref_range": ref_text})
            return xml, notes
        xml = _set_formula_xml(xml, new_master, _shared_formula(shifted(new_master), ref_text, group["si"]))
        notes.append({"ref": new_master, "action": "promoted", "formula": shifted(new_master), "ref_range": ref_text})
        return xml, notes
    for cell in cells:
        if cell == ref:
            continue
        text = shifted(cell)
        xml = _set_formula_xml(xml, cell, _plain_formula(text))
        notes.append({"ref": cell, "action": "unshared", "formula": text})
    return xml, notes


def table_role(box: dict, ref: str) -> str:
    if not box.get("ref") or not cell_in_range(ref, box["ref"]):
        return "outside"
    _c, row, _, _ = split_ref(ref)
    c1, r1, _c2, r2 = split_ref(box["ref"])
    header = int(box.get("header_rows") or 0)
    totals = int(box.get("total_rows") or 0)
    if row < r1 + header:
        return "header"
    if totals and row > r2 - totals:
        return "totals"
    return "body"


def replace_text(package: Package, find: str, replace: str, occurrence: int | None = None):
    from .xlsx import EditResult, _read_cells, _shared_strings, _sheet_by_name, _styles, _write_cell, sheet_map

    if not find:
        raise StetwiseRefusal("The text to find is empty.", "empty_query")
    reject_controls(replace, "Replacement text")
    strings = _shared_strings(package)
    formats = _styles(package)
    hits = []
    seen = 0
    for sheet in sheet_map(package):
        root_xml = package.parts()[sheet.path]
        from .xmlutil import parse_xml

        root = parse_xml(root_xml, sheet.path)
        for cell in _read_cells(root, strings, formats):
            if cell.get("formula") is not None:
                continue
            value = cell.get("value")
            if not isinstance(value, str) or find not in value:
                continue
            start = 0
            while True:
                found = value.find(find, start)
                if found < 0:
                    break
                if occurrence is None or seen == occurrence - 1:
                    hits.append((sheet, cell, found))
                seen += 1
                start = found + len(find)
    if not hits:
        raise StetwiseRefusal(
            "No text values contained that text. Formula text is not replaced.",
            "not_found",
        )
    by_cell: dict[tuple[str, str], list] = {}
    offsets: dict[tuple[str, str], list[int]] = {}
    for sheet, cell, found in hits:
        key = (sheet.path, cell["ref"])
        by_cell[key] = [sheet, cell]
        offsets.setdefault(key, []).append(found)
    result = EditResult()
    changes = []
    current = {sheet.path: package.parts()[sheet.path] for sheet in sheet_map(package)}
    for (path, ref), (sheet, cell) in by_cell.items():
        value = str(cell["value"])
        updated = value
        for found in sorted(offsets[(path, ref)], reverse=True):
            updated = updated[:found] + replace + updated[found + len(find) :]
        if updated == value:
            continue
        inner = f'<is><t xml:space="preserve">{xml_escape(updated)}</t></is>'.encode("utf-8") if (
            updated[:1].isspace() or updated[-1:].isspace()
        ) else f"<is><t>{xml_escape(updated)}</t></is>".encode("utf-8")
        current[path] = _write_cell(current[path], ref, inner, "inlineStr")
        changes.append({"sheet": sheet.name, "ref": ref, "before": value, "after": updated})
    for path, xml in current.items():
        if xml != package.parts()[path]:
            result.replacements[path] = xml
    if not result.replacements:
        raise StetwiseRefusal("The text replace did not change a cell.", "empty_edit")
    result.diff.append({"op": "replace_text", "changes": changes})
    result.warnings.append("Only text values were changed. Formulas and sharedStrings.xml were left byte-for-byte identical.")
    return result


def format_cells(package: Package, sheet_name: str, refs: list[str], options: dict):
    from .xlsx import EditResult, _expand_dimension, _protected, _sheet_by_name, _write_cell

    bold = options.get("bold")
    italic = options.get("italic")
    fill = options.get("fill")
    number_format = options.get("number_format")
    horizontal = options.get("horizontal")
    width = options.get("column_width")
    font_size = options.get("font_size")
    font_color = options.get("font_color")
    border = options.get("border")
    if font_size is not None:
        try:
            font_size = float(font_size)
        except (TypeError, ValueError):
            font_size = -1
        if not (font_size > 0 and font_size <= 409):
            raise StetwiseRefusal("Font size must be greater than 0 and at most 409.", "bad_format")
    if font_color is not None and not re.fullmatch(r"[0-9A-Fa-f]{6}", str(font_color)):
        raise StetwiseRefusal("Font colour must be six hex digits, RRGGBB.", "bad_format")
    if border is not None and border is not True:
        raise StetwiseRefusal("border must be true to add a thin black outline.", "bad_format")
    if not any(item is not None for item in (bold, italic, fill, number_format, horizontal, width, font_size, font_color, border)):
        raise StetwiseRefusal(
            "format_cells needs a number format, bold, italic, fill, alignment, column width, font size, font colour, or a border.",
            "empty_edit",
        )
    if not refs:
        raise StetwiseRefusal("format_cells needs at least one cell.", "empty_edit")
    if len(refs) > 5000:
        raise StetwiseRefusal("v1 formats at most 5000 cells in one edit.", "too_many")
    if fill is not None and not re.fullmatch(r"[0-9A-Fa-f]{6}", str(fill)):
        raise StetwiseRefusal("Fill must be six hex digits, RRGGBB.", "bad_format")
    if horizontal is not None and horizontal not in {"left", "center", "right"}:
        raise StetwiseRefusal("Alignment must be left, center, or right.", "bad_format")
    sheet = _sheet_by_name(package, sheet_name)
    parts = package.parts()
    if _protected(parts[sheet.path]) or _protected(parts["xl/workbook.xml"]):
        raise StetwiseRefusal(f"Sheet {sheet.name!r} or the workbook is protected. v1 will not bypass protection.", "protected")
    styles = parts.get("xl/styles.xml")
    if styles is None:
        raise StetwiseRefusal("This workbook has no styles.xml, so v1 will not invent a stylesheet.", "malformed_xml")
    if b"theme" in styles[:200].lower() and b"<theme" in styles:
        raise StetwiseRefusal("styles.xml does not look like a stylesheet. Nothing was changed.", "malformed_xml")
    normalized = []
    for ref in refs:
        text = str(ref).replace("$", "")
        if not re.fullmatch(r"[A-Za-z]{1,3}\d+", text):
            raise StetwiseRefusal(f"Cell reference {ref!r} is not a single cell.", "bad_ref")
        col = "".join(ch for ch in text if ch.isalpha())
        row = "".join(ch for ch in text if ch.isdigit())
        normalized.append(f"{num_to_col(col_to_num(col))}{row}")
    xml = parts[sheet.path]
    style_xml = styles
    changes = []
    cache: dict[tuple, int] = {}
    for ref in normalized:
        index = _style_index(xml, ref)
        key = (
            index,
            bold,
            italic,
            str(fill).upper() if fill else None,
            number_format,
            horizontal,
            font_size,
            str(font_color).upper() if font_color else None,
            border is True,
        )
        if key not in cache:
            style_xml, new_index = _append_style(
                style_xml, index, bold, italic, fill, number_format, horizontal, font_size, font_color, border is True
            )
            cache[key] = new_index
        xml = _set_style_attr(xml, ref, cache[key])
        changes.append({"ref": ref, "style_index": cache[key]})
    if width is not None:
        columns = sorted({col_to_num("".join(ch for ch in ref if ch.isalpha())) for ref in normalized})
        for column in columns:
            xml = _set_column_width(xml, column, float(width))
    xml = _expand_dimension(xml, normalized)
    result = EditResult(
        diff=[{"op": "format_cells", "sheet": sheet.name, "changes": changes}],
        warnings=["New style records were appended. Existing style records and the theme were not rewritten."],
    )
    if xml != parts[sheet.path]:
        result.replacements[sheet.path] = xml
    if style_xml != styles:
        result.replacements["xl/styles.xml"] = style_xml
    if not result.replacements:
        raise StetwiseRefusal("The format edit did not change the sheet.", "empty_edit")
    return result


def _style_index(xml: bytes, ref: str) -> int:
    from .xlsx import _CELL_OPEN, _attrs, _iter_tag_spans

    for _start, _end, open_tag, _inner in _iter_tag_spans(xml, _CELL_OPEN, b"c"):
        attrs = dict(_attrs(open_tag))
        if attrs.get("r") == ref and attrs.get("s", "").isdigit():
            return int(attrs["s"])
    return 0


def _set_style_attr(xml: bytes, ref: str, index: int) -> bytes:
    from .xlsx import _CELL_OPEN, _attrs, _insert_missing_cell, _iter_tag_spans, _render_cell

    for start, end, open_tag, inner in _iter_tag_spans(xml, _CELL_OPEN, b"c"):
        attrs = _attrs(open_tag)
        if dict(attrs).get("r") != ref:
            continue
        attrs = _set_s(attrs, index)
        if open_tag.endswith(b"/>"):
            rendered = _render_cell(attrs, b"")
        else:
            rendered = _render_cell(attrs, inner)
        return xml[:start] + rendered + xml[end:]
    return _insert_missing_cell(xml, ref, _render_cell([("r", ref), ("s", str(index))], b""))


def _set_s(attrs: list[tuple[str, str]], index: int) -> list[tuple[str, str]]:
    out = []
    found = False
    for key, value in attrs:
        if key == "s":
            out.append((key, str(index)))
            found = True
        else:
            out.append((key, value))
    if not found:
        # Keep r first when it is already first.
        out.append(("s", str(index)))
    return out


def _parent(xml: bytes, tag: str) -> tuple[int, int, int]:
    match = re.search(br"<" + tag.encode() + br"\b([^>]*)>", xml)
    if not match:
        raise StetwiseRefusal(f"styles.xml has no {tag} list, so v1 will not invent the stylesheet.", "unsafe_style")
    close = xml.find(f"</{tag}>".encode(), match.end())
    if close < 0:
        raise StetwiseRefusal(f"styles.xml {tag} list is not closed.", "unsafe_style")
    return match.start(), match.end(), close


def _children(inner: bytes, tag: str) -> list[bytes]:
    items = []
    i = 0
    token = f"<{tag}".encode()
    while True:
        start = inner.find(token, i)
        if start < 0:
            break
        nxt = inner[start + len(token) : start + len(token) + 1]
        if nxt not in (b">", b" ", b"/"):
            i = start + len(token)
            continue
        gt = inner.find(b">", start)
        if gt < 0:
            break
        if inner[gt - 1 : gt + 1] == b"/>":
            items.append(inner[start : gt + 1])
            i = gt + 1
            continue
        close = inner.find(f"</{tag}>".encode(), gt)
        if close < 0:
            break
        items.append(inner[start : close + len(tag) + 3])
        i = close + len(tag) + 3
    return items


def _append_child(xml: bytes, tag: str, child: bytes) -> bytes:
    open_at, inner_at, close_at = _parent(xml, tag)
    open_tag = xml[open_at:inner_at]
    count = re.search(br'count="(\d+)"', open_tag)
    if not count:
        raise StetwiseRefusal(f"styles.xml {tag} has no count attribute.", "unsafe_style")
    new_open = open_tag[: count.start(1)] + str(int(count.group(1)) + 1).encode() + open_tag[count.end(1) :]
    return xml[:open_at] + new_open + xml[inner_at:close_at] + child + xml[close_at:]


_THIN_BORDER = (
    b'<border><left style="thin"><color rgb="FF000000"/></left>'
    b'<right style="thin"><color rgb="FF000000"/></right>'
    b'<top style="thin"><color rgb="FF000000"/></top>'
    b'<bottom style="thin"><color rgb="FF000000"/></bottom>'
    b"<diagonal/></border>"
)


def _size_text(size: float) -> str:
    if float(size).is_integer():
        return str(int(size))
    return format(size, ".2f").rstrip("0").rstrip(".")


def _append_style(
    xml: bytes, index: int, bold, italic, fill, number_format, horizontal, font_size=None, font_color=None, border=False
) -> tuple[bytes, int]:
    from .xlsx import BUILTIN_FMT

    _open, inner_at, close_at = _parent(xml, "cellXfs")
    xfs = _children(xml[inner_at:close_at], "xf")
    if index >= len(xfs):
        raise StetwiseRefusal(f"Style index {index} is outside cellXfs.", "unsafe_style")
    xf = xfs[index]
    font_id = _attr(xf, "fontId") or "0"
    fill_id = _attr(xf, "fillId") or "0"
    num_id = _attr(xf, "numFmtId") or "0"
    border_id = None
    font_changed = bold is not None or italic is not None or font_size is not None or font_color is not None
    if font_changed:
        fonts_open, fonts_inner, fonts_close = _parent(xml, "fonts")
        fonts = _children(xml[fonts_inner:fonts_close], "font")
        if int(font_id) >= len(fonts):
            raise StetwiseRefusal("The cell's font record is missing.", "unsafe_style")
        font = _font_with(fonts[int(font_id)], bold, italic, font_size, font_color)
        xml = _append_child(xml, "fonts", font)
        font_id = str(len(fonts))
    if border:
        _bo, borders_inner, borders_close = _parent(xml, "borders")
        borders = _children(xml[borders_inner:borders_close], "border")
        xml = _append_child(xml, "borders", _THIN_BORDER)
        border_id = str(len(borders))
    if fill:
        paint = (
            f'<fill><patternFill patternType="solid"><fgColor rgb="FF{fill.upper()}"/>'
            f'<bgColor indexed="64"/></patternFill></fill>'
        ).encode()
        _fo, fills_inner, fills_close = _parent(xml, "fills")
        fills = _children(xml[fills_inner:fills_close], "fill")
        xml = _append_child(xml, "fills", paint)
        fill_id = str(len(fills))
    if number_format:
        wanted = str(number_format)
        builtin = next((str(key) for key, label in BUILTIN_FMT.items() if label == wanted), None)
        if builtin is not None:
            num_id = builtin
        else:
            num_id = str(_next_num_fmt(xml))
            record = f'<numFmt numFmtId="{num_id}" formatCode="{xml_escape(wanted)}"/>'.encode()
            if b"<numFmts" not in xml:
                fonts_at = xml.find(b"<fonts")
                if fonts_at < 0:
                    raise StetwiseRefusal("styles.xml has no fonts list.", "unsafe_style")
                xml = xml[:fonts_at] + b'<numFmts count="1">' + record + b"</numFmts>" + xml[fonts_at:]
            else:
                xml = _append_child(xml, "numFmts", record)
    xf_xml = _xf_with(
        xf,
        font_id,
        fill_id,
        num_id,
        horizontal,
        number_format is not None,
        font_changed,
        fill is not None,
        border_id,
    )
    _xo, x_inner, x_close = _parent(xml, "cellXfs")
    count = len(_children(xml[x_inner:x_close], "xf"))
    xml = _append_child(xml, "cellXfs", xf_xml)
    return xml, count


def _attr(tag: bytes, name: str) -> str | None:
    match = re.search(br"\b" + name.encode() + br'="([^"]*)"', tag)
    if not match:
        return None
    return match.group(1).decode("utf-8")


def _font_with(font: bytes, bold, italic, size=None, color=None) -> bytes:
    if font.endswith(b"/>"):
        inner = b""
        body = b"<font>"
        end = b"</font>"
    else:
        gt = font.find(b">")
        body = font[: gt + 1]
        end = b"</font>"
        inner = font[gt + 1 : -len(end)]
    inner = re.sub(br"<b\b[^>]*/>", b"", inner)
    inner = re.sub(br"<i\b[^>]*/>", b"", inner)
    prefix = b""
    if bold is True:
        prefix += b"<b/>"
    elif bold is False:
        prefix += b'<b val="0"/>'
    if italic is True:
        prefix += b"<i/>"
    elif italic is False:
        prefix += b'<i val="0"/>'
    if size is not None:
        inner = re.sub(br"<sz\b[^>]*/>", b"", inner)
        inner = re.sub(br"<sz\b[^>]*>.*?</sz>", b"", inner, flags=re.DOTALL)
        prefix += f'<sz val="{_size_text(float(size))}"/>'.encode()
    if color is not None:
        inner = re.sub(br"<color\b[^>]*/>", b"", inner)
        inner = re.sub(br"<color\b[^>]*>.*?</color>", b"", inner, flags=re.DOTALL)
        prefix += f'<color rgb="FF{str(color).upper()}"/>'.encode()
    return body + prefix + inner + end


def _next_num_fmt(xml: bytes) -> int:
    ids = [int(item) for item in re.findall(br'numFmtId="(\d+)"', xml)]
    return max([164, *ids]) + 1 if ids else 164


def _xf_with(
    xf: bytes,
    font_id: str,
    fill_id: str,
    num_id: str,
    horizontal,
    number: bool,
    font: bool,
    fill: bool,
    border_id: str | None = None,
) -> bytes:
    self_close = xf.endswith(b"/>")
    if self_close:
        head = xf[:-2].rstrip()
        inner = b""
        tail = b"/>"
    else:
        gt = xf.find(b">")
        head = xf[:gt]
        inner = xf[gt + 1 : -5]
        tail = b">"
    head = _replace_attr(head, "fontId", font_id)
    head = _replace_attr(head, "fillId", fill_id)
    head = _replace_attr(head, "numFmtId", num_id)
    if number:
        head = _replace_attr(head, "applyNumberFormat", "1")
    if font:
        head = _replace_attr(head, "applyFont", "1")
    if fill:
        head = _replace_attr(head, "applyFill", "1")
    if border_id is not None:
        head = _replace_attr(head, "borderId", border_id)
        head = _replace_attr(head, "applyBorder", "1")
    if horizontal:
        head = _replace_attr(head, "applyAlignment", "1")
        align = f'<alignment horizontal="{horizontal}"/>'.encode()
        inner = re.sub(br"<alignment\b[^>]*/>", b"", inner) + align
        if self_close:
            return head + b">" + inner + b"</xf>"
    if self_close:
        return head + b"/>"
    return head + tail + inner + b"</xf>"


def _replace_attr(head: bytes, name: str, value: str) -> bytes:
    pattern = br"\b" + name.encode() + br'="[^"]*"'
    replacement = f'{name}="{value}"'.encode()
    if re.search(pattern, head):
        return re.sub(pattern, replacement, head, count=1)
    return head + b" " + replacement


def _set_column_width(xml: bytes, column: int, width: float) -> bytes:
    text = format(width, ".2f").rstrip("0").rstrip(".")
    snippet = f'<col min="{column}" max="{column}" width="{text}" customWidth="1"/>'.encode()
    match = re.search(br"<cols\b[^>]*>(.*?)</cols>", xml, re.DOTALL)
    if not match:
        empty = re.search(br"<cols\b[^>]*/>", xml)
        block = b"<cols>" + snippet + b"</cols>"
        if empty:
            return xml[: empty.start()] + block + xml[empty.end() :]
        marker = xml.find(b"<sheetData")
        if marker < 0:
            raise StetwiseRefusal("The worksheet has no sheetData element.", "malformed_xml")
        return xml[:marker] + block + xml[marker:]
    inner = match.group(1)
    cols = _children(inner, "col")
    rebuilt: list[bytes] = []
    inserted = False
    for col in cols:
        start = int(_attr(col, "min") or "0")
        end = int(_attr(col, "max") or "0")
        if end < column:
            rebuilt.append(col)
            continue
        if start > column:
            if not inserted:
                rebuilt.append(snippet)
                inserted = True
            rebuilt.append(col)
            continue
        if start == column == end:
            rebuilt.append(snippet)
            inserted = True
            continue
        # Split a span so the other columns keep their width.
        if start < column:
            rebuilt.append(_col_span(col, start, column - 1))
        rebuilt.append(snippet)
        inserted = True
        if end > column:
            rebuilt.append(_col_span(col, column + 1, end))
    if not inserted:
        rebuilt.append(snippet)
    return xml[: match.start(1)] + b"".join(rebuilt) + xml[match.end(1) :]


def _col_span(col: bytes, start: int, end: int) -> bytes:
    updated = _replace_attr(col, "min", str(start))
    return _replace_attr(updated, "max", str(end))
