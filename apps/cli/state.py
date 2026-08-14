"""`open-career experience add|list|edit` and `capability add|list`.

Career state keeps changing after onboarding: a project started this month has
to be able to reach the CV, and a capability named for the first time in the
story bank has to be creatable where the user noticed it was missing. These
commands are that surface, and they write nothing the interview does not write:
the same experience rows, the same approved user-stated facts with their
interview evidence row and PROVES edge, and the same capability chain
(PROVES to its self-assessment fact, SUPPORTS to the capability) that makes a
capability packageable the moment it exists.

Two rules the code cannot show on its own. Capabilities are born `unrated` and
strength is never asked (OC-40): what a capability rests on is the computed
evidence depth, which `capability list` reports. A fact is never deleted
(OC-31): retraction sets status, so the trail survives the change. Interactive
I/O goes through the ask/say seams, so every flow here also drives one line at
a time over the session transport (OC-36).
"""

import sqlite3
import time
from typing import Callable, get_args

from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.tx import transaction
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteCareerFactRepository,
    SqliteEvidenceRepository,
    SqliteExperienceRepository,
)
from apps.cli.interview import ask_choice, ask_date, write_stated_fact
from domain.edges import CareerEdge
from domain.entities import (
    Capability,
    Evidence,
    Experience,
    ExperienceKind,
    FactType,
)
from domain.ids import new_id
from domain.traversal import EvidenceTraversal, evidence_depth

Ask = Callable[[str], str]
Say = Callable[[str], None]

EXPERIENCE_KINDS = get_args(ExperienceKind)
FACT_TYPES = get_args(FactType)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class _InterviewEvidence:
    """The user_statement row everything stated in one command hangs off,
    minted on first use so a flow the user abandons leaves no orphan row. It
    carries no notes: the story-bank marker is what distinguishes a story
    (domain/traversal.py) and this is not one."""

    def __init__(self, evidence_repo: SqliteEvidenceRepository, title: str):
        self._repo = evidence_repo
        self._title = title
        self._evidence: Evidence | None = None

    def get(self) -> Evidence:
        if self._evidence is None:
            self._evidence = Evidence(
                id=new_id("ev"), evidence_type="user_statement",
                title=f"{self._title} {_now()[:10]}")
            self._repo.add(self._evidence)
        return self._evidence


def write_capability_chain(conn: sqlite3.Connection, name: str,
                           evidence_supplier: Callable[[], Evidence],
                           provenance: str) -> Capability:
    """The one way a self-assessed capability is created, shared by onboarding
    and `capability add` so the shape cannot drift between them: the capability
    row (`unrated`, OC-40), its self-assessment fact, the evidence row backing
    it, PROVES to the fact and SUPPORTS to the capability, all inside one
    transaction. Atomicity is the guarantee: a capability is packageable the
    moment it exists, so it must never be visible without the chain that makes
    it so.

    `provenance` is the one deliberate difference between the callers, passed
    in rather than fixed here: provenance records where a row actually came
    from, and flattening the interview and the command into one value would
    make the graph less honest, not more. Everything else (entity fields,
    approval flags, edge types, claim_kind, created_by, user_verified, the
    verification stamp) is identical by construction."""
    with transaction(conn):
        capability = Capability(id=new_id("cap"), name=name, strength="unrated")
        SqliteCapabilityRepository(conn).add(capability)
        write_stated_fact(conn, evidence_supplier,
                          f"Self-assessed capability: {name}", "skill_use", provenance)
        SqliteCareerEdgeRepository(conn).add(CareerEdge(
            id=new_id("edge"), source_type="evidence", source_id=evidence_supplier().id,
            edge_type="SUPPORTS", target_type="capability", target_id=capability.id,
            claim_kind="fact", provenance=provenance,
            created_by="user", user_verified=1))
    return capability


