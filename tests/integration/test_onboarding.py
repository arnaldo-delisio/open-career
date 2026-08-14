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


def test_onboarding_with_cv_accept_edit_reject(instance, tmp_path):
    """One surface, one mark per item: item 1 is the experience, 2 and 3 its
    facts, 4 the experience-independent fact."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\nBackend Engineer at Acme 2021-2023\n")
    answers = [
        "1a",                             # the experience, marked first
        "2a 3e 4r",                       # then its facts and the loose one
        "Contributed to the platform team",  # item 3: scope inflation caught
        "",                               # numbers, the experience's group: skipped
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
    drops it and its dependent draft facts, saying so in the same surface; only
    experience-independent facts remain markable."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    answers = [
        "1r",         # the experience rejected: 2 and 3 cascade with it
        "4a",         # only the loose fact is left to mark
        "",           # numbers, the no-experience group: skipped
        "",           # capabilities: none
        "",           # goals: none
        "", "", "", "",   # profile basics skipped
    ]
    says = []
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=_scripted(answers), say=says.append)
        assert any("its facts are rejected with it: 2, 3" in line for line in says)
        assert SqliteExperienceRepository(conn).list_all() == []
        # The dependent facts are dropped outright, not left as retracted
        # residue, so nothing of the rejected experience survives anywhere.
        facts = SqliteCareerFactRepository(conn).list_all()
        assert [f.statement for f in facts] == ["Wrote Python daily"]
        assert facts[0].user_approved == 1
        # And the review is recorded complete, so the surviving independent
        # fact is not what makes the re-run skip re-extraction.
        cv_rows = [e for e in SqliteEvidenceRepository(conn).list_all()
                   if e.evidence_type == "cv"]
        assert len(cv_rows) == 1 and cv_rows[0].review_completed_at
        says.clear()
        run_onboarding(conn, LocalStorageAdapter(instance), RaisingModel(), cv,
                       ask=_scripted(["", "", "", "", "", ""]), say=says.append)
        assert any("review is complete" in line for line in says)
        assert len([e for e in SqliteEvidenceRepository(conn).list_all()
                    if e.evidence_type == "cv"]) == 1
    finally:
        conn.close()


def test_two_cvs_with_the_same_basename_do_not_collide(instance, tmp_path):
    """Locators derive from evidence ids: a second upload named identically
    leaves the first file intact and every hash matching its own file."""
    conn = _conn(instance)
    try:
        for index, (directory, content) in enumerate(
                (("a", "first CV body\n"), ("b", "second CV body\n"))):
            cv = tmp_path / directory / "cv.txt"
            cv.parent.mkdir()
            cv.write_text(content)
            # The second run reuses the identical experience, so only its three
            # facts are markable.
            marks = ["1a", "2a 3a 4a"] if index == 0 else ["1a 2a 3a"]
            run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                           ask=_scripted(marks + ["", "", "", "", "", "", "", ""]),
                           say=lambda _: None)
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
        "python backend",              # capability (no strength question, OC-40)
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
        assert capability.strength == "unrated"

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


def test_capability_ends_with_an_eligible_chain_without_any_link_question(
        instance, tmp_path):
    """The CV-to-capability question is gone (OC-39): creating a capability
    still mints its self-assessed fact, the interview evidence row, and the
    PROVES plus SUPPORTS edges, so the capability is packageable from the
    user's own assertion alone and no prompt is spent on a blanket CV link."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\nBackend Engineer at Acme 2021-2023\n")
    answers = [
        "1a", "2a 3a 4a", "", "",                    # review, then two number groups
        "Backend service design",                    # capability (no strength)
        "",                                          # capabilities done
        "",                                          # goals: none
        "", "", "", "",                              # profile basics skipped
    ]
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return answers.pop(0)

    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=ask, say=lambda _: None)
        assert not any("as supporting" in p for p in prompts)  # question deleted
        # No strength question either (OC-40): the capability is stored unrated
        # and what it rests on is computed from the graph.
        assert not any("Strength" in p for p in prompts)
        capability = SqliteCapabilityRepository(conn).get_by_name("Backend service design")
        assert capability.strength == "unrated"
        edges = SqliteCareerEdgeRepository(conn)
        supports = edges.active_edges_to("capability", capability.id, "SUPPORTS")
        assert len(supports) == 1  # the interview self-assessment, and only it
        interview = [e for e in SqliteEvidenceRepository(conn).list_all()
                     if e.evidence_type == "user_statement"][0]
        assert supports[0].source_id == interview.id
        # The chain capability <- SUPPORTS <- evidence -> PROVES -> approved fact
        # is complete, which is what makes the capability packageable.
        proven = edges.active_edges_from("evidence", interview.id, "PROVES")
        facts_repo = SqliteCareerFactRepository(conn)
        assert [facts_repo.get(e.target_id).statement for e in proven] == [
            "Self-assessed capability: Backend service design"]
        assert facts_repo.get(proven[0].target_id).user_approved == 1
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

    answers = ["1a", "2a 3a 4a", "", "", "", "", "", "", "", ""]
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

    answers = ["1a", "2a 3a 4a", "", "", "", "", "", "", "", ""]
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
        "banana", "1a",                 # an unparseable mark re-asks with a reason
                                        # (this CV has no facts, so no phase two)
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
        assert "'banana' is not an item number or a range" in joined
        assert "nothing was recorded from that line" in joined
        assert "invalid value: 'not-an-email' does not look like an email address" in joined
        assert SqliteUserProfileRepository(conn).get_fields() == {"email": "jane@example.com"}
    finally:
        conn.close()


