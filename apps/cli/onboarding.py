"""CV-first onboarding (cold-start contract in decisions/career-graph-schema.md).

One flow: CV upload and extraction first, then the interview as a
confirmation-and-gaps pass. Extracted facts are born user_approved=0; the
confirm/edit step is where CV-parsing loss is caught. Without a CV the flow
degrades to the same gap questions from a blank slate. Interactive I/O goes
through the ask/say seams so tests drive the flow with scripted answers.
"""

import hashlib
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Callable

from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteCareerFactRepository,
    SqliteCareerGoalRepository,
    SqliteEvidenceRepository,
    SqliteExperienceRepository,
)
from adapters.storage.sqlite_profile import SqliteUserProfileRepository
from domain.edges import CareerEdge
from domain.entities import Capability, CareerFact, CareerGoal, Evidence, Experience
from domain.extraction import CvExtraction, CvExtractionService
from domain.ids import new_id
from domain.ports import ModelAdapter, StorageAdapter
from apps.cli.interview import ask_yes_no, offer_quantifier
from domain.profile import InvalidProfileValueError
from prompts import load_prompt

class CvReadError(RuntimeError):
    """The CV file's text could not be extracted (e.g. a PDF with no
    pdftotext available). Callers degrade like other extraction failures."""


_STRENGTHS = ("none", "weak", "moderate", "strong")
_HORIZONS = ("near", "mid", "long")
_PROFILE_BASICS = ("full_name", "email", "phone", "location")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ask_choice(ask: Callable[[str], str], say: Callable[[str], None], prompt: str,
                choices: tuple[str, ...], default: str) -> str:
    while True:
        answer = ask(f"{prompt} ({'/'.join(choices)}) [{default}]: ").strip().lower()
        if not answer:
            return default
        if answer in choices:
            return answer
        say(f"invalid choice, expected {'/'.join(choices)}")


