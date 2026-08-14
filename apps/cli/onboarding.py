"""CV-first onboarding (cold-start contract in decisions/career-graph-schema.md).

One flow: CV upload and extraction first, then the interview as a
confirmation-and-gaps pass. Extracted facts are born user_approved=0; the
review step is where CV-parsing loss is caught. Without a CV the flow degrades
to the same gap questions from a blank slate. Interactive I/O goes through the
ask/say seams so tests drive the flow with scripted answers.

The review is one surface, not a sequential walk (OC-39, spec: the scope's
decisions/onboarding-ux-redesign.md): every extracted experience and every
draft fact is listed at once with a stable index, and each carries its own
explicit mark. Speed comes from parallelizing the decisions and from asking for
numbers once per experience, never from turning a decision into a default:
there is no approve-the-remainder, an unmarked item is not approved, and the
system never proposes a number. Resume stays derived from the persisted rows
(no cursor): an interrupted review renders whatever is still unmarked.
"""

import hashlib
import sqlite3
import subprocess
import time
from dataclasses import dataclass
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
from domain import dates
from domain.edges import CareerEdge
from domain.entities import Capability, CareerFact, CareerGoal, Evidence, Experience
from domain.extraction import CvExtraction, CvExtractionService
from domain.ids import new_id
from domain.ports import ModelAdapter, StorageAdapter
from apps.cli.interview import QUANTIFIABLE_FACT_TYPES, is_unquantified
from domain.profile import InvalidProfileValueError
from prompts import load_prompt

class CvReadError(RuntimeError):
    """The CV file's text could not be extracted (e.g. a PDF with no
    pdftotext available). Callers degrade like other extraction failures."""


_MARKS = {"a": "accept", "e": "edit", "r": "reject"}
_MARK_HELP = ("mark items as '1a 2r 3e' (a=accept, e=edit, r=reject),"
              " several per line, ranges allowed ('2-6a')")

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
        # file was already ingested, so extraction (the only model call) never
        # re-runs; only the drafts still unmarked are reviewed again. The
        # row's own review_completed_at (migration 0008) decides first, since a
        # review can legitimately end with no surviving draft at all.
        cv_bytes = cv_path.read_bytes()
        content_hash = hashlib.sha256(cv_bytes).hexdigest()
        re_asking = False  # set when an interrupted experience phase is re-asked
        cv_rows = [e for e in SqliteEvidenceRepository(conn).list_all()
                   if e.evidence_type == "cv"]
        matches = [e for e in cv_rows if e.content_hash == content_hash]
        # Deterministic pick when the same bytes were somehow ingested twice:
        # the most recent row wins (created_at, id as the tiebreak).
        existing = max(matches, key=lambda e: (e.created_at or "", e.id),
                       default=None)
        if existing is not None and existing.review_completed_at:
            # The completion flag is consulted first and answers on its own: a
            # review where every item was rejected leaves no surviving draft,
            # which the fact-based reasoning below cannot tell apart from an
            # extraction whose drafts never landed, and re-extracting would
            # re-ask a CV the user has already been through.
            say(f"This CV was already ingested ({existing.title}) and its"
                " review is complete; continuing with the remaining questions.")
        elif existing is not None:
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
                # An evidence row with no facts at all means the earlier review
                # died inside its experience phase: nothing to resume, so fall
                # through to a fresh extraction (the orphaned older row is
                # acceptable residue). The surface says that the experience
                # marks of that lost phase are being asked again.
                say(f"This CV was stored earlier ({existing.title}) but its"
                    " draft facts never landed; re-extracting.")
                existing = None
                re_asking = True
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
                        f" the review over {len(pending)} pending draft facts.")
                    reviewed = _review_pending_drafts(facts_repo, edges_repo, existing,
                                                      pending, ask, say)
                    SqliteEvidenceRepository(conn).mark_review_completed(
                        existing.id, _now())
                    # Facts approved before the interrupt never reached a
                    # number ask, and the completion stamp about to be written
                    # would make that permanent, so they join this one. A
                    # resume necessarily precedes any completed number pass for
                    # this CV, so nothing is asked twice.
                    approved_earlier = [f.id for f in attributed
                                        if f.user_approved and f.status == "active"]
                    _ask_numbers(conn, facts_repo, approved_earlier + reviewed,
                                 ask, say)
                else:
                    # Rows predating migration 0008 carry no completion flag:
                    # every attributed draft already marked means the same
                    # thing, and the flag is recorded now.
                    SqliteEvidenceRepository(conn).mark_review_completed(
                        existing.id, _now())
                    say(f"This CV was already ingested ({existing.title}) and its"
                        " review is complete; continuing with the remaining questions.")
        if existing is None:
            cv_evidence, extraction = _ingest_cv(conn, storage, model, cv_path,
                                                 cv_bytes, content_hash, say)
            reviewed = _review_extraction(conn, extraction, cv_evidence, facts_repo,
                                          edges_repo, ask, say, re_asking)
            # The review completed, whatever the marks were: recording that on
            # the evidence row is what lets a re-run tell an all-rejected CV
            # apart from one whose drafts never landed.
            SqliteEvidenceRepository(conn).mark_review_completed(
                cv_evidence.id, _now())
            _ask_numbers(conn, facts_repo, reviewed, ask, say)
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


