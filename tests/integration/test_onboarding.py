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


class RaisingModel(ModelAdapter):
    """The resume paths must never call the model; this one proves it."""

    def complete(self, prompt: str) -> str:
        raise AssertionError("the model was called on a resume path")


def _interrupting(answers):
    remaining = list(answers)

    def ask(_prompt: str) -> str:
        if not remaining:
            raise KeyboardInterrupt
        return remaining.pop(0)

    return ask


def test_onboard_resume_confirms_pending_drafts_without_the_model(instance, tmp_path):
    """A re-run with the same CV bytes and pending cv-source drafts skips
    storage, extraction, and the experience walk, and resumes the confirmation
    walk over exactly the pending drafts (OC-36; resume derived from data,
    never a stored cursor)."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\nBackend Engineer at Acme 2021-2023\n")
    conn = _conn(instance)
    try:
        # First run: interrupted at the first fact confirmation, leaving all
        # three drafts pending (every earlier answer already persisted).
        with pytest.raises(KeyboardInterrupt):
            run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                           ask=_interrupting(["confirm"]), say=lambda _: None)
        drafts = [f for f in SqliteCareerFactRepository(conn).list_all()
                  if f.source == "cv" and not f.user_approved]
        assert len(drafts) == 3

        says = []
        answers = [
            "confirm", "", "confirm", "", "confirm", "",  # 3 drafts, quantifiers skipped
            "", "",                                       # capabilities, goals: none
            "", "", "", "",                               # profile basics skipped
        ]
        run_onboarding(conn, LocalStorageAdapter(instance), RaisingModel(), cv,
                       ask=_scripted(answers), say=says.append)

        assert any("resuming" in line for line in says)
        # No duplicate ingestion: one cv evidence row, one experience.
        cv_rows = [e for e in SqliteEvidenceRepository(conn).list_all()
                   if e.evidence_type == "cv"]
        assert len(cv_rows) == 1
        assert len(SqliteExperienceRepository(conn).list_all()) == 1
        facts = [f for f in SqliteCareerFactRepository(conn).list_all()
                 if f.source == "cv"]
        assert all(f.user_approved == 1 for f in facts)
        # PROVES edges land on the existing evidence row.
        edges = SqliteCareerEdgeRepository(conn).active_edges_from(
            "evidence", cv_rows[0].id, "PROVES")
        assert {e.target_id for e in edges} == {f.id for f in facts}
    finally:
        conn.close()


def test_onboard_resume_with_walk_complete_skips_straight_to_gap_questions(
        instance, tmp_path):
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\nBackend Engineer at Acme 2021-2023\n")
    conn = _conn(instance)
    try:
        first = ["confirm", "confirm", "", "confirm", "", "confirm", "",
                 "", "", "", "", "", ""]
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=_scripted(first), say=lambda _: None)
        facts_before = {f.id for f in SqliteCareerFactRepository(conn).list_all()}

        says = []
        run_onboarding(conn, LocalStorageAdapter(instance), RaisingModel(), cv,
                       ask=_scripted(["", "", "", "", "", ""]), say=says.append)

        assert any("walk is complete" in line for line in says)
        assert len([e for e in SqliteEvidenceRepository(conn).list_all()
                    if e.evidence_type == "cv"]) == 1
        assert len(SqliteExperienceRepository(conn).list_all()) == 1
        assert {f.id for f in SqliteCareerFactRepository(conn).list_all()} == facts_before
    finally:
        conn.close()


def test_onboard_resume_scopes_to_the_matching_cvs_drafts(instance, tmp_path):
    """Pending drafts from two different CVs: resuming one CV walks only its
    own drafts (origin_evidence_id scoping) and never touches the other's."""
    class PerCvModel(ModelAdapter):
        """Distinct experiences per CV, so the runs share nothing reusable."""

        def __init__(self, name: str):
            self._name = name

        def complete(self, prompt: str) -> str:
            return json.dumps({
                "experiences": [{"kind": "role", "title": f"Engineer {self._name}",
                                 "org": self._name, "start_date": "2021",
                                 "end_date": "2023", "summary": None}],
                "facts": [
                    {"experience_index": 0, "fact_type": "achievement",
                     "statement": f"Achievement one at {self._name}",
                     "source_location": None},
                    {"experience_index": 0, "fact_type": "scope",
                     "statement": f"Scope two at {self._name}",
                     "source_location": None},
                    {"experience_index": None, "fact_type": "skill_use",
                     "statement": f"Skill three at {self._name}",
                     "source_location": None},
                ],
            })

    conn = _conn(instance)
    try:
        cvs = {}
        for name, body in (("a", "first CV body\n"), ("b", "second CV body\n")):
            cv = tmp_path / name / "cv.txt"
            cv.parent.mkdir()
            cv.write_text(body)
            cvs[name] = cv
            with pytest.raises(KeyboardInterrupt):
                run_onboarding(conn, LocalStorageAdapter(instance), PerCvModel(name), cv,
                               ask=_interrupting(["confirm"]), say=lambda _: None)
        cv_rows = {e.content_hash: e for e in SqliteEvidenceRepository(conn).list_all()
                   if e.evidence_type == "cv"}
        assert len(cv_rows) == 2

        answers = ["confirm", "", "confirm", "", "confirm", "",
                   "", "", "", "", "", ""]
        run_onboarding(conn, LocalStorageAdapter(instance), RaisingModel(), cvs["a"],
                       ask=_scripted(answers), say=lambda _: None)

        a_row = cv_rows[hashlib.sha256(cvs["a"].read_bytes()).hexdigest()]
        drafts = [f for f in SqliteCareerFactRepository(conn).list_all()
                  if f.source == "cv"]
        for fact in drafts:
            if fact.origin_evidence_id == a_row.id:
                assert fact.user_approved == 1
            else:
                assert fact.user_approved == 0 and fact.status == "active"
    finally:
        conn.close()


