"""The deterministic eligibility gate (OC-37 §5): fail only on a decisive
structured conflict; every ambiguity skips with its reason; a skip never
excludes; work authorization and language are explicitly inactive."""

import pytest

from domain.gate import (
    CompanyMetadata,
    GateContext,
    PostingFacts,
    evaluate_gate,
)

FLOOR = {"amount": 70000, "currency": "EUR", "period": "annual"}


def context(**overrides) -> GateContext:
    defaults = dict(
        policies={
            "compensation_floor": FLOOR,
            "relocation_whitelist": ["Italy", "Berlin, Germany"],
            "timezone_bounds": {"min_utc_offset": -2, "max_utc_offset": 3},
            "industry_pref": {"in": [], "out": ["gambling"]},
            "company_stage_pref": {"in": [], "out": ["pre-seed"]},
            "company_size_pref": {"in": [], "out": ["10000+"]},
        },
        residence_country="Italy",
        active_family_target_seniorities=("senior", "staff"),
    )
    defaults.update(overrides)
    return GateContext(**defaults)


# --- overall verdict ----------------------------------------------------------

def test_all_skips_is_a_pass_skip_never_excludes():
    result = evaluate_gate(PostingFacts(), GateContext())
    assert result.verdict == "pass"
    assert all(d.verdict == "skip" for d in result.dimensions)
    assert all(d.reason for d in result.dimensions)


def test_one_failing_dimension_fails_the_gate():
    posting = PostingFacts(salary={"min": 40000, "max": 50000, "currency": "EUR",
                                   "period": "annual", "equity_only": False})
    result = evaluate_gate(posting, context())
    assert result.verdict == "fail"
    assert result.dimension("compensation").verdict == "fail"


# --- compensation (via check_compensation_floor) --------------------------------

def test_absent_salary_skips_with_reason():
    result = evaluate_gate(PostingFacts(), context())
    check = result.dimension("compensation")
    assert check.verdict == "skip" and "no compensation" in check.reason


def test_currency_mismatch_skips_never_fails():
    posting = PostingFacts(salary={"min": 40000, "max": 50000, "currency": "USD",
                                   "period": "annual", "equity_only": False})
    assert evaluate_gate(posting, context()).dimension("compensation").verdict == "skip"


# --- location matrix (§5, exactly) ---------------------------------------------

@pytest.mark.parametrize("posting, expected, reason_fragment", [
    # unrestricted remote passes
    (PostingFacts(remote_mode="remote"), "pass", "unrestricted remote"),
    # remote restricted to an allowed country passes
    (PostingFacts(remote_mode="remote", remote_restriction_countries=("Italy", "Spain")),
     "pass", "includes an allowed country"),
    # remote restriction excluding residence and every whitelist country fails
    (PostingFacts(remote_mode="remote", remote_restriction_countries=("United States",)),
     "fail", "excludes the residence country"),
    # unresolvable restriction entry skips
    (PostingFacts(remote_mode="remote", remote_restriction_countries=("EMEA",)),
     "skip", "could not be normalized"),
    # onsite in the residence country passes
    (PostingFacts(remote_mode="onsite", locations=("Rome, Italy",)), "pass", "allowed"),
    # onsite where every stated country is outside the allowed set fails
    (PostingFacts(remote_mode="onsite", locations=("Boston, United States",)),
     "fail", "outside the whitelist"),
    # multi-location: one allowed country is enough to pass
    (PostingFacts(remote_mode="onsite",
                  locations=("Boston, United States", "Milan, Italy")), "pass", "allowed"),
    # city mismatch inside a whitelisted country skips with a note, never fails
    (PostingFacts(remote_mode="onsite", locations=("Munich, Germany",)),
     "skip", "no listed city matches"),
    # city match inside a whitelisted country passes
    (PostingFacts(remote_mode="onsite", locations=("Berlin, Germany",)), "pass", "allowed"),
    # unparsed location skips
    (PostingFacts(remote_mode="onsite", locations=("Anywhere",)), "skip", "could not be normalized"),
    # hybrid without a stated base skips
    (PostingFacts(remote_mode="hybrid"), "skip", "no base location"),
    # nothing stated at all skips
    (PostingFacts(), "skip", "no location or remote mode"),
])
def test_location_matrix(posting, expected, reason_fragment):
    check = evaluate_gate(posting, context()).dimension("location")
    assert check.verdict == expected, check.reason
    assert reason_fragment in check.reason or (check.note and reason_fragment in check.note)