def _stored_date(value: str | None) -> str | None:
    """The stored form of a date label: an open-ended role stores null, which
    is the one representation the ordering and coherence rules read as
    'has not ended' (domain/dates.py)."""
    return None if dates.is_open_ended(value) else value


def _ask_date(ask: Callable[[str], str], say: Callable[[str], None],
              label: str, current: str | None, editing: bool) -> str | None:
    """A date the system can compare and order. Asked when the walk is in edit
    mode, and whenever the extracted label is one the canonical parser cannot
    read: an unreadable date silently accepted here is a package that can never
    clear the Gauntlet's date-coherence check, discovered six minutes later."""
    if not editing and dates.is_readable(current):
        return _stored_date(current)
    if not editing:
        say(f"  '{current}' is not a date this system can order.")
    while True:
        raw = ask(f"{label} [{current or ''}]: ").strip()
        value = raw or current
        if dates.is_readable(value):
            return _stored_date(value)
        say("  expected a month and year (e.g. '2015-09', 'September 2015',"
            " '09/2015'), a year ('2015'), or 'present' for an ongoing role")


@dataclass
class _ReviewItem:
    """One markable line of the review surface. `ref` is the item's index in
    the extraction (experience or fact); `parent` is the extraction index of a
    fact's experience, which is what makes the rejection cascade visible."""

    index: int
    kind: str
    label: str
    ref: int
    parent: int | None = None


def _parse_marks(line: str, pending: set[int]) -> tuple[dict[int, str] | None, str]:
    """Parse one marks line into {item index: mark}. A line is taken whole or
    not at all: anything unusable returns None with the reason, so garbage is
    never consumed as data and the caller re-asks with nothing recorded.
    `pending` is every index the line may name, which during the experience
    phase includes the facts waiting their turn; the caller separates them."""
    marks: dict[int, str] = {}
    for token in line.replace(",", " ").split():
        mark = _MARKS.get(token[-1:].lower())
        if mark is None:
            return None, f"'{token}' does not end in a mark (a, e or r)"
        body = token[:-1]
        if "-" in body:
            low, _, high = body.partition("-")
            if not (low.isdigit() and high.isdigit()) or int(high) < int(low):
                return None, f"'{token}' is not an item number or a range"
            numbers = list(range(int(low), int(high) + 1))
        elif body.isdigit():
            numbers = [int(body)]
        else:
            return None, f"'{token}' is not an item number or a range"
        for number in numbers:
            if number not in pending:
                return None, f"item {number} is not waiting for a mark"
            if number in marks:
                return None, f"item {number} is marked twice in one line"
            marks[number] = mark
    if not marks:
        return None, "no marks in that line"
    return marks, ""