def run_onboarding(conn: sqlite3.Connection, storage: StorageAdapter,
                   model: ModelAdapter | None, cv_path: Path | None,
                   ask: Callable[[str], str] | None = None,
                   say: Callable[[str], None] = print) -> None:
    ask = ask or input  # resolved at call time so tests can patch builtins.input
    facts_repo = SqliteCareerFactRepository(conn)
    edges_repo = SqliteCareerEdgeRepository(conn)
    evidence_repo = SqliteEvidenceRepository(conn)

    if cv_path is not None:
        if model is None:
            raise ValueError("onboarding with a CV needs a ModelAdapter for extraction")
        # Resume is derived from the data, never a stored cursor (OC-22): the
        # CV bytes' hash finding an existing cv evidence row means this exact
        # file was already ingested, so extraction (the only model call) and
        # the experience walk never re-run; only still-pending drafts are
        # walked again.
        cv_bytes = cv_path.read_bytes()
        content_hash = hashlib.sha256(cv_bytes).hexdigest()
        cv_rows = [e for e in SqliteEvidenceRepository(conn).list_all()
                   if e.evidence_type == "cv"]
        matches = [e for e in cv_rows if e.content_hash == content_hash]
        # Deterministic pick when the same bytes were somehow ingested twice:
        # the most recent row wins (created_at, id as the tiebreak).
        existing = max(matches, key=lambda e: (e.created_at or "", e.id),
                       default=None)
        if existing is not None:
            cv_facts = [f for f in facts_repo.list_all() if f.source == "cv"]
            null_pending = [f for f in cv_facts
                            if f.origin_evidence_id is None
                            and not f.user_approved and f.status == "active"]
            # Drafts predating migration 0005 carry no origin: with exactly
            # one cv evidence row they can only be its; with several, guessing
            # could hand one CV's drafts to another's walk, so refuse.
            if null_pending and len(cv_rows) > 1:
                raise ValueError(
                    "cannot resume: pending draft facts carry no CV provenance"
                    " and several CV evidence rows exist; approve or retract"
                    " them via a fresh review before re-running")
            attributed = [f for f in cv_facts
                          if f.origin_evidence_id == existing.id
                          or (f.origin_evidence_id is None and len(cv_rows) == 1)]
            if not attributed:
                # An evidence row with no facts at all means the earlier walk
                # died before drafts persisted: nothing to resume, so fall
                # through to a fresh extraction (the orphaned older row is
                # acceptable residue).
                say(f"This CV was stored earlier ({existing.title}) but its"
                    " draft facts never landed; re-extracting.")
                existing = None
            else:
                # Repair pass for data approved before the edge write moved
                # ahead of the quantifier prompt: an approved active fact
                # attributed here but lacking its active PROVES edge from
                # this evidence row gets the missing link minted.
                proven = {e.target_id for e in edges_repo.active_edges_from(
                    "evidence", existing.id, "PROVES")}
                unlinked = [f for f in attributed
                            if f.user_approved and f.status == "active"
                            and f.id not in proven]
                for fact in unlinked:
                    edges_repo.add(CareerEdge(
                        id=new_id("edge"), source_type="evidence",
                        source_id=existing.id, edge_type="PROVES",
                        target_type="career_fact", target_id=fact.id,
                        claim_kind="fact", provenance="onboarding:cv-confirmation",
                        created_by="user", user_verified=1,
                    ))
                if unlinked:
                    say(f"Repaired {len(unlinked)} approved facts that were"
                        " missing their evidence link.")
                pending = [f.id for f in attributed
                           if not f.user_approved and f.status == "active"]
                if pending:
                    say(f"This CV was already ingested ({existing.title}); resuming"
                        f" the confirmation walk over {len(pending)} pending draft facts.")
                    _confirm_drafts(facts_repo, edges_repo, existing, pending, ask, say)
                else:
                    say(f"This CV was already ingested ({existing.title}) and its"
                        " walk is complete; continuing with the remaining questions.")
        if existing is None:
            cv_evidence, extraction = _ingest_cv(conn, storage, model, cv_path,
                                                 cv_bytes, content_hash, say)
            experience_ids = _review_experiences(conn, extraction, ask, say)
            draft_fact_ids = _persist_fact_drafts(conn, extraction, experience_ids,
                                                  cv_evidence, say)
            _confirm_drafts(facts_repo, edges_repo, cv_evidence, draft_fact_ids, ask, say)
    else:
        say("No CV given; starting from the interview questions.")

    _gap_questions(conn, evidence_repo, facts_repo, edges_repo, ask, say)
    say("Onboarding complete.")


def _read_cv_text(cv_path: Path, cv_bytes: bytes) -> str:
    """Plain text reads directly; a PDF (by suffix or %PDF magic) goes through
    the pdftotext binary. Failure is a CvReadError with an actionable message."""
    if cv_path.suffix.lower() != ".pdf" and not cv_bytes.startswith(b"%PDF"):
        return cv_bytes.decode()
    try:
        proc = subprocess.run(["pdftotext", str(cv_path), "-"], capture_output=True)
    except FileNotFoundError as e:
        raise CvReadError(
            "pdftotext not found; install poppler-utils, or supply a text CV") from e
    try:
        # pdftotext emits UTF-8 by default; decode explicitly (never the process
        # locale) so stray bytes become replacement characters, not a crash.
        text = proc.stdout.decode("utf-8", errors="replace")
    except Exception as e:
        raise CvReadError(
            f"pdftotext output from {cv_path.name} could not be decoded; supply a text CV") from e
    if proc.returncode != 0 or not text.strip():
        raise CvReadError(
            f"pdftotext extracted no text from {cv_path.name}; supply a text CV")
    return text


