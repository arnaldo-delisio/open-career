"""`open-career stories`: the depth interview's six clusters, resume state
computed from data, pacing checkpoints (OC-35)."""

import hashlib
import sqlite3

from adapters.storage.local import LocalStorageAdapter
from adapters.storage.migrations import migrate
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteCareerFactRepository,
    SqliteEvidenceRepository,
    SqliteExperienceRepository,
)
from adapters.storage.sqlite_policies import SqliteUserPolicyRepository
from apps.cli.stories import run_stories
from domain.entities import Capability, CareerFact, Experience
from domain.ids import new_id


def _scripted(answers):
    remaining = list(answers)
    return lambda _prompt: remaining.pop(0)


def _instance(tmp_path):
    migrate(tmp_path / "open-career.sqlite3")
    conn = sqlite3.connect(tmp_path / "open-career.sqlite3")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn, LocalStorageAdapter(tmp_path)


def _seed_experience(conn, title="Backend Engineer", org="Acme"):
    experience = Experience(id=new_id("exp"), kind="role", title=title, org=org)
    SqliteExperienceRepository(conn).add(experience)
    return experience


def _seed_fact(conn, experience_id, statement):
    fact = CareerFact(id=new_id("fact"), fact_type="achievement", statement=statement,
                      source="cv", user_approved=1, experience_id=experience_id,
                      verified_at="2026-08-12T00:00:00Z")
    SqliteCareerFactRepository(conn).add(fact)
    return fact


def _seed_capability(conn, name="python backend"):
    capability = Capability(id=new_id("cap"), name=name, strength="strong")
    SqliteCapabilityRepository(conn).add(capability)
    return capability


def test_menu_shows_per_cluster_completeness_and_blank_quits(tmp_path):
    conn, storage = _instance(tmp_path)
    says = []
    try:
        run_stories(conn, storage, ask=_scripted([""]), say=says.append)
    finally:
        conn.close()
    joined = "\n".join(says)
    assert "story bank: 0/0 experiences have stories" in joined
    assert "capability deepening: 0/0" in joined
    assert "narratives: 0/4 recorded" in joined
    assert "logistics depth: 0/4 policies set" in joined


def test_story_bank_mints_file_evidence_and_edges(tmp_path):
    conn, storage = _instance(tmp_path)
    experience = _seed_experience(conn)
    fact = _seed_fact(conn, experience.id, "Built the order service handling 2M requests")
    capability = _seed_capability(conn)
    answers = [
        "1",                       # cluster: story bank
        "Payments kept failing under load",    # situation
        "I redesigned the retry pipeline",     # what you did
        "Failures dropped to near zero",       # outcome
        "y",                       # PROVES the seeded fact
        "python backend",          # SUPPORTS the capability
        "",                        # capability links done
        "n",                       # no second cluster
    ]
    try:
        run_stories(conn, storage, ask=_scripted(answers), say=lambda _: None)
        stories = [e for e in SqliteEvidenceRepository(conn).list_all()
                   if e.title.startswith("story: ")]
        assert len(stories) == 1
        story = stories[0]
        assert story.evidence_type == "user_statement"
        assert story.locator == f"files/stories/{story.id}.md"
        body = (tmp_path / story.locator).read_bytes()
        assert b"I redesigned the retry pipeline" in body
        assert story.content_hash == hashlib.sha256(body).hexdigest()
        edges = SqliteCareerEdgeRepository(conn)
        proves = edges.active_edges_from("evidence", story.id, "PROVES")
        assert [e.target_id for e in proves] == [fact.id]
        supports = edges.active_edges_from("evidence", story.id, "SUPPORTS")
        assert [e.target_id for e in supports] == [capability.id]
        assert all(e.created_by == "user" and e.user_verified == 1
                   for e in proves + supports)
    finally:
        conn.close()


def test_story_bank_resume_state_is_computed_from_data(tmp_path):
    """An experience with a story is not offered again; completeness reflects it."""
    conn, storage = _instance(tmp_path)
    _seed_experience(conn, title="Role A")
    answers = ["1", "S", "A", "O", "", "n"]  # one story, no fact/capability links
    try:
        run_stories(conn, storage, ask=_scripted(answers), say=lambda _: None)
        says = []
        run_stories(conn, storage, ask=_scripted(["1", "n"]), say=says.append)
        joined = "\n".join(says)
        assert "story bank: 1/1 experiences have stories" in joined
        assert "Every experience already has a story" in joined
    finally:
        conn.close()


