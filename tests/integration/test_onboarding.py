"""Onboarding flow driven with scripted answers through the ask seam
(cold-start contract: CV-first, interview as confirmation-and-gaps)."""

import hashlib
import json
import sqlite3

import pytest

from adapters.storage.local import LocalStorageAdapter
from adapters.storage.migrations import migrate
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteCareerFactRepository,
    SqliteCareerGoalRepository,
    SqliteEvidenceRepository,
    SqliteExperienceRepository,
)
from adapters.storage.sqlite_profile import SqliteUserProfileRepository
from apps.cli.onboarding import run_onboarding
from domain.ports import ModelAdapter

EXTRACTION = json.dumps({
    "experiences": [{"kind": "role", "title": "Backend Engineer", "org": "Acme",
                     "start_date": "2021", "end_date": "2023", "summary": None}],
    "facts": [
        {"experience_index": 0, "fact_type": "achievement",
         "statement": "Built the order service", "source_location": None},
        {"experience_index": 0, "fact_type": "scope",
         "statement": "Led the platform team", "source_location": None},
        {"experience_index": None, "fact_type": "skill_use",
         "statement": "Wrote Python daily", "source_location": None},
    ],
})


class OneShotModel(ModelAdapter):
    def complete(self, prompt: str) -> str:
        return EXTRACTION


def _scripted(answers):
    remaining = list(answers)

    def ask(_prompt: str) -> str:
        return remaining.pop(0)

    return ask


@pytest.fixture
def instance(tmp_path):
    migrate(tmp_path / "open-career.sqlite3")
    return tmp_path


def _conn(instance):
    conn = sqlite3.connect(instance / "open-career.sqlite3")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_onboarding_with_cv_confirm_edit_reject(instance, tmp_path):
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\nBackend Engineer at Acme 2021-2023\n")
    answers = [
        "confirm",                        # experience 1: confirm
        "confirm",                        # fact 1: confirm as-is
        "edit", "Contributed to the platform team",  # fact 2: scope inflation caught, edited
        "reject",                         # fact 3: rejected
        "",                               # capabilities: none
        "",                               # goals: none
        "", "", "",                       # profile basics skipped
    ]
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=_scripted(answers), say=lambda _: None)

        # CV stored via StorageAdapter, evidence row minted with locator + hash.
        # The locator derives from the evidence id; the basename lives in title.
        evidence = SqliteEvidenceRepository(conn).list_all()
        assert [e.evidence_type for e in evidence] == ["cv"]
        assert evidence[0].title == "cv.txt"
        assert evidence[0].locator == f"files/cv/{evidence[0].id}.txt"
        assert (instance / evidence[0].locator).exists()
        assert evidence[0].content_hash

        facts = {f.statement: f for f in SqliteCareerFactRepository(conn).list_all()}
        confirmed = facts["Built the order service"]
        assert (confirmed.user_approved, confirmed.status, confirmed.source) == (1, "active", "cv")
        edited = facts["Contributed to the platform team"]
        assert edited.user_approved == 1
        rejected = facts["Wrote Python daily"]
        assert (rejected.user_approved, rejected.status) == (0, "retracted")

        # PROVES edges exist only for the approved facts, user-verified.
        edges = SqliteCareerEdgeRepository(conn).active_edges_from(
            "evidence", evidence[0].id, "PROVES")
        assert {e.target_id for e in edges} == {confirmed.id, edited.id}
        assert all(e.user_verified == 1 and e.created_by == "user" for e in edges)
    finally:
        conn.close()


