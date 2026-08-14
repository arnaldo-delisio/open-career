"""`open-career experience add|list|edit` and `capability add|list`: the
post-onboarding editing surface writes exactly what the interview writes, a
blank end date stores the ongoing null, retraction sets status instead of
deleting (OC-31), display order continues after the existing maximum, and every
flow drives one line at a time over the session transport (OC-36)."""

import json
import sqlite3
from dataclasses import replace
import threading
import time

import pytest

from adapters.storage.local import LocalStorageAdapter
from adapters.storage.migrations import migrate
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteCareerFactRepository,
    SqliteEvidenceRepository,
    SqliteExperienceRepository,
)
from apps.cli.onboarding import run_onboarding
from apps.cli.stories import run_stories
from apps.cli.session import FileTransport
from apps.cli.state import (
    run_capability_add,
    run_capability_list,
    run_experience_add,
    run_experience_edit,
    run_experience_list,
)
from domain.edges import is_generation_eligible
from domain.entities import Capability, Experience
from domain.ids import new_id
from domain.traversal import EvidenceTraversal, evidence_depth


def _scripted(answers):
    remaining = list(answers)
    return lambda _prompt: remaining.pop(0)


def _instance(tmp_path):
    migrate(tmp_path / "open-career.sqlite3")
    conn = sqlite3.connect(tmp_path / "open-career.sqlite3")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _edge_shape(edge):
    """Every attribute of a persisted edge except the ids, the timestamp and
    the provenance: identity and creation time cannot match across two writes,
    and provenance is the one intended difference between the paths, asserted
    explicitly where it matters."""
    return (edge.source_type, edge.edge_type, edge.target_type, edge.claim_kind,
            edge.confidence, edge.derived_from_fact_id, edge.created_by,
            edge.user_verified, edge.superseded_at)


def _fact_shape(fact):
    """Every attribute of a persisted fact except the ids and the timestamps,
    with the verification stamp reduced to whether it was set."""
    return (fact.fact_type, fact.statement, fact.source, fact.user_approved,
            fact.status, fact.confidence, fact.source_location,
            fact.experience_id, bool(fact.verified_at))


# One experience, one achievement fact, then finish.
_ADD_ONE = ["role", "Founding Engineer", "Acme", "2024-01", "",
            "Shipped the billing pipeline", "achievement", ""]


def test_experience_add_writes_the_rows_and_edges_the_interview_writes(tmp_path):
    conn = _instance(tmp_path)
    says = []
    try:
        run_experience_add(conn, _scripted(_ADD_ONE), says.append)
        experiences = SqliteExperienceRepository(conn).list_all()
        facts = SqliteCareerFactRepository(conn).list_all()
        evidence = SqliteEvidenceRepository(conn).list_all()
        edges = SqliteCareerEdgeRepository(conn).list_all()
    finally:
        conn.close()

    assert len(experiences) == 1
    experience = experiences[0]
    assert (experience.kind, experience.title, experience.org) == (
        "role", "Founding Engineer", "Acme")
    assert experience.start_date == "2024-01"
    assert len(facts) == 1
    fact = facts[0]
    # The interview's fact shape: user-stated, approved on arrival, active, and
    # attached to its experience with a verification stamp.
    assert _fact_shape(fact) == (
        "achievement", "Shipped the billing pipeline", "interview", 1, "active",
        None, None, experience.id, True)
    assert len(evidence) == 1
    assert evidence[0].evidence_type == "user_statement"
    assert evidence[0].notes is None  # not a story: the story marker is notes
    assert len(edges) == 1
    assert _edge_shape(edges[0]) == (
        "evidence", "PROVES", "career_fact", "fact", None, None, "user", 1, None)
    assert edges[0].provenance == "experience:add"
    assert (edges[0].source_id, edges[0].target_id) == (evidence[0].id, fact.id)
    assert is_generation_eligible(edges[0])


