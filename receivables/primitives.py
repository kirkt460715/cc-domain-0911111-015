import re
import json
import hashlib
from decimal import Decimal, InvalidOperation
from datetime import date

CURRENCIES = {"CNY": 2, "USD": 2, "EUR": 2, "JPY": 0, "KWD": 3}


class BookError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self):
        return {"code": self.code, "message": str(self), "details": self.details}


def error(code, message, **details):
    raise BookError(code, message, **details)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", value
    ):
        error("input", "invalid identifier")
    return value


def day(value):
    if not isinstance(value, str):
        error("input", "date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        error("input", "invalid calendar date")
    if parsed.isoformat() != value or not 1970 <= parsed.year <= 2200:
        error("input", "unsupported calendar date")
    return value


def currency(value):
    if value not in CURRENCIES:
        error("currency", "unsupported currency", currency=value)
    return value


def money(value, code, allow_zero=False):
    currency(code)
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        error("input", "money must be exact text or integer")
    try:
        decimal = Decimal(value)
    except InvalidOperation:
        error("input", "invalid money")
    if not decimal.is_finite() or abs(decimal) > Decimal("1e15"):
        error("input", "money outside supported range")
    scaled = decimal * (10 ** CURRENCIES[code])
    if scaled != scaled.to_integral_value():
        error("precision", "money exceeds currency precision")
    if scaled < 0 or (not allow_zero and scaled == 0):
        error("input", "money must be positive")
    return int(scaled)


def display(value, code):
    places = CURRENCIES[currency(code)]
    return format(Decimal(value) / (10**places), f".{places}f")


def integer(value, minimum=0, maximum=2**53 - 1):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        error("input", "invalid bounded integer")
    return value


def obj(value, allowed=None, required=()):
    if not isinstance(value, dict):
        error("input", "expected object")
    if set(required) - value.keys():
        error("input", "missing fields", fields=sorted(set(required) - value.keys()))
    if allowed is not None and value.keys() - set(allowed):
        error("input", "unknown fields", fields=sorted(value.keys() - set(allowed)))
    return value


def sequence(value, minimum=0, maximum=10000):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        error("input", "invalid list size")
    return value


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def decode(content):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                error("input", "duplicate JSON key", key=key)
            result[key] = value
        return result

    def nonfinite(token):
        error("input", "nonfinite JSON number", token=token)

    try:
        return json.loads(content, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, UnicodeError) as exc:
        error("input", "malformed JSON", reason=str(exc))