def test_unnormalizable_whitelist_entry_skips_the_dimension():
    ctx = context(policies={"relocation_whitelist": ["Atlantis"]},
                  residence_country="Italy")
    check = evaluate_gate(
        PostingFacts(remote_mode="onsite", locations=("Rome, Italy",)), ctx
    ).dimension("location")
    assert check.verdict == "skip" and "Atlantis" in check.reason


def test_residence_country_alone_is_enough_to_compare():
    ctx = context(policies={}, residence_country="Italy")
    check = evaluate_gate(
        PostingFacts(remote_mode="onsite", locations=("Milan, Italy",)), ctx
    ).dimension("location")
    assert check.verdict == "pass"


# --- timezone -------------------------------------------------------------------

def test_stated_overlapping_timezone_requirement_passes():
    posting = PostingFacts(timezone_requirement={"min_utc_offset": 0, "max_utc_offset": 2})
    assert evaluate_gate(posting, context()).dimension("timezone").verdict == "pass"


def test_disjoint_timezone_requirement_fails():
    posting = PostingFacts(timezone_requirement={"min_utc_offset": -8, "max_utc_offset": -5})
    assert evaluate_gate(posting, context()).dimension("timezone").verdict == "fail"


def test_no_structured_timezone_requirement_skips():
    assert evaluate_gate(PostingFacts(), context()).dimension("timezone").verdict == "skip"


def test_timezone_requirement_without_user_bounds_skips():
    ctx = context(policies={})
    posting = PostingFacts(timezone_requirement={"min_utc_offset": -8, "max_utc_offset": -5})
    assert evaluate_gate(posting, ctx).dimension("timezone").verdict == "skip"


# --- hard exclusions (reviewed registry metadata only) ---------------------------

def test_reviewed_industry_match_fails():
    ctx = context(company=CompanyMetadata(industry="Gambling"))
    check = evaluate_gate(PostingFacts(), ctx).dimension("industry_exclusion")
    assert check.verdict == "fail"


def test_unreviewed_metadata_skips_never_fails():
    check = evaluate_gate(PostingFacts(), context()).dimension("industry_exclusion")
    assert check.verdict == "skip" and "not reviewed" in check.reason


def test_reviewed_non_matching_metadata_passes():
    ctx = context(company=CompanyMetadata(industry="fintech", company_stage="series-b",
                                          company_size_band="51-200"))
    result = evaluate_gate(PostingFacts(), ctx)
    assert result.dimension("industry_exclusion").verdict == "pass"
    assert result.dimension("company_stage_exclusion").verdict == "pass"
    assert result.dimension("company_size_exclusion").verdict == "pass"


def test_no_exclusion_list_skips():
    ctx = context(policies={}, company=CompanyMetadata(industry="gambling"))
    assert evaluate_gate(PostingFacts(), ctx).dimension("industry_exclusion").verdict == "skip"


# --- seniority --------------------------------------------------------------------

def test_matching_band_passes():
    posting = PostingFacts(seniority="senior")
    assert evaluate_gate(posting, context()).dimension("seniority").verdict == "pass"


def test_decisive_band_mismatch_fails():
    posting = PostingFacts(seniority="intern")
    check = evaluate_gate(posting, context()).dimension("seniority")
    assert check.verdict == "fail" and "outside every active" in check.reason


def test_unstated_posting_seniority_skips():
    assert evaluate_gate(PostingFacts(), context()).dimension("seniority").verdict == "skip"


def test_families_with_no_normalizable_target_contribute_no_constraint():
    ctx = context(active_family_target_seniorities=(None, "whatever fits"))
    check = evaluate_gate(PostingFacts(seniority="intern"), ctx).dimension("seniority")
    assert check.verdict == "skip" and "no active role family" in check.reason


def test_family_targets_normalize_through_the_token_table():
    ctx = context(active_family_target_seniorities=("Staff or Principal",))
    posting = PostingFacts(seniority="staff_plus")
    assert evaluate_gate(posting, ctx).dimension("seniority").verdict == "pass"


# --- inactive dimensions -------------------------------------------------------------

def test_work_authorization_and_language_always_skip_with_the_stated_reason():
    result = evaluate_gate(PostingFacts(), context())
    for name in ("work_authorization", "language"):
        check = result.dimension(name)
        assert check.verdict == "skip"
        assert "explicitly inactive" in check.reason