def _experience_fact_write(conn, statement):
    """The complete persisted write behind one experience-attached fact: the
    fact row, the evidence row backing it, and every attribute of its PROVES
    edge, reduced to what two independent writes can be expected to share."""
    facts = [f for f in SqliteCareerFactRepository(conn).list_all()
             if f.statement == statement]
    assert len(facts) == 1
    fact = facts[0]
    evidence = {e.id: e for e in SqliteEvidenceRepository(conn).list_all()}
    proves = [e for e in SqliteCareerEdgeRepository(conn).list_all()
              if e.edge_type == "PROVES" and e.target_id == fact.id]
    assert len(proves) == 1
    source = evidence[proves[0].source_id]
    return {
        # The statement is the caller's own words, so it is normalized out and
        # the rest of the fact row compared attribute by attribute.
        "fact": _fact_shape(replace(fact, statement="<statement>")),
        "evidence": (source.evidence_type, source.locator, source.content_hash,
                     source.notes, source.review_completed_at),
        "proves": _edge_shape(proves[0]),
        "provenance": proves[0].provenance,
    }


def test_experience_add_writes_the_fact_the_interview_path_writes(tmp_path):
    """An experience-added fact against one written by an actual interview
    flow (the story bank's capability deepening), compared attribute by
    attribute, with provenance the one intended difference."""
    conn = _instance(tmp_path)
    storage = LocalStorageAdapter(tmp_path)
    SqliteCapabilityRepository(conn).add(
        Capability(id=new_id("cap"), name="python backend", strength="unrated"))
    try:
        run_experience_add(conn, _scripted(_ADD_ONE), lambda _line: None)
        # Cluster 2 attaches its fact to the same experience the command made,
        # so both writes are the same kind of write.
        run_stories(conn, storage,
                    ask=_scripted(["2", "1", "Ran the checkout services", "", "n"]),
                    say=lambda _line: None)
        from_command = _experience_fact_write(conn, "Shipped the billing pipeline")
        from_interview = _experience_fact_write(conn, "Ran the checkout services")
    finally:
        conn.close()

    assert from_command["provenance"] == "experience:add"
    assert from_interview["provenance"] == "stories:capability-deepening"
    assert ({k: v for k, v in from_command.items() if k != "provenance"}
            == {k: v for k, v in from_interview.items() if k != "provenance"})


def test_blank_end_date_stores_the_ongoing_null_and_renders_present(tmp_path):
    conn = _instance(tmp_path)
    says = []
    try:
        run_experience_add(conn, _scripted(_ADD_ONE), says.append)
        run_experience_list(conn, says.append)
        experience = SqliteExperienceRepository(conn).list_all()[0]
    finally:
        conn.close()
    assert experience.end_date is None
    assert "(2024-01 - Present), 1 facts" in "\n".join(says)


def test_display_order_continues_after_the_current_maximum(tmp_path):
    conn = _instance(tmp_path)
    repo = SqliteExperienceRepository(conn)
    repo.add(Experience(id=new_id("exp"), kind="role", title="Earlier",
                        org="Old", display_order=4))
    try:
        run_experience_add(conn, _scripted(_ADD_ONE), lambda _line: None)
        added = [e for e in repo.list_all() if e.title == "Founding Engineer"][0]
    finally:
        conn.close()
    assert added.display_order == 5


def test_edit_changes_the_mutable_fields_and_keeps_the_summary(tmp_path):
    conn = _instance(tmp_path)
    repo = SqliteExperienceRepository(conn)
    repo.add(Experience(id="exp-1", kind="project", title="Old title", org="Old org",
                        start_date="2020-01", end_date="2021-06",
                        summary="extractor prose", display_order=0))
    answers = ["New title", "New org", "2020-03", "present", "done"]
    try:
        run_experience_edit(conn, "exp-1", _scripted(answers), lambda _line: None)
        experience = repo.get("exp-1")
    finally:
        conn.close()
    assert (experience.title, experience.org) == ("New title", "New org")
    assert experience.start_date == "2020-03"
    assert experience.end_date is None  # 'present' is the ongoing null
    assert experience.summary == "extractor prose"  # not editable here (OC-41)