def test_onboard_resume_refuses_ambiguous_null_provenance(instance, tmp_path):
    """Legacy drafts (origin NULL) resolve to the single cv evidence row; with
    several cv rows the attribution would be a guess, so resume refuses."""
    conn = _conn(instance)
    try:
        cvs = []
        for name, body in (("a", "first CV body\n"), ("b", "second CV body\n")):
            cv = tmp_path / name / "cv.txt"
            cv.parent.mkdir()
            cv.write_text(body)
            cvs.append(cv)
            with pytest.raises(KeyboardInterrupt):
                run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                               ask=_interrupting(["confirm"]), say=lambda _: None)
        with conn:
            conn.execute("UPDATE career_facts SET origin_evidence_id = NULL")
        with pytest.raises(ValueError, match="no CV provenance"):
            run_onboarding(conn, LocalStorageAdapter(instance), RaisingModel(), cvs[0],
                           ask=_scripted([]), say=lambda _: None)
    finally:
        conn.close()


def test_onboard_resume_null_provenance_with_one_cv_row_still_resumes(
        instance, tmp_path):
    """The live-instance case: pre-0005 drafts (origin NULL) and exactly one
    cv evidence row resume as that row's drafts."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    conn = _conn(instance)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                           ask=_interrupting(["confirm"]), say=lambda _: None)
        with conn:
            conn.execute("UPDATE career_facts SET origin_evidence_id = NULL")
        answers = ["confirm", "", "confirm", "", "confirm", "",
                   "", "", "", "", "", ""]
        says = []
        run_onboarding(conn, LocalStorageAdapter(instance), RaisingModel(), cv,
                       ask=_scripted(answers), say=says.append)
        assert any("resuming" in line for line in says)
        assert all(f.user_approved == 1
                   for f in SqliteCareerFactRepository(conn).list_all()
                   if f.source == "cv")
    finally:
        conn.close()


def test_failed_extraction_leaves_no_evidence_row_and_retry_re_extracts(
        instance, tmp_path):
    """The evidence row is the commit point: a failed extraction leaves no
    row, so the retry is a fresh ingest, never a false hash-match resume."""
    from adapters.models.claude_code import ModelCallError

    class FailingModel(ModelAdapter):
        def complete(self, prompt: str) -> str:
            raise ModelCallError("simulated failure")

    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    conn = _conn(instance)
    try:
        with pytest.raises(ModelCallError):
            run_onboarding(conn, LocalStorageAdapter(instance), FailingModel(), cv,
                           ask=_scripted([]), say=lambda _: None)
        assert [e for e in SqliteEvidenceRepository(conn).list_all()
                if e.evidence_type == "cv"] == []

        answers = ["confirm", "confirm", "", "confirm", "", "confirm", "",
                   "", "", "", "", "", ""]
        says = []
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=_scripted(answers), say=says.append)
        assert not any("already ingested" in line for line in says)
        cv_rows = [e for e in SqliteEvidenceRepository(conn).list_all()
                   if e.evidence_type == "cv"]
        assert len(cv_rows) == 1
    finally:
        conn.close()


def test_interrupt_before_drafts_re_extracts_instead_of_resuming_complete(
        instance, tmp_path):
    """An evidence row with zero originated facts means the walk died before
    drafts persisted: the re-run re-extracts rather than declaring the walk
    complete."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    conn = _conn(instance)
    try:
        # Interrupt at the first experience prompt: evidence row persisted
        # (extraction succeeded) but no draft facts yet.
        with pytest.raises(KeyboardInterrupt):
            run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                           ask=_interrupting([]), say=lambda _: None)
        assert len([e for e in SqliteEvidenceRepository(conn).list_all()
                    if e.evidence_type == "cv"]) == 1
        assert [f for f in SqliteCareerFactRepository(conn).list_all()
                if f.source == "cv"] == []

        answers = ["confirm", "confirm", "", "confirm", "", "confirm", "",
                   "", "", "", "", "", ""]
        says = []
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=_scripted(answers), say=says.append)
        assert any("never landed" in line for line in says)
        facts = [f for f in SqliteCareerFactRepository(conn).list_all()
                 if f.source == "cv"]
        assert len(facts) == 3 and all(f.user_approved == 1 for f in facts)
    finally:
        conn.close()