def test_rejected_experience_is_never_persisted_nor_its_facts(instance, tmp_path):
    """Review happens before persistence: rejecting an extracted experience
    drops it and its dependent draft facts; only experience-independent facts
    reach the confirm walk."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    answers = [
        "reject",     # the only experience: rejected (drops facts 1 and 2)
        "confirm",    # fact 3 (no experience): confirmed
        "",           # capabilities: none
        "",           # goals: none
        "", "", "",   # profile basics skipped
    ]
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=_scripted(answers), say=lambda _: None)
        assert SqliteExperienceRepository(conn).list_all() == []
        facts = SqliteCareerFactRepository(conn).list_all()
        assert [f.statement for f in facts] == ["Wrote Python daily"]
        assert facts[0].user_approved == 1
    finally:
        conn.close()


def test_two_cvs_with_the_same_basename_do_not_collide(instance, tmp_path):
    """Locators derive from evidence ids: a second upload named identically
    leaves the first file intact and every hash matching its own file."""
    answers = ["confirm", "confirm", "confirm", "confirm", "", "", "", "", ""]
    conn = _conn(instance)
    try:
        for directory, content in (("a", "first CV body\n"), ("b", "second CV body\n")):
            cv = tmp_path / directory / "cv.txt"
            cv.parent.mkdir()
            cv.write_text(content)
            run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                           ask=_scripted(answers), say=lambda _: None)
        cv_rows = [e for e in SqliteEvidenceRepository(conn).list_all()
                   if e.evidence_type == "cv"]
        assert len(cv_rows) == 2
        assert len({e.locator for e in cv_rows}) == 2
        for row in cv_rows:
            path = instance / row.locator
            assert path.exists()
            assert row.content_hash == hashlib.sha256(path.read_bytes()).hexdigest()
    finally:
        conn.close()


def test_onboarding_without_cv_degrades_to_questions(instance):
    answers = [
        "python backend", "strong",   # capability + strength
        "",                            # capabilities done
        "Ship a staff-level role", "mid",  # goal + horizon
        "",                            # goals done
        "Jane Placeholder", "jane@example.com", "Milan",  # profile basics
    ]
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), model=None, cv_path=None,
                       ask=_scripted(answers), say=lambda _: None)

        capability = SqliteCapabilityRepository(conn).get_by_name("python backend")
        assert capability.strength == "strong"

        goals = SqliteCareerGoalRepository(conn).list_all()
        assert [(g.statement, g.horizon) for g in goals] == [("Ship a staff-level role", "mid")]

        # Interview answers land as approved interview-sourced facts with
        # PROVES/SUPPORTS edges from the user_statement evidence row.
        evidence = SqliteEvidenceRepository(conn).list_all()
        assert [e.evidence_type for e in evidence] == ["user_statement"]
        edges = SqliteCareerEdgeRepository(conn)
        supports = edges.active_edges_to("capability", capability.id, "SUPPORTS")
        assert len(supports) == 1 and supports[0].source_id == evidence[0].id
        facts = SqliteCareerFactRepository(conn).list_all()
        assert len(facts) == 1 and facts[0].source == "interview" and facts[0].user_approved == 1
        assert len(edges.active_edges_from("evidence", evidence[0].id, "PROVES")) == 1

        assert SqliteUserProfileRepository(conn).get_fields() == {
            "full_name": "Jane Placeholder", "email": "jane@example.com", "location": "Milan"}
    finally:
        conn.close()


def test_onboarding_with_cv_requires_a_model(instance, tmp_path):
    cv = tmp_path / "cv.txt"
    cv.write_text("text")
    conn = _conn(instance)
    try:
        with pytest.raises(ValueError, match="needs a ModelAdapter"):
            run_onboarding(conn, LocalStorageAdapter(instance), model=None, cv_path=cv,
                           ask=_scripted([]), say=lambda _: None)
    finally:
        conn.close()


# --- CLI degradation when a live model call fails operationally -------------
# `open-career onboard cv.txt` with a failing `claude` call informs the user
# and continues down the no-CV question path instead of dying. The subprocess
# boundary is faked; the suite never calls the real CLI.

import subprocess

from apps.cli.main import main


def _completed(returncode=0, stdout="", stderr=""):
    def run(argv, input=None, capture_output=None, text=None, timeout=None):
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)
    return run


def _raising(exc):
    def run(argv, input=None, capture_output=None, text=None, timeout=None):
        raise exc
    return run


@pytest.mark.parametrize("fake_run,expected", [
    (_raising(subprocess.TimeoutExpired(cmd="claude", timeout=600)), "timed out after"),
    (_completed(returncode=2, stderr="boom"), "exited 2"),
    (_completed(stdout="this is not json"), "invalid JSON envelope"),
    (_completed(stdout=json.dumps({"result": {"nested": 1}})), "not text"),
    (_raising(FileNotFoundError("claude")), "not found on PATH"),
], ids=["timeout", "nonzero-exit", "malformed-envelope", "non-string-result",
        "absent-executable"])
def test_cli_onboard_degrades_when_model_call_fails(tmp_path, monkeypatch, capsys,
                                                    fake_run, expected):
    instance = tmp_path / "instance"
    migrate(instance / "open-career.sqlite3")
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    monkeypatch.setattr("adapters.models.claude_code.subprocess.run", fake_run)
    answers = iter(["", "", "", "", ""])  # no capabilities, no goals, basics skipped
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    main(["onboard", str(cv)])  # must not raise SystemExit

    captured = capsys.readouterr()
    assert captured.err.startswith("CV extraction failed:")
    assert expected in captured.err
    assert "Continuing without the CV" in captured.out
    assert "Onboarding complete." in captured.out