def test_edit_clears_nullable_fields_with_the_clear_sentinel(tmp_path):
    """Blank keeps a value, so every nullable field needs an explicit clear:
    without one, an org (or a date) stored wrongly could never go back to the
    absent state `experience add` allows from the start."""
    conn = _instance(tmp_path)
    repo = SqliteExperienceRepository(conn)
    repo.add(Experience(id="exp-1", kind="role", title="Role", org="Wrong Org",
                        start_date="2020-01", end_date="2021-06", display_order=0))
    says = []
    try:
        run_experience_edit(conn, "exp-1", _scripted(["", "-", "-", "-", "done"]),
                            says.append)
        experience = repo.get("exp-1")
    finally:
        conn.close()
    assert experience.title == "Role"  # blank kept it
    assert experience.org is None
    assert experience.start_date is None
    assert experience.end_date is None  # cleared end date means ongoing (OC-41)
    prompts_and_lines = "\n".join(says)
    assert "clears it" not in prompts_and_lines  # the hint rides on the prompts


def test_the_edit_prompts_state_how_to_clear_each_nullable_field(tmp_path):
    conn = _instance(tmp_path)
    SqliteExperienceRepository(conn).add(
        Experience(id="exp-1", kind="role", title="Role", org="Acme",
                   start_date="2020-01", display_order=0))
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return {0: "", 1: "", 2: "", 3: ""}.get(len(prompts) - 1, "done")

    try:
        run_experience_edit(conn, "exp-1", ask, lambda _line: None)
    finally:
        conn.close()
    assert "(blank keeps it, '-' clears it)" in prompts[1]        # org
    assert "(blank keeps it, '-' clears it)" in prompts[2]        # start date
    assert "'-' or 'present' means ongoing" in prompts[3]         # end date


def test_edit_adds_a_fact_and_retraction_sets_status_without_deleting(tmp_path):
    conn = _instance(tmp_path)
    SqliteExperienceRepository(conn).add(
        Experience(id="exp-1", kind="role", title="Role", org="Acme", display_order=0))
    facts_repo = SqliteCareerFactRepository(conn)
    edges_repo = SqliteCareerEdgeRepository(conn)
    answers = ["", "", "", "",                       # keep every container field
               "add", "Ran the migration", "achievement", "",
               "retract", "1",
               "done"]
    says = []
    try:
        run_experience_edit(conn, "exp-1", _scripted(answers), says.append)
        facts = facts_repo.list_all()
        edges = edges_repo.list_all()
    finally:
        conn.close()
    assert len(facts) == 1  # the row survives its retraction
    assert facts[0].statement == "Ran the migration"
    assert facts[0].status == "retracted"
    assert len(edges) == 1  # its PROVES edge is kept too, never deleted
    assert "the row is kept, not deleted" in "\n".join(says)


def _capability_write(conn, capability):
    """The complete persisted write behind one capability: the capability row,
    its self-assessment fact, the evidence row backing it, and both edges, each
    reduced to the attributes two independent writes can be expected to share.
    Provenance is deliberately excluded here and asserted per path by the
    caller, which is the only difference the two paths are allowed to have."""
    facts = {f.id: f for f in SqliteCareerFactRepository(conn).list_all()}
    evidence = {e.id: e for e in SqliteEvidenceRepository(conn).list_all()}
    edges = SqliteCareerEdgeRepository(conn).list_all()
    supports = [e for e in edges
                if e.edge_type == "SUPPORTS" and e.target_id == capability.id]
    assert len(supports) == 1
    source = evidence[supports[0].source_id]
    proves = [e for e in edges if e.edge_type == "PROVES" and e.source_id == source.id
              and facts[e.target_id].statement.startswith("Self-assessed capability:")]
    assert len(proves) == 1
    fact = facts[proves[0].target_id]
    return {
        "capability": (capability.strength, capability.description,
                       capability.last_assessed_at),
        # The capability's own name is the one word the two statements cannot
        # share, so it is normalized out here and asserted separately.
        "fact": _fact_shape(replace(
            fact, statement="Self-assessed capability: <name>")),
        "fact_name": fact.statement.removeprefix("Self-assessed capability: "),
        "evidence": (source.evidence_type, source.locator, source.content_hash,
                     source.notes, source.review_completed_at),
        "evidence_title_prefix": source.title.rsplit(" ", 1)[0],
        "supports": _edge_shape(supports[0]),
        "proves": _edge_shape(proves[0]),
        "provenances": (supports[0].provenance, proves[0].provenance),
    }


