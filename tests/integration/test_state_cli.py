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
    SqliteRoleFamilyRepository,
)
from apps.cli.onboarding import run_onboarding
from apps.cli.stories import run_stories
from apps.cli.session import FileTransport
from apps.cli.interview import write_capability_link, write_stated_fact
from apps.cli.state import (
    _ask_capability_choices,
    run_capability_add,
    run_capability_list,
    run_experience_add,
    run_experience_edit,
    run_experience_list,
)
from domain.edges import CareerEdge, is_generation_eligible
from domain.entities import Capability, Evidence, Experience, RoleFamily
from domain.ids import new_id
from domain.selection import FamilyEvidenceSelection
from domain.traversal import EvidenceTraversal, evidence_depth, is_story_evidence


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
    # The owner marker: one statement row per experience, so facts stated in a
    # later edit share the owner the capability links hang off. It is not the
    # story marker, so this is still not a story.
    assert evidence[0].notes == f"facts-for-experience:{experience.id}"
    assert not is_story_evidence(evidence[0])
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
        # The owner marker names the experience, so it cannot be shared between
        # two writes; it is normalized out here and asserted where it matters.
        "evidence": (source.evidence_type, source.locator, source.content_hash,
                     source.review_completed_at),
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
        # A capability exists here, so the add flow offers the link ask; the
        # blank keeps this comparison about the fact write alone.
        run_experience_add(conn, _scripted(_ADD_ONE + [""]), lambda _line: None)
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


# --- capability links: what makes an added experience renderable --------------

def _targeted_family(conn, capability_id):
    """A role family targeting one capability: the first hop of the package
    walk (role_family -> TARGETS -> capability -> SUPPORTS -> PROVES -> fact)."""
    family = RoleFamily(id=new_id("fam"), name="FDE", rationale="target",
                        display_order=0)
    SqliteRoleFamilyRepository(conn).add(family)
    SqliteCareerEdgeRepository(conn).add(CareerEdge(
        id=new_id("edge"), source_type="role_family", source_id=family.id,
        edge_type="TARGETS", target_type="capability", target_id=capability_id,
        claim_kind="fact", provenance="test", created_by="user", user_verified=1))
    return family


def _selection(conn, family_id):
    traversal = EvidenceTraversal(
        SqliteCareerEdgeRepository(conn), SqliteEvidenceRepository(conn),
        SqliteCareerFactRepository(conn), SqliteExperienceRepository(conn))
    return FamilyEvidenceSelection(
        SqliteCareerEdgeRepository(conn), SqliteCapabilityRepository(conn),
        traversal).select(family_id)


def test_an_added_experience_with_links_is_reached_by_the_package_walk(tmp_path):
    """The defect this fixes: facts added by `experience add` had no SUPPORTS
    edge, so the walk stopped at the capability and the experience rendered as
    a bare title and date. Asserted through the real selection, not the edge
    rows: reachability is the point."""
    conn = _instance(tmp_path)
    capability = Capability(id=new_id("cap"), name="python backend",
                            strength="unrated")
    SqliteCapabilityRepository(conn).add(capability)
    try:
        family = _targeted_family(conn, capability.id)
        run_experience_add(conn, _scripted(_ADD_ONE + ["1"]), lambda _line: None)
        report = _selection(conn, family.id)
        statements = [fc.fact.statement for s in report.selections
                      for chain in s.chains for fc in chain.facts]
        experiences = [fc.experience.title for s in report.selections
                       for chain in s.chains for fc in chain.facts if fc.experience]
    finally:
        conn.close()
    assert report.gaps == ()  # the capability is covered, not a gap
    assert statements == ["Shipped the billing pipeline"]
    assert experiences == ["Founding Engineer"]