def _collect_marks(items: list[_ReviewItem], ask: Callable[[str], str],
                   say: Callable[[str], None],
                   apply_mark: Callable[[_ReviewItem, str], list[int]] | None = None,
                   deferred: set[int] | None = None) -> dict[int, str]:
    """Take marks until every listed item carries one. There is no
    approve-the-remainder default: the loop only ends when nothing is left
    unmarked. `apply_mark` acts on each mark as it lands, which is what makes
    every decision durable at the moment it is made, and returns the indexes it
    cascaded to. `deferred` names the indexes that belong to a later phase:
    they may be typed, they are not recorded, and the loop says why."""
    deferred = deferred or set()
    by_index = {item.index: item for item in items}
    pending = set(by_index)
    marks: dict[int, str] = {}
    while pending:
        waiting = ", ".join(str(i) for i in sorted(pending))
        line = ask(f"Marks for items {waiting}: ")
        parsed, problem = _parse_marks(line, pending | deferred)
        if parsed is None:
            say(f"  {problem}; nothing was recorded from that line. To continue,"
                f" {_MARK_HELP}.")
            continue
        # The deferred set is snapshotted before anything is applied: a
        # cascade removes the rejected experience's facts from it, and an index
        # this line already named as a fact must not become a current-phase
        # index halfway through the line (it is not even in `by_index`).
        too_early_set = {index for index in parsed if index in deferred}
        too_early = sorted(too_early_set)
        for index in sorted(parsed):
            if index in too_early_set:
                continue
            marks[index] = parsed[index]
            pending.discard(index)
            if apply_mark is None:
                continue
            for cascaded in apply_mark(by_index[index], parsed[index]):
                marks[cascaded] = "reject"
                pending.discard(cascaded)
                deferred.discard(cascaded)
        if too_early:
            # Facts are marked only once every experience is resolved, because
            # a rejected experience takes its facts with it: recording a fact
            # mark before that would record a decision about a line that may be
            # about to disappear.
            named = ", ".join(str(i) for i in too_early)
            say(f"  {'item' if len(too_early) == 1 else 'items'} {named}:"
                " facts are marked after every experience is, so those marks"
                " were not recorded.")
    return marks


def _experience_label(draft) -> str:
    return (f"[{draft.kind}] {draft.title} @ {draft.org}"
            f" ({draft.start_date or '?'} - {draft.end_date or 'present'})")