class RaisingModel(ModelAdapter):
    """The resume paths must never call the model; this one proves it."""

    def complete(self, prompt: str) -> str:
        raise AssertionError("the model was called on a resume path")


def _dies_between_batches(*args, **kwargs):
    """The one window that leaves persisted experiences and no draft facts:
    the process dying between the two batch writes of a completed experience
    phase. Patched over _persist_fact_drafts to reproduce that state."""
    raise KeyboardInterrupt


def _review_listing(says):
    """Just the lines the review surface rendered, from its header to the mark
    instruction. A later numbers group may legitimately mention a fact that the
    surface must not have re-listed, so the two are asserted separately."""
    start = next(i for i, line in enumerate(says) if "Extraction review:" in line)
    end = next(i for i, line in enumerate(says) if "Every item needs a mark" in line)
    return "\n".join(says[start:end + 1])


def _interrupting(answers):
    remaining = list(answers)

    def ask(_prompt: str) -> str:
        if not remaining:
            raise KeyboardInterrupt
        return remaining.pop(0)

    return ask


def test_onboard_resume_confirms_pending_drafts_without_the_model(instance, tmp_path):
    """A re-run with the same CV bytes and pending cv-source drafts skips
    storage, extraction, and the experience review, and resumes over exactly
    the drafts still unmarked (OC-36; resume derived from data, never a stored
    cursor)."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\nBackend Engineer at Acme 2021-2023\n")
    conn = _conn(instance)
    try:
        # First run: every item marked, then interrupted at item 3's
        # replacement text, so items 3 and 4 never got applied.
        with pytest.raises(KeyboardInterrupt):
            run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                           ask=_interrupting(["1a", "2a"]), say=lambda _: None)
        drafts = [f for f in SqliteCareerFactRepository(conn).list_all()
                  if f.source == "cv" and not f.user_approved]
        assert len(drafts) == 2

        says = []
        answers = [
            "1a 2a",                                      # the two unmarked drafts
            "", "",                                       # both number groups skipped
            "", "",                                       # capabilities, goals: none
            "", "", "", "",                               # profile basics skipped
        ]
        run_onboarding(conn, LocalStorageAdapter(instance), RaisingModel(), cv,
                       ask=_scripted(answers), say=says.append)

        assert any("resuming" in line for line in says)
        # The surface renders only what is still unmarked: the fact applied in
        # the first run is not listed again (it may still appear later, in its
        # experience's number group, which is a different surface).
        listing = _review_listing(says)
        assert "2 draft facts still" in listing
        assert "Built the order service" not in listing
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


def test_onboard_resume_with_review_complete_skips_straight_to_gap_questions(
        instance, tmp_path):
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\nBackend Engineer at Acme 2021-2023\n")
    conn = _conn(instance)
    try:
        first = ["1a", "2a 3a 4a", "", "",
                 "", "", "", "", "", ""]
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=_scripted(first), say=lambda _: None)
        facts_before = {f.id for f in SqliteCareerFactRepository(conn).list_all()}

        says = []
        run_onboarding(conn, LocalStorageAdapter(instance), RaisingModel(), cv,
                       ask=_scripted(["", "", "", "", "", ""]), say=says.append)

        assert any("review is complete" in line for line in says)
        assert len([e for e in SqliteEvidenceRepository(conn).list_all()
                    if e.evidence_type == "cv"]) == 1
        assert len(SqliteExperienceRepository(conn).list_all()) == 1
        assert {f.id for f in SqliteCareerFactRepository(conn).list_all()} == facts_before
    finally:
        conn.close()


def test_onboard_resume_scopes_to_the_matching_cvs_drafts(instance, tmp_path):
    """Pending drafts from two different CVs: resuming one CV reviews only
    its own drafts (origin_evidence_id scoping) and never touches the other's."""
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
                               ask=_interrupting(["1a", "2a"]), say=lambda _: None)
        cv_rows = {e.content_hash: e for e in SqliteEvidenceRepository(conn).list_all()
                   if e.evidence_type == "cv"}
        assert len(cv_rows) == 2
        a_row = cv_rows[hashlib.sha256(cvs["a"].read_bytes()).hexdigest()]
        others_pending = {f.id for f in SqliteCareerFactRepository(conn).list_all()
                          if f.source == "cv" and not f.user_approved
                          and f.origin_evidence_id != a_row.id}
        assert others_pending

        answers = ["1a 2a", "", "",
                   "", "", "", "", "", ""]
        run_onboarding(conn, LocalStorageAdapter(instance), RaisingModel(), cvs["a"],
                       ask=_scripted(answers), say=lambda _: None)

        by_id = {f.id: f for f in SqliteCareerFactRepository(conn).list_all()}
        for fact in by_id.values():
            if fact.source == "cv" and fact.origin_evidence_id == a_row.id:
                assert fact.user_approved == 1
        for fact_id in others_pending:  # the other CV's drafts are untouched
            assert by_id[fact_id].user_approved == 0
            assert by_id[fact_id].status == "active"
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
                               ask=_interrupting(["1a", "2a"]), say=lambda _: None)
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
                           ask=_interrupting(["1a", "2a"]), say=lambda _: None)
        with conn:
            conn.execute("UPDATE career_facts SET origin_evidence_id = NULL")
        answers = ["1a 2a", "", "",
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

        answers = ["1a", "2a 3a 4a", "", "",
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
    """An evidence row with zero originated facts means the review died before
    drafts persisted: the re-run re-extracts rather than declaring the review
    complete."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    conn = _conn(instance)
    try:
        # Interrupt at the marks prompt: evidence row persisted
        # (extraction succeeded) but no draft facts yet.
        with pytest.raises(KeyboardInterrupt):
            run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                           ask=_interrupting([]), say=lambda _: None)
        assert len([e for e in SqliteEvidenceRepository(conn).list_all()
                    if e.evidence_type == "cv"]) == 1
        assert [f for f in SqliteCareerFactRepository(conn).list_all()
                if f.source == "cv"] == []

        answers = ["1a", "2a 3a 4a", "", "",
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


def test_interrupt_mid_review_never_duplicates_confirmed_experiences(
        instance, tmp_path, monkeypatch):
    """The cross-run safety net (OC-36): a run that persisted its experiences
    and then died before the draft batch re-extracts on the next run, reuses
    the exactly-matching confirmed experience without listing it as markable,
    and asks only for the remaining items. The death between the two writes is
    the only window that leaves that state, so it is what the test injects."""
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
        # Experience one accepted, experience two rejected, then the process
        # dies between the experience batch and the draft batch.
        monkeypatch.setattr("apps.cli.onboarding._persist_fact_drafts",
                            _dies_between_batches)
        with pytest.raises(KeyboardInterrupt):
            run_onboarding(conn, LocalStorageAdapter(instance), TwoExperienceModel(), cv,
                           ask=_scripted(["1a 3r"]), say=lambda _: None)
        monkeypatch.undo()
        assert len(SqliteExperienceRepository(conn).list_all()) == 1
        assert SqliteCareerFactRepository(conn).list_all() == []

        # Re-run: the confirmed experience carries no index (it is not asked
        # about again), so the three markable items are its fact, experience
        # two, and experience two's fact.
        answers = [
            "2a",                          # experience two, asked again
            "1a 3a",                       # then both experiences' facts
            "", "",                        # both number groups skipped
            "", "",                        # capabilities, goals
            "", "", "", "",                # basics skipped
        ]
        says = []
        run_onboarding(conn, LocalStorageAdapter(instance), TwoExperienceModel(), cv,
                       ask=_scripted(answers), say=says.append)
        assert any("already confirmed earlier; reusing it" in line for line in says)
        # The disclosure describes what was actually found: one experience row
        # survived and is reused, the other never persisted and is re-asked.
        joined = "\n".join(says)
        assert ("the experiences that review had already stored are reused here"
                " and carry no mark number; the ones it never stored are asked"
                " again") in joined
        assert "were not recorded and are asked again" not in joined
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
        instance, tmp_path, monkeypatch):
    """A CV can carry the same role twice (same kind, title, org, dates): the
    replay consumes each persisted match at most once, so the second
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
        # Accept the first identical entry, reject the second, then die
        # between the experience batch and the draft batch.
        monkeypatch.setattr("apps.cli.onboarding._persist_fact_drafts",
                            _dies_between_batches)
        with pytest.raises(KeyboardInterrupt):
            run_onboarding(conn, LocalStorageAdapter(instance), RepeatedRoleModel(), cv,
                           ask=_scripted(["1a 3r"]), say=lambda _: None)
        monkeypatch.undo()
        assert len(SqliteExperienceRepository(conn).list_all()) == 1

        # Replay: entry one reuses the persisted row, entry two is asked (the
        # scripted marks prove the ask happened by alignment).
        answers = [
            "2a",                          # entry two, marked first
            "1a 3a",                       # then both entries' facts
            "", "",                        # both number groups skipped
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


def test_review_interrupt_never_strands_an_approved_fact_without_its_edge(
        instance, tmp_path):
    """The PROVES edge lands with the approval, before any numbers ask, and the
    resume path repairs legacy approved facts missing their link without
    duplicating edges that exist."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    conn = _conn(instance)
    edges_repo = SqliteCareerEdgeRepository(conn)
    try:
        # Item 2 is approved (with its edge) before item 3's replacement
        # text is asked; the interrupt lands there.
        with pytest.raises(KeyboardInterrupt):
            run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                           ask=_interrupting(["1a", "2a"]), say=lambda _: None)
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

        # Resume: the two remaining drafts are reviewed, the missing edge is
        # repaired, and no fact ends up with duplicates.
        answers = ["1a 2a", "", "",
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
    try:
        for index, (name, content) in enumerate(
                (("cv.txt", "first body\n"), ("cv2.txt", "second body\n"))):
            cv = tmp_path / name
            cv.write_text(content)
            # The second CV re-extracts the identical experience, which is
            # reused, leaving only its three facts markable.
            marks = ["1a", "2a 3a 4a"] if index == 0 else ["1a 2a 3a"]
            run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                           ask=_scripted(marks + ["", "", "", "", "", "", "", ""]),
                           say=lambda _: None)
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
        if prompt.startswith("Marks for items 1:"):
            return "1a"
        return "2a 3a 4a" if prompt.startswith("Marks for items") else ""

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
    answers = {"Marks for items 1:": "1a", "Marks for items": "2a 3a 4a",
               "Start date": "September 2015"}
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


# --- the review surface's own rules (OC-39) --------------------------------


def test_every_item_needs_a_mark_before_the_review_completes(instance, tmp_path):
    """There is no approve-the-remainder default: the surface keeps asking
    while anything is unmarked, and an item is not approved until its own mark
    lands."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    prompts = []
    answers = [
        "1a",              # the experience, phase one
        "2a",              # one fact
        "3r 4a",           # the rest
        "",                # numbers, the experience's group
        "",                # numbers, the no-experience group
        "", "",            # capabilities, goals
        "", "", "", "",    # basics
    ]

    def ask(prompt):
        prompts.append(prompt)
        return answers.pop(0)

    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=ask, say=lambda _: None)
        mark_prompts = [p for p in prompts if p.startswith("Marks for items")]
        assert mark_prompts == [
            "Marks for items 1: ",
            "Marks for items 2, 3, 4: ",
            "Marks for items 3, 4: ",
        ]
        facts = {f.statement: f for f in SqliteCareerFactRepository(conn).list_all()}
        assert facts["Led the platform team"].status == "retracted"
        assert facts["Wrote Python daily"].user_approved == 1
    finally:
        conn.close()


def test_an_unparseable_marks_line_consumes_nothing_and_re_asks(instance, tmp_path):
    """A line is taken whole or not at all: one bad token discards the whole
    line, so the marks beside it are never silently recorded."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    prompts = []
    answers = [
        "1a 9r",           # item 9 does not exist: nothing recorded
        "1a 2x",           # 'x' is not a mark: nothing recorded
        "1a",              # the experience
        "2a 3a 4a",        # its facts and the loose one
        "", "",            # both number groups skipped
        "", "",            # capabilities, goals
        "", "", "", "",    # basics
    ]

    def ask(prompt):
        prompts.append(prompt)
        return answers.pop(0)

    says = []
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=ask, say=says.append)
        # Item 1 is still waiting after each rejected line: nothing consumed.
        assert [p for p in prompts if p.startswith("Marks for items")] == [
            "Marks for items 1: "] * 3 + ["Marks for items 2, 3, 4: "]
        joined = "\n".join(says)
        assert "item 9 is not waiting for a mark" in joined
        assert "'2x' does not end in a mark" in joined
        assert SqliteExperienceRepository(conn).list_all()  # one experience, once
        assert len(SqliteExperienceRepository(conn).list_all()) == 1
    finally:
        conn.close()


NUMBERS_EXTRACTION = json.dumps({
    "experiences": [
        {"kind": "role", "title": "Backend Engineer", "org": "Acme",
         "start_date": "2021", "end_date": "2023", "summary": None},
        {"kind": "role", "title": "Analyst", "org": "Beta",
         "start_date": "2019", "end_date": "2021", "summary": None},
    ],
    "facts": [
        {"experience_index": 0, "fact_type": "achievement",
         "statement": "Built the order service", "source_location": None},
        {"experience_index": 0, "fact_type": "scope",
         "statement": "Led the platform team", "source_location": None},
        {"experience_index": 1, "fact_type": "achievement",
         "statement": "Rebuilt the reporting pipeline", "source_location": None},
        {"experience_index": None, "fact_type": "skill_use",
         "statement": "Wrote Python daily", "source_location": None},
    ],
})


class NumbersModel(ModelAdapter):
    def complete(self, prompt: str) -> str:
        return NUMBERS_EXTRACTION


def test_numbers_are_asked_once_per_experience_and_addressed_by_index(
        instance, tmp_path):
    """One ask per experience, restatements addressed by index, skip the
    default for everything not addressed, and facts with no experience get
    their own final group."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    prompts = []
    answers = [
        "1a 4a",                               # both experiences
        "2a 3a 5a 6a",                         # then every fact
        "2: Led a platform team of 6", "",     # first experience: item 2 only
        "",                                    # second experience: skipped
        "Wrote Python daily for 8 years", "",  # invalid form, then blank
        "", "",                                # capabilities, goals
        "", "", "", "",                        # basics
    ]

    def ask(prompt):
        prompts.append(prompt)
        return answers.pop(0)

    says = []
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), NumbersModel(), cv,
                       ask=ask, say=says.append)
        joined = "\n".join(says)
        assert "Backend Engineer @ Acme:" in joined
        assert "Analyst @ Beta:" in joined
        assert "Facts with no experience:" in joined
        assert "expected '<n>: statement'; nothing changed" in joined
        facts = {f.statement: f for f in SqliteCareerFactRepository(conn).list_all()}
        # The restatement landed on item 2 of its group, and only there.
        assert "Led a platform team of 6" in facts
        assert facts["Led a platform team of 6"].user_approved == 1
        assert "Built the order service" in facts
        assert "Rebuilt the reporting pipeline" in facts
        assert "Wrote Python daily" in facts
    finally:
        conn.close()


