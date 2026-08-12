"""The policy domain: closed key set, per-key shapes, and the deterministic
compensation rules (OC-35; OC-22: code, tested, no model involvement)."""

import pytest

from domain.policies import (
    CANONICAL_POLICY_KEYS,
    InvalidPolicyValueError,
    UnknownPolicyKeyError,
    annualize,
    check_compensation_floor,
    scalar_fill,
    validate_policy_key,
    validate_policy_value,
)

FLOOR = {"amount": 60000, "currency": "EUR", "period": "annual"}


def test_policy_set_is_the_twelve_ratified_keys():
    assert CANONICAL_POLICY_KEYS == {
        "eeo_stance", "compensation_floor", "compensation_target",
        "company_stage_pref", "company_size_pref", "industry_pref",
        "work_track", "mission_themes",
        "relocation_whitelist", "timezone_bounds", "visa_details", "earliest_start",
    }


def test_unknown_key_is_rejected():
    with pytest.raises(UnknownPolicyKeyError, match="unknown policy key 'favourite_color'"):
        validate_policy_key("favourite_color")


def test_every_canonical_key_has_a_validator():
    """Pin the fail-closed contract (Codex round 2): the validator map covers
    the canonical set exactly, and an unmapped key is refused, never passed."""
    from domain.policies import _VALIDATORS

    assert set(_VALIDATORS) == CANONICAL_POLICY_KEYS


def test_none_always_clears():
    for key in CANONICAL_POLICY_KEYS:
        validate_policy_value(key, None)  # must not raise


@pytest.mark.parametrize("key,value", [
    ("eeo_stance", "answer_honestly"),
    ("compensation_floor", FLOOR),
    ("compensation_target", {"min": 60000, "max": 80000, "currency": "EUR",
                             "period": "annual", "scalar": "mid"}),
    ("company_stage_pref", {"in": ["growth"], "out": ["seed"]}),
    ("company_size_pref", {"in": [], "out": ["10000+"]}),
    ("industry_pref", {"in": ["dev tools"], "out": ["adtech"]}),
    ("work_track", "either"),
    ("mission_themes", ["clear thinking", "better systems"]),
    ("relocation_whitelist", ["Dublin", "Amsterdam"]),
    ("timezone_bounds", {"min_utc_offset": -2, "max_utc_offset": 3}),
    ("visa_details", {"status_note": "EU citizen"}),
    ("visa_details", {"status_note": "H-1B", "expiry_date": "2027-03-01"}),
    ("earliest_start", "2026-10-01"),
])
def test_valid_shapes_pass(key, value):
    validate_policy_value(key, value)


@pytest.mark.parametrize("key,value,message", [
    ("eeo_stance", "whatever", "expected one of"),
    ("compensation_floor", {"amount": 60000.5, "currency": "EUR", "period": "annual"},
     "positive integer"),           # floats never (OC-22)
    ("compensation_floor", {"amount": True, "currency": "EUR", "period": "annual"},
     "positive integer"),           # bool is not an amount
    ("compensation_floor", {"amount": 60000, "currency": "EURO", "period": "annual"},
     "3-letter code"),
    ("compensation_floor", {"amount": 60000, "currency": "EUR", "period": "weekly"},
     "'period' must be one of"),
    ("compensation_floor", {"amount": 60000, "currency": "EUR"}, "missing keys"),
    ("compensation_target", {"min": 90000, "max": 80000, "currency": "EUR",
                             "period": "annual", "scalar": "mid"}, "must not exceed"),
    ("compensation_target", {"min": 60000, "max": 80000, "currency": "EUR",
                             "period": "annual", "scalar": "median"}, "'scalar'"),
    ("company_stage_pref", {"in": ["ok"], "out": [""]}, "non-empty strings"),
    ("company_stage_pref", {"in": ["ok"]}, "missing keys"),
    ("company_stage_pref", {"in": [], "out": [], "maybe": []}, "unknown keys"),
    ("work_track", "founder", "expected one of"),
    ("mission_themes", "not-a-list", "must be a list"),
    ("timezone_bounds", {"min_utc_offset": 3, "max_utc_offset": -2}, "must not exceed"),
    ("timezone_bounds", {"min_utc_offset": -13, "max_utc_offset": 0}, "between -12 and 14"),
    ("timezone_bounds", {"min_utc_offset": 1.5, "max_utc_offset": 3}, "integer"),
    ("visa_details", {"status_note": ""}, "non-empty"),
    ("visa_details", {"status_note": "ok", "expiry_date": "March 2027"}, "YYYY-MM-DD"),
    ("earliest_start", "soon", "YYYY-MM-DD"),
])
def test_invalid_shapes_are_rejected(key, value, message):
    with pytest.raises(InvalidPolicyValueError, match=message):
        validate_policy_value(key, value)


