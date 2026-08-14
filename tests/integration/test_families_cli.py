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
    SqliteCareerFactRepository,
    SqliteEvidenceRepository,
    SqliteRoleFamilyRepository,
)
from apps.cli.families import (
    run_families_add,
    run_families_edit,
    run_families_init,
    run_families_list,
    run_families_pause,
)
from domain.edges import CareerEdge
from domain.entities import Capability, CareerFact, Evidence
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


def test_add_collects_all_input_before_persisting_targets_included(conn):
    run_families_init(conn, FakeModel(), _script(["c", "r", "4", "obj", "y"]),
                      lambda _s: None)
    run_families_add(conn, _script(["Platform Engineer", "infra", "", "2",
                                    "Python", "Nope Capability", ""]),
                     lambda _s: None)
    family = [f for f in SqliteRoleFamilyRepository(conn).list_all()
              if f.name == "Platform Engineer"][0]
    targets = SqliteCareerEdgeRepository(conn).active_edges_from(
        "role_family", family.id, "TARGETS")
    assert [e.target_id for e in targets] == ["cap_1"]  # unknown name skipped
    assert targets[0].created_by == "user" and targets[0].user_verified == 1


def test_add_interrupted_mid_dialog_persists_nothing(conn):
    """Drive regression: EOF mid-dialog used to crash with a raw traceback
    AFTER the family row landed, so the retry hit a raw UNIQUE error. Nothing
    may persist until every input is collected."""
    run_families_init(conn, FakeModel(), _script(["c", "r", "4", "obj", "y"]),
                      lambda _s: None)
    versions_before = conn.execute("SELECT COUNT(*) FROM strategy_versions").fetchone()[0]

    def eof_after_name(_prompt):
        if not eof_after_name.asked:
            eof_after_name.asked = True
            return "Engineering Manager"
        raise EOFError("EOF when reading a line")
    eof_after_name.asked = False

    said = []
    with pytest.raises(SystemExit) as excinfo:  # scripts see the abort as nonzero
        run_families_add(conn, eof_after_name, said.append)
    assert excinfo.value.code == 1
    assert any("nothing persisted" in s for s in said)
    names = {f.name for f in SqliteRoleFamilyRepository(conn).list_all()}
    assert "Engineering Manager" not in names
    assert conn.execute("SELECT COUNT(*) FROM strategy_versions").fetchone()[0] \
        == versions_before
    # The retry now succeeds instead of hitting a UNIQUE constraint.
    run_families_add(conn, _script(["Engineering Manager", "people", "", "3", ""]),
                     lambda _s: None)
    assert "Engineering Manager" in {
        f.name for f in SqliteRoleFamilyRepository(conn).list_all()}


def test_add_duplicate_name_reports_cleanly(conn):
    run_families_init(conn, FakeModel(), _script(["c", "r", "4", "obj", "y"]),
                      lambda _s: None)
    said = []
    with pytest.raises(SystemExit) as excinfo:  # refusal is a nonzero exit
        run_families_add(conn, _script(["forward deployed engineer"]), said.append)
    assert excinfo.value.code == 1
    assert any("already exists" in s for s in said)
    assert len(SqliteRoleFamilyRepository(conn).list_all()) == 1


def test_add_empty_name_aborts_nonzero(conn):
    said = []
    with pytest.raises(SystemExit) as excinfo:
        run_families_add(conn, _script([""]), said.append)
    assert excinfo.value.code == 1
    assert any("empty name" in s for s in said)


class CapturingModel(ModelAdapter):
    """Same proposals, but keeps the prompt so the payload can be inspected."""

    def __init__(self):
        self.prompts = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return PROPOSALS


def test_proposal_payload_carries_evidence_depth_not_strength(conn):
    """OC-40: the model sees what a capability rests on (approved facts and
    stories reaching it), never the user's self-rating."""
    facts = SqliteCareerFactRepository(conn)
    evidence = SqliteEvidenceRepository(conn)
    edges = SqliteCareerEdgeRepository(conn)
    evidence.add(Evidence(id="ev_1", evidence_type="cv", title="cv.txt"))
    facts.add(CareerFact(id="fact_1", fact_type="achievement",
                         statement="Built the order service", source="cv",
                         user_approved=1, verified_at="2026-08-13T00:00:00Z"))

    def edge(eid, edge_type, target_type, target_id):
        edges.add(CareerEdge(
            id=eid, source_type="evidence", source_id="ev_1", edge_type=edge_type,
            target_type=target_type, target_id=target_id, claim_kind="fact",
            provenance="test", created_by="user", user_verified=1))

    edge("edge_p", "PROVES", "career_fact", "fact_1")
    edge("edge_s", "SUPPORTS", "capability", "cap_1")

    model = CapturingModel()
    run_families_init(conn, model, _script(["r", "r"]), lambda _: None)

    prompt = model.prompts[0]
    assert '"supporting_facts": 1' in prompt and '"supporting_stories": 0' in prompt
    assert "strength" not in prompt