def test_a_restatement_without_a_number_never_overwrites_the_fact(instance, tmp_path):
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    answers = [
        "1a", "2a 3a 4a",
        "1: confirm", "",   # no number in it: refused, then the group is left
        "",                 # the no-experience group
        "", "",             # capabilities, goals
        "", "", "", "",     # basics
    ]
    says = []
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=_scripted(answers), say=says.append)
        assert any("has no number in it either" in line for line in says)
        statements = {f.statement for f in SqliteCareerFactRepository(conn).list_all()}
        assert "Built the order service" in statements
        assert "confirm" not in statements
    finally:
        conn.close()


def test_fact_marks_are_durable_the_moment_they_land(instance, tmp_path):
    """A partial fact-marks line is stored as it lands, not buffered to the end
    of the review: an interrupt right after it resumes over exactly the facts
    that never got a mark, with the approvals and PROVES edges of the marked
    ones already in place."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\nBackend Engineer at Acme 2021-2023\n")
    conn = _conn(instance)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                           ask=_interrupting(["1a", "2a 3r"]), say=lambda _: None)
        facts = {f.statement: f for f in SqliteCareerFactRepository(conn).list_all()}
        approved = facts["Built the order service"]
        assert approved.user_approved == 1 and approved.status == "active"
        assert facts["Led the platform team"].status == "retracted"
        assert facts["Wrote Python daily"].user_approved == 0
        cv_row = [e for e in SqliteEvidenceRepository(conn).list_all()
                  if e.evidence_type == "cv"][0]
        edges = SqliteCareerEdgeRepository(conn).active_edges_from(
            "evidence", cv_row.id, "PROVES")
        assert [e.target_id for e in edges] == [approved.id]  # edge already durable

        says = []
        # Two number groups now: the fact approved before the interrupt gets
        # the layer-1 ask it never reached, alongside the newly reviewed one.
        answers = ["1a", "", "", "", "", "", "", "", ""]
        run_onboarding(conn, LocalStorageAdapter(instance), RaisingModel(), cv,
                       ask=_scripted(answers), say=says.append)
        listing = _review_listing(says)
        assert "1 draft facts still" in listing
        assert "Built the order service" not in listing  # never re-listed
        assert "Led the platform team" not in listing
        assert len([e for e in SqliteEvidenceRepository(conn).list_all()
                    if e.evidence_type == "cv"]) == 1  # never re-extracted
    finally:
        conn.close()


def test_a_review_that_rejected_everything_resumes_as_complete(instance, tmp_path):
    """The all-reject case: no draft survives, so the surviving-facts reasoning
    cannot tell this apart from an extraction whose drafts never landed. The
    evidence row's review_completed_at settles it, and the re-run never
    re-extracts (migration 0008)."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\nBackend Engineer at Acme 2021-2023\n")
    conn = _conn(instance)
    try:
        answers = ["1r",              # the experience, cascading to facts 2 and 3
                   "4r",              # the loose fact
                   "", "", "", "", "", ""]
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=_scripted(answers), say=lambda _: None)
        assert SqliteExperienceRepository(conn).list_all() == []
        assert all(f.status == "retracted"
                   for f in SqliteCareerFactRepository(conn).list_all())
        cv_rows = [e for e in SqliteEvidenceRepository(conn).list_all()
                   if e.evidence_type == "cv"]
        assert len(cv_rows) == 1 and cv_rows[0].review_completed_at

        says = []
        run_onboarding(conn, LocalStorageAdapter(instance), RaisingModel(), cv,
                       ask=_scripted(["", "", "", "", "", ""]), say=says.append)
        assert any("review is complete" in line for line in says)
        assert len([e for e in SqliteEvidenceRepository(conn).list_all()
                    if e.evidence_type == "cv"]) == 1  # no second row
    finally:
        conn.close()


