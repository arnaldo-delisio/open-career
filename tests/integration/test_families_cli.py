"""families init|list|add|edit|pause flow with a scripted user and a fake
proposal model: nothing persists unconfirmed, confirmed rows land with
allocations and user-verified TARGETS edges."""

import json
import sqlite3

import pytest

from adapters.storage.family_strategy import FamilyStrategyService
from adapters.storage.migrations import migrate
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteRoleFamilyRepository,
)
from apps.cli.families import (
    run_families_add,
    run_families_edit,
    run_families_init,
    run_families_list,
    run_families_pause,
)
from domain.entities import Capability
from domain.ports import ModelAdapter

PROPOSALS = json.dumps([
    {"name": "Forward Deployed Engineer", "rationale": "hands-on customer work",
     "target_seniority": "senior", "adjacent_titles": ["Solutions Engineer"],
     "search_vocabulary": ["forward deployed"], "target_capability_names": ["Python"]},
    {"name": "Data Engineer", "rationale": "pipelines", "target_seniority": None,
     "adjacent_titles": [], "search_vocabulary": [], "target_capability_names": []},
])


class FakeModel(ModelAdapter):
    def complete(self, prompt: str) -> str:
        return PROPOSALS


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    SqliteCapabilityRepository(conn).add(Capability(id="cap_1", name="Python", strength="strong"))
    yield conn
    conn.close()


def _script(answers):
    it = iter(answers)
    return lambda _prompt: next(it)


def test_init_confirms_families_mints_strategy_and_targets(conn):
    answers = _script([
        "c",        # confirm FDE
        "r",        # reject Data Engineer
        "5",        # emphasis FDE
        "Land a hands-on role",  # objective
        "y",        # target Python
    ])
    said = []
    run_families_init(conn, FakeModel(), answers, said.append)
    families = SqliteRoleFamilyRepository(conn).list_all()
    assert [f.name for f in families] == ["Forward Deployed Engineer"]  # rejected one never persisted
    objective, allocations = FamilyStrategyService(conn).current_allocations()
    assert objective == "Land a hands-on role"
    assert allocations == {families[0].id: 5}
    targets = SqliteCareerEdgeRepository(conn).active_edges_from(
        "role_family", families[0].id, "TARGETS")
    assert len(targets) == 1
    assert targets[0].created_by == "user" and targets[0].user_verified == 1
    assert targets[0].claim_kind == "fact"


def test_init_with_nothing_confirmed_persists_nothing(conn):
    run_families_init(conn, FakeModel(), _script(["r", "r"]), lambda _s: None)
    assert SqliteRoleFamilyRepository(conn).list_all() == []
    assert FamilyStrategyService(conn).current_allocations() == (None, {})


def test_list_add_edit_pause_roundtrip(conn):
    run_families_init(conn, FakeModel(),
                      _script(["c", "r", "4", "obj", "y"]), lambda _s: None)
    family = SqliteRoleFamilyRepository(conn).list_all()[0]

    run_families_add(conn, _script(["Platform Engineer", "infra", "", "2", ""]),
                     lambda _s: None)
    _obj, allocations = FamilyStrategyService(conn).current_allocations()
    assert len(allocations) == 2 and allocations[family.id] == 4

    run_families_edit(conn, family.name, _script(["1", ""]), lambda _s: None)
    _obj, allocations = FamilyStrategyService(conn).current_allocations()
    assert allocations[family.id] == 1

    run_families_pause(conn, family.name, lambda _s: None)
    _obj, allocations = FamilyStrategyService(conn).current_allocations()
    assert family.id not in allocations

    said = []
    run_families_list(conn, True, said.append)
    payload = json.loads(said[0])
    assert {f["name"] for f in payload["families"]} == {
        "Forward Deployed Engineer", "Platform Engineer"}
    statuses = {f["name"]: f["status"] for f in payload["families"]}
    assert statuses["Forward Deployed Engineer"] == "paused"
