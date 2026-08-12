"""Onboarding flow driven with scripted answers through the ask seam
(cold-start contract: CV-first, interview as confirmation-and-gaps)."""

import hashlib
import json
import sqlite3
import subprocess

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
from apps.cli.main import main
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
        "confirm", "",                    # fact 1: confirm as-is, quantifier skipped
        "edit", "Contributed to the platform team",  # fact 2: scope inflation caught, edited
        "",                               # fact 2: quantifier skipped
        "reject",                         # fact 3: rejected
        "",                               # capabilities: none
        "",                               # goals: none
        "", "", "", "",                   # profile basics skipped
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
        "confirm", "",  # fact 3 (no experience): confirmed, quantifier skipped
        "",           # capabilities: none
        "",           # goals: none
        "", "", "", "",   # profile basics skipped
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
    answers = ["confirm", "confirm", "", "confirm", "", "confirm", "", "", "", "", "", "", ""]
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
        "Jane Placeholder", "jane@example.com", "+351 900 000 000", "Milan",  # profile basics
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
            "full_name": "Jane Placeholder", "email": "jane@example.com",
            "phone": "+351 900 000 000", "location": "Milan"}
    finally:
        conn.close()


def test_capability_step_offers_linking_cv_evidence(instance, tmp_path):
    """Graph-starvation regression (drive 2026-08-11): CV evidence PROVES the
    confirmed facts, and the capability step offers to link it as SUPPORTS so
    the family walk can reach experience-backed facts. One confirmation, no
    model values (candidates derived in code)."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\nBackend Engineer at Acme 2021-2023\n")
    answers = [
        "confirm", "confirm", "", "confirm", "", "confirm", "",  # experience + 3 facts (quantifiers skipped)
        "Backend service design", "strong",          # capability + strength
        "y",                                         # link the CV evidence
        "",                                          # capabilities done
        "",                                          # goals: none
        "", "", "", "",                              # profile basics skipped
    ]
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=_scripted(answers), say=lambda _: None)
        cv_evidence = [e for e in SqliteEvidenceRepository(conn).list_all()
                       if e.evidence_type == "cv"][0]
        capability = SqliteCapabilityRepository(conn).get_by_name("Backend service design")
        supports = SqliteCareerEdgeRepository(conn).active_edges_to(
            "capability", capability.id, "SUPPORTS")
        by_source = {e.source_id: e for e in supports}
        assert cv_evidence.id in by_source  # the CV evidence now supports it
        edge = by_source[cv_evidence.id]
        assert edge.created_by == "user" and edge.user_verified == 1
        assert edge.claim_kind == "fact"
        assert len(supports) == 2  # interview self-assessment plus the CV link
    finally:
        conn.close()


def test_capability_step_link_declined_mints_nothing(instance, tmp_path):
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    answers = [
        "confirm", "confirm", "", "confirm", "", "confirm", "",
        "Backend service design", "strong",
        "n",                                         # decline the link
        "", "", "", "", "", "",
    ]
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=_scripted(answers), say=lambda _: None)
        cv_evidence = [e for e in SqliteEvidenceRepository(conn).list_all()
                       if e.evidence_type == "cv"][0]
        capability = SqliteCapabilityRepository(conn).get_by_name("Backend service design")
        supports = SqliteCareerEdgeRepository(conn).active_edges_to(
            "capability", capability.id, "SUPPORTS")
        assert cv_evidence.id not in {e.source_id for e in supports}
    finally:
        conn.close()


def test_onboarding_asks_for_phone(instance):
    answers = [
        "", "",                        # no capabilities, no goals
        "Jane Placeholder", "jane@example.com", "+351 900 000 000", "Milan",
    ]
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), model=None, cv_path=None,
                       ask=_scripted(answers), say=lambda _: None)
        assert SqliteUserProfileRepository(conn).get_fields()["phone"] == "+351 900 000 000"
    finally:
        conn.close()


PDF_BYTES = b"%PDF-1.4 fake pdf body with binary \x00\x80 bytes"


def test_onboarding_with_pdf_cv_extracts_via_pdftotext(instance, tmp_path, monkeypatch):
    """A PDF CV goes through pdftotext (faked here); the stored evidence file
    and its hash are the original PDF bytes, extraction sees the text."""
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(PDF_BYTES)

    def fake_pdftotext(argv, capture_output=None):
        assert argv == ["pdftotext", str(cv), "-"]
        return subprocess.CompletedProcess(argv, 0, stdout=b"Jane Placeholder\nBackend text\n", stderr=b"")

    monkeypatch.setattr("apps.cli.onboarding.subprocess.run", fake_pdftotext)

    seen_prompts = []

    class CapturingModel(ModelAdapter):
        def complete(self, prompt: str) -> str:
            seen_prompts.append(prompt)
            return EXTRACTION

    answers = ["confirm", "confirm", "", "confirm", "", "confirm", "", "", "", "", "", "", ""]
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), CapturingModel(), cv,
                       ask=_scripted(answers), say=lambda _: None)
        assert "Backend text" in seen_prompts[0]  # extraction got the pdftotext output
        evidence = SqliteEvidenceRepository(conn).list_all()
        assert evidence[0].locator.endswith(".pdf")
        stored = (instance / evidence[0].locator).read_bytes()
        assert stored == PDF_BYTES  # original bytes, not extracted text
        assert evidence[0].content_hash == hashlib.sha256(PDF_BYTES).hexdigest()
    finally:
        conn.close()


def test_cli_onboard_degrades_when_pdftotext_is_absent(tmp_path, monkeypatch, capsys):
    instance = tmp_path / "instance"
    migrate(instance / "open-career.sqlite3")
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(PDF_BYTES)
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))

    def raising_run(argv, capture_output=None):
        raise FileNotFoundError("pdftotext")

    monkeypatch.setattr("apps.cli.onboarding.subprocess.run", raising_run)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")  # every prompt skipped, tier-1 included

    main(["onboard", str(cv)])  # must not raise SystemExit

    captured = capsys.readouterr()
    assert "pdftotext not found; install poppler-utils, or supply a text CV" in captured.err
    assert "Continuing without the CV" in captured.out
    assert "Onboarding complete." in captured.out
    # Degraded before storing: no evidence row, no stored file.
    conn = _conn(instance)
    try:
        assert SqliteEvidenceRepository(conn).list_all() == []
    finally:
        conn.close()
    assert not (instance / "files").exists()


def test_invalid_utf8_pdftotext_output_is_replaced_not_a_crash(instance, tmp_path, monkeypatch):
    """Stray non-UTF-8 bytes in pdftotext output become replacement characters
    and extraction proceeds; no locale-dependent UnicodeDecodeError escapes."""
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(PDF_BYTES)
    monkeypatch.setattr(
        "apps.cli.onboarding.subprocess.run",
        lambda argv, capture_output=None: subprocess.CompletedProcess(
            argv, 0, stdout=b"Jane\xff\xfe Placeholder\n", stderr=b""))

    seen_prompts = []

    class CapturingModel(ModelAdapter):
        def complete(self, prompt: str) -> str:
            seen_prompts.append(prompt)
            return EXTRACTION

    answers = ["confirm", "confirm", "", "confirm", "", "confirm", "", "", "", "", "", "", ""]
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), CapturingModel(), cv,
                       ask=_scripted(answers), say=lambda _: None)
        assert "Jane�� Placeholder" in seen_prompts[0]
    finally:
        conn.close()


def test_cli_onboard_degrades_when_pdftotext_output_is_undecodable(tmp_path, monkeypatch, capsys):
    """A residual decode failure is wrapped as CvReadError and degrades
    cleanly, before any storage write."""
    instance = tmp_path / "instance"
    migrate(instance / "open-career.sqlite3")
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(PDF_BYTES)
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))

    class Undecodable:
        def decode(self, *args, **kwargs):
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "simulated residual failure")

    monkeypatch.setattr(
        "apps.cli.onboarding.subprocess.run",
        lambda argv, capture_output=None: subprocess.CompletedProcess(
            argv, 0, stdout=Undecodable(), stderr=b""))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")  # every prompt skipped, tier-1 included

    main(["onboard", str(cv)])  # must not raise

    captured = capsys.readouterr()
    assert "could not be decoded; supply a text CV" in captured.err
    assert "Continuing without the CV" in captured.out
    assert "Onboarding complete." in captured.out
    conn = _conn(instance)
    try:
        assert SqliteEvidenceRepository(conn).list_all() == []
    finally:
        conn.close()
    assert not (instance / "files").exists()


def test_onboarding_ux_messages(instance, tmp_path):
    """Invalid choices and invalid profile values re-prompt with a reason, and
    a missing end date renders as 'present', never 'None'."""
    extraction = json.dumps({
        "experiences": [{"kind": "role", "title": "Eng", "org": "Acme",
                         "start_date": "2021", "end_date": None, "summary": None}],
        "facts": [],
    })

    class CurrentRoleModel(ModelAdapter):
        def complete(self, prompt: str) -> str:
            return extraction

    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    answers = [
        "banana", "confirm",            # invalid choice re-prompts with a message
        "",                              # capabilities: none
        "",                              # goals: none
        "",                              # full_name skipped
        "not-an-email", "jane@example.com",  # email: rejected with reason, then valid
        "",                              # phone skipped
        "",                              # location skipped
    ]
    says = []
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), CurrentRoleModel(), cv,
                       ask=_scripted(answers), say=says.append)
        joined = "\n".join(says)
        assert "(2021 - present)" in joined
        assert "None" not in joined
        assert "invalid choice, expected confirm/edit/reject" in joined
        assert "invalid value: 'not-an-email' does not look like an email address" in joined
        assert SqliteUserProfileRepository(conn).get_fields() == {"email": "jane@example.com"}
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


def _completed(returncode=0, stdout="", stderr=""):
    def run(argv, input=None, capture_output=None, text=None, timeout=None):
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)
    return run


def _raising(exc):
    def run(argv, input=None, capture_output=None, text=None, timeout=None):
        raise exc
    return run


def test_interrupt_during_families_init_says_family_answers_unsaved(
        tmp_path, monkeypatch, capsys):
    """The families flow buffers answers until the strategy version mints, so
    an interrupt there gets its own accurate message (family answers not yet
    saved), exit 130, and no version minted (Codex round 6). The generic
    progress-saved message must not appear."""
    instance = tmp_path / "instance"
    migrate(instance / "open-career.sqlite3")
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    proposal = json.dumps([{
        "name": "Backend Engineering", "rationale": "matches the approved state",
        "target_seniority": None, "adjacent_titles": [],
        "search_vocabulary": [], "target_capability_names": []}])
    monkeypatch.setattr("adapters.models.claude_code.subprocess.run",
                        _completed(stdout=json.dumps({"result": proposal})))
    answers = iter([
        "python backend", "strong", "",  # one capability -> approved state exists
        "",                              # goals done
        "", "", "", "",                  # basics skipped
        "c",                             # family confirmed (buffered, not persisted)
    ])

    def fake_input(_prompt=""):
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt from None  # interrupt at the emphasis prompt

    monkeypatch.setattr("builtins.input", fake_input)
    with pytest.raises(SystemExit) as exc:
        main(["onboard"])
    assert exc.value.code == 130
    out = capsys.readouterr().out
    assert ("interrupted during family setup; family answers were not yet saved,"
            " everything before this step is") in out
    assert "everything answered so far is saved" not in out
    conn = _conn(instance)
    try:
        assert conn.execute("SELECT COUNT(*) FROM strategy_versions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM role_families").fetchone()[0] == 0
        # Everything before the families step did persist.
        assert SqliteCapabilityRepository(conn).get_by_name("python backend")
    finally:
        conn.close()


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
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")  # every prompt skipped, tier-1 included

    main(["onboard", str(cv)])  # must not raise SystemExit

    captured = capsys.readouterr()
    assert captured.err.startswith("CV extraction failed:")
    assert expected in captured.err
    assert "Continuing without the CV" in captured.out
    assert "Onboarding complete." in captured.out
