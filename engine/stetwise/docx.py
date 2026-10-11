"""Word .docx reads and run-aware text edits.

Edits change w:t text nodes in word/document.xml and leave every other part
byte-for-byte identical. Documents with track-changes currently turned on, or
with document protection, are refused rather than rewritten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from .errors import StetwiseRefusal
from .xmlutil import all_named, local, parse_xml, reject_controls, replace_word_texts, word_text_spans
from .zippack import Package

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


@dataclass
class EditResult:
    replacements: dict[str, bytes] = field(default_factory=dict)
    drop: set[str] = field(default_factory=set)
    diff: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _document(package: Package) -> bytes:
    data = package.parts().get("word/document.xml")
    if data is None:
        raise StetwiseRefusal("This file has no word/document.xml.", "not_docx")
    return data


def _settings(package: Package) -> bytes:
    return package.parts().get("word/settings.xml", b"")


def tracking_on(package: Package) -> bool:
    settings = _settings(package)
    return b"<w:trackRevisions" in settings


def protected(package: Package) -> bool:
    settings = _settings(package)
    return b"<w:documentProtection" in settings


def _in_ancestor(node: etree._Element, names: set[str]) -> bool:
    parent = node.getparent()
    while parent is not None:
        if local(parent.tag) in names:
            return True
        parent = parent.getparent()
    return False


def _paragraphs(root: etree._Element) -> list[etree._Element]:
    return [node for node in root.iter() if local(node.tag) == "p"]


def _visible_runs(paragraph: etree._Element) -> list[etree._Element]:
    runs = []
    for node in paragraph.iter():
        if local(node.tag) == "t" and not _in_ancestor(node, {"del"}):
            runs.append(node)
    return runs


def _style_of(paragraph: etree._Element) -> str | None:
    for node in paragraph.iter():
        if local(node.tag) == "pStyle":
            return node.get(f"{{{W}}}val") or node.get("val")
    return None


def _paragraph_text(paragraph: etree._Element) -> str:
    return "".join((node.text or "") for node in _visible_runs(paragraph))


def _global_t_index(root: etree._Element) -> dict[etree._Element, int]:
    index = {}
    cursor = 0
    for node in root.iter():
        if local(node.tag) == "t":
            index[node] = cursor
            cursor += 1
    return index


def inspect_docx(package: Package) -> dict:
    xml = _document(package)
    root = parse_xml(xml, "word/document.xml")
    paragraphs = []
    headings = []
    for number, paragraph in enumerate(_paragraphs(root)):
        style = _style_of(paragraph)
        text = _paragraph_text(paragraph)
        revisions = sum(1 for node in paragraph.iter() if local(node.tag) in {"ins", "del"})
        item = {"anchor": f"p-{number}", "style": style, "text": text, "revision_marks": revisions}
        paragraphs.append(item)
        if style and style.lower().startswith("heading"):
            headings.append(item)
    tables = [node for node in root.iter() if local(node.tag) == "tbl" and not _in_ancestor(node, {"tbl"})]
    comments = 0
    if "word/comments.xml" in package.parts():
        comments_root = parse_xml(package.parts()["word/comments.xml"], "word/comments.xml")
        comments = sum(1 for node in comments_root.iter() if local(node.tag) == "comment")
    ins = sum(1 for node in root.iter() if local(node.tag) == "ins")
    deletes = sum(1 for node in root.iter() if local(node.tag) == "del")
    return {
        "kind": "docx",
        "paragraphs": len(paragraphs),
        "headings": [{"anchor": item["anchor"], "style": item["style"], "text": item["text"]} for item in headings],
        "tables": len(tables),
        "comments": comments,
        "tracked_insertions": ins,
        "tracked_deletions": deletes,
        "track_revisions_on": tracking_on(package),
        "protected": protected(package),
        "styles_part": "word/styles.xml" in package.parts(),
        "features_left_standing": [
            "styles",
            "numbering",
            "comments",
            "existing tracked changes",
            "themes",
            "headers and footers",
        ],
        "preview": paragraphs[:30],
    }


def read_section(package: Package, anchor: str | None = None, limit: int = 80) -> dict:
    root = parse_xml(_document(package), "word/document.xml")
    rows = []
    for number, paragraph in enumerate(_paragraphs(root)):
        item = {
            "anchor": f"p-{number}",
            "style": _style_of(paragraph),
            "text": _paragraph_text(paragraph),
            "revision_marks": sum(1 for node in paragraph.iter() if local(node.tag) in {"ins", "del"}),
        }
        if anchor and item["anchor"] != anchor:
            continue
        rows.append(item)
        if len(rows) >= limit:
            break
    return {"paragraphs": rows, "truncated": len(rows) >= limit}


def _search_part(xml: bytes, part: str, query: str, editable: bool) -> list[dict]:
    try:
        root = parse_xml(xml, part)
    except StetwiseRefusal:
        return []
    hits = []
    for number, paragraph in enumerate(_paragraphs(root)):
        text = _paragraph_text(paragraph)
        start = 0
        while True:
            found = text.find(query, start)
            if found < 0:
                break
            hits.append(
                {
                    "part": part,
                    "anchor": f"p-{number}",
                    "offset": found,
                    "text": text,
                    "editable": editable and part == "word/document.xml",
                }
            )
            start = found + max(len(query), 1)
    return hits


def search_docx(package: Package, query: str, limit: int = 50) -> dict:
    if not query:
        raise StetwiseRefusal("Search text is empty.", "empty_query")
    parts = package.parts()
    hits: list[dict] = []
    ordered = ["word/document.xml"]
    ordered += sorted(
        name
        for name in parts
        if name.startswith("word/")
        and name.endswith(".xml")
        and name not in {"word/document.xml", "word/styles.xml", "word/stylesWithEffects.xml", "word/fontTable.xml", "word/settings.xml", "word/webSettings.xml", "word/numbering.xml"}
        and any(token in name for token in ("header", "footer", "footnote", "endnote", "comment"))
    )
    for name in ordered:
        if name not in parts:
            continue
        editable = name == "word/document.xml"
        hits.extend(_search_part(parts[name], name, query, editable))
        if len(hits) >= limit:
            break
    return {"matches": hits[:limit], "truncated": len(hits) > limit}


def _refuse_tracking(package: Package) -> None:
    if protected(package):
        raise StetwiseRefusal(
            "This document is protected. v1 will not bypass document protection.",
            "protected",
        )
    if tracking_on(package):
        raise StetwiseRefusal(
            "This document has track changes turned on. v1 does not write new revision marks, and it will not bypass tracking with a direct edit. Existing revisions were not modified.",
            "track_revisions",
        )


def _ensure_text_index(xml: bytes, root: etree._Element) -> None:
    nodes = [node for node in root.iter() if local(node.tag) == "t"]
    if len(nodes) != len(word_text_spans(xml)):
        raise StetwiseRefusal(
            "Text runs in this document are not in a form v1 can edit without rewriting the part. Nothing was changed.",
            "unsafe_text",
        )


def _compose_piece_texts(pieces: list[dict], spans: list[tuple[int, int]], replace: str) -> list[str]:
    """Rebuild each run from the original offsets.

    A longer or shorter replacement must not move the coordinates of the next
    match. Spans are original visible-text offsets and do not overlap.
    """
    outputs: list[str] = []
    for piece in pieces:
        original = piece["text"]
        start = piece["start"]
        end = start + len(original)
        if not original or not spans:
            outputs.append(original)
            continue
        chunks: list[str] = []
        pos = start
        for begin, stop in spans:
            if stop <= start or begin >= end:
                continue
            keep_to = min(max(begin, start), end)
            if keep_to > pos:
                chunks.append(original[pos - start : keep_to - start])
            if start <= begin < end:
                chunks.append(replace)
            pos = min(stop, end)
            if pos >= end:
                break
        if pos < end:
            chunks.append(original[pos - start :])
        outputs.append("".join(chunks))
    return outputs


def replace_text(package: Package, find: str, replace: str, occurrence: int | None = None) -> EditResult:
    if not find:
        raise StetwiseRefusal("The text to find is empty.", "empty_query")
    reject_controls(replace, "Replacement text")
    _refuse_tracking(package)
    xml = _document(package)
    root = parse_xml(xml, "word/document.xml")
    _ensure_text_index(xml, root)
    indexes = _global_t_index(root)
    updates: dict[int, str] = {}
    applied = []
    skipped = []
    seen = 0
    for number, paragraph in enumerate(_paragraphs(root)):
        runs = _visible_runs(paragraph)
        pieces = []
        cursor = 0
        for node in runs:
            text = node.text or ""
            pieces.append(
                {
                    "node": node,
                    "text": text,
                    "start": cursor,
                    "revision": _in_ancestor(node, {"ins", "del"}),
                }
            )
            cursor += len(text)
        visible = "".join(piece["text"] for piece in pieces)
        start = 0
        spans = []
        while True:
            found = visible.find(find, start)
            if found < 0:
                break
            spans.append((found, found + len(find)))
            start = found + len(find)
        chosen = []
        for span in spans:
            if occurrence is None or seen == occurrence:
                chosen.append(span)
            seen += 1
        if not chosen:
            continue
        safe: list[tuple[int, int]] = []
        for begin, end in chosen:
            touched = [
                index
                for index, piece in enumerate(pieces)
                if piece["text"] and not (piece["start"] + len(piece["text"]) <= begin or piece["start"] >= end)
            ]
            if not touched:
                continue
            if any(pieces[index]["revision"] for index in touched):
                skipped.append({"anchor": f"p-{number}", "reason": "match overlaps existing tracked changes"})
                continue
            safe.append((begin, end))
            applied.append({"anchor": f"p-{number}", "before": visible[begin:end], "after": replace})
        for piece, text in zip(pieces, _compose_piece_texts(pieces, safe, replace), strict=True):
            if text != piece["text"]:
                updates[indexes[piece["node"]]] = text
    if not applied:
        outside = search_docx(package, find, limit=20)
        elsewhere = [hit for hit in outside["matches"] if hit["part"] != "word/document.xml"]
        if skipped and not elsewhere:
            raise StetwiseRefusal(
                "Every match overlaps existing tracked changes. v1 will not rewrite revision marks.",
                "tracked_change_overlap",
            )
        if elsewhere:
            raise StetwiseRefusal(
                "The only matches are in comments, headers, footers, or notes. v1 does not rewrite those parts, so they stay byte-for-byte identical.",
                "unsupported_part",
            )
        raise StetwiseRefusal(f"No occurrences of {find!r} were found in the document body.", "not_found")
    new_xml = replace_word_texts(xml, updates)
    warnings = []
    if skipped:
        warnings.append(
            f"{len(skipped)} match(es) overlapped existing tracked changes and were left untouched."
        )
    return EditResult(
        replacements={"word/document.xml": new_xml},
        diff=[{"op": "replace_text", "changes": applied}],
        warnings=warnings,
    )


def replace_paragraph(package: Package, anchor: str, text: str) -> EditResult:
    reject_controls(text, "Paragraph text")
    _refuse_tracking(package)
    match = re.fullmatch(r"p-(\d+)", anchor or "")
    if not match:
        raise StetwiseRefusal("Paragraph anchor must look like p-0.", "bad_anchor")
    xml = _document(package)
    root = parse_xml(xml, "word/document.xml")
    _ensure_text_index(xml, root)
    paragraphs = _paragraphs(root)
    number = int(match.group(1))
    if number >= len(paragraphs):
        raise StetwiseRefusal(f"There is no paragraph {anchor}.", "bad_anchor")
    paragraph = paragraphs[number]
    for node in paragraph.iter():
        if local(node.tag) in {"ins", "del", "drawing", "pict", "object"}:
            raise StetwiseRefusal(
                f"Paragraph {anchor} contains tracked changes or embedded objects. v1 will not flatten it.",
                "unsafe_paragraph",
            )
    runs = _visible_runs(paragraph)
    if not runs:
        raise StetwiseRefusal(f"Paragraph {anchor} has no text run to keep formatting from.", "unsafe_paragraph")
    indexes = _global_t_index(root)
    before = _paragraph_text(paragraph)
    updates = {indexes[runs[0]]: text}
    for node in runs[1:]:
        updates[indexes[node]] = ""
    style = _style_of(paragraph)
    return EditResult(
        replacements={"word/document.xml": replace_word_texts(xml, updates)},
        diff=[{"op": "replace_paragraph", "anchor": anchor, "style": style, "before": before, "after": text}],
        warnings=["Paragraph style and the first run's formatting were left in place."],
    )


def update_table_cells(package: Package, table: int, cells: list[dict]) -> EditResult:
    _refuse_tracking(package)
    xml = _document(package)
    root = parse_xml(xml, "word/document.xml")
    _ensure_text_index(xml, root)
    tables = [node for node in root.iter() if local(node.tag) == "tbl" and not _in_ancestor(node, {"tbl"})]
    if table < 0 or table >= len(tables):
        raise StetwiseRefusal(f"There is no table {table}.", "bad_anchor")
    target = tables[table]
    rows = [node for node in target if local(node.tag) == "tr"]
    indexes = _global_t_index(root)
    updates: dict[int, str] = {}
    changes = []
    for spec in cells:
        row_i = spec["row"]
        col_i = spec["col"]
        text = spec["text"]
        reject_controls(text, "Table cell")
        if row_i >= len(rows):
            raise StetwiseRefusal(f"Table {table} has no row {row_i}.", "bad_anchor")
        cols = [node for node in rows[row_i] if local(node.tag) == "tc"]
        if col_i >= len(cols):
            raise StetwiseRefusal(f"Table {table} row {row_i} has no column {col_i}.", "bad_anchor")
        cell = cols[col_i]
        for node in cell.iter():
            if local(node.tag) in {"ins", "del", "drawing", "pict", "object"}:
                raise StetwiseRefusal(
                    f"Table cell {row_i},{col_i} contains tracked changes or objects. v1 will not flatten it.",
                    "unsafe_paragraph",
                )
        paras = [node for node in cell.iter() if local(node.tag) == "p"]
        if len(paras) != 1:
            raise StetwiseRefusal(
                f"Table cell {row_i},{col_i} has {len(paras)} paragraphs. v1 only edits a single-paragraph cell.",
                "unsafe_paragraph",
            )
        runs = _visible_runs(paras[0])
        if not runs:
            raise StetwiseRefusal(f"Table cell {row_i},{col_i} has no text run.", "unsafe_paragraph")
        before = _paragraph_text(paras[0])
        updates[indexes[runs[0]]] = text
        for node in runs[1:]:
            updates[indexes[node]] = ""
        changes.append({"table": table, "row": row_i, "col": col_i, "before": before, "after": text})
    return EditResult(
        replacements={"word/document.xml": replace_word_texts(xml, updates)},
        diff=[{"op": "update_table_cells", "changes": changes}],
    )