def _review_extraction(conn: sqlite3.Connection, extraction: CvExtraction,
                       cv_evidence: Evidence, facts_repo: SqliteCareerFactRepository,
                       edges_repo: SqliteCareerEdgeRepository,
                       ask: Callable[[str], str], say: Callable[[str], None],
                       re_asking: bool = False) -> list[str]:
    """The CV review surface: every extracted experience and draft fact listed
    once, each marked individually. Experiences are marked first and persist as
    they are accepted; once every one is resolved, the surviving drafts persist
    together; then each fact's mark approves or retracts its row the moment it
    lands.

    Fact decisions are durable per mark. Experience decisions become durable
    together when the phase completes: an interrupt inside phase one writes
    nothing at all, so the marks given so far are genuinely re-asked and a
    revised mark on the next run genuinely decides (an experience persisted
    early would outlive the interrupt the surface promised to re-ask, and no
    later reject could undo the row). `re_asking` makes the surface state that
    re-ask instead of silently re-listing decisions the user believes they
    already made. Returns the ids of the facts this review persisted, in
    display order."""
    experiences_repo = SqliteExperienceRepository(conn)
    # A re-extraction after an interrupted review (experiences persist per
    # mark, facts only once every experience is resolved) must not re-ask or
    # duplicate what is already confirmed: an exact (kind, title, org, dates)
    # match is reused. A reworded draft is still asked; only exact duplicates
    # are prevented. Per-shape FIFO queues, each persisted row consumed at most
    # once, so a CV legitimately carrying the same role twice keeps both rows.
    existing_rows = experiences_repo.list_all()
    by_shape: dict[tuple, list[Experience]] = {}
    for row in sorted(existing_rows,
                      key=lambda e: (e.display_order is None, e.display_order)):
        by_shape.setdefault(
            (row.kind, row.title, row.org, _stored_date(row.start_date),
             _stored_date(row.end_date)), []).append(row)
    order_base = max((e.display_order for e in existing_rows
                      if e.display_order is not None), default=-1) + 1

    experience_ids: list[str | None] = [None] * len(extraction.experiences)
    resolved: set[int] = set()  # experience positions needing no mark (reused)
    facts_by_experience: dict[int | None, list[int]] = {}
    for position, draft in enumerate(extraction.facts):
        facts_by_experience.setdefault(draft.experience_index, []).append(position)

    items: list[_ReviewItem] = []
    lines: list[str] = []
    counter = 0

    def add_fact_items(experience_position: int | None, indent: str) -> None:
        nonlocal counter
        for fact_position in facts_by_experience.get(experience_position, []):
            draft = extraction.facts[fact_position]
            counter += 1
            items.append(_ReviewItem(counter, "fact",
                                     f"[{draft.fact_type}] {draft.statement}",
                                     fact_position, experience_position))
            lines.append(f"{indent}{counter}. [{draft.fact_type}] {draft.statement}")

    for position, draft in enumerate(extraction.experiences):
        queue = by_shape.get(
            (draft.kind, draft.title, draft.org, _stored_date(draft.start_date),
             _stored_date(draft.end_date)))
        if queue:
            experience_ids[position] = queue.pop(0).id
            resolved.add(position)
            lines.append(f"  {_experience_label(draft)}"
                         "  (already confirmed earlier; reusing it)")
        else:
            counter += 1
            items.append(_ReviewItem(counter, "experience",
                                     _experience_label(draft), position))
            lines.append(f"  {counter}. {_experience_label(draft)}")
        add_fact_items(position, "     ")
    if facts_by_experience.get(None):
        lines.append("  Facts with no experience:")
        add_fact_items(None, "     ")

    say(f"\nExtraction review: {len(extraction.experiences)} experiences,"
        f" {len(extraction.facts)} facts. Nothing here is approved until you"
        " mark it, and only approved facts ever feed generation.")
    for line in lines:
        say(line)
    # The disclosure describes the state actually found, never a generic
    # apology: the interrupted review may have stored its whole experience
    # batch (those rows are reused, not re-asked) or none of it.
    unresolved = any(item.kind == "experience" for item in items)
    if re_asking and (resolved or unresolved):
        reused = ("the experiences that review had already stored are reused"
                  " here and carry no mark number")
        if resolved and unresolved:
            body = f"{reused}; the ones it never stored are asked again"
        elif resolved:
            body = f"{reused}, so no experience is left to mark"
        else:
            body = ("experience marks from the interrupted review were not"
                    " recorded and are asked again here")
        say(f"Note: {body}. Fact decisions you already made are durable and"
            " are not re-asked.")
    say(f"Every item needs a mark: {_MARK_HELP}.")

    fact_items = {item.ref: item for item in items if item.kind == "fact"}
    # Experience decisions are held here until the phase completes, then
    # written together: an accept persisted as its mark landed would survive an
    # interrupt the surface has promised to re-ask, and re-marking that item
    # reject on the next run could not undo the row.
    accepted: dict[int, Experience] = {}

    def apply_mark(item: _ReviewItem, mark: str) -> list[int]:
        if item.kind != "experience":
            return []
        if mark == "reject":
            cascaded = [fact_items[p].index
                        for p in facts_by_experience.get(item.ref, [])
                        if p in fact_items]
            if cascaded:
                say(f"  {item.index} rejected; its facts are rejected with it:"
                    f" {', '.join(str(i) for i in cascaded)}.")
            return cascaded
        draft = extraction.experiences[item.ref]
        title, org = draft.title, draft.org
        editing = mark == "edit"
        if editing:
            say(f"  editing item {item.index}: {item.label}")
            title = ask(f"Title [{title}]: ").strip() or title
            org = ask(f"Org [{org}]: ").strip() or org
        start_date = _ask_date(ask, say, "Start date", draft.start_date, editing)
        end_date = _ask_date(ask, say, "End date", draft.end_date, editing)
        experience = Experience(
            id=new_id("exp"), kind=draft.kind, title=title, org=org,
            start_date=start_date, end_date=end_date, summary=draft.summary,
            # Display order follows the CV, never the order the marks arrived
            # in; a rejected draft simply leaves a gap.
            display_order=order_base + item.ref - len(
                [p for p in resolved if p < item.ref]),
        )
        accepted[item.ref] = experience
        return []

    # Phase one, the experiences: every decision is taken before any of them is
    # written, and a rejection cascades to its facts here, before those facts
    # exist as rows.
    experience_items = [item for item in items if item.kind == "experience"]
    cascaded_marks = _collect_marks(
        experience_items, ask, say, apply_mark,
        deferred={item.index for item in items if item.kind == "fact"})

    # The phase completed, so its decisions become durable together: the
    # accepted experiences in CV order, then the whole surviving draft batch,
    # before a single fact mark is taken. An interrupt in phase two then
    # resumes over exactly the facts that never got one.
    for position in sorted(accepted):
        experiences_repo.add(accepted[position])
        experience_ids[position] = accepted[position].id
    fact_ids = _persist_fact_drafts(conn, extraction, experience_ids,
                                    cv_evidence, say)

    def apply_fact_mark(item: _ReviewItem, mark: str) -> list[int]:
        _apply_fact_mark(facts_repo, edges_repo, cv_evidence,
                         fact_ids[item.ref], mark, ask, say)
        return []

    # Phase two, the facts: each decision hits its persisted row immediately,
    # never buffered to the end of the review.
    pending_facts = [item for item in items
                     if item.kind == "fact" and item.ref in fact_ids
                     and item.index not in cascaded_marks]
    _collect_marks(pending_facts, ask, say, apply_fact_mark)
    return [fact_ids[item.ref] for item in pending_facts]