def _ask_facts(conn: sqlite3.Connection, evidence_supplier: Callable[[], Evidence],
               experience_id: str, ask: Ask, say: Say, provenance: str) -> int:
    """Facts about one experience, each persisted the moment it is stated, so
    an interrupt loses only the answer being typed."""
    added = 0
    while True:
        statement = ask("Fact (blank to finish): ").strip()
        if not statement:
            return added
        fact_type = ask_choice(ask, say, "  Fact type", FACT_TYPES, "achievement")
        write_stated_fact(conn, evidence_supplier, statement, fact_type,
                          provenance, experience_id)
        added += 1
        say("  fact saved (user-stated, approved).")


def _next_display_order(experiences_repo: SqliteExperienceRepository) -> int:
    return max((e.display_order for e in experiences_repo.list_all()
                if e.display_order is not None), default=-1) + 1


def _fact_count(facts_repo: SqliteCareerFactRepository, experience_id: str) -> int:
    return len([f for f in facts_repo.list_all()
                if f.experience_id == experience_id and f.status == "active"])


def _experience_line(experience: Experience, facts: int) -> str:
    return (f"{experience.id}  [{experience.kind}] {experience.title}"
            f" @ {experience.org or '?'}"
            f" ({experience.start_date or '?'} - {experience.end_date or 'Present'})"
            f", {facts} facts")


def run_experience_add(conn: sqlite3.Connection, ask: Ask, say: Say) -> None:
    """A new experience plus the facts hanging off it. The row lands before the
    facts are asked, because a fact needs its experience to exist; ending input
    after that keeps the experience and whatever facts were stated."""
    experiences_repo = SqliteExperienceRepository(conn)
    evidence = _InterviewEvidence(SqliteEvidenceRepository(conn), "Stated experience")
    say("New experience. Nothing is proposed for you; every field is what you state.")
    kind = ask_choice(ask, say, "Kind", EXPERIENCE_KINDS, "role")
    title = ask("Title: ").strip()
    if not title:
        say("aborted (empty title); nothing persisted")
        raise SystemExit(1)
    org = ask("Org (blank for none): ").strip() or None
    start_date = ask_date(ask, say, "Start date", None, True)
    say("  End date: blank means ongoing (renders as 'Present').")
    end_date = ask_date(ask, say, "End date", None, True)
    experience = Experience(
        id=new_id("exp"), kind=kind, title=title, org=org,
        start_date=start_date, end_date=end_date,
        display_order=_next_display_order(experiences_repo))
    experiences_repo.add(experience)
    say(f"Added {experience.id}. Now the facts about it"
        " (what you did, what it was worth).")
    added = _ask_facts(conn, evidence.get, experience.id, ask, say,
                       "experience:add")
    say(f"Saved '{title}' ({experience.id}) with {added} facts.")


def run_experience_list(conn: sqlite3.Connection, say: Say) -> None:
    experiences_repo = SqliteExperienceRepository(conn)
    facts_repo = SqliteCareerFactRepository(conn)
    experiences = experiences_repo.list_all()
    if not experiences:
        say("No experiences (add one: open-career experience add).")
        return
    for experience in experiences:
        say(f"  {_experience_line(experience, _fact_count(facts_repo, experience.id))}")


# The explicit clear on the edit path. Blank means keep, so a nullable field
# set wrongly would otherwise be uncorrectable: a value once stored could never
# go back to none, which `experience add` allows from the start.
CLEAR = "-"


def _ask_date_edit(ask: Ask, say: Say, label: str, current: str | None,
                   hint: str) -> str | None:
    """A date already stored: blank keeps it, the clear sentinel empties it,
    and 'present' (or any other open-ended word) is read as the same empty."""
    raw = ask(f"{label} [{current or 'Present'}] ({hint}): ").strip()
    if not raw:
        return current
    if raw == CLEAR:
        return None
    return ask_date(ask, say, label, raw, False)


