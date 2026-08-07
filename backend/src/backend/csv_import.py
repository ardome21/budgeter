"""Parsing bank CSV exports.

Banks do not agree on anything — column names, date formats, or whether a
purchase is a positive or a negative number. This module normalises all of it
into candidate rows, resolving merchants and inferring categories from history,
and flags anything it had to guess so the preview can show it.
"""

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# Header names seen in the wild, lowercased. First match wins.
DATE_HEADERS = ("transaction date", "posted date", "date", "post date")
DESC_HEADERS = ("description", "merchant", "name", "payee", "memo", "details")
AMOUNT_HEADERS = ("amount", "debit", "charge")
CREDIT_HEADERS = ("credit", "deposit")
CATEGORY_HEADERS = ("category", "type")

DATE_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%d/%m/%Y",
    "%b %d, %Y",
    "%Y/%m/%d",
    "%m-%d-%Y",
)

CURRENCY = re.compile(r"[^0-9.\-()]")


@dataclass
class CandidateRow:
    row_number: int
    occurred_on: date | None
    raw_description: str
    amount: Decimal
    suggested_category_id: int | None = None
    suggested_category_name: str | None = None
    merchant_id: int | None = None
    merchant_name: str | None = None
    import_hash: str = ""
    duplicate_of: int | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class ParseResult:
    rows: list[CandidateRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    detected_columns: dict[str, str] = field(default_factory=dict)


def _pick(headers: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {h.strip().lower(): h for h in headers}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    for key, original in lowered.items():
        if any(candidate in key for candidate in candidates):
            return original
    return None


def parse_amount(text: str) -> Decimal | None:
    """'$1,234.56', '(45.00)' and '-45' all become Decimals."""
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = CURRENCY.sub("", raw).replace("(", "").replace(")", "")
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if negative:
        value = -value
    return value.quantize(Decimal("0.01"))


def parse_date(text: str) -> date | None:
    raw = (text or "").strip()
    if not raw:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def row_hash(occurred_on: date | None, description: str, amount: Decimal) -> str:
    """Identity of an imported row, for making a re-drop of the same file a no-op.

    Only used for CSV. Hand-entered rows are never hashed, because two genuinely
    identical charges on one night must both survive.
    """
    basis = f"{occurred_on or ''}|{description.strip().lower()}|{amount}"
    return hashlib.sha256(basis.encode()).hexdigest()


def parse_csv(text: str, *, flip_sign: bool = False) -> ParseResult:
    """Turn CSV text into candidate rows.

    `flip_sign` handles exports where a purchase is negative — budgeter stores
    spending as positive and refunds as negative, the same convention the
    spreadsheet used.
    """
    result = ParseResult()
    text = text.lstrip("﻿")
    if not text.strip():
        result.errors.append("the file is empty")
        return result

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [h for h in (reader.fieldnames or []) if h]
    if not headers:
        result.errors.append("no header row found")
        return result

    date_col = _pick(headers, DATE_HEADERS)
    desc_col = _pick(headers, DESC_HEADERS)
    amount_col = _pick(headers, AMOUNT_HEADERS)
    credit_col = _pick(headers, CREDIT_HEADERS)
    category_col = _pick(headers, CATEGORY_HEADERS)

    if desc_col is None:
        result.errors.append(
            f"could not find a description column among: {', '.join(headers)}"
        )
    if amount_col is None and credit_col is None:
        result.errors.append(
            f"could not find an amount column among: {', '.join(headers)}"
        )
    if result.errors:
        return result

    result.detected_columns = {
        k: v
        for k, v in {
            "date": date_col,
            "description": desc_col,
            "amount": amount_col,
            "credit": credit_col,
            "category": category_col,
        }.items()
        if v
    }

    for number, raw_row in enumerate(reader, start=2):
        description = (raw_row.get(desc_col) or "").strip()
        amount = parse_amount(raw_row.get(amount_col)) if amount_col else None
        if amount is None and credit_col:
            credit = parse_amount(raw_row.get(credit_col))
            if credit is not None:
                amount = -credit

        if not description and amount is None:
            continue  # trailing blank line
        if not description:
            result.errors.append(f"row {number}: no description")
            continue
        if amount is None:
            result.errors.append(f"row {number}: could not read an amount")
            continue
        if amount == 0:
            continue

        if flip_sign:
            amount = -amount

        occurred_on = parse_date(raw_row.get(date_col)) if date_col else None
        row = CandidateRow(
            row_number=number,
            occurred_on=occurred_on,
            raw_description=description[:200],
            amount=amount,
        )
        if date_col and occurred_on is None and (raw_row.get(date_col) or "").strip():
            row.notes.append(f"unreadable date {raw_row.get(date_col)!r}")
        if category_col and (raw_row.get(category_col) or "").strip():
            row.notes.append(f"file said category {raw_row[category_col].strip()!r}")
        row.import_hash = row_hash(occurred_on, description, amount)
        result.rows.append(row)

    if not result.rows and not result.errors:
        result.errors.append("no usable rows found")
    return result
