"""The target-families runtime config (OC-42): discovery's candidate side is
operator-authored `instance/families.json`, not a career-graph walk. Missing or
malformed is a refused run, never a silent empty vocabulary. Placeholder
family content only: real target families live in the operator's instance."""

import json

import pytest

from adapters.storage.local import LocalStorageAdapter
from domain.requirements import coverage_bp
from workers.discovery.run import load_families, vocabulary_terms

VALID = {"families": [
    {"name": "Example Platform Family", "seniority": "senior",
     "search_vocabulary": ["example platform engineering", "another term"],
     "adjacent_titles": ["Example Adjacent Title"]},
    {"name": "Example Delivery Family", "seniority": "lead",
     "search_vocabulary": ["another term"], "adjacent_titles": []},
]}


def storage_with(tmp_path, document) -> LocalStorageAdapter:
    storage = LocalStorageAdapter(tmp_path)
    storage.write_text("families.json", document if isinstance(document, str)
                       else json.dumps(document))
    return storage


def test_a_valid_config_loads_every_family(tmp_path):
    families = load_families(storage_with(tmp_path, VALID))
    assert [f.name for f in families] == ["Example Platform Family",
                                          "Example Delivery Family"]
    assert [f.seniority for f in families] == ["senior", "lead"]
    assert families[0].search_vocabulary == ("example platform engineering",
                                             "another term")
    assert families[1].adjacent_titles == ()


def test_a_missing_file_refuses_the_run_and_names_the_file(tmp_path):
    """The whole point of the hard error: an absent config would compute an
    empty coverage vocabulary and skip the gate's seniority dimension while
    still reporting a successful run."""
    with pytest.raises(ValueError, match="families.json") as e:
        load_families(LocalStorageAdapter(tmp_path))
    assert "required" in str(e.value)
    assert "families.example.json" in str(e.value)


def test_invalid_json_is_a_clean_config_error(tmp_path):
    with pytest.raises(ValueError, match="families.json .* is not valid JSON"):
        load_families(storage_with(tmp_path, "{not json"))


def test_an_unknown_top_level_key_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown families.json keys"):
        load_families(storage_with(tmp_path, dict(VALID, cvs=[])))


def test_an_unknown_family_key_is_refused(tmp_path):
    document = {"families": [dict(VALID["families"][0], cv="cv/example.pdf")]}
    with pytest.raises(ValueError, match=r"unknown families.json families\[0\] keys"):
        load_families(storage_with(tmp_path, document))


def test_a_missing_family_key_is_refused(tmp_path):
    entry = {k: v for k, v in VALID["families"][0].items() if k != "seniority"}
    with pytest.raises(ValueError, match="is missing keys"):
        load_families(storage_with(tmp_path, {"families": [entry]}))


def test_an_empty_family_list_is_refused(tmp_path):
    with pytest.raises(ValueError, match="must be a non-empty list"):
        load_families(storage_with(tmp_path, {"families": []}))


def test_a_non_string_vocabulary_entry_is_refused(tmp_path):
    document = {"families": [dict(VALID["families"][0], search_vocabulary=[7])]}
    with pytest.raises(ValueError, match="must be a non-empty string"):
        load_families(storage_with(tmp_path, document))
    document = {"families": [dict(VALID["families"][0], search_vocabulary="term")]}
    with pytest.raises(ValueError, match="must be a list of non-empty strings"):
        load_families(storage_with(tmp_path, document))


def test_an_empty_name_is_refused(tmp_path):
    document = {"families": [dict(VALID["families"][0], name="  ")]}
    with pytest.raises(ValueError, match="'name' must be a non-empty string"):
        load_families(storage_with(tmp_path, document))


def test_the_vocabulary_is_deduplicated_in_first_seen_order(tmp_path):
    families = load_families(storage_with(tmp_path, VALID))
    assert vocabulary_terms(families) == [
        "example platform engineering", "another term",
        "Example Adjacent Title", "Example Platform Family",
        "Example Delivery Family"]


def test_vocabulary_terms_match_requirement_phrases_like_names_did(tmp_path):
    """The terms are multi-token phrases, which is exactly what the calibrated
    content-fraction matcher scores: they drop into coverage unchanged."""
    terms = vocabulary_terms(load_families(storage_with(tmp_path, VALID)))
    requirements = ("5+ years of example platform engineering experience",
                    "willingness to travel")
    assert coverage_bp(requirements, terms) == 5000
    assert coverage_bp(("willingness to travel",), terms) == 0
