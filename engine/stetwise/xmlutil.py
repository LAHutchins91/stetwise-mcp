"""XML helpers. Entities and DTDs are never resolved."""

from __future__ import annotations

import re

from lxml import etree

from .errors import StetwiseRefusal

_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    huge_tree=False,
    load_dtd=False,
    remove_blank_text=False,
    dtd_validation=False,
)


def parse_xml(data: bytes, part: str) -> etree._Element:
    head = data.lstrip()[:200].lower()
    if head.startswith(b"<!doctype") or b"<!entity" in data[:2000].lower():
        raise StetwiseRefusal(
            f"Part {part} contains a DTD or entity declaration. v1 will not parse it.",
            "xxe",
        )
    try:
        root = etree.fromstring(data, _PARSER)
    except etree.XMLSyntaxError as exc:
        raise StetwiseRefusal(f"Part {part} is not well-formed XML.", "malformed_xml") from exc
    return root


def local(tag: str) -> str:
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag


def children(el: etree._Element, name: str) -> list[etree._Element]:
    return [child for child in el if local(child.tag) == name]


def first(el: etree._Element, name: str) -> etree._Element | None:
    for child in el.iter():
        if local(child.tag) == name:
            return child
    return None


def all_named(el: etree._Element, name: str) -> list[etree._Element]:
    return [node for node in el.iter() if local(node.tag) == name]


def xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def reject_controls(text: str, label: str) -> None:
    for char in text:
        code = ord(char)
        if code in (9, 10, 13):
            continue
        if code < 32 or code == 0xFFFE or code == 0xFFFF:
            raise StetwiseRefusal(
                f"{label} contains a control character v1 will not write into OOXML.",
                "invalid_text",
            )


_WT = re.compile(br"<w:t(\s[^>]*)?>(.*?)</w:t>", re.DOTALL)


def word_text_spans(xml: bytes) -> list[tuple[int, int, int, int]]:
    """Return (start, end, inner_start, inner_end) for each w:t in order."""
    return [(m.start(), m.end(), m.start(2), m.end(2)) for m in _WT.finditer(xml)]


def replace_word_texts(xml: bytes, updates: dict[int, str]) -> bytes:
    spans = word_text_spans(xml)
    if any(index >= len(spans) or index < 0 for index in updates):
        raise StetwiseRefusal(
            "A text run index did not line up with the document XML. v1 will not guess.",
            "unsafe_text",
        )
    out = bytearray()
    cursor = 0
    for index, (_start, _end, inner_start, inner_end) in enumerate(spans):
        out += xml[cursor:inner_start]
        if index in updates:
            out += xml_escape(updates[index]).encode("utf-8")
        else:
            out += xml[inner_start:inner_end]
        cursor = inner_end
    out += xml[cursor:]
    return bytes(out)