def test_an_extraction_with_no_facts_at_all_resumes_as_complete(instance, tmp_path):
    """A CV whose extraction yielded experiences and no facts completes its
    review with nothing to attribute; the re-run must not re-extract it."""
    extraction = json.dumps({
        "experiences": [{"kind": "role", "title": "Eng", "org": "Acme",
                         "start_date": "2021", "end_date": "2023", "summary": None}],
        "facts": [],
    })

    class NoFactsModel(ModelAdapter):
        def complete(self, prompt: str) -> str:
            return extraction

    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), NoFactsModel(), cv,
                       ask=_scripted(["1a", "", "", "", "", "", ""]),
                       say=lambda _: None)
        says = []
        run_onboarding(conn, LocalStorageAdapter(instance), RaisingModel(), cv,
                       ask=_scripted(["", "", "", "", "", ""]), say=says.append)
        assert any("review is complete" in line for line in says)
        assert len([e for e in SqliteEvidenceRepository(conn).list_all()
                    if e.evidence_type == "cv"]) == 1
        assert len(SqliteExperienceRepository(conn).list_all()) == 1
    finally:
        conn.close()


def test_fact_marks_typed_before_the_experiences_are_not_recorded(instance, tmp_path):
    """Phase rule: a fact mark typed while an experience is still unmarked is
    refused and said so, because a rejected experience takes its facts with it.
    The experience marks in the same line are recorded."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    prompts = []
    answers = [
        "1a 2a 3a",        # the experience lands, the two fact marks do not
        "2a 3a 4a",        # phase two asks for every fact, item 2 included
        "", "",            # both number groups
        "", "",            # capabilities, goals
        "", "", "", "",    # basics
    ]

    def ask(prompt):
        prompts.append(prompt)
        return answers.pop(0)

    says = []
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=ask, say=says.append)
        assert any("items 2, 3: facts are marked after every experience is"
                   in line for line in says)
        assert [p for p in prompts if p.startswith("Marks for items")] == [
            "Marks for items 1: ", "Marks for items 2, 3, 4: "]
        assert all(f.user_approved == 1
                   for f in SqliteCareerFactRepository(conn).list_all())
    finally:
        conn.close()


def test_rejecting_an_experience_and_marking_its_fact_in_one_line(instance, tmp_path):
    """Regression: the cascade removes the fact from the deferred set, so a
    line naming both used to treat the fact as an experience index and crash.
    The cascade wins, and the fact mark in that line is refused as too early."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    answers = [
        "1r 2a",           # experience rejected, its fact marked in the same line
        "4a",              # only the loose fact remains
        "",                # the no-experience number group
        "", "",            # capabilities, goals
        "", "", "", "",    # basics
    ]
    says = []
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=_scripted(answers), say=says.append)
        joined = "\n".join(says)
        assert "its facts are rejected with it: 2, 3" in joined
        assert "item 2: facts are marked after every experience is" in joined
        assert SqliteExperienceRepository(conn).list_all() == []
        # The accept in that line never landed: the fact is gone with its
        # experience, not persisted as approved.
        assert [f.statement for f in SqliteCareerFactRepository(conn).list_all()] == [
            "Wrote Python daily"]
    finally:
        conn.close()