def test_a_blank_selection_mints_no_link_edges(tmp_path):
    """Blank means none, and nothing is preselected: a blanket link from an
    experience to every capability is the meaningless edge OC-39 deleted."""
    conn = _instance(tmp_path)
    SqliteCapabilityRepository(conn).add(
        Capability(id=new_id("cap"), name="python backend", strength="unrated"))
    try:
        run_experience_add(conn, _scripted(_ADD_ONE + [""]), lambda _line: None)
        edges = SqliteCareerEdgeRepository(conn).list_all()
    finally:
        conn.close()
    assert [e.edge_type for e in edges] == ["PROVES"]
    rule = _ask_capability_choices.__doc__
    assert "Nothing is preselected" in rule and "OC-39" in rule


def test_edit_links_an_experience_that_had_none_and_unlinking_supersedes(tmp_path):
    """The repair path for an experience added before the links existed, and
    the removal that retires an edge instead of deleting it (OC-31)."""
    conn = _instance(tmp_path)
    capability = Capability(id=new_id("cap"), name="python backend",
                            strength="unrated")
    SqliteCapabilityRepository(conn).add(capability)
    says = []
    try:
        family = _targeted_family(conn, capability.id)
        run_experience_add(conn, _scripted(_ADD_ONE + [""]), lambda _line: None)
        experience = SqliteExperienceRepository(conn).list_all()[0]
        assert _selection(conn, family.id).gaps  # unreachable before the repair
        run_experience_edit(conn, experience.id,
                            _scripted(["", "", "", "",
                                       "links", "link", "1", "done"]), says.append)
        after_link = _selection(conn, family.id)
        statements = [fc.fact.statement for s in after_link.selections
                      for chain in s.chains for fc in chain.facts]
        run_experience_edit(conn, experience.id,
                            _scripted(["", "", "", "",
                                       "links", "unlink", "1", "done"]), says.append)
        after_unlink = _selection(conn, family.id)
        edges = SqliteCareerEdgeRepository(conn).list_all()
    finally:
        conn.close()
    assert statements == ["Shipped the billing pipeline"]
    assert after_unlink.gaps  # the capability is uncovered again
    retired = [e for e in edges if e.edge_type in ("SUPPORTS", "DEMONSTRATES")]
    assert len(retired) == 2  # both rows survive their removal
    assert all(e.superseded_at is not None for e in retired)
    assert "not deleted" in "\n".join(says)


def test_the_shared_writer_does_not_mint_a_second_demonstrates_edge(tmp_path):
    """Two evidence rows can support the same capability from the same
    experience; the summary edge between them is one edge, not two."""
    conn = _instance(tmp_path)
    capability = Capability(id=new_id("cap"), name="python backend",
                            strength="unrated")
    SqliteCapabilityRepository(conn).add(capability)
    SqliteExperienceRepository(conn).add(
        Experience(id="exp-1", kind="role", title="Role", org="Acme", display_order=0))
    evidence_repo = SqliteEvidenceRepository(conn)
    try:
        for number in (1, 2):
            evidence = Evidence(id=f"ev-{number}", evidence_type="user_statement",
                                title=f"Stated experience {number}")
            evidence_repo.add(evidence)
            write_capability_link(conn, capability.id, evidence.id,
                                  "experience:edit", "exp-1")
        edges = SqliteCareerEdgeRepository(conn).list_all()
    finally:
        conn.close()
    assert len([e for e in edges if e.edge_type == "SUPPORTS"]) == 2
    assert len([e for e in edges if e.edge_type == "DEMONSTRATES"]) == 1


def test_end_of_input_in_the_fact_loop_reports_what_was_saved(tmp_path):
    """EOF is not a crash: the experience and its stated facts are on disk, so
    the command says so and exits nonzero rather than printing a traceback."""
    conn = _instance(tmp_path)
    remaining = list(_ADD_ONE[:-1])  # every answer except the blank that finishes

    def ask(_prompt):
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    says = []
    try:
        with pytest.raises(SystemExit) as exit_info:
            run_experience_add(conn, ask, says.append)
        facts = SqliteCareerFactRepository(conn).list_all()
        experiences = SqliteExperienceRepository(conn).list_all()
    finally:
        conn.close()
    assert exit_info.value.code == 1
    assert len(experiences) == 1 and len(facts) == 1
    transcript = "\n".join(says)
    assert "input ended" in transcript
    assert "with 1 facts" in transcript
    assert "experience edit" in transcript  # how to add the rest