def test_capability_deepening_mints_the_full_eligible_chain(tmp_path):
    conn, storage = _instance(tmp_path)
    experience = _seed_experience(conn)
    capability = _seed_capability(conn)
    answers = [
        "2",                                  # cluster: capability deepening
        "1",                                  # experience 1 demonstrates it
        "Ran the Python services powering checkout",   # what happened
        "",                                   # quantifier skipped
        "n",                                  # no second cluster
    ]
    try:
        run_stories(conn, storage, ask=_scripted(answers), say=lambda _: None)
        edges = SqliteCareerEdgeRepository(conn)
        supports = edges.active_edges_to("capability", capability.id, "SUPPORTS")
        assert len(supports) == 1
        evidence_id = supports[0].source_id
        proves = edges.active_edges_from("evidence", evidence_id, "PROVES")
        assert len(proves) == 1
        fact = SqliteCareerFactRepository(conn).get(proves[0].target_id)
        assert fact.user_approved == 1 and fact.experience_id == experience.id
        demonstrates = edges.active_edges_to("capability", capability.id, "DEMONSTRATES")
        assert [e.source_id for e in demonstrates] == [experience.id]
        # The chain is now eligible: a second run reports no gaps.
        says = []
        run_stories(conn, storage, ask=_scripted(["2", "n"]), say=says.append)
        assert any("1/1 capabilities have" in s for s in says)
        assert any("already has an eligible evidence chain" in s for s in says)
    finally:
        conn.close()


def test_preferences_cluster_writes_audited_policies(tmp_path):
    conn, storage = _instance(tmp_path)
    answers = [
        "3",
        "growth, public", "seed",   # company stage in/out
        "", "",                     # size skipped
        "", "adtech",               # industry: out only
        "ic",                       # work track
        "clear thinking",           # mission themes
        "n",
    ]
    try:
        run_stories(conn, storage, ask=_scripted(answers), say=lambda _: None)
        policies = SqliteUserPolicyRepository(conn).get_policies()
        assert policies["company_stage_pref"] == {"in": ["growth", "public"], "out": ["seed"]}
        assert "company_size_pref" not in policies
        assert policies["industry_pref"] == {"in": [], "out": ["adtech"]}
        assert policies["work_track"] == "ic"
        assert policies["mission_themes"] == ["clear thinking"]
        writes = SqliteUserPolicyRepository(conn).list_writes()
        assert len(writes) == 4  # every set audited, skips write nothing
    finally:
        conn.close()


def test_logistics_cluster_writes_policies_and_canonical_notice_period(tmp_path):
    conn, storage = _instance(tmp_path)
    answers = [
        "6",
        "Dublin, Amsterdam",       # relocation whitelist
        "-2", "3",                 # timezone bounds
        "EU citizen", "",          # visa details, no expiry
        "2026-10-01",              # earliest start
        "2 months",                # notice period (canonical field)
        "n",
    ]
    try:
        run_stories(conn, storage, ask=_scripted(answers), say=lambda _: None)
        policies = SqliteUserPolicyRepository(conn).get_policies()
        assert policies["relocation_whitelist"] == ["Dublin", "Amsterdam"]
        assert policies["timezone_bounds"] == {"min_utc_offset": -2, "max_utc_offset": 3}
        assert policies["visa_details"] == {"status_note": "EU citizen"}
        assert policies["earliest_start"] == "2026-10-01"
        from adapters.storage.sqlite_profile import SqliteUserProfileRepository
        assert SqliteUserProfileRepository(conn).get_fields()["notice_period"] == "2 months"
    finally:
        conn.close()


def test_narratives_store_named_non_claim_files_with_no_edges(tmp_path):
    conn, storage = _instance(tmp_path)
    answers = [
        "5",
        "I build systems that prove what they claim",  # elevator pitch
        "",                                            # differentiators skipped
        "",                                            # career change skipped
        "",                                            # gap explanation skipped
        "n",
    ]
    try:
        run_stories(conn, storage, ask=_scripted(answers), say=lambda _: None)
        evidence = SqliteEvidenceRepository(conn).list_all()
        assert [e.title for e in evidence] == ["narrative:elevator_pitch"]
        narrative = evidence[0]
        body = (tmp_path / narrative.locator).read_bytes()
        assert b"prove what they claim" in body
        assert narrative.content_hash == hashlib.sha256(body).hexdigest()
        # Non-claim source material: no edges to facts required, none minted.
        assert SqliteCareerEdgeRepository(conn).list_all() == []
        says = []
        run_stories(conn, storage, ask=_scripted([""]), say=says.append)
        assert any("narratives: 1/4 recorded" in s for s in says)
    finally:
        conn.close()


def test_pacing_offers_stop_every_five_items_and_stopping_loses_nothing(tmp_path):
    conn, storage = _instance(tmp_path)
    for i in range(6):
        _seed_experience(conn, title=f"Role {i}")
    # Five stories answered (no facts to link, no capabilities), then stop.
    answers = ["1"]
    for i in range(5):
        answers += [f"S{i}", f"A{i}", f"O{i}", ""]
    answers += ["s", "n"]  # the checkpoint after item five: stop, then quit
    try:
        run_stories(conn, storage, ask=_scripted(answers), say=lambda _: None)
        stories = [e for e in SqliteEvidenceRepository(conn).list_all()
                   if e.title.startswith("story: ")]
        assert len(stories) == 5  # everything answered so far persisted
        says = []
        run_stories(conn, storage, ask=_scripted([""]), say=says.append)
        assert any("story bank: 5/6" in s for s in says)
    finally:
        conn.close()
