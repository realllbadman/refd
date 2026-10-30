"""Exact money helpers. Amounts are integer cents; floats are never used."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# $1,000,000,000.00 — far below SQLite's 64-bit integer range, so sums of many
# balances cannot overflow either.
MAX_AMOUNT_CENTS = 100_000_000_000

_AMOUNT_RE = re.compile(r"^([+-])?\$?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?$")
_EXCESS_PRECISION_RE = re.compile(r"^[+-]?\$?[\d,]+\.\d{3,}$")


class AmountError(ValueError):
    pass


def parse_amount_to_cents(raw: str) -> int:
    """Parse a decimal string such as ``"-1,250.5"`` into cents (``-125050``).

    Rejects empty input, exponent notation, more than two decimal places and
    values outside ``±MAX_AMOUNT_CENTS``.
    """
    if not isinstance(raw, str):
        raise AmountError("Amount must be a string.")
    text = raw.strip()
    if not text:
        raise AmountError("Enter an amount.")
    match = _AMOUNT_RE.match(text)
    if not match:
        if _EXCESS_PRECISION_RE.match(text):
            raise AmountError("Amounts can have at most two decimal places.")
        raise AmountError("Enter a valid amount, for example 125.50.")
    sign, whole, frac = match.groups()
    try:
        value = Decimal(whole.replace(",", "") + "." + (frac or "0"))
    except InvalidOperation as exc:  # pragma: no cover - regex guarantees validity
        raise AmountError("Enter a valid amount.") from exc
    cents = int(value * 100)
    if sign == "-":
        cents = -cents
    if abs(cents) > MAX_AMOUNT_CENTS:
        raise AmountError(f"Amounts must not exceed {format_cents(MAX_AMOUNT_CENTS)}.")
    return cents


def format_cents(cents: int, signed: bool = False) -> str:
    """``123456`` -> ``$1,234.56``; with ``signed=True`` -> ``+$1,234.56``."""
    negative = cents < 0
    dollars, rem = divmod(abs(cents), 100)
    body = f"${dollars:,}.{rem:02d}"
    if negative:
        return "-" + body
    return ("+" + body) if signed and cents > 0 else body
