"""User policies: the closed policy set (OC-35) and its shape validation, plus
the deterministic compensation comparison rules the eligibility gate and form
fills consume (OC-22: code, tested, no model involvement).

Policies are standing stances that *derive* answers (EEO handling, compensation
floor/target, preferences, logistics depth); they never carry a profile-derivable
form answer, which stays a canonical profile field (OC-29). One JSON row in
user_policies, every write audited through the policy seam. All amounts and
offsets are integers, never floats (OC-22: no fake precision).
"""

import re
from dataclasses import dataclass
from typing import Literal

EEO_STANCES = ("answer_honestly", "always_decline", "per_application")
WORK_TRACKS = ("ic", "management", "either")

# Fixed period normalization factors to an annual amount. Stated, never
# configurable: hourly assumes a 40-hour week x 52 weeks.
PERIOD_FACTORS: dict[str, int] = {"hourly": 2080, "monthly": 12, "annual": 1}

CANONICAL_POLICY_KEYS: frozenset[str] = frozenset({
    # standing stances (sitting one / two)
    "eeo_stance", "compensation_floor", "compensation_target",
    # preferences and dealbreakers (depth cluster 3)
    "company_stage_pref", "company_size_pref", "industry_pref",
    "work_track", "mission_themes",
    # logistics depth (depth cluster 6)
    "relocation_whitelist", "timezone_bounds", "visa_details", "earliest_start",
})


class UnknownPolicyKeyError(ValueError):
    pass


class InvalidPolicyValueError(ValueError):
    pass