def _reachable(conn, family_id):
    return sorted(fc.fact.statement for s in _selection(conn, family_id).selections
                  for chain in s.chains for fc in chain.facts)


def test_a_fact_added_after_the_link_is_reachable_too(tmp_path):
    """The per-experience evidence row earns its keep here: a fact stated in a
    later edit lands on the row the link already hangs off, so it reaches
    packaging instead of arriving silently invisible."""
    conn = _instance(tmp_path)
    capability = Capability(id=new_id("cap"), name="python backend",
                            strength="unrated")
    SqliteCapabilityRepository(conn).add(capability)
    try:
        family = _targeted_family(conn, capability.id)
        run_experience_add(conn, _scripted(_ADD_ONE + ["1"]), lambda _line: None)
        experience = SqliteExperienceRepository(conn).list_all()[0]
        run_experience_edit(conn, experience.id,
                            _scripted(["", "", "", "",
                                       "add", "Cut latency by 40%", "metric", "",
                                       "done"]), lambda _line: None)
        reachable = _reachable(conn, family.id)
        evidence = SqliteEvidenceRepository(conn).list_all()
    finally:
        conn.close()
    assert reachable == ["Cut latency by 40%", "Shipped the billing pipeline"]
    # One owner row for both sittings, not one per session.
    assert [e.notes for e in evidence] == [f"facts-for-experience:{experience.id}"]


def test_an_experience_linked_with_no_facts_reaches_the_facts_stated_later(tmp_path):
    """Linking first and stating facts afterwards is the other order, and it
    has to end in the same reachable chain."""
    conn = _instance(tmp_path)
    capability = Capability(id=new_id("cap"), name="python backend",
                           strength="unrated")
    SqliteCapabilityRepository(conn).add(capability)
    try:
        family = _targeted_family(conn, capability.id)
        # No facts at add time, then the link, then the facts.
        run_experience_add(conn, _scripted(
            ["role", "Founding Engineer", "Acme", "2024-01", "", "", "1"]),
            lambda _line: None)
        experience = SqliteExperienceRepository(conn).list_all()[0]
        assert _reachable(conn, family.id) == []
        run_experience_edit(conn, experience.id,
                            _scripted(["", "", "", "",
                                       "add", "Shipped the billing pipeline",
                                       "achievement", "",
                                       "done"]), lambda _line: None)
        reachable = _reachable(conn, family.id)
    finally:
        conn.close()
    assert reachable == ["Shipped the billing pipeline"]


def _shared_cv_evidence(conn, first_id, second_id):
    """The live instance's shape: one CV extraction row proving facts for
    several experiences, so its SUPPORTS edge belongs to none of them."""
    evidence = Evidence(id="ev-cv", evidence_type="document", title="CV")
    SqliteEvidenceRepository(conn).add(evidence)
    for experience_id, statement in ((first_id, "Led the platform team"),
                                     (second_id, "Ran the data migration")):
        write_stated_fact(conn, lambda: evidence, statement, "achievement",
                          "onboarding:cv", experience_id)
    return evidence


