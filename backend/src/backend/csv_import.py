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
from collections import Counter
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
class NearMatch:
    """A transaction already on file that this row might be a second copy of."""

    id: int
    occurred_on: date | None
    raw_description: str
    amount: Decimal
    days_apart: int


@dataclass
class CandidateRow:
    row_number: int
    occurred_on: date | None
    raw_description: str
    amount: Decimal
    suggested_category_id: int | None = None
    suggested_category_name: str | None = None
    # (category_id, name, times used) for this merchant, most used first.
    category_options: list[tuple[int, str, int]] = field(default_factory=list)
    merchant_id: int | None = None
    merchant_name: str | None = None
    import_hash: str = ""
    # 0 for the first row with this date/description/amount, 1 for the next,
    # and so on. Two identical charges in one export are two real purchases.
    occurrence: int = 0
    duplicate_of: int | None = None
    # Existing transactions for the same amount within a few days — the check
    # that catches a bank export overlapping history imported another way,
    # where the descriptions differ and so the hashes cannot match.
    near_duplicates: list["NearMatch"] = field(default_factory=list)
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


def hash_key(
    occurred_on: date | None, description: str, amount: Decimal
) -> tuple[str, str, str]:
    """The content a hash is taken over. Shared so callers can count repeats."""
    return (str(occurred_on or ""), description.strip().lower(), str(amount))


def row_hash(
    occurred_on: date | None,
    description: str,
    amount: Decimal,
    *,
    account_id: int | None = None,
    occurrence: int = 0,
) -> str:
    """Identity of an imported row, for making a re-drop of the same file a no-op.

    Only used for CSV. Hand-entered rows are never hashed, because two genuinely
    identical charges on one night must both survive.

    `occurrence` is what lets that also be true *within* a file. Two $3.50
    coffees on the same day are two real rounds, and a hash over content alone
    cannot tell them apart — so the second one collided with the first against
    a unique index and took the whole import down with a 500. Numbering repeats
    in the order they appear keeps both rows, and keeps a re-drop of the same
    export a no-op, because the same file numbers them the same way again.

    `account_id` separates the same charge seen through two different accounts.
    Both are appended only when set, so a hash taken without them is unchanged.
    """
    date_part, desc_part, amount_part = hash_key(occurred_on, description, amount)
    basis = f"{date_part}|{desc_part}|{amount_part}"
    if account_id is not None:
        basis = f"{basis}|acct:{account_id}"
    if occurrence:
        basis = f"{basis}|#{occurrence}"
    return hashlib.sha256(basis.encode()).hexdigest()


def parse_csv(
    text: str, *, flip_sign: bool = False, account_id: int | None = None
) -> ParseResult:
    """Turn CSV text into candidate rows.

    `flip_sign` handles exports where a purchase is negative — budgeter stores
    spending as positive and refunds as negative, the same convention the
    spreadsheet used.

    `account_id` is the account the export came from. It goes into each row's
    hash, so the same charge arriving on a card export and a checking export
    stays two rows rather than one silently swallowing the other.
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

    # How many times this exact date/description/amount has already appeared in
    # the file, so the second identical charge gets a different hash from the
    # first instead of colliding with it.
    repeats: Counter[tuple[str, str, str]] = Counter()
    first_seen: dict[tuple[str, str, str], int] = {}

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

        key = hash_key(occurred_on, description, amount)
        row.occurrence = repeats[key]
        repeats[key] += 1
        if row.occurrence:
            row.notes.append(f"identical to row {first_seen[key]} in this file")
        else:
            first_seen[key] = number
        row.import_hash = row_hash(
            occurred_on,
            description,
            amount,
            account_id=account_id,
            occurrence=row.occurrence,
        )
        result.rows.append(row)

    if not result.rows and not result.errors:
        result.errors.append("no usable rows found")
    return result
