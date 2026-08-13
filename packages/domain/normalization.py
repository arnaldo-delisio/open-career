"""Deterministic normalization for discovery (OC-37 §5): canonical location
through a tested ISO 3166-1 lookup table, seniority through an explicit
title-token table, and vendor salary payloads into the exact seam shape
`policies.check_compensation_floor` consumes.

No model anywhere (OC-5): everything here is a lookup table or a shape rule,
and anything malformed or unrecognized normalizes to absent, which downstream
yields an auditable skip, never a crash and never a fail.
"""

import re
from dataclasses import dataclass

# --- Canonical location (ISO 3166-1 alpha-2) ---------------------------------

# The tested lookup table: lowercase name or code -> ISO 3166-1 alpha-2.
# Coverage is EU/EEA plus the countries the search actually touches; an entry
# missing here makes the location dimension skip with a reason, never guess.
ISO_3166_LOOKUP: dict[str, str] = {
    # codes map to themselves so already-canonical input passes through
    **{code.lower(): code for code in (
        "AT", "BE", "BG", "CA", "CH", "CY", "CZ", "DE", "DK", "EE", "ES", "FI",
        "FR", "GB", "GR", "HR", "HU", "IE", "IS", "IT", "LI", "LT", "LU", "LV",
        "MT", "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK", "US",
    )},
    "austria": "AT", "belgium": "BE", "bulgaria": "BG", "canada": "CA",
    "switzerland": "CH", "cyprus": "CY", "czech republic": "CZ", "czechia": "CZ",
    "germany": "DE", "denmark": "DK", "estonia": "EE", "spain": "ES",
    "finland": "FI", "france": "FR",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB", "england": "GB",
    "greece": "GR", "croatia": "HR", "hungary": "HU", "ireland": "IE",
    "iceland": "IS", "italy": "IT", "italia": "IT", "liechtenstein": "LI",
    "lithuania": "LT", "luxembourg": "LU", "latvia": "LV", "malta": "MT",
    "netherlands": "NL", "the netherlands": "NL", "holland": "NL",
    "norway": "NO", "poland": "PL", "portugal": "PT", "romania": "RO",
    "sweden": "SE", "slovenia": "SI", "slovakia": "SK",
    "united states": "US", "usa": "US", "united states of america": "US",
}


@dataclass(frozen=True)
class CanonicalLocation:
    """ISO country code, optional region, optional normalized city, with the
    provenance of the raw string it was resolved from."""

    country: str
    region: str | None = None
    city: str | None = None
    raw: str | None = None


def normalize_country(raw: str | None) -> str | None:
    """Country string -> ISO 3166-1 alpha-2 via the lookup table, else None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    return ISO_3166_LOOKUP.get(raw.strip().lower())


def normalize_location(raw: str | None) -> CanonicalLocation | None:
    """'City, Country' or 'Country' -> CanonicalLocation, else None. The last
    comma-separated segment must resolve as the country; a leading segment, if
    present, is kept as the normalized (lowercased) city."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    country = normalize_country(parts[-1])
    if country is None:
        return None
    city = parts[0].lower() if len(parts) > 1 else None
    return CanonicalLocation(country=country, city=city, raw=raw)


# --- Seniority (the dimension OC-22 locks) ------------------------------------

SENIORITY_BANDS: tuple[str, ...] = (
    "intern", "junior", "mid", "senior", "staff_plus", "manager", "director_plus",
)

# Explicit title-token table (a tested constant, no model). First match in
# this order wins, most senior first, so "Senior Engineering Manager" reads as
# manager-track and "Staff Software Engineer" as staff_plus.
TITLE_TOKEN_TABLE: tuple[tuple[str, str], ...] = (
    ("director", "director_plus"), ("vp", "director_plus"),
    ("vice president", "director_plus"), ("head of", "director_plus"),
    ("chief", "director_plus"),
    ("manager", "manager"),
    ("staff", "staff_plus"), ("principal", "staff_plus"),
    ("distinguished", "staff_plus"),
    ("senior", "senior"), ("sr.", "senior"), ("sr ", "senior"),
    ("junior", "junior"), ("jr.", "junior"), ("jr ", "junior"),
    ("graduate", "junior"), ("entry level", "junior"), ("entry-level", "junior"),
    ("intern", "intern"), ("internship", "intern"), ("working student", "intern"),
    ("mid-level", "mid"), ("mid level", "mid"), ("intermediate", "mid"),
)


def normalize_seniority(structured: str | None = None, title: str | None = None) -> str | None:
    """Structured ATS seniority field where present, else the title-token
    table. Unstated or unmatched is None: an auditable skip, never a guess."""
    if isinstance(structured, str) and structured.strip().lower() in SENIORITY_BANDS:
        return structured.strip().lower()
    if not isinstance(title, str):
        return None
    haystack = f" {title.lower()} "
    for token, band in TITLE_TOKEN_TABLE:
        if token in haystack:
            return band
    return None


# --- Compensation (into the policies.py seam shape) ---------------------------

# Vendor period spellings -> the seam's canonical periods (hourly/monthly/
# annual only, matching PERIOD_FACTORS in policies.py).
_PERIOD_MAP: dict[str, str] = {
    "hour": "hourly", "hourly": "hourly",
    "month": "monthly", "monthly": "monthly",
    "year": "annual", "yearly": "annual", "annual": "annual", "annually": "annual",
}

_CURRENCY = re.compile(r"^[A-Za-z]{3}$")


def _as_whole_int(value) -> int | None:
    """A non-negative whole-unit integer amount (no floats, no minor units,
    matching policies.py); an int-valued float is still rejected as malformed
    because the seam is integers from birth (OC-22)."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def normalize_salary(payload: dict | None) -> dict | None:
    """Vendor salary payload -> the check_compensation_floor posting shape:
    {"min", "max", "currency", "period", "equity_only"}, or None.

    Accepted input keys: min, max, currency, period, equity_only. Any
    unsupported period, fractional or negative amount, contradictory range
    (min above max), unrecognized currency, or otherwise malformed data
    normalizes to absent (§5): the seam's auditable skip, never a fail."""
    if not isinstance(payload, dict):
        return None
    if payload.get("equity_only") is True:
        return {"min": None, "max": None, "currency": None, "period": None,
                "equity_only": True}
    raw_min, raw_max = payload.get("min"), payload.get("max")
    low = _as_whole_int(raw_min) if raw_min is not None else None
    high = _as_whole_int(raw_max) if raw_max is not None else None
    # A stated amount that fails the integer rule is malformed, not ignorable.
    if (raw_min is not None and low is None) or (raw_max is not None and high is None):
        return None
    if low is None and high is None:
        return None
    if low is not None and high is not None and low > high:
        return None
    period = _PERIOD_MAP.get(str(payload.get("period", "")).strip().lower())
    if period is None:
        return None
    currency = payload.get("currency")
    if not isinstance(currency, str) or not _CURRENCY.match(currency.strip()):
        return None
    return {"min": low, "max": high, "currency": currency.strip().upper(),
            "period": period, "equity_only": False}
