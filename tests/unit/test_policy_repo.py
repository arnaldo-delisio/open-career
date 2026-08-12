"""The policy write seam: closed key set, single audited mutation path,
Resolution source reserved (migration 0004, mirroring the profile seam)."""

import json
import sqlite3

import pytest

from adapters.storage.migrations import migrate
from adapters.storage.sqlite_policies import SqliteUserPolicyRepository
from domain.policies import InvalidPolicyValueError, UnknownPolicyKeyError


@pytest.fixture
def repo(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    yield SqliteUserPolicyRepository(conn)
    conn.close()


def test_set_policy_writes_value_and_audit_row(repo):
    repo.set_policy("work_track", "ic", source="user_edit")
    assert repo.get_policies() == {"work_track": "ic"}
    writes = repo.list_writes()
    assert len(writes) == 1
    assert (writes[0].key, writes[0].old_value, writes[0].new_value, writes[0].source) == (
        "work_track", None, json.dumps("ic"), "user_edit")


def test_overwrite_records_old_value_json(repo):
    floor_a = {"amount": 60000, "currency": "EUR", "period": "annual"}
    floor_b = {"amount": 70000, "currency": "EUR", "period": "annual"}
    repo.set_policy("compensation_floor", floor_a, source="user_edit")
    repo.set_policy("compensation_floor", floor_b, source="user_edit")
    assert repo.get_policies()["compensation_floor"] == floor_b
    last = repo.list_writes()[-1]
    assert json.loads(last.old_value) == floor_a
    assert json.loads(last.new_value) == floor_b


def test_clearing_a_policy_is_an_audited_none(repo):
    repo.set_policy("eeo_stance", "always_decline", source="user_edit")
    repo.set_policy("eeo_stance", None, source="user_edit")
    assert repo.get_policies()["eeo_stance"] is None
    assert repo.list_writes()[-1].new_value == "null"


def test_unknown_key_is_rejected_with_no_write(repo):
    with pytest.raises(UnknownPolicyKeyError):
        repo.set_policy("favourite_color", "blue", source="user_edit")
    assert repo.get_policies() == {}
    assert repo.list_writes() == []


def test_invalid_shape_is_rejected_with_no_write(repo):
    with pytest.raises(InvalidPolicyValueError, match="positive integer"):
        repo.set_policy("compensation_floor",
                        {"amount": 1.5, "currency": "EUR", "period": "annual"},
                        source="user_edit")
    assert repo.get_policies() == {}
    assert repo.list_writes() == []


def test_resolution_source_is_reserved_not_silent(repo):
    with pytest.raises(NotImplementedError, match="Resolution protocol"):
        repo.set_policy("work_track", "ic", source="resolution", resolution_id="res_1")
    assert repo.get_policies() == {}


def test_unknown_source_is_rejected(repo):
    with pytest.raises(ValueError, match="unknown policy write source"):
        repo.set_policy("work_track", "ic", source="matcher")