def _persist_fact_drafts(conn: sqlite3.Connection, extraction: CvExtraction,
                         experience_ids: list[str | None], cv_evidence: Evidence,
                         say: Callable[[str], None]) -> dict[int, str]:
    """Persist draft facts (user_approved=0, source='cv', origin recorded so a
    resume reviews only this CV's drafts), dropping any fact whose experience
    was rejected. Returns extraction index -> fact id for what persisted."""
    facts_repo = SqliteCareerFactRepository(conn)
    fact_ids: dict[int, str] = {}
    dropped = 0
    for position, draft in enumerate(extraction.facts):
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
        fact_ids[position] = fact.id
    if dropped:
        say(f"Dropped {dropped} draft facts belonging to rejected experiences.")
    return fact_ids


def _apply_fact_mark(facts_repo: SqliteCareerFactRepository,
                     edges_repo: SqliteCareerEdgeRepository, cv_evidence: Evidence,
                     fact_id: str, mark: str, ask: Callable[[str], str],
                     say: Callable[[str], None]) -> None:
    """Turn one fact's mark into stored state, at the moment the mark lands: a
    rejected fact goes to retracted, an accepted or edited one is approved with
    its PROVES edge. Nothing about a fact decision is held in memory, so an
    interrupt leaves exactly the unmarked facts pending."""
    fact = facts_repo.get(fact_id)
    if mark == "reject":
        facts_repo.set_status(fact_id, "retracted")
        return
    statement = fact.statement
    if mark == "edit":
        edited = ask(f"Replacement text for '{fact.statement}'"
                     " (blank keeps it as extracted): ").strip()
        if edited:
            statement = edited
        else:
            say("  kept as extracted.")
    facts_repo.set_approval(fact_id, statement, _now())
    # The PROVES edge lands with the approval, before the numbers ask:
    # stopping there must not strand an approved fact without its evidence
    # link (a restatement only edits the statement).
    edges_repo.add(CareerEdge(
        id=new_id("edge"), source_type="evidence", source_id=cv_evidence.id,
        edge_type="PROVES", target_type="career_fact", target_id=fact_id,
        claim_kind="fact", provenance="onboarding:cv-confirmation",
        created_by="user", user_verified=1,
    ))