def test_interrupt_mid_experience_walk_never_duplicates_confirmed_experiences(
        instance, tmp_path):
    """Experiences persist per answer but facts only after the walk: a re-run
    after an interrupt re-extracts, reuses the exactly-matching confirmed
    experience without asking again, and asks only the remaining ones."""
    extraction = json.dumps({
        "experiences": [
            {"kind": "role", "title": "Backend Engineer", "org": "Acme",
             "start_date": "2021", "end_date": "2023", "summary": None},
            {"kind": "project", "title": "Side Tool", "org": "Self",
             "start_date": "2024", "end_date": None, "summary": None},
        ],
        "facts": [
            {"experience_index": 0, "fact_type": "achievement",
             "statement": "Built the order service", "source_location": None},
            {"experience_index": 1, "fact_type": "achievement",
             "statement": "Shipped the side tool", "source_location": None},
        ],
    })

    class TwoExperienceModel(ModelAdapter):
        def complete(self, prompt: str) -> str:
            return extraction

    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    conn = _conn(instance)
    try:
        # Accept experience one, interrupt at experience two: one experience
        # persisted, zero facts.
        with pytest.raises(KeyboardInterrupt):
            run_onboarding(conn, LocalStorageAdapter(instance), TwoExperienceModel(), cv,
                           ask=_interrupting(["confirm"]), say=lambda _: None)
        assert len(SqliteExperienceRepository(conn).list_all()) == 1

        # Re-run: the confirmed experience is reused without a prompt (the
        # script would misalign if it were re-asked), the second is asked.
        answers = [
            "confirm",                    # experience two only
            "confirm", "", "confirm", "",  # both facts, quantifiers skipped
            "", "",                        # capabilities, goals
            "", "", "", "",                # basics skipped
        ]
        says = []
        run_onboarding(conn, LocalStorageAdapter(instance), TwoExperienceModel(), cv,
                       ask=_scripted(answers), say=says.append)
        assert any("already confirmed earlier; reusing it" in line for line in says)
        experiences = SqliteExperienceRepository(conn).list_all()
        assert sorted(e.title for e in experiences) == ["Backend Engineer", "Side Tool"]
        orders = {e.title: e.display_order for e in experiences}
        assert orders["Backend Engineer"] == 0 and orders["Side Tool"] == 1
        facts = [f for f in SqliteCareerFactRepository(conn).list_all()
                 if f.source == "cv"]
        assert len(facts) == 2 and all(f.user_approved == 1 for f in facts)
    finally:
        conn.close()