def _ingest_cv(conn: sqlite3.Connection, storage: StorageAdapter, model: ModelAdapter,
               cv_path: Path, cv_bytes: bytes, content_hash: str,
               say: Callable[[str], None]) -> tuple[Evidence, CvExtraction]:
    """Store the CV file, mint its evidence row, run extraction. Nothing the
    model drafted is persisted here: experiences and facts land only through
    the interactive review."""
    cv_text = _read_cv_text(cv_path, cv_bytes)  # may raise CvReadError: degrade before storing
    evidence_id = new_id("ev")
    # Locator derives from the evidence id, not the original basename: a later
    # upload named the same can never overwrite an earlier file out from under
    # its evidence row's hash. The original filename stays in the title. The
    # stored file and its hash are always the original bytes (a PDF stays a PDF).
    locator = f"files/cv/{evidence_id}{cv_path.suffix}"
    storage.write_bytes(locator, cv_bytes)
    say(f"Stored CV at instance/{locator}; extracting structure (model proposes, you decide)...")
    service = CvExtractionService(model, load_prompt("cv_extraction.md"))
    extraction = service.extract(cv_text)
    # The evidence row is the commit point, minted only once extraction
    # succeeded: a failed extraction leaves no row, so a retry is a fresh
    # ingest, never a hash match resuming a walk that never happened.
    cv_evidence = Evidence(
        id=evidence_id, evidence_type="cv", title=cv_path.name, locator=locator,
        content_hash=content_hash,
    )
    SqliteEvidenceRepository(conn).add(cv_evidence)
    return cv_evidence, extraction


def _review_experiences(conn: sqlite3.Connection, extraction: CvExtraction,
                        ask: Callable[[str], str], say: Callable[[str], None]) -> list[str | None]:
    """Walk each extracted experience: confirm/edit/reject. An experience is
    persisted only on confirm or edit; a rejected one is dropped, with its
    dependent draft facts. Returns extraction index -> experience id (None
    where rejected)."""
    experiences_repo = SqliteExperienceRepository(conn)
    # A re-extraction after an interrupted walk (experiences persist per
    # answer, facts only after the walk) must not re-ask or duplicate what is
    # already confirmed: an exact (kind, title, org, dates) match is reused.
    # A reworded draft is still asked; only exact duplicates are prevented.
    # Per-shape FIFO queues, each persisted row consumed at most once, so a
    # CV legitimately carrying the same role twice keeps both rows: the
    # replay reuses one per draft and asks for the rest as new.
    existing_rows = experiences_repo.list_all()
    by_shape: dict[tuple, list[Experience]] = {}
    for row in sorted(existing_rows,
                      key=lambda e: (e.display_order is None, e.display_order)):
        by_shape.setdefault(
            (row.kind, row.title, row.org, row.start_date, row.end_date),
            []).append(row)
    say(f"Extracted {len(extraction.experiences)} experiences. Confirm, edit, or reject each.")
    experience_ids: list[str | None] = []
    order = max((e.display_order for e in existing_rows
                 if e.display_order is not None), default=-1) + 1
    for draft in extraction.experiences:
        queue = by_shape.get(
            (draft.kind, draft.title, draft.org, draft.start_date, draft.end_date))
        if queue:
            say(f"\n[{draft.kind}] {draft.title} @ {draft.org}: already"
                " confirmed earlier; reusing it.")
            experience_ids.append(queue.pop(0).id)
            continue
        say(f"\n[{draft.kind}] {draft.title} @ {draft.org}"
            f" ({draft.start_date or '?'} - {draft.end_date or 'present'})")
        action = _ask_choice(ask, say, "confirm/edit/reject",
                             ("confirm", "edit", "reject", "c", "e", "r"), "confirm")
        if action in ("reject", "r"):
            experience_ids.append(None)
            continue
        title, org, start_date, end_date = draft.title, draft.org, draft.start_date, draft.end_date
        if action in ("edit", "e"):
            title = ask(f"Title [{title}]: ").strip() or title
            org = ask(f"Org [{org}]: ").strip() or org
            start_date = ask(f"Start date [{start_date}]: ").strip() or start_date
            end_date = ask(f"End date [{end_date}]: ").strip() or end_date
        experience = Experience(
            id=new_id("exp"), kind=draft.kind, title=title, org=org,
            start_date=start_date, end_date=end_date, summary=draft.summary,
            display_order=order,
        )
        experiences_repo.add(experience)
        experience_ids.append(experience.id)
        order += 1
    return experience_ids