def test_unlink_refuses_to_retire_support_shared_with_another_experience(tmp_path):
    """Unlink never breaks a chain it does not own: the shared row's SUPPORTS
    edge stays, the experience's own DEMONSTRATES edge goes, and the refusal is
    stated rather than guessed at."""
    conn = _instance(tmp_path)
    capability = Capability(id=new_id("cap"), name="python backend",
                           strength="unrated")
    SqliteCapabilityRepository(conn).add(capability)
    experiences_repo = SqliteExperienceRepository(conn)
    experiences_repo.add(Experience(id="exp-1", kind="role", title="First",
                                    org="Acme", display_order=0))
    experiences_repo.add(Experience(id="exp-2", kind="role", title="Second",
                                    org="Beta", display_order=1))
    says = []
    try:
        family = _targeted_family(conn, capability.id)
        evidence = _shared_cv_evidence(conn, "exp-1", "exp-2")
        # The CV row already supports the capability (the onboarding walk's
        # own link), so both experiences' facts are reachable through it.
        write_capability_link(conn, capability.id, evidence.id, "onboarding:cv",
                              "exp-1")
        assert _reachable(conn, family.id) == ["Led the platform team",
                                               "Ran the data migration"]
        run_experience_edit(conn, "exp-1",
                            _scripted(["", "", "", "",
                                       "links", "unlink", "1", "done"]), says.append)
        edges = SqliteCareerEdgeRepository(conn).list_all()
        reachable = _reachable(conn, family.id)
    finally:
        conn.close()
    supports = [e for e in edges if e.edge_type == "SUPPORTS"]
    demonstrates = [e for e in edges if e.edge_type == "DEMONSTRATES"]
    assert [e.superseded_at for e in supports] == [None]  # untouched
    assert all(e.superseded_at is not None for e in demonstrates)
    # exp-2's chain survives intact, which is the whole point of the refusal.
    assert reachable == ["Led the platform team", "Ran the data migration"]
    transcript = "\n".join(says)
    assert "it also proves other experiences' facts" in transcript
    assert "would break" in transcript


def test_unlink_retires_only_the_selected_experiences_own_support(tmp_path):
    """Two experiences with their own owner rows: unlinking one removes only
    its reachable facts."""
    conn = _instance(tmp_path)
    capability = Capability(id=new_id("cap"), name="python backend",
                           strength="unrated")
    SqliteCapabilityRepository(conn).add(capability)
    try:
        family = _targeted_family(conn, capability.id)
        run_experience_add(conn, _scripted(_ADD_ONE + ["1"]), lambda _line: None)
        run_experience_add(conn, _scripted(
            ["role", "Second", "Beta", "2022-01", "2023-01",
             "Ran the data migration", "achievement", "", "1"]),
            lambda _line: None)
        first = [e for e in SqliteExperienceRepository(conn).list_all()
                 if e.title == "Founding Engineer"][0]
        assert _reachable(conn, family.id) == ["Ran the data migration",
                                              "Shipped the billing pipeline"]
        run_experience_edit(conn, first.id,
                            _scripted(["", "", "", "",
                                       "links", "unlink", "1", "done"]),
                            lambda _line: None)
        reachable = _reachable(conn, family.id)
    finally:
        conn.close()
    assert reachable == ["Ran the data migration"]


def test_a_fact_added_to_a_pre_fix_experience_is_reachable(tmp_path):
    """The live instance's other legacy shape: an experience whose link hangs
    off an old per-session evidence row. Facts stated now land on the
    experience's own owner row, so the link the user already made is extended
    to that row with the fact, inside the same transaction."""
    conn = _instance(tmp_path)
    capability = Capability(id=new_id("cap"), name="python backend",
                            strength="unrated")
    SqliteCapabilityRepository(conn).add(capability)
    SqliteExperienceRepository(conn).add(
        Experience(id="exp-1", kind="role", title="Role", org="Acme", display_order=0))
    session_evidence = Evidence(id="ev-session", evidence_type="user_statement",
                                title="Stated experience 2026-08-12")
    SqliteEvidenceRepository(conn).add(session_evidence)
    try:
        family = _targeted_family(conn, capability.id)
        write_stated_fact(conn, lambda: session_evidence, "Shipped the pipeline",
                          "achievement", "experience:add", "exp-1")
        write_capability_link(conn, capability.id, session_evidence.id,
                              "experience:edit", "exp-1")
        assert _reachable(conn, family.id) == ["Shipped the pipeline"]
        run_experience_edit(conn, "exp-1",
                            _scripted(["", "", "", "",
                                       "add", "Cut latency by 40%", "metric", "",
                                       "done"]), lambda _line: None)
        reachable = _reachable(conn, family.id)
    finally:
        conn.close()
    assert reachable == ["Cut latency by 40%", "Shipped the pipeline"]