def test_capability_add_mints_the_chain_onboarding_mints(tmp_path):
    """The complete persisted write of both paths, compared attribute by
    attribute, with provenance the one intended difference (the interview and
    the command genuinely are different origins, and the graph says so)."""
    conn = _instance(tmp_path)
    storage = LocalStorageAdapter(tmp_path)
    try:
        run_onboarding(conn, storage, None, None,
                       ask=_scripted(["from onboarding", "", "", "", "", "", ""]),
                       say=lambda _line: None)
        run_capability_add(conn, _scripted(["from the command"]), lambda _line: None)

        capabilities = {c.name: c for c in SqliteCapabilityRepository(conn).list_all()}
        writes = {name: _capability_write(conn, capability)
                  for name, capability in capabilities.items()}
        traversal = EvidenceTraversal(
            SqliteCareerEdgeRepository(conn), SqliteEvidenceRepository(conn),
            SqliteCareerFactRepository(conn), SqliteExperienceRepository(conn))
        chains = {name: traversal.evidence_for_capability(capability.id)
                  for name, capability in capabilities.items()}
        depths = {name: evidence_depth(chain) for name, chain in chains.items()}
    finally:
        conn.close()

    from_command = writes["from the command"]
    from_onboarding = writes["from onboarding"]
    assert capabilities["from the command"].strength == "unrated"  # OC-40
    # Provenance, asserted per path rather than skipped: it is what the two
    # writes are supposed to differ in.
    assert from_command["provenances"] == ("capability:add", "capability:add")
    assert from_onboarding["provenances"] == ("onboarding:interview",
                                              "onboarding:interview")
    # Everything else about the write is identical, name and title aside.
    comparable = {k: v for k, v in from_command.items()
                  if k not in ("provenances", "fact_name", "evidence_title_prefix")}
    assert comparable == {k: v for k, v in from_onboarding.items()
                          if k not in ("provenances", "fact_name",
                                       "evidence_title_prefix")}
    assert from_command["fact_name"] == "from the command"
    assert from_onboarding["fact_name"] == "from onboarding"
    # Eligible chain: SUPPORTS to evidence, PROVES to the approved fact.
    assert len(chains["from the command"]) == 1
    assert [fc.fact.statement for fc in chains["from the command"][0].facts] == [
        "Self-assessed capability: from the command"]
    assert depths["from the command"] == depths["from onboarding"]


def test_a_failed_write_inside_the_chain_leaves_no_capability_at_all(tmp_path,
                                                                     monkeypatch):
    """The chain is one transaction: a capability is packageable the moment it
    exists, so a failure part-way through must leave nothing behind rather than
    a capability with no evidence chain."""
    conn = _instance(tmp_path)
    real_add = SqliteCareerEdgeRepository.add

    def failing_add(self, edge):
        if edge.edge_type == "SUPPORTS":
            raise sqlite3.OperationalError("injected failure")
        return real_add(self, edge)

    monkeypatch.setattr(SqliteCareerEdgeRepository, "add", failing_add)
    try:
        with pytest.raises(sqlite3.OperationalError):
            run_capability_add(conn, _scripted(["python backend"]), lambda _line: None)
        monkeypatch.undo()
        capabilities = SqliteCapabilityRepository(conn).list_all()
        facts = SqliteCareerFactRepository(conn).list_all()
        evidence = SqliteEvidenceRepository(conn).list_all()
        edges = SqliteCareerEdgeRepository(conn).list_all()
    finally:
        conn.close()
    assert capabilities == []  # never visible without its chain
    assert (facts, evidence, edges) == ([], [], [])


