"""Task C's two server-calendar-day recovery guard; no I/O or live actions."""
from datetime import date, timedelta
import math
import re


def guard_metadata(server_date: date) -> dict:
    previous = server_date - timedelta(days=1)
    return {
        "version": 1,
        "serverDate": server_date.isoformat(),
        "previousDate": previous.isoformat(),
        "headers": [server_date.strftime("%m-%d回复率"), previous.strftime("%m-%d回复率")],
        "comparison": "any_strictly_greater_than",
        "unknownPolicy": "exclude",
    }


def daily_rate(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    text = str(value).strip()
    if not text:
        return None
    percent = text.endswith("%")
    numeric = (text[:-1] if percent else text).strip()
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?", numeric, re.IGNORECASE):
        return None
    try:
        number = float(numeric)
    except (ValueError, OverflowError):
        return None
    rate = number / 100 if percent or number > 1 else number
    return rate if math.isfinite(rate) and 0 <= rate <= 1 else None


def exclusion_reason(values: dict, headers: list[str], threshold: float):
    rates = [daily_rate(values.get(header)) for header in headers]
    if any(rate is not None and rate > threshold for rate in rates):
        return "above_threshold"
    if any(rate is None for rate in rates):
        return "missing_or_invalid"
    return None