def _persist_fact_drafts(conn: sqlite3.Connection, extraction: CvExtraction,
                         experience_ids: list[str | None], cv_evidence: Evidence,
                         say: Callable[[str], None]) -> list[str]:
    """Persist draft facts (user_approved=0, source='cv', origin recorded so a
    resume walks only this CV's drafts), dropping any fact whose experience
    was rejected in review."""
    facts_repo = SqliteCareerFactRepository(conn)
    fact_ids = []
    dropped = 0
    for draft in extraction.facts:
        if draft.experience_index is not None and experience_ids[draft.experience_index] is None:
            dropped += 1
            continue
        fact = CareerFact(
            id=new_id("fact"), fact_type=draft.fact_type, statement=draft.statement,
            source="cv", user_approved=0,
            experience_id=(experience_ids[draft.experience_index]
                           if draft.experience_index is not None else None),
            source_location=draft.source_location,
            origin_evidence_id=cv_evidence.id,
        )
        facts_repo.add(fact)
        fact_ids.append(fact.id)
    if dropped:
        say(f"Dropped {dropped} draft facts belonging to rejected experiences.")
    return fact_ids


def _confirm_drafts(facts_repo: SqliteCareerFactRepository, edges_repo: SqliteCareerEdgeRepository,
                    cv_evidence: Evidence, draft_fact_ids: list[str],
                    ask: Callable[[str], str], say: Callable[[str], None]) -> None:
    say(f"Extracted {len(draft_fact_ids)} draft facts. Confirm, edit, or reject each;"
        " only confirmed facts ever feed generation.")
    for fact_id in draft_fact_ids:
        fact = facts_repo.get(fact_id)
        say(f"\n[{fact.fact_type}] {fact.statement}")
        action = _ask_choice(ask, say, "confirm/edit/reject",
                             ("confirm", "edit", "reject", "c", "e", "r"), "confirm")
        if action in ("reject", "r"):
            facts_repo.set_status(fact_id, "retracted")
            continue
        statement = fact.statement
        if action in ("edit", "e"):
            edited = ask("Corrected statement: ").strip()
            if edited:
                statement = edited
        facts_repo.set_approval(fact_id, statement, _now())
        # The PROVES edge lands with the approval, before the optional
        # quantifier prompt: stopping there must not strand an approved fact
        # without its evidence link (a quantifier only edits the statement).
        edges_repo.add(CareerEdge(
            id=new_id("edge"), source_type="evidence", source_id=cv_evidence.id,
            edge_type="PROVES", target_type="career_fact", target_id=fact_id,
            claim_kind="fact", provenance="onboarding:cv-confirmation",
            created_by="user", user_verified=1,
        ))
        # Inline metric backfill (OC-35, layer 1): one optional follow-up while
        # context is freshest; a supplied restatement is a user edit, approved.
        offer_quantifier(facts_repo, fact_id, statement, fact.fact_type, ask, say)


def _link_evidence_to_capability(evidence_repo: SqliteEvidenceRepository,
                                 facts_repo: SqliteCareerFactRepository,
                                 edges_repo: SqliteCareerEdgeRepository,
                                 capability: Capability, exclude_evidence_id: str,
                                 ask: Callable[[str], str],
                                 say: Callable[[str], None]) -> None:
    """Reachability for the family walk: candidates derived in code (no model
    values, OC-5) as the existing evidence rows whose PROVES edges reach
    confirmed active facts; each gets one confirmation minting a SUPPORTS
    (evidence -> capability) edge, user-created and generation-eligible.
    Without this, CV evidence proves facts no capability can reach."""
    for evidence in evidence_repo.list_all():
        if evidence.id == exclude_evidence_id:
            continue  # the interview evidence already SUPPORTS this capability
        proven = [
            fact for edge in edges_repo.active_edges_from("evidence", evidence.id, "PROVES")
            for fact in [facts_repo.get(edge.target_id)]
            if fact is not None and fact.user_approved and fact.status == "active"
        ]
        if not proven:
            continue
        if any(e.target_id == capability.id
               for e in edges_repo.active_edges_from("evidence", evidence.id, "SUPPORTS")):
            continue
        if ask_yes_no(ask, say,
                      f"Link evidence '{evidence.title}' ({len(proven)} confirmed facts)"
                      f" as supporting '{capability.name}'?", default=True):
            edges_repo.add(CareerEdge(
                id=new_id("edge"), source_type="evidence", source_id=evidence.id,
                edge_type="SUPPORTS", target_type="capability", target_id=capability.id,
                claim_kind="fact", provenance="onboarding:capability-evidence",
                created_by="user", user_verified=1,
            ))
            say(f"  linked: {evidence.title} -SUPPORTS-> {capability.name}")