def test_a_range_crossing_an_experience_and_its_own_fact(instance, tmp_path):
    """'1-2r' names the experience and one of its facts: same crash path, and
    the range must resolve the same way as the two tokens written out."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    answers = ["1-2r", "4r", "", "", "", "", "", ""]
    says = []
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=_scripted(answers), say=says.append)
        assert "item 2: facts are marked after every experience is" in "\n".join(says)
        assert SqliteExperienceRepository(conn).list_all() == []
        assert all(f.status == "retracted"
                   for f in SqliteCareerFactRepository(conn).list_all())
    finally:
        conn.close()


def test_a_range_crossing_into_another_experiences_facts(instance, tmp_path):
    """A range spanning both experiences and the facts between them: every
    experience mark in it lands, every fact index in it is refused, and the
    cascades are applied without a crash."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\n")
    answers = [
        "1-5r",            # experiences 1 and 4 rejected, facts 2, 3, 5 refused
        "6a",              # the loose fact is the only survivor
        "",                # its number group
        "", "",            # capabilities, goals
        "", "", "", "",    # basics
    ]
    says = []
    conn = _conn(instance)
    try:
        run_onboarding(conn, LocalStorageAdapter(instance), NumbersModel(), cv,
                       ask=_scripted(answers), say=says.append)
        joined = "\n".join(says)
        assert "items 2, 3, 5: facts are marked after every experience is" in joined
        assert "its facts are rejected with it: 2, 3" in joined
        assert "its facts are rejected with it: 5" in joined
        assert SqliteExperienceRepository(conn).list_all() == []
        facts = SqliteCareerFactRepository(conn).list_all()
        assert [(f.statement, f.user_approved) for f in facts] == [
            ("Wrote Python daily", 1)]
    finally:
        conn.close()