# --- deterministic compensation comparison ------------------------------------

def test_annualize_uses_fixed_stated_factors():
    assert annualize(30, "hourly") == 62400   # 40h x 52 weeks
    assert annualize(5000, "monthly") == 60000
    assert annualize(70000, "annual") == 70000


def test_no_floor_skips_with_reason():
    check = check_compensation_floor({"min": 1, "max": 2, "currency": "EUR",
                                      "period": "annual"}, None)
    assert check.verdict == "skip" and "no compensation floor" in check.reason


@pytest.mark.parametrize("posting,reason", [
    (None, "states no compensation"),
    ({"min": None, "max": None, "currency": "EUR", "period": "annual"},
     "no compensation amount"),
    ({"min": 50000, "max": 70000, "currency": "USD", "period": "annual"},
     "currency mismatch"),
    ({"min": 50000, "max": 70000, "currency": None, "period": "annual"},
     "currency mismatch"),
    ({"min": 50000, "max": 70000, "currency": "EUR", "period": None},
     "period unstated"),
    ({"equity_only": True}, "equity-only"),
])
def test_unknown_or_incomparable_compensation_skips_with_reason(posting, reason):
    check = check_compensation_floor(posting, FLOOR)
    assert check.verdict == "skip"
    assert reason in check.reason


def test_posting_max_below_floor_fails():
    check = check_compensation_floor(
        {"min": 40000, "max": 55000, "currency": "EUR", "period": "annual"}, FLOOR)
    assert check.verdict == "fail"


def test_posting_min_at_or_above_floor_passes():
    check = check_compensation_floor(
        {"min": 60000, "max": 90000, "currency": "eur", "period": "annual"}, FLOOR)
    assert check.verdict == "pass" and check.note is None  # currency case-insensitive


def test_straddling_range_passes_with_below_floor_note():
    check = check_compensation_floor(
        {"min": 50000, "max": 80000, "currency": "EUR", "period": "annual"}, FLOOR)
    assert check.verdict == "pass"
    assert "below-floor" in check.note


def test_periods_normalize_before_comparison():
    # 4000/month = 48000 annual, below a 60000 floor.
    check = check_compensation_floor(
        {"min": None, "max": 4000, "currency": "EUR", "period": "monthly"}, FLOOR)
    assert check.verdict == "fail"


def test_min_only_posting_below_floor_passes_with_accurate_note():
    """Regression (Codex round 1): a min-only posting below the floor used to
    pass with a reason claiming a posting maximum and a note claiming no
    minimum, both false."""
    check = check_compensation_floor(
        {"min": 50000, "max": None, "currency": "EUR", "period": "annual"}, FLOOR)
    assert check.verdict == "pass"
    assert "only a minimum" in check.reason
    assert "below the floor" in check.note
    assert "maximum" not in check.reason and "no minimum" not in check.note


def test_max_only_posting_above_floor_passes_with_note():
    check = check_compensation_floor(
        {"min": None, "max": 70000, "currency": "EUR", "period": "annual"}, FLOOR)
    assert check.verdict == "pass" and "no minimum" in check.note


def test_scalar_fill_is_the_users_preselection_or_nothing():
    target = {"min": 60000, "max": 80001, "currency": "EUR", "period": "annual",
              "scalar": "mid"}
    assert scalar_fill(target) == 70000  # integer floor division, no floats
    assert scalar_fill({**target, "scalar": "min"}) == 60000
    assert scalar_fill({**target, "scalar": "max"}) == 80001
    assert scalar_fill(None) is None  # no target: the question routes to the floor
    assert scalar_fill({"min": 1, "max": 2}) is None  # no scalar: never a guess