def test_replay_keeps_legitimately_repeated_identical_experiences_distinct(
        instance, tmp_path):
    """A CV can carry the same role twice (same kind, title, org, dates): the
    replay walk consumes each persisted match at most once, so the second
    identical draft is asked and minted as its own row, and each entry's
    facts attach to their own experience."""
    shape = {"kind": "role", "title": "Contract Engineer", "org": "Acme",
             "start_date": "2021", "end_date": "2023", "summary": None}
    extraction = json.dumps({
        "experiences": [dict(shape), dict(shape)],
        "facts": [
            {"experience_index": 0, "fact_type": "achievement",
             "statement": "First stint achievement", "source_location": None},
            {"experience_index": 1, "fact_type": "achievement",
             "statement": "Second stint achievement", "source_location": None},
        ],
    })

    class RepeatedRoleModel(ModelAdapter):
        def complete(self, prompt: str) -> str:
            return extraction

    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    conn = _conn(instance)
    try:
        # Accept the first identical entry, interrupt at the second.
        with pytest.raises(KeyboardInterrupt):
            run_onboarding(conn, LocalStorageAdapter(instance), RepeatedRoleModel(), cv,
                           ask=_interrupting(["confirm"]), say=lambda _: None)
        assert len(SqliteExperienceRepository(conn).list_all()) == 1

        # Replay: entry one reuses the persisted row, entry two is asked (the
        # scripted answers prove the ask happened by alignment).
        answers = [
            "confirm",                     # the second identical entry
            "confirm", "", "confirm", "",  # both facts, quantifiers skipped
            "", "",                        # capabilities, goals
            "", "", "", "",                # basics skipped
        ]
        says = []
        run_onboarding(conn, LocalStorageAdapter(instance), RepeatedRoleModel(), cv,
                       ask=_scripted(answers), say=says.append)
        assert any("reusing it" in line for line in says)
        experiences = SqliteExperienceRepository(conn).list_all()
        assert len(experiences) == 2
        assert len({e.id for e in experiences}) == 2
        facts = {f.statement: f for f in SqliteCareerFactRepository(conn).list_all()
                 if f.source == "cv"}
        first, second = facts["First stint achievement"], facts["Second stint achievement"]
        assert first.experience_id != second.experience_id
        assert {first.experience_id, second.experience_id} == {e.id for e in experiences}
    finally:
        conn.close()


