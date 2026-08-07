"""Collapsing bank descriptors to merchants.

Shared by the workbook importer and the CSV import endpoint — if the two used
different rules, the same shop would resolve one way in history and another way
going forward, and the category defaults learned from history would stop
applying to new transactions.
"""

import re

# Payment-processor prefixes the bank glues on: TST*, SQ *, COT*, GUESTRS*.
PROCESSOR_PREFIX = re.compile(
    r"^(TST|SQ|COT|GUESTRS|SP|PY|POS|PAYPAL)\s*\*\s*", re.IGNORECASE
)
STORE_NUMBER = re.compile(r"#\s*\d+")
PHONE = re.compile(r"\b\d{3}-\d{3}-\d{4}\b")
# An opaque reference the bank appends: APPLE.COM/BILL's CAMMGGH21Q0DA0, or a
# bare account number. It must contain a digit — that is what separates an
# identifier from a word. Matching on length alone ate merchant names instead:
# DUKEENERGY, POTBELLY, GRANDFATHER, CHARLOTTE OBSERVER. Descriptors arrive in
# capitals, so every long name looked exactly like an id, and 'DUKEENERGY BILL
# PAY 910175813041' collapsed to 'bill pay' — close enough to 'BILT CARD
# HOUSING' that the first-word rule then offered the power bill and the rent as
# the same merchant.
LONG_ID = re.compile(r"\b(?=[A-Z0-9]{8,}\b)[A-Z0-9]*\d[A-Z0-9]*\b")
TRAILING_STATE = re.compile(r"\s+[A-Z]{2}\s*$")
NON_ALNUM = re.compile(r"[^a-z0-9 ]+")

# Cities that appear glued onto descriptors in this data set.
CITIES = ["charlotte", "matthews", "raleigh", "concord", "huntersville"]


def normalize_merchant(raw: str) -> str:
    """Collapse a bank descriptor to a stable key.

    'HARRIS TEETER #412 CHARLOTTE NC' and 'Harris Teeter' both become
    'harris teeter'. Conservative on purpose — it is far better to leave two
    spellings unmerged, which a human can fix in one click, than to merge two
    different merchants, which silently corrupts every total they appear in.
    """
    s = raw.strip()
    s = PROCESSOR_PREFIX.sub("", s)
    s = PHONE.sub(" ", s)
    s = STORE_NUMBER.sub(" ", s)
    s = TRAILING_STATE.sub(" ", s)
    s = LONG_ID.sub(" ", s)
    s = s.lower()
    s = NON_ALNUM.sub(" ", s)
    for city in CITIES:
        s = re.sub(rf"\b{city}\b", " ", s)
    return " ".join(s.split())


def display_name(key: str) -> str:
    """Human-facing name for a normalized key."""
    return key.title()