def run_experience_edit(conn: sqlite3.Connection, experience_id: str,
                        ask: Ask, say: Say) -> None:
    """Correct the container fields, then add or retract facts. `summary` is
    not editable here: it is unconfirmed extractor prose (OC-41), and a
    retraction sets status rather than deleting (OC-31)."""
    experiences_repo = SqliteExperienceRepository(conn)
    experience = experiences_repo.get(experience_id)
    if experience is None:
        say(f"unknown experience '{experience_id}'")
        raise SystemExit(1)
    facts_repo = SqliteCareerFactRepository(conn)
    evidence = _InterviewEvidence(SqliteEvidenceRepository(conn), "Stated experience")
    say(f"Editing {_experience_line(experience, _fact_count(facts_repo, experience.id))}")
    # Title is the one field with no empty state: an experience with no title
    # cannot be rendered, so blank keeps it and there is nothing to clear.
    title = ask(f"Title [{experience.title}]: ").strip() or experience.title
    org = ask(f"Org [{experience.org or ''}]"
              f" (blank keeps it, '{CLEAR}' clears it): ").strip() or experience.org
    if org == CLEAR:
        org = None
    start_date = _ask_date_edit(ask, say, "Start date", experience.start_date,
                                f"blank keeps it, '{CLEAR}' clears it")
    end_date = _ask_date_edit(ask, say, "End date", experience.end_date,
                              f"blank keeps it, '{CLEAR}' or 'present' means ongoing")
    experiences_repo.update_fields(experience.id, title, org, start_date, end_date)
    say("Updated.")

    while True:
        facts = [f for f in facts_repo.list_all()
                 if f.experience_id == experience.id and f.status == "active"]
        say("Facts:")
        if not facts:
            say("  (none)")
        for number, fact in enumerate(facts, 1):
            say(f"  {number}. [{fact.fact_type}] {fact.statement}")
        action = ask("Add a fact, retract one, or finish? (add/retract/done)"
                     " [done]: ").strip().lower()
        if action in ("", "done"):
            return
        if action == "add":
            _ask_facts(conn, evidence.get, experience.id, ask, say,
                       "experience:edit")
            continue
        if action != "retract":
            say("expected add, retract or done")
            continue
        raw = ask("Retract which? (number, blank to cancel): ").strip()
        if not raw:
            continue
        if not raw.isdigit() or not 1 <= int(raw) <= len(facts):
            say(f"  there is no fact {raw}; nothing changed")
            continue
        fact = facts[int(raw) - 1]
        # Retracted, never deleted (OC-31): the row and its edges stay, and the
        # status is what keeps it out of generation.
        facts_repo.set_status(fact.id, "retracted")
        say(f"  retracted '{fact.statement}' (the row is kept, not deleted).")


def run_capability_add(conn: sqlite3.Connection, ask: Ask, say: Say) -> None:
    """A capability plus the chain that makes it packageable immediately: its
    self-assessment fact, an interview evidence row, PROVES to the fact and
    SUPPORTS to the capability. Strength is not asked (OC-40)."""
    capabilities_repo = SqliteCapabilityRepository(conn)
    name = ask("Capability name: ").strip()
    if not name:
        say("aborted (empty name); nothing persisted")
        raise SystemExit(1)
    if capabilities_repo.get_by_name(name):
        say(f"'{name}' already exists; nothing persisted")
        raise SystemExit(1)
    evidence = _InterviewEvidence(SqliteEvidenceRepository(conn), "Stated capability")
    capability = write_capability_chain(conn, name, evidence.get, "capability:add")
    say(f"Added '{name}' ({capability.id}), unrated;"
        " deepen it with `open-career stories`.")


def run_capability_list(conn: sqlite3.Connection, say: Say) -> None:
    """Stored strength beside the computed evidence depth (OC-40): the depth is
    what the model reads, and it is computed on demand, never stored."""
    capabilities = SqliteCapabilityRepository(conn).list_all()
    if not capabilities:
        say("No capabilities (add one: open-career capability add).")
        return
    traversal = EvidenceTraversal(
        SqliteCareerEdgeRepository(conn), SqliteEvidenceRepository(conn),
        SqliteCareerFactRepository(conn), SqliteExperienceRepository(conn))
    for capability in capabilities:
        depth = evidence_depth(traversal.evidence_for_capability(capability.id))
        say(f"  {capability.id}  {capability.name} ({capability.strength}),"
            f" {depth.supporting_facts} supporting facts,"
            f" {depth.supporting_stories} stories")