def test_an_interrupted_experience_phase_says_it_is_asking_again(instance, tmp_path):
    """Experience decisions become durable only when the phase completes: an
    interrupt inside it stores nothing, and the re-run says the marks are being
    asked again rather than silently re-listing them as if untouched."""
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
        # Reject experience one, then die at the prompt for experience three.
        with pytest.raises(KeyboardInterrupt):
            run_onboarding(conn, LocalStorageAdapter(instance), TwoExperienceModel(), cv,
                           ask=_interrupting(["1r"]), say=lambda _: None)
        # Nothing from the lost phase was applied: no experience row for
        # either draft, and no fact ever persisted.
        assert SqliteExperienceRepository(conn).list_all() == []
        assert SqliteCareerFactRepository(conn).list_all() == []

        says = []
        answers = ["1a 3a", "2a 4a", "", "", "", "", "", "", "", ""]
        run_onboarding(conn, LocalStorageAdapter(instance), TwoExperienceModel(), cv,
                       ask=_scripted(answers), say=says.append)
        joined = "\n".join(says)
        assert ("experience marks from the interrupted review were not"
                " recorded and are asked again here") in joined
        assert "Fact decisions you already made are durable" in joined
        # Both experiences are rendered as markable again, the rejected one
        # included: the user re-decides, nothing decides for them.
        assert "1. [role] Backend Engineer @ Acme" in joined
        assert "3. [project] Side Tool @ Self" in joined
        assert len(SqliteExperienceRepository(conn).list_all()) == 2
    finally:
        conn.close()


