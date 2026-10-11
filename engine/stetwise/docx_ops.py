"""Structural Word edits: tracked revisions, paragraphs, and run formatting.

Direct text replacement stays in docx.py. These paths still change only
word/document.xml, plus word/settings.xml when tracking is switched on.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .errors import StetwiseRefusal
from .xmlutil import reject_controls, xml_escape

W_AUTHOR = "Stetwise"


def _date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Ids:
    def __init__(self, xml: bytes):
        found = [int(item) for item in re.findall(br'w:id="(\d+)"', xml)]
        self.value = max(found, default=0)

    def next(self) -> int:
        self.value += 1
        return self.value


def _paragraph_spans(xml: bytes) -> list[tuple[int, int]]:
    spans = []
    i = 0
    while i < len(xml):
        start = xml.find(b"<w:p", i)
        if start < 0:
            break
        nxt = xml[start + 4 : start + 5]
        if nxt not in (b">", b" ", b"/"):
            i = start + 4
            continue
        if xml.startswith(b"<w:p/>", start):
            spans.append((start, start + 6))
            i = start + 6
            continue
        end = xml.find(b"</w:p>", start)
        if end < 0:
            raise StetwiseRefusal("A paragraph is not closed.", "malformed_xml")
        spans.append((start, end + 6))
        i = end + 6
    return spans


def _close_run(xml: bytes, start: int) -> int:
    depth = 0
    i = start
    while i < len(xml):
        nxt_open = xml.find(b"<w:r", i)
        nxt_close = xml.find(b"</w:r>", i)
        if nxt_close < 0:
            raise StetwiseRefusal("A run is not closed.", "malformed_xml")
        if nxt_open != -1 and nxt_open < nxt_close:
            nxt = xml[nxt_open + 4 : nxt_open + 5]
            if nxt in (b">", b" ", b"/"):
                if xml.startswith(b"<w:r/>", nxt_open):
                    i = nxt_open + 5
                    continue
                depth += 1
                i = nxt_open + 4
                continue
            i = nxt_open + 4
            continue
        depth -= 1
        i = nxt_close + 6
        if depth == 0:
            return i
    raise StetwiseRefusal("A run is not closed.", "malformed_xml")


def _run_containing(xml: bytes, pos: int) -> tuple[int, int]:
    i = pos
    while i > 0:
        found = xml.rfind(b"<w:r", 0, i)
        if found < 0:
            break
        nxt = xml[found + 4 : found + 5]
        if nxt in (b">", b" ", b"/"):
            end = _close_run(xml, found)
            if found <= pos < end:
                return found, end
        i = found
    raise StetwiseRefusal("A text run could not be located without rewriting the document.", "unsafe_text")


def _rpr(run: bytes) -> bytes:
    match = re.search(br"<w:rPr\b[^>]*>.*?</w:rPr>|<w:rPr\b[^>]*/>", run, re.DOTALL)
    return match.group(0) if match else b""


def _text_tag(tag: str, text: str) -> bytes:
    space = ' xml:space="preserve"' if text[:1].isspace() or text[-1:].isspace() else ""
    return f"<{tag}{space}>{xml_escape(text)}</{tag}>".encode("utf-8")


def _plain_run(rpr: bytes, text: str, tag: str = "w:t") -> bytes:
    return b"<w:r>" + rpr + _text_tag(tag, text) + b"</w:r>"


def _wrap(kind: str, rev_id: int, rpr: bytes, text: str, tag: str, when: str) -> bytes:
    inner = _plain_run(rpr, text, tag)
    return (
        f'<{kind} w:id="{rev_id}" w:author="{W_AUTHOR}" w:date="{when}">'.encode()
        + inner
        + f"</{kind}>".encode()
    )


def _segments(piece: dict, spans: list[tuple[int, int]], replace: str) -> list[tuple[str, str]] | None:
    original = piece["text"]
    start = piece["start"]
    end = start + len(original)
    if not original or not spans:
        return None
    segments: list[tuple[str, str]] = []
    pos = start
    touched = False
    for begin, stop in spans:
        if stop <= start or begin >= end:
            continue
        touched = True
        keep_to = min(max(begin, start), end)
        if keep_to > pos:
            segments.append(("keep", original[pos - start : keep_to - start]))
        overlap_from = max(begin, start)
        overlap_to = min(stop, end)
        if overlap_to > overlap_from:
            segments.append(("del", original[overlap_from - start : overlap_to - start]))
        if start <= begin < end and replace:
            segments.append(("ins", replace))
        pos = min(stop, end)
        if pos >= end:
            break
    if pos < end:
        segments.append(("keep", original[pos - start :]))
    if not touched:
        return None
    return segments


def _render(run: bytes, segments: list[tuple[str, str]], ids: _Ids, when: str) -> bytes:
    rpr = _rpr(run)
    out = []
    for kind, text in segments:
        if not text:
            continue
        if kind == "keep":
            out.append(_plain_run(rpr, text))
        elif kind == "del":
            out.append(_wrap("w:del", ids.next(), rpr, text, "w:delText", when))
        else:
            out.append(_wrap("w:ins", ids.next(), rpr, text, "w:t", when))
    return b"".join(out)


def _settings_on(package, requested: bool) -> dict[str, bytes]:
    if not requested or package_tracking(package):
        return {}
    settings = package.parts().get("word/settings.xml")
    if not settings or b"</w:settings>" not in settings or b"<w:trackRevisions" in settings:
        return {}
    updated = settings.replace(b"</w:settings>", b"<w:trackRevisions/></w:settings>", 1)
    return {"word/settings.xml": updated}


def package_tracking(package) -> bool:
    from .docx import tracking_on

    return tracking_on(package)


def replace_text_tracked(package, find: str, replace: str, occurrence: int | None, requested: bool):
    from .docx import (
        EditResult,
        _document,
        _ensure_text_index,
        _global_t_index,
        _paragraphs,
        _visible_runs,
        search_docx,
    )
    from .xmlutil import parse_xml, word_text_spans

    xml = _document(package)
    root = parse_xml(xml, "word/document.xml")
    _ensure_text_index(xml, root)
    indexes = _global_t_index(root)
    spans_of_t = word_text_spans(xml)
    applied = []
    skipped = []
    seen = 0
    replacements: list[tuple[int, int, bytes]] = []
    ids = _Ids(xml)
    when = _date()
    for number, paragraph in enumerate(_paragraphs(root)):
        runs = _visible_runs(paragraph)
        pieces = []
        cursor = 0
        for node in runs:
            text = node.text or ""
            pieces.append({"node": node, "text": text, "start": cursor, "revision": _ancestor(node)})
            cursor += len(text)
        visible = "".join(piece["text"] for piece in pieces)
        start = 0
        found_spans = []
        while True:
            found = visible.find(find, start)
            if found < 0:
                break
            found_spans.append((found, found + len(find)))
            start = found + len(find)
        chosen = []
        for span in found_spans:
            seen += 1
            if occurrence is None or seen == occurrence:
                chosen.append(span)
        safe = []
        for begin, end in chosen:
            touched = [
                index
                for index, piece in enumerate(pieces)
                if piece["text"] and not (piece["start"] + len(piece["text"]) <= begin or piece["start"] >= end)
            ]
            if not touched:
                continue
            if any(pieces[index]["revision"] for index in touched):
                skipped.append({"anchor": f"p-{number}"})
                continue
            safe.append((begin, end))
            applied.append({"anchor": f"p-{number}", "before": visible[begin:end], "after": replace, "tracked": True})
        grouped: dict[tuple[int, int], list] = {}
        for piece in pieces:
            segments = _segments(piece, safe, replace)
            if not segments:
                continue
            span = spans_of_t[indexes[piece["node"]]]
            run_span = _run_containing(xml, span[0])
            grouped.setdefault(run_span, []).append((piece, segments))
        for run_span, items in grouped.items():
            if len(items) != 1:
                raise StetwiseRefusal(
                    "A run contains more than one text node. v1 will not rewrite it while recording tracked changes.",
                    "unsafe_text",
                )
            piece, segments = items[0]
            run = xml[run_span[0] : run_span[1]]
            if run.count(b"<w:t") != 1:
                raise StetwiseRefusal(
                    "A run contains more than one text node. v1 will not rewrite it while recording tracked changes.",
                    "unsafe_text",
                )
            replacements.append((run_span[0], run_span[1], _render(run, segments, ids, when)))
    if not applied:
        _raise_missing(package, find, skipped, search_docx)
    replacements.sort(key=lambda item: item[0], reverse=True)
    out = xml
    for start, end, blob in replacements:
        out = out[:start] + blob + out[end:]
    result_parts = {"word/document.xml": out}
    result_parts.update(_settings_on(package, requested))
    warnings = ["Word edits were written as tracked revisions by Stetwise."]
    if skipped:
        warnings.append(f"{len(skipped)} match(es) overlapped existing tracked changes and were left untouched.")
    return EditResult(
        replacements=result_parts,
        diff=[{"op": "replace_text", "tracked": True, "author": W_AUTHOR, "changes": applied}],
        warnings=warnings,
    )


def _ancestor(node) -> bool:
    from .docx import _in_ancestor

    return _in_ancestor(node, {"ins", "del"})


def _raise_missing(package, find, skipped, search_docx) -> None:
    outside = search_docx(package, find, limit=20)
    elsewhere = [hit for hit in outside["matches"] if hit["part"] != "word/document.xml"]
    if skipped and not elsewhere:
        raise StetwiseRefusal(
            "Every match overlaps existing tracked changes. v1 will not rewrite those revision marks.",
            "tracked_change_overlap",
        )
    if elsewhere:
        raise StetwiseRefusal(
            "The only matches are in comments, headers, footers, or notes. Those parts stay byte-for-byte identical.",
            "unsupported_part",
        )
    raise StetwiseRefusal(f"No occurrences of {find!r} were found in the document body.", "not_found")


def _paragraph(xml: bytes, anchor: str) -> tuple[int, tuple[int, int]]:
    match = re.fullmatch(r"p-(\d+)", anchor or "")
    if not match:
        raise StetwiseRefusal("Paragraph anchor must look like p-0.", "bad_anchor")
    spans = _paragraph_spans(xml)
    number = int(match.group(1))
    if number >= len(spans):
        raise StetwiseRefusal(f"There is no paragraph {anchor}.", "bad_anchor")
    return number, spans[number]


def _unsafe_paragraph(paragraph) -> None:
    from .docx import _in_ancestor
    from .xmlutil import local

    for node in paragraph.iter():
        if local(node.tag) in {
            "ins",
            "del",
            "drawing",
            "pict",
            "object",
            "commentRangeStart",
            "commentReference",
            "commentRangeEnd",
        }:
            raise StetwiseRefusal(
                "That paragraph contains tracked changes, a comment anchor, or an embedded object. v1 will not flatten it.",
                "unsafe_paragraph",
            )


def _paragraph_style_defined(package, style_id: str) -> bool:
    styles = package.parts().get("word/styles.xml")
    if not styles or any(ch in style_id for ch in '<>&"'):
        return False
    token = re.escape(style_id.encode("utf-8"))
    pattern = (
        br'<w:style\b[^>]*w:type="paragraph"[^>]*w:styleId="' + token + br'"'
        br'|<w:style\b[^>]*w:styleId="' + token + br'"[^>]*w:type="paragraph"'
    )
    return re.search(pattern, styles) is not None


def _pstyle(ppr: bytes | None, style_id: str) -> bytes:
    from .xmlutil import xml_escape

    tag = f'<w:pStyle w:val="{xml_escape(style_id)}"/>'.encode("utf-8")
    if not ppr:
        return b"<w:pPr>" + tag + b"</w:pPr>"
    if re.search(br"<w:pStyle\b", ppr):
        replaced = re.sub(br"<w:pStyle\b[^>]*/>", tag, ppr, count=1)
        if replaced == ppr:
            replaced = re.sub(br"<w:pStyle\b[^>]*>.*?</w:pStyle>", tag, ppr, count=1, flags=re.DOTALL)
        return replaced
    gt = ppr.find(b">")
    return ppr[: gt + 1] + tag + ppr[gt + 1 :]


def insert_paragraph(package, anchor: str, text: str, track_changes: bool = False, style: str | None = None):
    from .docx import EditResult, _document, _paragraphs, _style_of, protected, tracking_on
    from .xmlutil import parse_xml

    if protected(package):
        raise StetwiseRefusal("This document is protected. v1 will not bypass document protection.", "protected")
    reject_controls(text, "Paragraph text")
    if style is not None and not _paragraph_style_defined(package, str(style)):
        raise StetwiseRefusal(
            f"Paragraph style {style!r} is not already defined in this document. v1 will not create or edit styles.",
            "unknown_style",
        )
    xml = _document(package)
    root = parse_xml(xml, "word/document.xml")
    number, (start, end) = _paragraph(xml, anchor)
    paragraph = _paragraphs(root)[number]
    para = xml[start:end]
    ppr = re.search(br"<w:pPr\b[^>]*>.*?</w:pPr>|<w:pPr\b[^>]*/>", para, re.DOTALL)
    rpr = re.search(br"<w:rPr\b[^>]*>.*?</w:rPr>|<w:rPr\b[^>]*/>", para, re.DOTALL)
    ppr_bytes = ppr.group(0) if ppr else b""
    chosen = _style_of(paragraph)
    if style is not None:
        ppr_bytes = _pstyle(ppr_bytes or None, str(style))
        chosen = str(style)
    tracked = track_changes or tracking_on(package)
    run = _plain_run(rpr.group(0) if rpr else b"", text)
    if tracked:
        run = _wrap("w:ins", _Ids(xml).next(), rpr.group(0) if rpr else b"", text, "w:t", _date())
    built = b"<w:p>" + ppr_bytes + run + b"</w:p>"
    out = xml[:end] + built + xml[end:]
    parts = {"word/document.xml": out}
    parts.update(_settings_on(package, track_changes))
    warning = (
        "The new paragraph uses a paragraph style already defined in the document. styles.xml was not changed."
        if style is not None
        else "The new paragraph copies the anchor paragraph's style and the first run's formatting."
    )
    return EditResult(
        replacements=parts,
        diff=[{"op": "insert_paragraph", "anchor": anchor, "after": f"p-{number + 1}", "text": text, "style": chosen, "tracked": tracked}],
        warnings=[warning],
    )


def delete_paragraph(package, anchor: str, track_changes: bool = False):
    from .docx import EditResult, _document, _paragraphs, protected, tracking_on
    from .xmlutil import local, parse_xml

    if protected(package):
        raise StetwiseRefusal("This document is protected. v1 will not bypass document protection.", "protected")
    xml = _document(package)
    root = parse_xml(xml, "word/document.xml")
    number, (start, end) = _paragraph(xml, anchor)
    paragraph = _paragraphs(root)[number]
    _unsafe_paragraph(paragraph)
    parent = paragraph.getparent()
    siblings = [node for node in parent if local(node.tag) == "p"] if parent is not None else []
    if len(siblings) <= 1:
        raise StetwiseRefusal(
            f"Paragraph {anchor} is the only paragraph in its container. v1 will not leave that container empty.",
            "unsafe_paragraph",
        )
    tracked = track_changes or tracking_on(package)
    para = xml[start:end]
    if tracked:
        out = xml[:start] + _delete_marks(para, _Ids(xml), _date()) + xml[end:]
        warning = "The paragraph was marked deleted by Stetwise. Accepting the deletion in Word removes it."
    else:
        body, sect = _extract_sect(para)
        out = xml[:start] + xml[end:]
        if sect:
            prev = out.rfind(b"</w:p>", 0, start)
            if prev >= 0:
                out = out[:prev] + sect + out[prev:]
            else:
                marker = out.find(b"</w:body>")
                out = out[:marker] + sect + out[marker:]
        warning = "The paragraph was removed. A section mark in it was kept on the previous paragraph."
        del body
    parts = {"word/document.xml": out}
    parts.update(_settings_on(package, track_changes))
    return EditResult(
        replacements=parts,
        diff=[{"op": "delete_paragraph", "anchor": anchor, "tracked": tracked}],
        warnings=[warning],
    )


def _extract_sect(para: bytes) -> tuple[bytes, bytes]:
    match = re.search(br"<w:sectPr\b[^>]*>.*?</w:sectPr>|<w:sectPr\b[^>]*/>", para, re.DOTALL)
    if not match:
        return para, b""
    return para[: match.start()] + para[match.end() :], match.group(0)


def _delete_marks(para: bytes, ids: _Ids, when: str) -> bytes:
    runs = []
    i = 0
    while i < len(para):
        start = para.find(b"<w:r", i)
        if start < 0:
            break
        nxt = para[start + 4 : start + 5]
        if nxt not in (b">", b" ", b"/"):
            i = start + 4
            continue
        end = _close_run(para, start)
        runs.append((start, end))
        i = end
    out = para
    for start, end in reversed(runs):
        run = out[start:end]
        if b"<w:t" not in run:
            continue
        deleted = re.sub(br"<w:t(\s[^>]*)?>", b"<w:delText\\1>", run)
        deleted = deleted.replace(b"</w:t>", b"</w:delText>")
        wrapped = (
            f'<w:del w:id="{ids.next()}" w:author="{W_AUTHOR}" w:date="{when}">'.encode()
            + deleted
            + b"</w:del>"
        )
        out = out[:start] + wrapped + out[end:]
    return out


def _rewrite_paragraph(xml: bytes, anchor: str, text: str, ids: _Ids, when: str) -> tuple[bytes, str]:
    from .docx import _paragraph_text, _paragraphs, _visible_runs
    from .xmlutil import parse_xml

    number, (start, end) = _paragraph(xml, anchor)
    root = parse_xml(xml, "word/document.xml")
    paragraph = _paragraphs(root)[number]
    _unsafe_paragraph(paragraph)
    if not _visible_runs(paragraph):
        raise StetwiseRefusal(f"Paragraph {anchor} has no text run to keep formatting from.", "unsafe_paragraph")
    before = _paragraph_text(paragraph)
    para = _delete_marks(xml[start:end], ids, when)
    rpr = re.search(br"<w:rPr\b[^>]*>.*?</w:rPr>|<w:rPr\b[^>]*/>", para, re.DOTALL)
    inserted = _wrap("w:ins", ids.next(), rpr.group(0) if rpr else b"", text, "w:t", when)
    close = para.rfind(b"</w:p>")
    para = para[:close] + inserted + para[close:]
    return xml[:start] + para + xml[end:], before


def replace_paragraph_tracked(package, anchor: str, text: str, requested: bool):
    from .docx import EditResult, _document, _style_of, _paragraphs, protected
    from .xmlutil import parse_xml, reject_controls

    if protected(package):
        raise StetwiseRefusal("This document is protected. v1 will not bypass document protection.", "protected")
    reject_controls(text, "Paragraph text")
    xml = _document(package)
    root = parse_xml(xml, "word/document.xml")
    number, _span = _paragraph(xml, anchor)
    style = _style_of(_paragraphs(root)[number])
    ids = _Ids(xml)
    when = _date()
    out, before = _rewrite_paragraph(xml, anchor, text, ids, when)
    parts = {"word/document.xml": out}
    parts.update(_settings_on(package, requested))
    return EditResult(
        replacements=parts,
        diff=[{"op": "replace_paragraph", "anchor": anchor, "style": style, "before": before, "after": text, "tracked": True, "author": W_AUTHOR}],
        warnings=["The paragraph was replaced with tracked revisions by Stetwise. Its style was left in place."],
    )


def update_table_cells_tracked(package, table: int, cells: list[dict], requested: bool):
    from .docx import EditResult, _document, _in_ancestor, _paragraphs, _visible_runs, protected
    from .xmlutil import local, parse_xml, reject_controls

    if protected(package):
        raise StetwiseRefusal("This document is protected. v1 will not bypass document protection.", "protected")
    xml = _document(package)
    root = parse_xml(xml, "word/document.xml")
    tables = [node for node in root.iter() if local(node.tag) == "tbl" and not _in_ancestor(node, {"tbl"})]
    if table < 0 or table >= len(tables):
        raise StetwiseRefusal(f"There is no table {table}.", "bad_anchor")
    target = tables[table]
    rows = [node for node in target if local(node.tag) == "tr"]
    planned = []
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
        paras = [node for node in cell.iter() if local(node.tag) == "p"]
        if len(paras) != 1:
            raise StetwiseRefusal(
                f"Table cell {row_i},{col_i} has {len(paras)} paragraphs. v1 only edits a single-paragraph cell.",
                "unsafe_paragraph",
            )
        if not _visible_runs(paras[0]):
            raise StetwiseRefusal(f"Table cell {row_i},{col_i} has no text run.", "unsafe_paragraph")
        index = next(number for number, paragraph in enumerate(_paragraphs(root)) if paragraph is paras[0])
        planned.append((f"p-{index}", text, row_i, col_i))
    ids = _Ids(xml)
    when = _date()
    changes = []
    for anchor, text, row_i, col_i in planned:
        xml, before = _rewrite_paragraph(xml, anchor, text, ids, when)
        changes.append({"table": table, "row": row_i, "col": col_i, "before": before, "after": text, "tracked": True})
    parts = {"word/document.xml": xml}
    parts.update(_settings_on(package, requested))
    return EditResult(
        replacements=parts,
        diff=[{"op": "update_table_cells", "tracked": True, "author": W_AUTHOR, "changes": changes}],
        warnings=["Table cells were replaced with tracked revisions by Stetwise."],
    )


def format_runs(package, find: str, bold, italic, underline, occurrence: int | None):
    from .docx import EditResult, _document, _ensure_text_index, _global_t_index, _paragraphs, _visible_runs, protected
    from .xmlutil import parse_xml, word_text_spans

    if protected(package):
        raise StetwiseRefusal("This document is protected. v1 will not bypass document protection.", "protected")
    if not find:
        raise StetwiseRefusal("The text to format is empty.", "empty_query")
    if bold is None and italic is None and underline is None:
        raise StetwiseRefusal("format_runs needs bold, italic, or underline.", "empty_edit")
    xml = _document(package)
    root = parse_xml(xml, "word/document.xml")
    _ensure_text_index(xml, root)
    indexes = _global_t_index(root)
    spans_of_t = word_text_spans(xml)
    from .docx import tracking_on

    tracked = tracking_on(package)
    applied = []
    seen = 0
    edits: list[tuple[int, int, bytes]] = []
    used_runs: set[tuple[int, int]] = set()
    ids = _Ids(xml)
    when = _date()
    for number, paragraph in enumerate(_paragraphs(root)):
        runs = _visible_runs(paragraph)
        pieces = []
        cursor = 0
        for node in runs:
            text = node.text or ""
            pieces.append({"node": node, "text": text, "start": cursor, "revision": _ancestor(node)})
            cursor += len(text)
        visible = "".join(piece["text"] for piece in pieces)
        start = 0
        found_spans = []
        while True:
            found = visible.find(find, start)
            if found < 0:
                break
            seen += 1
            if occurrence is None or seen == occurrence:
                begin, end = found, found + len(find)
                touched = [
                    index
                    for index, piece in enumerate(pieces)
                    if piece["text"] and not (piece["start"] + len(piece["text"]) <= begin or piece["start"] >= end)
                ]
                if touched and any(pieces[index]["revision"] for index in touched):
                    raise StetwiseRefusal(
                        "That text overlaps an existing tracked change. v1 will not restyle revision marks.",
                        "tracked_change_overlap",
                    )
                if touched:
                    found_spans.append((begin, end))
                    applied.append({"anchor": f"p-{number}", "text": find})
            start = found + len(find)
        for piece in pieces:
            segments = _format_segments(piece, found_spans)
            if not segments:
                continue
            span = spans_of_t[indexes[piece["node"]]]
            run_span = _run_containing(xml, span[0])
            if run_span in used_runs:
                raise StetwiseRefusal(
                    "A run contains more than one text node. v1 will not restyle it without rewriting the run.",
                    "unsafe_text",
                )
            used_runs.add(run_span)
            run = xml[run_span[0] : run_span[1]]
            edits.append((run_span[0], run_span[1], _render_format(run, segments, bold, italic, underline, ids, when, tracked)))
    if not applied:
        raise StetwiseRefusal(f"No occurrences of {find!r} were found in the document body.", "not_found")
    out = xml
    for start, end, blob in sorted(edits, key=lambda item: item[0], reverse=True):
        out = out[:start] + blob + out[end:]
    flags = {"bold": bold, "italic": italic, "underline": underline}
    return EditResult(
        replacements={"word/document.xml": out},
        diff=[{"op": "format_runs", "changes": applied, "format": {key: value for key, value in flags.items() if value is not None}}],
        warnings=[
            "Run formatting was set on the matched text. word/styles.xml and the theme were not rewritten."
            + (" The change is stored as a formatting revision because tracking is on." if tracked else "")
        ],
    )


def _format_segments(piece: dict, spans: list[tuple[int, int]]) -> list[tuple[str, str]] | None:
    original = piece["text"]
    start = piece["start"]
    end = start + len(original)
    segments: list[tuple[str, str]] = []
    pos = start
    touched = False
    for begin, stop in spans:
        if stop <= start or begin >= end:
            continue
        touched = True
        keep_to = min(max(begin, start), end)
        if keep_to > pos:
            segments.append(("keep", original[pos - start : keep_to - start]))
        mid_from = max(begin, start)
        mid_to = min(stop, end)
        if mid_to > mid_from:
            segments.append(("fmt", original[mid_from - start : mid_to - start]))
        pos = min(stop, end)
    if pos < end:
        segments.append(("keep", original[pos - start :]))
    return segments if touched else None


def _render_format(run: bytes, segments: list[tuple[str, str]], bold, italic, underline, ids: _Ids, when: str, tracked: bool) -> bytes:
    rpr = _rpr(run)
    formatted = _flags(rpr, bold, italic, underline)
    if tracked:
        previous = rpr or b"<w:rPr/>"
        change = f'<w:rPrChange w:id="{ids.next()}" w:author="{W_AUTHOR}" w:date="{when}">'.encode() + previous + b"</w:rPrChange>"
        formatted = formatted.replace(b"</w:rPr>", change + b"</w:rPr>", 1)
    out = []
    for kind, text in segments:
        if not text:
            continue
        out.append(_plain_run(formatted if kind == "fmt" else rpr, text))
    return b"".join(out)


def _flags(rpr: bytes, bold, italic, underline) -> bytes:
    if not rpr:
        rpr = b"<w:rPr></w:rPr>"
    elif rpr.endswith(b"/>"):
        rpr = b"<w:rPr></w:rPr>"
    def strip(xml: bytes, tag: str) -> bytes:
        xml = re.sub(br"<w:" + tag.encode() + br"\b[^>]*/>", b"", xml)
        return re.sub(br"<w:" + tag.encode() + br"\b[^>]*>.*?</w:" + tag.encode() + br">", b"", xml, flags=re.DOTALL)
    rpr = strip(strip(strip(rpr, "b"), "i"), "u")
    extra = b""
    if bold is True:
        extra += b"<w:b/>"
    elif bold is False:
        extra += b'<w:b w:val="0"/>'
    if italic is True:
        extra += b"<w:i/>"
    elif italic is False:
        extra += b'<w:i w:val="0"/>'
    if underline is True:
        extra += b'<w:u w:val="single"/>'
    elif underline is False:
        extra += b'<w:u w:val="none"/>'
    return rpr.replace(b"</w:rPr>", extra + b"</w:rPr>", 1)