def _review_pending_drafts(facts_repo: SqliteCareerFactRepository,
                           edges_repo: SqliteCareerEdgeRepository,
                           cv_evidence: Evidence, pending_fact_ids: list[str],
                           ask: Callable[[str], str],
                           say: Callable[[str], None]) -> list[str]:
    """The resume surface: the same review over the drafts still unmarked from
    an earlier run. Review state is derived from the persisted rows, so this is
    simply whatever never got a mark."""
    say(f"\nExtraction review: {len(pending_fact_ids)} draft facts still"
        " unmarked. Only approved facts ever feed generation.")
    items = []
    for index, fact_id in enumerate(pending_fact_ids, 1):
        fact = facts_repo.get(fact_id)
        items.append(_ReviewItem(index, "fact",
                                 f"[{fact.fact_type}] {fact.statement}", index - 1))
        say(f"  {index}. [{fact.fact_type}] {fact.statement}")
    say(f"Every item needs a mark: {_MARK_HELP}.")

    def apply_fact_mark(item: _ReviewItem, mark: str) -> list[int]:
        _apply_fact_mark(facts_repo, edges_repo, cv_evidence,
                         pending_fact_ids[item.ref], mark, ask, say)
        return []

    _collect_marks(items, ask, say, apply_fact_mark)
    return list(pending_fact_ids)


def _ask_numbers(conn: sqlite3.Connection, facts_repo: SqliteCareerFactRepository,
                 fact_ids: list[str], ask: Callable[[str], str],
                 say: Callable[[str], None]) -> None:
    """Metric backfill layer 1 (OC-35), asked once per experience (OC-39): the
    experience's unquantified quantifiable facts are listed and any subset can
    be restated by index, skip being the default for the rest. A restatement is
    a user edit, approved by the act of stating it; the system never proposes a
    number (OC-5). Facts with no experience form one final group."""
    groups: dict[str | None, list[CareerFact]] = {}
    for fact_id in fact_ids:
        fact = facts_repo.get(fact_id)
        if fact is None or not fact.user_approved or fact.status != "active":
            continue
        if fact.fact_type not in QUANTIFIABLE_FACT_TYPES or not is_unquantified(fact.statement):
            continue
        groups.setdefault(fact.experience_id, []).append(fact)
    if not groups:
        return
    experiences_repo = SqliteExperienceRepository(conn)
    say("\nNumbers: these confirmed facts carry no number. Restate any of them"
        " with an honest one; anything you do not address stays as it is.")
    ordered = [key for key in groups if key is not None]
    if None in groups:
        ordered.append(None)
    for experience_id in ordered:
        facts = groups[experience_id]
        experience = experiences_repo.get(experience_id) if experience_id else None
        say(f"\n{experience.title} @ {experience.org}:" if experience
            else "\nFacts with no experience:")
        for number, fact in enumerate(facts, 1):
            say(f"  {number}. [{fact.fact_type}] {fact.statement}")
        while True:
            raw = ask("Restate one as '<n>: statement with a number'"
                      " (blank to move on): ").strip()
            if not raw:
                break
            head, separator, restated = raw.partition(":")
            restated = restated.strip()
            if not separator or not head.strip().isdigit() or not restated:
                say("  expected '<n>: statement'; nothing changed")
                continue
            number = int(head.strip())
            if not 1 <= number <= len(facts):
                say(f"  there is no item {number} in this group; nothing changed")
                continue
            if is_unquantified(restated):
                # The approved statement is never overwritten by an answer that
                # does not do what the prompt asked: a stray 'confirm' typed
                # here used to replace a real achievement and ride into the PDF.
                say("  that restatement has no number in it either;"
                    " the fact is unchanged")
                continue
            fact = facts[number - 1]
            facts_repo.set_approval(fact.id, restated, _now())
            facts[number - 1] = facts_repo.get(fact.id)
            say(f"  {number} updated (user edit, approved).")


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
        # No strength question (OC-40): what the capability rests on is computed
        # from the graph, and a rating stays available deliberately later.
        capability = Capability(id=new_id("cap"), name=name, strength="unrated")
        capabilities_repo.add(capability)
        fact = CareerFact(
            id=new_id("fact"), fact_type="skill_use",
            statement=f"Self-assessed capability: {name}",
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