def test_a_failed_proves_edge_leaves_no_fact_row(tmp_path, monkeypatch):
    """The stated-fact writer is a transaction of its own, not only when it
    nests inside the capability chain: a fact whose PROVES edge fails is not
    an approved fact with nothing backing it, it is nothing at all."""
    conn = _instance(tmp_path)
    SqliteExperienceRepository(conn).add(
        Experience(id="exp-1", kind="role", title="Role", org="Acme", display_order=0))

    def failing_add(self, edge):
        raise sqlite3.OperationalError("injected failure")

    monkeypatch.setattr(SqliteCareerEdgeRepository, "add", failing_add)
    answers = ["", "", "", "", "add", "Ran the migration", "achievement"]
    try:
        with pytest.raises(sqlite3.OperationalError):
            run_experience_edit(conn, "exp-1", _scripted(answers), lambda _line: None)
        monkeypatch.undo()
        facts = SqliteCareerFactRepository(conn).list_all()
        evidence = SqliteEvidenceRepository(conn).list_all()
    finally:
        conn.close()
    assert (facts, evidence) == ([], [])


def test_capability_list_reports_stored_strength_and_evidence_depth(tmp_path):
    conn = _instance(tmp_path)
    says = []
    try:
        run_capability_add(conn, _scripted(["python backend"]), lambda _line: None)
        run_capability_list(conn, says.append)
    finally:
        conn.close()
    assert ("python backend (unrated), 1 supporting facts, 0 stories"
            in "\n".join(says))


def test_capability_add_refuses_a_duplicate_name_as_onboarding_does(tmp_path):
    conn = _instance(tmp_path)
    says = []
    try:
        run_capability_add(conn, _scripted(["python backend"]), says.append)
        with pytest.raises(SystemExit):
            run_capability_add(conn, _scripted(["python backend"]), says.append)
        capabilities = SqliteCapabilityRepository(conn).list_all()
        facts = SqliteCareerFactRepository(conn).list_all()
    finally:
        conn.close()
    assert len(capabilities) == 1
    assert len(facts) == 1  # the refused run persisted nothing
    assert "already exists" in "\n".join(says)


def _drive_over_transport(transport_dir, target, answers):
    """Run one flow against the file transport, answering its questions one
    line at a time exactly as `session answer` does."""
    transport = FileTransport(transport_dir)
    thread = threading.Thread(
        target=lambda: target(transport.ask, transport.say), daemon=True)
    thread.start()
    remaining = list(answers)
    deadline = time.monotonic() + 15.0
    while remaining and time.monotonic() < deadline:
        pending_path = transport_dir / "pending.json"
        if not pending_path.exists():
            time.sleep(0.05)
            continue
        pending = json.loads(pending_path.read_text())
        (transport_dir / f"answer-{pending['seq']}.json").write_text(
            json.dumps({"text": remaining.pop(0), "session": None}))
        while (transport_dir / f"answer-{pending['seq']}.json").exists() \
                and time.monotonic() < deadline:
            time.sleep(0.05)
    thread.join(timeout=15.0)
    assert not thread.is_alive()


def test_the_flows_drive_one_line_at_a_time_over_the_session_transport(tmp_path):
    # The flow runs in the transport's thread, so it opens its own connection
    # there (sqlite objects belong to the thread that created them).
    def flow(runner, args):
        def target(ask, say):
            conn = sqlite3.connect(tmp_path / "open-career.sqlite3")
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                runner(conn, *args, ask, say)
            finally:
                conn.close()
        return target

    migrate(tmp_path / "open-career.sqlite3")
    transport_dir = tmp_path / "session"
    transport_dir.mkdir()
    _drive_over_transport(transport_dir, flow(run_experience_add, ()), _ADD_ONE)
    _drive_over_transport(transport_dir, flow(run_capability_add, ()), ["shipping"])

    conn = sqlite3.connect(tmp_path / "open-career.sqlite3")
    try:
        experiences = SqliteExperienceRepository(conn).list_all()
        capabilities = SqliteCapabilityRepository(conn).list_all()
        facts = SqliteCareerFactRepository(conn).list_all()
    finally:
        conn.close()
    assert [e.title for e in experiences] == ["Founding Engineer"]
    assert [c.name for c in capabilities] == ["shipping"]
    assert len(facts) == 2
    transcript = (transport_dir / "transcript.log").read_text()
    assert "Fact (blank to finish): " in transcript
