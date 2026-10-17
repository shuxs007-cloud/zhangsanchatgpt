"""Time-bounded source-carrier protection, evaluated in absolute UTC time."""
import re
from datetime import datetime

# User explicitly resumed B11 early; retain the timestamp for historical audits.
B11_RESUME_AT = datetime.fromisoformat("2026-09-11T23:01:08+08:00")


def isolation_carrier_held(name):
    return str(name or "").strip() == "三方隔离"


def temporary_carrier_held(name, now=None):
    now = now or datetime.now(B11_RESUME_AT.tzinfo)
    if now.tzinfo is None:
        raise ValueError("temporary carrier hold requires timezone-aware time")
    return now < B11_RESUME_AT and bool(re.match(r"^B11(?:-|\s|$)", str(name or "").strip(), re.I))
