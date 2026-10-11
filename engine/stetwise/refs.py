"""Excel A1 references and formula shifting.

Row and column insertion follows Excel's rule: a reference at or below the
insertion point moves, and a range that contains the insertion point expands.
Both relative and absolute references move. Whole-column refs are unchanged
by a row insert, and the reverse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import StetwiseRefusal

MAX_ROW = 1048576
MAX_COL = 16384

_CELL = re.compile(r"^(\$?)([A-Za-z]{1,3})(\$?)(\d+)")
_COL_RANGE = re.compile(r"^(\$?)([A-Za-z]{1,3}):(\$?)([A-Za-z]{1,3})(?![A-Za-z0-9_])")
_ROW_RANGE = re.compile(r"^(\$?)(\d+):(\$?)(\d+)(?!\d)")


@dataclass(frozen=True)
class Shift:
    sheet: str
    row_at: int | None = None
    row_count: int = 0
    col_at: int | None = None
    col_count: int = 0


def col_to_num(col: str) -> int:
    n = 0
    for char in col.upper():
        if not ("A" <= char <= "Z"):
            raise StetwiseRefusal(f"Column {col!r} is not a valid Excel column.", "bad_ref")
        n = n * 26 + (ord(char) - 64)
    if n < 1 or n > MAX_COL:
        raise StetwiseRefusal(f"Column {col} is outside the Excel grid.", "bad_ref")
    return n


def num_to_col(n: int) -> str:
    if n < 1 or n > MAX_COL:
        raise StetwiseRefusal("A formula reference would move past the last Excel column.", "ref_overflow")
    out = []
    while n:
        n, rem = divmod(n - 1, 26)
        out.append(chr(65 + rem))
    return "".join(reversed(out))


def split_ref(ref: str) -> tuple[int, int, int, int]:
    text = ref.replace("$", "")
    if ":" in text:
        left, right = text.split(":", 1)
    else:
        left = right = text
    c1, r1 = _split_cell(left)
    c2, r2 = _split_cell(right)
    return min(c1, c2), min(r1, r2), max(c1, c2), max(r1, r2)


def _split_cell(cell: str) -> tuple[int, int]:
    match = re.fullmatch(r"([A-Za-z]{1,3})(\d+)", cell)
    if not match:
        raise StetwiseRefusal(f"Cell reference {cell!r} is not a single cell or range.", "bad_ref")
    row = int(match.group(2))
    if row < 1 or row > MAX_ROW:
        raise StetwiseRefusal(f"Row {row} is outside the Excel grid.", "bad_ref")
    return col_to_num(match.group(1)), row


def cell_in_range(cell: str, ref: str) -> bool:
    c, r = _split_cell(cell.replace("$", ""))
    c1, r1, c2, r2 = split_ref(ref)
    return c1 <= c <= c2 and r1 <= r <= r2


def formula_blockers(formula: str) -> str | None:
    if "[" in formula or "]" in formula:
        return "a structured table reference or external workbook link"
    if re.search(r"[A-Za-z0-9_.]+:[A-Za-z0-9_.]+!", formula):
        return "a 3D sheet-range reference"
    if re.search(r"R\[|-?\d+\]C|C\[", formula):
        return "an R1C1 reference"
    return None


def _shift_row(row: int, shift: Shift) -> int:
    if shift.row_at is None:
        return row
    if row >= shift.row_at:
        row += shift.row_count
    if row > MAX_ROW:
        raise StetwiseRefusal("A formula reference would move past the last Excel row.", "ref_overflow")
    return row


def _shift_col(col: int, shift: Shift) -> int:
    if shift.col_at is None:
        return col
    if col >= shift.col_at:
        col += shift.col_count
    if col > MAX_COL:
        raise StetwiseRefusal("A formula reference would move past the last Excel column.", "ref_overflow")
    return col


def _shift_rows_span(start: int, end: int, shift: Shift) -> tuple[int, int]:
    if shift.row_at is None:
        return start, end
    at = shift.row_at
    count = shift.row_count
    if end < at:
        pair = (start, end)
    elif start >= at:
        pair = (start + count, end + count)
    else:
        pair = (start, end + count)
    if pair[0] > MAX_ROW or pair[1] > MAX_ROW:
        raise StetwiseRefusal("A formula reference would move past the last Excel row.", "ref_overflow")
    return pair


def _shift_cols_span(start: int, end: int, shift: Shift) -> tuple[int, int]:
    if shift.col_at is None:
        return start, end
    at = shift.col_at
    count = shift.col_count
    if end < at:
        return start, end
    if start >= at:
        return start + count, end + count
    return start, end + count


def _sheet_matches(token: str | None, home: str, shift: Shift) -> bool:
    name = home if token is None else token
    return name.casefold() == shift.sheet.casefold()


def offset_formula(formula: str, d_col: int, d_row: int) -> str:
    """Move relative references by a fixed offset. Absolute references stay.

    Used when a shared formula is copied from its master cell onto another cell.
    """
    if d_col == 0 and d_row == 0:
        return formula
    blocker = formula_blockers(formula)
    if blocker:
        raise StetwiseRefusal(
            f"v1 will not split a shared formula that contains {blocker}.",
            "unsafe_formula",
        )
    return _offset_walk(formula, d_col, d_row)


def _offset_walk(formula: str, d_col: int, d_row: int) -> str:
    out: list[str] = []
    i = 0
    length = len(formula)
    while i < length:
        char = formula[i]
        if char == '"':
            j = i + 1
            while j < length:
                if formula[j] == '"':
                    if j + 1 < length and formula[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(formula[i:j])
            i = j
            continue
        sheet, nxt = _read_sheet(formula, i)
        if sheet is not None and nxt < length and formula[nxt] == "!":
            body, after = _read_ref_body(formula, nxt + 1)
            out.append(formula[i : nxt + 1])
            if body is None:
                i = nxt + 1
                continue
            out.append(_offset_body(body, d_col, d_row))
            i = after
            continue
        body, after = _read_ref_body(formula, i)
        if body is not None:
            out.append(_offset_body(body, d_col, d_row))
            i = after
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _offset_body(body: str, d_col: int, d_row: int) -> str:
    col = _COL_RANGE.fullmatch(body)
    if col:
        c1 = col_to_num(col.group(2)) + (0 if col.group(1) else d_col)
        c2 = col_to_num(col.group(4)) + (0 if col.group(3) else d_col)
        return f"{col.group(1)}{num_to_col(c1)}:{col.group(3)}{num_to_col(c2)}"
    row = _ROW_RANGE.fullmatch(body)
    if row:
        r1 = int(row.group(2)) + (0 if row.group(1) else d_row)
        r2 = int(row.group(4)) + (0 if row.group(3) else d_row)
        if r1 < 1 or r2 < 1 or r1 > MAX_ROW or r2 > MAX_ROW:
            raise StetwiseRefusal("A shared formula would move past the Excel grid.", "ref_overflow")
        return f"{row.group(1)}{r1}:{row.group(3)}{r2}"
    if ":" in body:
        left, right = body.split(":", 1)
        return _offset_cell(left, d_col, d_row) + ":" + _offset_cell(right, d_col, d_row)
    return _offset_cell(body, d_col, d_row)


def _offset_cell(cell: str, d_col: int, d_row: int) -> str:
    match = _CELL.fullmatch(cell)
    if not match:
        raise StetwiseRefusal(f"Could not rewrite reference {cell!r}.", "unsafe_formula")
    col_abs, col, row_abs, row_s = match.groups()
    col_n = col_to_num(col) + (0 if col_abs else d_col)
    row_n = int(row_s) + (0 if row_abs else d_row)
    if col_n < 1 or col_n > MAX_COL or row_n < 1 or row_n > MAX_ROW:
        raise StetwiseRefusal("A shared formula would move past the Excel grid.", "ref_overflow")
    return f"{col_abs}{num_to_col(col_n)}{row_abs}{row_n}"


def shift_formula(formula: str, home_sheet: str, shift: Shift) -> str:
    blocker = formula_blockers(formula)
    if blocker:
        raise StetwiseRefusal(
            f"v1 will not insert rows or columns while a formula contains {blocker}.",
            "unsafe_formula",
        )
    return _rewrite(formula, home_sheet, shift)


def shift_ref_text(ref: str, home_sheet: str, shift: Shift) -> str:
    """Shift a sqref-style list of ranges, optionally sheet-qualified."""
    pieces = []
    for token in ref.split():
        if "!" in token:
            pieces.append(shift_formula(token, home_sheet, shift))
        else:
            pieces.append(_rewrite_ref_body(token, True, shift))
    return " ".join(pieces)


def _rewrite(formula: str, home: str, shift: Shift) -> str:
    out: list[str] = []
    i = 0
    length = len(formula)
    while i < length:
        char = formula[i]
        if char == '"':
            j = i + 1
            while j < length:
                if formula[j] == '"':
                    if j + 1 < length and formula[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(formula[i:j])
            i = j
            continue
        sheet, nxt = _read_sheet(formula, i)
        if sheet is not None and nxt < length and formula[nxt] == "!":
            applies = _sheet_matches(sheet, home, shift)
            body, after = _read_ref_body(formula, nxt + 1)
            if body is None:
                out.append(formula[i : nxt + 1])
                i = nxt + 1
                continue
            out.append(formula[i : nxt + 1])
            out.append(_rewrite_ref_body(body, applies, shift))
            i = after
            continue
        body, after = _read_ref_body(formula, i)
        if body is not None and _sheet_matches(None, home, shift):
            out.append(_rewrite_ref_body(body, True, shift))
            i = after
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _read_sheet(formula: str, i: int) -> tuple[str | None, int]:
    if i >= len(formula):
        return None, i
    if formula[i] == "'":
        j = i + 1
        chars: list[str] = []
        while j < len(formula):
            if formula[j] == "'":
                if j + 1 < len(formula) and formula[j + 1] == "'":
                    chars.append("'")
                    j += 2
                    continue
                j += 1
                break
            chars.append(formula[j])
            j += 1
        else:
            return None, i
        if j < len(formula) and formula[j] == "!":
            return "".join(chars), j
        return None, i
    match = re.match(r"[A-Za-z_][A-Za-z0-9_.]*!", formula[i:])
    if not match:
        return None, i
    return match.group(0)[:-1], i + len(match.group(0)) - 1


def _read_ref_body(formula: str, i: int) -> tuple[str | None, int]:
    rest = formula[i:]
    col = _COL_RANGE.match(rest)
    if col:
        return rest[: col.end()], i + col.end()
    row = _ROW_RANGE.match(rest)
    if row:
        return rest[: row.end()], i + row.end()
    cell = _CELL.match(rest)
    if not cell:
        return None, i
    end = cell.end()
    if end < len(rest) and rest[end] == ":":
        cell2 = _CELL.match(rest[end + 1 :])
        if cell2:
            end = end + 1 + cell2.end()
    # Avoid treating scientific notation or identifiers as cells. A cell ref
    # must not be preceded by a name character; the caller checks the previous
    # character only when this is invoked at an arbitrary index.
    if i > 0 and (formula[i - 1].isalnum() or formula[i - 1] in "_."):
        return None, i
    return rest[:end], i + end


def _rewrite_ref_body(body: str, applies: bool, shift: Shift) -> str:
    if not applies:
        return body
    col = _COL_RANGE.fullmatch(body)
    if col:
        c1_old, c2_old = col_to_num(col.group(2)), col_to_num(col.group(4))
        c1, c2 = _shift_cols_span(c1_old, c2_old, shift)
        if (c1, c2) == (c1_old, c2_old):
            return body
        return f"{col.group(1)}{num_to_col(c1)}:{col.group(3)}{num_to_col(c2)}"
    row = _ROW_RANGE.fullmatch(body)
    if row:
        r1_old, r2_old = int(row.group(2)), int(row.group(4))
        r1, r2 = _shift_rows_span(r1_old, r2_old, shift)
        if (r1, r2) == (r1_old, r2_old):
            return body
        return f"{row.group(1)}{r1}:{row.group(3)}{r2}"
    if ":" in body:
        left, right = body.split(":", 1)
        return _rewrite_cell(left, shift) + ":" + _rewrite_cell(right, shift)
    return _rewrite_cell(body, shift)


def _rewrite_cell(cell: str, shift: Shift) -> str:
    match = _CELL.fullmatch(cell)
    if not match:
        raise StetwiseRefusal(f"Could not rewrite reference {cell!r}.", "unsafe_formula")
    col_abs, col, row_abs, row_s = match.groups()
    col_n = col_to_num(col)
    row_n = int(row_s)
    new_col = _shift_col(col_n, shift)
    new_row = _shift_row(row_n, shift)
    if new_col == col_n and new_row == row_n:
        return cell
    return f"{col_abs}{num_to_col(new_col)}{row_abs}{new_row}"


def shift_cell_ref(ref: str, shift: Shift) -> str:
    return _rewrite_ref_body(ref, True, shift)


def shift_range_pair(start_row: int, end_row: int, start_col: int, end_col: int, shift: Shift) -> tuple[int, int, int, int]:
    r1, r2 = _shift_rows_span(start_row, end_row, shift)
    c1, c2 = _shift_cols_span(start_col, end_col, shift)
    return c1, r1, c2, r2
