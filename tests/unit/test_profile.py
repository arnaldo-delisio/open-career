"""The profile write seam: closed field set, single audited mutation path,
Resolution source reserved (spec: user_profile in decisions/career-graph-schema.md)."""

import sqlite3

import pytest

from adapters.storage.migrations import migrate
from adapters.storage.sqlite_profile import SqliteUserProfileRepository
from domain.profile import (
    CANONICAL_PROFILE_FIELDS,
    InvalidProfileValueError,
    UnknownProfileFieldError,
)


@pytest.fixture
def repo(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    yield SqliteUserProfileRepository(conn)
    conn.close()


def test_canonical_set_has_28_fields():
    assert len(CANONICAL_PROFILE_FIELDS) == 28  # OC-29


def test_set_field_writes_value_and_audit_row(repo):
    repo.set_field("full_name", "Jane Placeholder", source="user_edit")
    assert repo.get_fields() == {"full_name": "Jane Placeholder"}
    writes = repo.list_writes()
    assert len(writes) == 1
    assert (writes[0].field, writes[0].old_value, writes[0].new_value, writes[0].source) == (
        "full_name", None, "Jane Placeholder", "user_edit")


def test_overwrite_records_old_value(repo):
    repo.set_field("location", "Milan", source="user_edit")
    repo.set_field("location", "Berlin", source="user_edit")
    assert repo.get_fields()["location"] == "Berlin"
    last = repo.list_writes()[-1]
    assert (last.old_value, last.new_value) == ("Milan", "Berlin")


def test_unknown_field_is_rejected_with_no_write(repo):
    with pytest.raises(UnknownProfileFieldError, match="unknown profile field 'favourite_color'"):
        repo.set_field("favourite_color", "blue", source="user_edit")
    assert repo.get_fields() == {}
    assert repo.list_writes() == []


def test_resolution_source_is_reserved_not_silent(repo):
    """The seam exists (schema allows source='resolution') but writes through it
    are refused until the Resolution protocol can prove a confirmed GLOBAL scope."""
    with pytest.raises(NotImplementedError, match="Resolution protocol"):
        repo.set_field("location", "Dublin", source="resolution", resolution_id="res_1")
    assert repo.get_fields() == {}


def test_email_shape_is_validated(repo):
    with pytest.raises(InvalidProfileValueError, match="does not look like an email address"):
        repo.set_field("email", "garbage", source="user_edit")
    assert repo.get_fields() == {}
    repo.set_field("email", "jane@example.com", source="user_edit")  # valid passes
    assert repo.get_fields()["email"] == "jane@example.com"


def test_url_fields_are_shape_validated(repo):
    with pytest.raises(InvalidProfileValueError, match="does not look like a URL"):
        repo.set_field("linkedin_url", "not a url", source="user_edit")
    with pytest.raises(InvalidProfileValueError, match="does not look like a URL"):
        repo.set_field("website_url", "http://", source="user_edit")  # scheme, no host
    repo.set_field("linkedin_url", "https://linkedin.com/in/jane", source="user_edit")
    repo.set_field("github_url", "github.com/jane", source="user_edit")  # scheme optional


@pytest.mark.parametrize("url", [
    "https://example.com?source=profile",   # query string
    "https://example.com#about",            # fragment
    "https://example.com:8443",             # port
    "HTTPS://example.com",                  # uppercase scheme
])
def test_url_validation_accepts_real_url_shapes(repo, url):
    repo.set_field("portfolio_url", url, source="user_edit")
    assert repo.get_fields()["portfolio_url"] == url


def test_scheme_less_urls_normalize_to_https_at_the_seam(repo):
    """Drive finding: github.com/x stored verbatim was not directly usable;
    the seam stores https://github.com/x and audits the normalized value."""
    repo.set_field("github_url", "github.com/x", source="user_edit")
    assert repo.get_fields()["github_url"] == "https://github.com/x"
    assert repo.list_writes()[-1].new_value == "https://github.com/x"
    # A URL that already carries a scheme is untouched.
    repo.set_field("website_url", "http://example.com", source="user_edit")
    assert repo.get_fields()["website_url"] == "http://example.com"


def test_free_text_fields_stay_free(repo):
    repo.set_field("notice_period", "3 months, negotiable!", source="user_edit")
    assert repo.get_fields()["notice_period"] == "3 months, negotiable!"


def test_unknown_source_is_rejected(repo):
    with pytest.raises(ValueError, match="unknown profile write source"):
        repo.set_field("location", "Rome", source="matcher")