def _gap_questions(conn: sqlite3.Connection, evidence_repo: SqliteEvidenceRepository,
                   facts_repo: SqliteCareerFactRepository, edges_repo: SqliteCareerEdgeRepository,
                   ask: Callable[[str], str], say: Callable[[str], None]) -> None:
    """Ask only what the CV cannot show: capabilities, goals, profile basics.
    Answers land as approved interview-sourced facts plus PROVES/SUPPORTS edges."""
    interview_evidence: Evidence | None = None

    def evidence_row() -> Evidence:
        nonlocal interview_evidence
        if interview_evidence is None:
            interview_evidence = Evidence(
                id=new_id("ev"), evidence_type="user_statement",
                title=f"Onboarding interview {_now()[:10]}",
            )
            evidence_repo.add(interview_evidence)
        return interview_evidence

    say("\nCapabilities: what are you operationally good at? (blank name to finish)")
    capabilities_repo = SqliteCapabilityRepository(conn)
    while True:
        name = ask("Capability name: ").strip()
        if not name:
            break
        if capabilities_repo.get_by_name(name):
            say(f"'{name}' already exists; skipping.")
            continue
        strength = _ask_choice(ask, say, "Strength", _STRENGTHS, "moderate")
        capability = Capability(id=new_id("cap"), name=name, strength=strength,
                                last_assessed_at=_now())
        capabilities_repo.add(capability)
        fact = CareerFact(
            id=new_id("fact"), fact_type="skill_use",
            statement=f"Self-assessed capability: {name} ({strength})",
            source="interview", user_approved=1, verified_at=_now(),
        )
        facts_repo.add(fact)
        edges_repo.add(CareerEdge(
            id=new_id("edge"), source_type="evidence", source_id=evidence_row().id,
            edge_type="PROVES", target_type="career_fact", target_id=fact.id,
            claim_kind="fact", provenance="onboarding:interview",
            created_by="user", user_verified=1,
        ))
        edges_repo.add(CareerEdge(
            id=new_id("edge"), source_type="evidence", source_id=evidence_row().id,
            edge_type="SUPPORTS", target_type="capability", target_id=capability.id,
            claim_kind="fact", provenance="onboarding:interview",
            created_by="user", user_verified=1,
        ))
        _link_evidence_to_capability(
            evidence_repo, facts_repo, edges_repo, capability,
            exclude_evidence_id=evidence_row().id, ask=ask, say=say)

    say("\nGoals: where should this career go? (blank to finish)")
    goals_repo = SqliteCareerGoalRepository(conn)
    while True:
        statement = ask("Goal: ").strip()
        if not statement:
            break
        horizon = _ask_choice(ask, say, "Horizon", _HORIZONS, "mid")
        goals_repo.add(CareerGoal(id=new_id("goal"), statement=statement, horizon=horizon))

    say("\nProfile basics (blank to skip a field):")
    profile_repo = SqliteUserProfileRepository(conn)
    for field in _PROFILE_BASICS:
        while True:
            value = ask(f"{field}: ").strip()
            if not value:
                break
            try:
                profile_repo.set_field(field, value, source="user_edit")
                break
            except InvalidProfileValueError as e:
                say(f"invalid value: {e}")
