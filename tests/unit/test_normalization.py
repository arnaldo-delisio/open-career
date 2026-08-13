"""Normalization (OC-37 §5): the tested ISO 3166 lookup table, the seniority
title-token table, and vendor salary payloads into the policies.py seam shape.
Malformed input normalizes to absent, never a crash and never a fail."""

from domain.normalization import (
    ISO_3166_LOOKUP,
    SENIORITY_BANDS,
    normalize_country,
    normalize_location,
    normalize_salary,
    normalize_seniority,
)


# --- location -----------------------------------------------------------------

def test_country_names_codes_and_variants_resolve():
    assert normalize_country("Italy") == "IT"
    assert normalize_country("italia") == "IT"
    assert normalize_country("it") == "IT"
    assert normalize_country("The Netherlands") == "NL"
    assert normalize_country("UK") == "GB"


def test_unknown_country_is_none_never_a_guess():
    assert normalize_country("Freedonia") is None
    assert normalize_country("") is None
    assert normalize_country(None) is None


def test_city_country_string_normalizes_with_city_kept():
    location = normalize_location("Milan, Italy")
    assert (location.country, location.city) == ("IT", "milan")


def test_bare_country_normalizes_without_city():
    location = normalize_location("Germany")
    assert (location.country, location.city) == ("DE", None)


def test_unnormalizable_location_is_none():
    assert normalize_location("Remote - Anywhere") is None
    assert normalize_location("Springfield, Freedonia") is None


def test_lookup_table_values_are_alpha2():
    assert all(len(code) == 2 and code.isupper() for code in ISO_3166_LOOKUP.values())


# --- seniority ----------------------------------------------------------------

def test_structured_field_wins_over_title():
    assert normalize_seniority(structured="senior", title="Junior Engineer") == "senior"


def test_title_token_table_maps_common_titles():
    assert normalize_seniority(title="Senior Software Engineer") == "senior"
    assert normalize_seniority(title="Staff Engineer") == "staff_plus"
    assert normalize_seniority(title="Engineering Manager") == "manager"
    assert normalize_seniority(title="Director of Engineering") == "director_plus"
    assert normalize_seniority(title="Software Engineering Intern") == "intern"
    assert normalize_seniority(title="Jr. Developer") == "junior"


def test_most_senior_token_wins():
    # "Senior Engineering Manager" is manager-track, not senior IC.
    assert normalize_seniority(title="Senior Engineering Manager") == "manager"


def test_unstated_or_unmatched_title_is_none():
    assert normalize_seniority(title="Software Engineer") is None
    assert normalize_seniority() is None


def test_token_table_bands_are_canonical():
    from domain.normalization import TITLE_TOKEN_TABLE
    assert all(band in SENIORITY_BANDS for _, band in TITLE_TOKEN_TABLE)


# --- salary -------------------------------------------------------------------

def test_valid_range_normalizes_to_seam_shape():
    assert normalize_salary(
        {"min": 60000, "max": 80000, "currency": "eur", "period": "year"}
    ) == {"min": 60000, "max": 80000, "currency": "EUR", "period": "annual",
          "equity_only": False}


def test_period_spellings_map_to_canonical_periods():
    assert normalize_salary({"min": 30, "max": 40, "currency": "USD",
                             "period": "hour"})["period"] == "hourly"
    assert normalize_salary({"min": 4000, "max": 5000, "currency": "EUR",
                             "period": "monthly"})["period"] == "monthly"


def test_fractional_amount_is_absent():
    assert normalize_salary({"min": 60000.5, "max": 80000, "currency": "EUR",
                             "period": "annual"}) is None


def test_int_valued_float_is_still_absent():
    # The seam is integers from birth (OC-22); 60000.0 is not an integer.
    assert normalize_salary({"min": 60000.0, "max": 80000, "currency": "EUR",
                             "period": "annual"}) is None


def test_contradictory_range_is_absent():
    assert normalize_salary({"min": 90000, "max": 80000, "currency": "EUR",
                             "period": "annual"}) is None


def test_unsupported_period_is_absent():
    assert normalize_salary({"min": 500, "max": 700, "currency": "EUR",
                             "period": "weekly"}) is None


def test_unrecognized_currency_is_absent():
    assert normalize_salary({"min": 60000, "max": 80000, "currency": "EURO",
                             "period": "annual"}) is None


def test_no_amounts_is_absent():
    assert normalize_salary({"currency": "EUR", "period": "annual"}) is None
    assert normalize_salary(None) is None
    assert normalize_salary("60k-80k") is None


def test_equity_only_flag_survives():
    result = normalize_salary({"equity_only": True})
    assert result["equity_only"] is True and result["min"] is None


def test_open_ended_minimum_only_is_kept():
    result = normalize_salary({"min": 70000, "currency": "EUR", "period": "annual"})
    assert (result["min"], result["max"]) == (70000, None)
