"""Atomic family/strategy copy-forward tests (spec:
decisions/package-generation-design.md, "Role-family onboarding step")."""

import sqlite3

import pytest

from adapters.storage.family_strategy import FamilyStrategyService, StrategyError
from adapters.storage.migrations import migrate
from adapters.storage.sqlite_entities import SqliteStrategyRepository
from domain.entities import RoleFamily


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


def _family(fid="rf_1", name="FDE"):
    return RoleFamily(id=fid, name=name, rationale="r")


def test_mint_initial_creates_families_and_version_1(conn):
    service = FamilyStrategyService(conn)
    version = service.mint_initial([_family(), _family("rf_2", "Platform")],
                                   {"rf_1": 5, "rf_2": 2}, "Land an FDE role")
    assert version == 1
    objective, allocations = service.current_allocations()
    assert objective == "Land an FDE role"
    assert allocations == {"rf_1": 5, "rf_2": 2}
    current = SqliteStrategyRepository(conn).current()
    assert current.created_by == "user" and current.user_approved == 1


def test_objective_is_required(conn):
    with pytest.raises(StrategyError, match="objective"):
        FamilyStrategyService(conn).mint_initial([_family()], {"rf_1": 3}, "  ")


def test_allocation_range_enforced(conn):
    with pytest.raises(StrategyError, match="1 to 5"):
        FamilyStrategyService(conn).mint_initial([_family()], {"rf_1": 6}, "obj")


def test_emphasis_edit_mints_complete_new_version(conn):
    service = FamilyStrategyService(conn)
    service.mint_initial([_family(), _family("rf_2", "Platform")],
                         {"rf_1": 5, "rf_2": 2}, "obj")
    version = service.set_emphasis("rf_1", 3)
    assert version == 2
    objective, allocations = service.current_allocations()
    assert objective == "obj"                      # copied forward
    assert allocations == {"rf_1": 3, "rf_2": 2}   # complete set, edit applied
    assert len(SqliteStrategyRepository(conn).list_versions()) == 2  # append-only


def test_pause_removes_allocation_copy_forward(conn):
    service = FamilyStrategyService(conn)
    service.mint_initial([_family(), _family("rf_2", "Platform")],
                         {"rf_1": 5, "rf_2": 2}, "obj")
    service.set_status("rf_1", "paused")
    _objective, allocations = service.current_allocations()
    assert allocations == {"rf_2": 2}
    status = conn.execute("SELECT status FROM role_families WHERE id = 'rf_1'").fetchone()[0]
    assert status == "paused"


def test_add_family_copies_forward(conn):
    service = FamilyStrategyService(conn)
    service.mint_initial([_family()], {"rf_1": 5}, "obj")
    version = service.add_family(_family("rf_3", "Consultant"), 2)
    assert version == 2
    _objective, allocations = service.current_allocations()
    assert allocations == {"rf_1": 5, "rf_3": 2}


def test_change_without_current_version_is_rejected(conn):
    with pytest.raises(StrategyError, match="families init"):
        FamilyStrategyService(conn).set_emphasis("rf_1", 3)


def test_failed_mint_is_atomic(conn):
    """A failure inside the mint leaves no family row and no version."""
    service = FamilyStrategyService(conn)
    with pytest.raises(StrategyError):
        service.mint_initial([_family(), _family("rf_2", "Platform")],
                             {"rf_1": 5, "rf_2": 99}, "obj")  # invalid allocation
    assert conn.execute("SELECT COUNT(*) FROM role_families").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM strategy_versions").fetchone()[0] == 0


def test_nothing_mutates_an_existing_version(conn):
    service = FamilyStrategyService(conn)
    service.mint_initial([_family()], {"rf_1": 5}, "obj")
    service.set_emphasis("rf_1", 1)
    rows = conn.execute(
        "SELECT sv.version, a.allocation FROM strategy_role_family_allocations a"
        " JOIN strategy_versions sv ON sv.id = a.strategy_version_id"
        " ORDER BY sv.version").fetchall()
    assert rows == [(1, 5), (2, 1)]  # version 1 untouched