_CURRENCY = re.compile(r"^[A-Za-z]{3}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_int(value) -> bool:
    """A real integer: bool is an int subclass and is rejected explicitly."""
    return isinstance(value, int) and not isinstance(value, bool)


def _require(condition: bool, key: str, message: str) -> None:
    if not condition:
        raise InvalidPolicyValueError(f"policy '{key}': {message}")


def _check_keys(value: dict, key: str, required: set[str], optional: set[str] = frozenset()) -> None:
    _require(isinstance(value, dict), key, "expected a JSON object")
    missing = required - set(value)
    unknown = set(value) - required - optional
    _require(not missing, key, f"missing keys {sorted(missing)}")
    _require(not unknown, key, f"unknown keys {sorted(unknown)}")


def _check_str_list(value, key: str, what: str) -> None:
    _require(isinstance(value, list), key, f"{what} must be a list of strings")
    _require(all(isinstance(v, str) and v.strip() for v in value), key,
             f"{what} entries must be non-empty strings")


def _check_money(value: dict, key: str, amount_keys: tuple[str, ...]) -> None:
    for amount_key in amount_keys:
        _require(_is_int(value.get(amount_key)) and value[amount_key] > 0, key,
                 f"'{amount_key}' must be a positive integer (no floats, OC-22)")
    _require(isinstance(value.get("currency"), str) and _CURRENCY.match(value["currency"]),
             key, "'currency' must be a 3-letter code (e.g. EUR)")
    _require(value.get("period") in PERIOD_FACTORS, key,
             f"'period' must be one of {sorted(PERIOD_FACTORS)}")


def _check_in_out_pref(key: str, value) -> None:
    _check_keys(value, key, {"in", "out"})
    _check_str_list(value["in"], key, "'in'")
    _check_str_list(value["out"], key, "'out'")


def validate_policy_key(key: str) -> None:
    """The set is closed: an unknown key is an error, never a silent write."""
    if key not in CANONICAL_POLICY_KEYS:
        raise UnknownPolicyKeyError(
            f"unknown policy key '{key}'; canonical policies: "
            + ", ".join(sorted(CANONICAL_POLICY_KEYS)))


def _validate_eeo_stance(key: str, value) -> None:
    _require(value in EEO_STANCES, key, f"expected one of {EEO_STANCES}")


def _validate_compensation_floor(key: str, value) -> None:
    _check_keys(value, key, {"amount", "currency", "period"})
    _check_money(value, key, ("amount",))


def _validate_compensation_target(key: str, value) -> None:
    _check_keys(value, key, {"min", "max", "currency", "period", "scalar"})
    _check_money(value, key, ("min", "max"))
    _require(value["min"] <= value["max"], key, "'min' must not exceed 'max'")
    _require(value["scalar"] in ("min", "mid", "max"), key,
             "'scalar' must be min, mid, or max")


def _validate_work_track(key: str, value) -> None:
    _require(value in WORK_TRACKS, key, f"expected one of {WORK_TRACKS}")


def _validate_str_list(key: str, value) -> None:
    _check_str_list(value, key, "value")


def _validate_timezone_bounds(key: str, value) -> None:
    _check_keys(value, key, {"min_utc_offset", "max_utc_offset"})
    for bound in ("min_utc_offset", "max_utc_offset"):
        _require(_is_int(value.get(bound)) and -12 <= value[bound] <= 14, key,
                 f"'{bound}' must be an integer UTC offset between -12 and 14")
    _require(value["min_utc_offset"] <= value["max_utc_offset"], key,
             "'min_utc_offset' must not exceed 'max_utc_offset'")


def _validate_visa_details(key: str, value) -> None:
    _check_keys(value, key, {"status_note"}, optional={"expiry_date"})
    _require(isinstance(value.get("status_note"), str) and value["status_note"].strip(),
             key, "'status_note' must be a non-empty string")
    if "expiry_date" in value:
        _require(isinstance(value["expiry_date"], str) and bool(_DATE.match(value["expiry_date"])),
                 key, "'expiry_date' must be a YYYY-MM-DD date")


def _validate_date(key: str, value) -> None:
    _require(isinstance(value, str) and bool(_DATE.match(value)), key,
             "expected a YYYY-MM-DD date")


# One validator per canonical key; the module-level check below (and a pinning
# test) makes a key without a validator impossible, so validation can never
# fall through permissively (Codex round 2).
_VALIDATORS = {
    "eeo_stance": _validate_eeo_stance,
    "compensation_floor": _validate_compensation_floor,
    "compensation_target": _validate_compensation_target,
    "company_stage_pref": _check_in_out_pref,
    "company_size_pref": _check_in_out_pref,
    "industry_pref": _check_in_out_pref,
    "work_track": _validate_work_track,
    "mission_themes": _validate_str_list,
    "relocation_whitelist": _validate_str_list,
    "timezone_bounds": _validate_timezone_bounds,
    "visa_details": _validate_visa_details,
    "earliest_start": _validate_date,
}

if set(_VALIDATORS) != CANONICAL_POLICY_KEYS:  # import-time backstop
    raise AssertionError("every canonical policy key needs exactly one validator")


def validate_policy_value(key: str, value) -> None:
    """Per-key shape validation. None (clearing a policy) is always allowed;
    a key with no registered validator fails closed, never silently passes."""
    if value is None:
        return
    validator = _VALIDATORS.get(key)
    if validator is None:
        raise InvalidPolicyValueError(
            f"policy '{key}': no validator registered; refusing the write")
    validator(key, value)


# --- Deterministic compensation comparison (OC-22) ---------------------------

CompVerdict = Literal["pass", "fail", "skip"]


@dataclass(frozen=True)
class CompCheck:
    """The floor gate's output: a verdict with its reason always stated, so a
    skipped check is visible in gate output, never a silent blank."""

    verdict: CompVerdict
    reason: str
    note: str | None = None


def annualize(amount: int, period: str) -> int:
    """Normalize an amount to annual by the fixed stated factors. Integer
    arithmetic only."""
    if period not in PERIOD_FACTORS:
        raise InvalidPolicyValueError(f"unknown compensation period '{period}'")
    return amount * PERIOD_FACTORS[period]


def check_compensation_floor(posting: dict | None, floor: dict | None) -> CompCheck:
    """Compare a posting's stated compensation against the user's floor.

    posting shape: {"min": int|None, "max": int|None, "currency": str|None,
    "period": str|None, "equity_only": bool (optional)}. Rules, all
    deterministic: absent floor skips (no data, no verdict); unknown, unstated,
    or equity-only posting compensation skips with the reason; currencies are
    never converted, a mismatch skips and says so; ranges compare
    conservatively (posting max below floor fails; posting min at or above
    floor passes; straddling ranges pass with a below-floor-possible note)."""
    if floor is None:
        return CompCheck("skip", "no compensation floor set; comp check skipped")
    if posting is None:
        return CompCheck("skip", "posting states no compensation; comp check skipped")
    if posting.get("equity_only"):
        return CompCheck("skip", "posting compensation is equity-only; comp check skipped")
    posting_min, posting_max = posting.get("min"), posting.get("max")
    if posting_min is None and posting_max is None:
        return CompCheck("skip", "posting states no compensation amount; comp check skipped")
    if posting.get("period") not in PERIOD_FACTORS:
        return CompCheck("skip", "posting compensation period unstated; comp check skipped")
    posting_currency = posting.get("currency")
    if not isinstance(posting_currency, str) or \
            posting_currency.upper() != floor["currency"].upper():
        return CompCheck(
            "skip", f"currency mismatch ({posting_currency or 'unstated'} vs"
                    f" {floor['currency']}); currencies are never converted, comp check skipped")

    floor_annual = annualize(floor["amount"], floor["period"])
    low = annualize(posting_min, posting["period"]) if posting_min is not None else None
    high = annualize(posting_max, posting["period"]) if posting_max is not None else None
    if high is not None and high < floor_annual:
        return CompCheck("fail", "posting maximum is below the compensation floor")
    if low is not None and low >= floor_annual:
        return CompCheck("pass", "posting minimum meets the compensation floor")
    if low is not None and high is not None:
        return CompCheck("pass", "posting range straddles the compensation floor",
                         note="below-floor offer possible: posting minimum is under the floor")
    # One endpoint stated and it does not decide: conservative pass whose
    # reason and note describe exactly what was stated.
    if low is not None:  # min-only, below floor (at-or-above returned above)
        return CompCheck(
            "pass", "posting states only a minimum, which is below the compensation floor",
            note="below-floor offer possible: only the posting minimum is stated"
                 " and it is below the floor")
    return CompCheck("pass", "posting maximum meets the compensation floor",
                     note="below-floor offer possible: posting states no minimum")


def scalar_fill(target: dict | None) -> int | None:
    """The single-number salary-field fill from the compensation target: the
    user's pre-selected scalar (min | mid | max), integer arithmetic. None when
    no target (or no scalar) is stored: the question routes to the progressive
    floor, never a number the user did not pre-select."""
    if target is None:
        return None
    scalar = target.get("scalar")
    if scalar == "min":
        return target["min"]
    if scalar == "max":
        return target["max"]
    if scalar == "mid":
        return (target["min"] + target["max"]) // 2
    return None