def test_quantifier_interrupt_never_strands_an_approved_fact_without_its_edge(
        instance, tmp_path):
    """The PROVES edge lands with the approval, before the quantifier prompt,
    and the resume path repairs legacy approved facts missing their link
    without duplicating edges that exist."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    conn = _conn(instance)
    edges_repo = SqliteCareerEdgeRepository(conn)
    try:
        # Confirm fact one, interrupt while its quantifier prompt is pending.
        with pytest.raises(KeyboardInterrupt):
            run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                           ask=_interrupting(["confirm", "confirm"]), say=lambda _: None)
        cv_row = [e for e in SqliteEvidenceRepository(conn).list_all()
                  if e.evidence_type == "cv"][0]
        approved = [f for f in SqliteCareerFactRepository(conn).list_all()
                    if f.source == "cv" and f.user_approved]
        assert len(approved) == 1
        edges = edges_repo.active_edges_from("evidence", cv_row.id, "PROVES")
        assert [e.target_id for e in edges] == [approved[0].id]  # edge already there

        # Simulate a pre-fix instance: the approved fact lost its edge.
        with conn:
            conn.execute("DELETE FROM career_edges WHERE target_id = ?",
                         (approved[0].id,))

        # Resume: the two remaining drafts are walked, the missing edge is
        # repaired, and no fact ends up with duplicates.
        answers = ["confirm", "", "confirm", "",
                   "", "", "", "", "", ""]
        says = []
        run_onboarding(conn, LocalStorageAdapter(instance), RaisingModel(), cv,
                       ask=_scripted(answers), say=says.append)
        assert any("Repaired 1 approved facts" in line for line in says)
        facts = [f for f in SqliteCareerFactRepository(conn).list_all()
                 if f.source == "cv"]
        assert all(f.user_approved == 1 for f in facts)
        by_target: dict[str, int] = {}
        for e in edges_repo.active_edges_from("evidence", cv_row.id, "PROVES"):
            by_target[e.target_id] = by_target.get(e.target_id, 0) + 1
        assert by_target == {f.id: 1 for f in facts}  # exactly one edge each
    finally:
        conn.close()


def test_onboard_with_a_different_cv_hash_ingests_fresh(instance, tmp_path):
    """A changed CV behaves as today: new extraction, new evidence row."""
    conn = _conn(instance)
    answers = ["confirm", "confirm", "", "confirm", "", "confirm", "",
               "", "", "", "", "", ""]
    try:
        for name, content in (("cv.txt", "first body\n"), ("cv2.txt", "second body\n")):
            cv = tmp_path / name
            cv.write_text(content)
            run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                           ask=_scripted(answers), say=lambda _: None)
        cv_rows = [e for e in SqliteEvidenceRepository(conn).list_all()
                   if e.evidence_type == "cv"]
        assert len(cv_rows) == 2
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


def _extraction_with_dates(start, end):
    payload = json.loads(EXTRACTION)
    payload["experiences"][0]["start_date"] = start
    payload["experiences"][0]["end_date"] = end
    return json.dumps(payload)


def test_human_month_year_dates_are_confirmed_without_a_re_ask(instance, tmp_path):
    """The dates people actually write are accepted as they are: they are the
    CV's display text, and the canonical time value is derived from them."""
    class Model(ModelAdapter):
        def complete(self, prompt: str) -> str:
            return _extraction_with_dates("September 2015", "July 2017")

    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return {"confirm/edit/reject": "confirm"}.get(
            prompt.split(" (")[0], "confirm" if "confirm/edit" in prompt else "")

    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), Model(), cv,
                       ask=ask, say=lambda _: None)
        (experience,) = SqliteExperienceRepository(conn).list_all()
        assert (experience.start_date, experience.end_date) == ("September 2015", "July 2017")
        assert not any("Start date" in p for p in prompts)  # never re-asked
    finally:
        conn.close()


def test_an_unreadable_extracted_date_is_asked_for_never_stored_silently(instance, tmp_path):
    """A date the canonical parser cannot read is a package that could never
    clear the Gauntlet's date-coherence check, discovered six minutes later.
    It is fixed here, at the point of entry."""
    class Model(ModelAdapter):
        def complete(self, prompt: str) -> str:
            return _extraction_with_dates("mid-2015", "Present")

    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    answers = {"confirm/edit/reject": "confirm", "Start date": "September 2015"}
    says = []

    def ask(prompt):
        for key, value in answers.items():
            if key in prompt:
                return value
        return ""

    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), Model(), cv,
                       ask=ask, say=says.append)
        (experience,) = SqliteExperienceRepository(conn).list_all()
        assert experience.start_date == "September 2015"
        # 'Present' is the ongoing role's null, the one form the rules read.
        assert experience.end_date is None
        assert any("is not a date this system can order" in s for s in says)
    finally:
        conn.close()