def test_an_accepted_experience_is_not_persisted_until_the_phase_completes(
        instance, tmp_path):
    """The re-ask the surface promises has to be real: an experience accepted
    before an interrupt writes nothing, so the resume re-renders it and a
    reject on the second pass genuinely rejects it, leaving no orphan row that
    the first accept had already created."""
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
        # Accept experience one, die at the prompt for experience two.
        with pytest.raises(KeyboardInterrupt):
            run_onboarding(conn, LocalStorageAdapter(instance), TwoExperienceModel(), cv,
                           ask=_interrupting(["1a"]), say=lambda _: None)
        assert SqliteExperienceRepository(conn).list_all() == []
        assert SqliteCareerFactRepository(conn).list_all() == []

        # Second pass: the same item is re-rendered as markable, and rejecting
        # it now actually rejects it.
        says = []
        answers = ["1r 3a", "4a", "", "", "", "", "", "", ""]
        run_onboarding(conn, LocalStorageAdapter(instance), TwoExperienceModel(), cv,
                       ask=_scripted(answers), say=says.append)
        joined = "\n".join(says)
        assert "1. [role] Backend Engineer @ Acme" in joined
        experiences = SqliteExperienceRepository(conn).list_all()
        assert [e.title for e in experiences] == ["Side Tool"]  # no orphan row
        facts = SqliteCareerFactRepository(conn).list_all()
        assert [f.statement for f in facts] == ["Shipped the side tool"]
    finally:
        conn.close()


def test_pre_0008_evidence_with_no_facts_re_extracts_rather_than_completing(
        instance, tmp_path):
    """The documented conservative fallback: for a row predating migration
    0008, a review that produced no surviving facts is indistinguishable from
    one interrupted before its drafts landed, so it re-extracts. No backfill
    invents a completion that was never recorded."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\nBackend Engineer at Acme 2021-2023\n")
    conn = _conn(instance)
    try:
        answers = ["1r", "4r", "", "", "", "", "", ""]  # everything rejected
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=_scripted(answers), say=lambda _: None)
        # Reshape the row as pre-0008 data: no completion stamp, and the
        # retracted rows that a legacy build would not have left behind.
        with conn:
            conn.execute("UPDATE evidence SET review_completed_at = NULL")
            conn.execute("DELETE FROM career_facts")
        says = []
        answers = ["1a", "2a 3a 4a", "", "", "", "", "", "", "", ""]
        run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                       ask=_scripted(answers), say=says.append)
        assert any("never landed" in line for line in says)  # re-extracted
        cv_rows = [e for e in SqliteEvidenceRepository(conn).list_all()
                   if e.evidence_type == "cv"]
        assert len(cv_rows) == 2  # the fresh ingest, the legacy row as residue
        assert all(f.user_approved == 1
                   for f in SqliteCareerFactRepository(conn).list_all())
    finally:
        conn.close()


def test_a_resume_asks_numbers_for_facts_approved_before_the_interrupt(
        instance, tmp_path):
    """A fact approved before the interrupt never reached its layer-1 number
    ask, and the completion stamp the resume writes would make that permanent.
    The resume asks for it too, in its experience's group, and a restatement
    lands on it."""
    cv = tmp_path / "cv.txt"
    cv.write_text("Jane Placeholder\nBackend Engineer at Acme 2021-2023\n")
    conn = _conn(instance)
    try:
        # Item 2 accepted (unquantified), then the process dies before its
        # numbers ask could ever run.
        with pytest.raises(KeyboardInterrupt):
            run_onboarding(conn, LocalStorageAdapter(instance), OneShotModel(), cv,
                           ask=_interrupting(["1a", "2a"]), say=lambda _: None)
        approved = [f for f in SqliteCareerFactRepository(conn).list_all()
                    if f.user_approved]
        assert [f.statement for f in approved] == ["Built the order service"]

        says = []
        answers = [
            "1a 2a",                                       # the two pending drafts
            "1: Built the order service, 200 requests/second",  # the earlier fact
            "",                                            # done with that group
            "",                                            # the no-experience group
            "", "",                                        # capabilities, goals
            "", "", "", "",                                # basics
        ]
        run_onboarding(conn, LocalStorageAdapter(instance), RaisingModel(), cv,
                       ask=_scripted(answers), say=says.append)
        joined = "\n".join(says)
        # It is listed as item 1 of its experience's group, ahead of the fact
        # reviewed in this run.
        assert "  1. [achievement] Built the order service" in joined
        assert "  2. [scope] Led the platform team" in joined
        statements = {f.statement for f in SqliteCareerFactRepository(conn).list_all()}
        assert "Built the order service, 200 requests/second" in statements
        assert "Built the order service" not in statements  # superseded, not doubled
    finally:
        conn.close()
