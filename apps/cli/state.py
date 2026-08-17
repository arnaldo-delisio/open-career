"""`open-career experience add|list|edit` and `capability add|list`.

Career state keeps changing after onboarding: a project started this month has
to be able to reach the CV, and a capability named for the first time in the
story bank has to be creatable where the user noticed it was missing. These
commands are that surface, and they write nothing the interview does not write:
the same experience rows, the same approved user-stated facts with their
interview evidence row and PROVES edge, the same SUPPORTS and DEMONSTRATES
links to the capabilities an experience demonstrates (without which its facts
are unreachable by the package walk), and the same capability chain
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
from apps.cli.interview import (
    ask_choice,
    ask_date,
    write_capability_link,
    write_stated_fact,
)
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


# The owner marker on an experience's own statement row. A SUPPORTS edge hangs
# off evidence, while the user is saying "this experience demonstrates that
# capability": when one row proves facts for several experiences (the CV
# extraction row proves facts across every one of them), a link off that row is
# neither ownable nor safely retireable. One dedicated row per experience is
# what makes the two coincide, so an experience's facts and its links share one
# owner. It is not the story marker, so a statement row is still not a story
# (domain/traversal.py).
EXPERIENCE_EVIDENCE_NOTE = "facts-for-experience:"


class _ExperienceEvidence:
    """The user_statement row one experience's stated facts hang off, found by
    its owner marker so a later `experience edit` writes into the same row the
    add flow used, and minted on first use so an abandoned flow leaves no
    orphan. Reuse is the point: a fresh row per session would receive no
    SUPPORTS edge, and the facts written to it would be unreachable while the
    experience looked linked."""

    def __init__(self, evidence_repo: SqliteEvidenceRepository,
                 experience_id: str, title: str):
        self._repo = evidence_repo
        self._experience_id = experience_id
        self._title = title
        self._evidence: Evidence | None = None

    def get(self) -> Evidence:
        if self._evidence is None:
            marker = f"{EXPERIENCE_EVIDENCE_NOTE}{self._experience_id}"
            existing = [e for e in self._repo.list_all() if e.notes == marker]
            self._evidence = existing[0] if existing else None
        if self._evidence is None:
            self._evidence = Evidence(
                id=new_id("ev"), evidence_type="user_statement",
                title=f"Stated experience: {self._title}",
                notes=f"{EXPERIENCE_EVIDENCE_NOTE}{self._experience_id}")
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


class _InputEnded(Exception):
    """End of input inside a persist-as-you-go loop, carrying how much was
    already saved so the command can say it. A traceback would be the wrong
    answer here: the rows are on disk, and the user needs to be told which."""

    def __init__(self, saved: int):
        super().__init__("input ended")
        self.saved = saved


def _demonstrated_capability_ids(conn: sqlite3.Connection,
                                 experience_id: str) -> list[str]:
    """The capabilities this experience currently demonstrates, read from its
    active DEMONSTRATES edges. This is the user's own statement of what the
    experience shows, and it is what a newly stated fact has to reach."""
    return [e.target_id for e in SqliteCareerEdgeRepository(conn).list_all()
            if e.edge_type == "DEMONSTRATES" and e.source_id == experience_id
            and e.superseded_at is None]


def _write_experience_fact(conn: sqlite3.Connection,
                           evidence_supplier: Callable[[], Evidence],
                           experience_id: str, statement: str, fact_type: str,
                           provenance: str) -> None:
    """One stated fact plus the links that make it reachable, in one
    transaction. A fact stated after the experience was linked would otherwise
    land silently unreachable, so the SUPPORTS edges to the capabilities the
    experience already demonstrates are minted with it (idempotently: the same
    link after every fact is a no-op)."""
    with transaction(conn):
        write_stated_fact(conn, evidence_supplier, statement, fact_type,
                          provenance, experience_id)
        evidence_id = evidence_supplier().id
        for capability_id in _demonstrated_capability_ids(conn, experience_id):
            write_capability_link(conn, capability_id, evidence_id, provenance,
                                  experience_id)


def _ask_facts(conn: sqlite3.Connection, evidence_supplier: Callable[[], Evidence],
               experience_id: str, ask: Ask, say: Say, provenance: str) -> int:
    """Facts about one experience, each persisted the moment it is stated, so
    an interrupt loses only the answer being typed."""
    added = 0
    while True:
        try:
            statement = ask("Fact (blank to finish): ").strip()
            if not statement:
                return added
            fact_type = ask_choice(ask, say, "  Fact type", FACT_TYPES, "achievement")
        except EOFError:
            raise _InputEnded(added) from None
        _write_experience_fact(conn, evidence_supplier, experience_id, statement,
                               fact_type, provenance)
        added += 1
        say("  fact saved (user-stated, approved).")


def _parse_numbers(raw: str, count: int) -> list[int] | None:
    """Several choices in one answer ('1 3 4', commas allowed). None means the
    answer was not a valid selection, which is reported, never guessed at."""
    parts = raw.replace(",", " ").split()
    numbers = []
    for part in parts:
        if not part.isdigit() or not 1 <= int(part) <= count:
            return None
        if int(part) not in numbers:
            numbers.append(int(part))
    return numbers


def _ask_capability_choices(capabilities: list[Capability], prompt: str,
                            ask: Ask, say: Say) -> list[Capability]:
    """Which capabilities the user picks from a numbered list, several by
    number in one answer, blank meaning none. Nothing is preselected and no
    default is offered on purpose: linking an experience to every capability
    because it is convenient mints exactly the meaningless edge OC-39 deleted,
    so the answer is the user's or there is no edge."""
    for number, capability in enumerate(capabilities, 1):
        say(f"  {number}. {capability.name}")
    while True:
        raw = ask(prompt).strip()
        if not raw:
            return []
        numbers = _parse_numbers(raw, len(capabilities))
        if numbers is None:
            say(f"  expected numbers between 1 and {len(capabilities)}"
                " (for example '1 3'), or blank for none")
            continue
        return [capabilities[n - 1] for n in numbers]


def _link_capabilities(conn: sqlite3.Connection,
                       evidence_supplier: Callable[[], Evidence],
                       experience_id: str, capabilities: list[Capability],
                       provenance: str, say: Say) -> None:
    """SUPPORTS from every evidence row that proves this experience's facts,
    plus DEMONSTRATES from the experience itself, for each chosen capability,
    in one transaction with the rest of the unit of work. The SUPPORTS edge is
    what makes those facts reachable: without it the package walk stops at the
    capability and the experience renders as a bare title and date.

    The link hangs off this experience's own statement row, which owns its
    facts and nothing else, and off any older row that proves only this
    experience's facts (the repair path for facts stated before the row was
    dedicated). A row shared with other experiences is left alone and named:
    linking it would pull those other experiences' facts into the capability
    too, which is not what the user said."""
    if not capabilities:
        return
    with transaction(conn):
        evidence_ids = [evidence_supplier().id]
        shared = []
        for evidence_id in sorted(_experience_evidence_ids(conn, experience_id)):
            if evidence_id in evidence_ids:
                continue
            if _proves_other_experiences(conn, evidence_id, experience_id):
                shared.append(evidence_id)
            else:
                evidence_ids.append(evidence_id)
        for capability in capabilities:
            for evidence_id in evidence_ids:
                write_capability_link(conn, capability.id, evidence_id,
                                      provenance, experience_id)
            say(f"  linked to '{capability.name}' (its facts are now reachable"
                " by packaging).")
        for evidence_id in shared:
            say(f"  ({evidence_id} also proves other experiences' facts, so it"
                " was left unlinked; re-state those facts here to link them.)")


def _ask_and_link_capabilities(conn: sqlite3.Connection,
                               evidence_supplier: Callable[[], Evidence],
                               experience_id: str, ask: Ask, say: Say,
                               provenance: str) -> None:
    """The single capability-link ask on the add path, after the facts: which
    of the existing capabilities this experience demonstrates."""
    linked = _linked_capability_ids(conn, experience_id)
    choices = [c for c in SqliteCapabilityRepository(conn).list_all()
               if c.id not in linked]
    if not choices:
        return
    say("Which of your capabilities does this experience demonstrate?"
        " Nothing is preselected; its facts reach packaging through this link.")
    chosen = _ask_capability_choices(
        choices, "Capabilities (numbers, blank for none): ", ask, say)
    _link_capabilities(conn, evidence_supplier, experience_id, chosen,
                       provenance, say)


def _experience_evidence_ids(conn: sqlite3.Connection, experience_id: str) -> set[str]:
    """The evidence rows that speak for one experience: those with an active
    PROVES edge to one of its facts. There is no evidence-to-experience edge in
    the vocabulary, so this is how the two are related."""
    fact_ids = {f.id for f in SqliteCareerFactRepository(conn).list_all()
                if f.experience_id == experience_id}
    return {e.source_id for e in SqliteCareerEdgeRepository(conn).list_all()
            if e.edge_type == "PROVES" and e.superseded_at is None
            and e.target_id in fact_ids}


def _proves_other_experiences(conn: sqlite3.Connection, evidence_id: str,
                              experience_id: str) -> bool:
    """Whether this evidence row proves active facts belonging to experiences
    other than this one. Such a row is shared (the CV extraction row proves
    facts across every experience), so its SUPPORTS edge is not this
    experience's to retire."""
    facts = {f.id: f for f in SqliteCareerFactRepository(conn).list_all()}
    for edge in SqliteCareerEdgeRepository(conn).active_edges_from(
            "evidence", evidence_id, "PROVES"):
        fact = facts.get(edge.target_id)
        if (fact is not None and fact.status == "active"
                and fact.experience_id is not None
                and fact.experience_id != experience_id):
            return True
    return False


def _capability_links(conn: sqlite3.Connection,
                      experience_id: str) -> dict[str, list[CareerEdge]]:
    """Capability id -> the active edges tying this experience to it: the
    SUPPORTS edges from its evidence rows (what packaging walks) and the
    DEMONSTRATES edge from the experience itself."""
    edges_repo = SqliteCareerEdgeRepository(conn)
    evidence_ids = _experience_evidence_ids(conn, experience_id)
    links: dict[str, list[CareerEdge]] = {}
    for edge in edges_repo.list_all():
        if edge.superseded_at is not None:
            continue
        if edge.edge_type == "SUPPORTS" and edge.source_id in evidence_ids:
            links.setdefault(edge.target_id, []).append(edge)
        elif edge.edge_type == "DEMONSTRATES" and edge.source_id == experience_id:
            links.setdefault(edge.target_id, []).append(edge)
    return links


def _linked_capability_ids(conn: sqlite3.Connection, experience_id: str) -> set[str]:
    return set(_capability_links(conn, experience_id))


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
    """A new experience, the facts hanging off it, and the capabilities it
    demonstrates. The row lands before the facts are asked, because a fact
    needs its experience to exist; ending input after that keeps the experience
    and whatever facts were stated. The capability ask comes once, after the
    facts: the SUPPORTS edges it mints are what let packaging reach those facts
    at all (family -> capability -> SUPPORTS -> PROVES -> fact), and without
    them the experience renders as a title and a date with no bullets."""
    experiences_repo = SqliteExperienceRepository(conn)
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
    # The evidence row is per experience, not per session: the facts stated now
    # and the ones stated in a later edit share one owner, so a link made once
    # keeps reaching them (Codex round 1).
    evidence = _ExperienceEvidence(SqliteEvidenceRepository(conn), experience.id,
                                   title)
    say(f"Added {experience.id}. Now the facts about it"
        " (what you did, what it was worth).")
    try:
        added = _ask_facts(conn, evidence.get, experience.id, ask, say,
                           "experience:add")
        _ask_and_link_capabilities(conn, evidence.get, experience.id, ask, say,
                                   "experience:add")
    except _InputEnded as ended:
        say(f"\ninput ended; saved '{title}' ({experience.id}) with"
            f" {ended.saved} facts. Add the rest with"
            f" `open-career experience edit {experience.id}`.")
        raise SystemExit(1) from None
    except EOFError:
        say(f"\ninput ended; saved '{title}' ({experience.id}) with {added} facts,"
            " and no capability links. Add them with"
            f" `open-career experience edit {experience.id}`.")
        raise SystemExit(1) from None
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
    """Correct the container fields, then add or retract facts, or fix the
    capability links (the repair path for an experience added before those
    links existed). `summary` is not editable here: it is unconfirmed extractor
    prose (OC-41), and a retraction sets status rather than deleting, as
    unlinking retires an edge rather than deleting it (OC-31)."""
    experiences_repo = SqliteExperienceRepository(conn)
    experience = experiences_repo.get(experience_id)
    if experience is None:
        say(f"unknown experience '{experience_id}'")
        raise SystemExit(1)
    facts_repo = SqliteCareerFactRepository(conn)
    evidence = _ExperienceEvidence(SqliteEvidenceRepository(conn), experience.id,
                                   experience.title)
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
        action = ask("Add a fact, retract one, edit capability links, or finish?"
                     " (add/retract/links/done) [done]: ").strip().lower()
        if action in ("", "done"):
            return
        if action == "add":
            try:
                _ask_facts(conn, evidence.get, experience.id, ask, say,
                           "experience:edit")
            except _InputEnded as ended:
                say(f"\ninput ended; {ended.saved} facts were saved.")
                raise SystemExit(1) from None
            continue
        if action == "links":
            _edit_capability_links(conn, evidence.get, experience.id, ask, say)
            continue
        if action != "retract":
            say("expected add, retract, links or done")
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


def _edit_capability_links(conn: sqlite3.Connection,
                           evidence_supplier: Callable[[], Evidence],
                           experience_id: str, ask: Ask, say: Say) -> None:
    """Current capability links, then add or remove. This is the repair path
    for an experience added before the links existed: without a SUPPORTS edge
    its facts are unreachable and it renders as a bare title and date.
    Removing retires the edges by superseding them, never deletes (OC-31)."""
    capabilities = {c.id: c for c in SqliteCapabilityRepository(conn).list_all()}
    if not capabilities:
        say("  no capabilities yet (add one: open-career capability add).")
        return
    links = _capability_links(conn, experience_id)
    say("Capability links:")
    if not links:
        say("  (none; this experience's facts cannot reach packaging yet)")
    for capability_id in links:
        say(f"  - {capabilities[capability_id].name}")
    action = ask("Link capabilities, unlink one, or go back?"
                 " (link/unlink/back) [back]: ").strip().lower()
    if action == "link":
        choices = [c for c in capabilities.values() if c.id not in links]
        if not choices:
            say("  every capability is already linked.")
            return
        say("Nothing is preselected; pick only what this experience"
            " demonstrates.")
        chosen = _ask_capability_choices(
            choices, "Capabilities (numbers, blank for none): ", ask, say)
        _link_capabilities(conn, evidence_supplier, experience_id, chosen,
                           "experience:edit", say)
        return
    if action != "unlink":
        return
    linked = list(links)
    chosen = _ask_capability_choices(
        [capabilities[cid] for cid in linked],
        "Unlink which? (numbers, blank to cancel): ", ask, say)
    with transaction(conn):
        edges_repo = SqliteCareerEdgeRepository(conn)
        for capability in chosen:
            kept = []
            for edge in links[capability.id]:
                # A SUPPORTS edge off a shared evidence row is not this
                # experience's to retire: retiring it would break the chains of
                # every other experience that row proves facts for. Refuse and
                # say so, rather than guess (the DEMONSTRATES edge, which is
                # unambiguously this experience's, still goes).
                if edge.edge_type == "SUPPORTS" and _proves_other_experiences(
                        conn, edge.source_id, experience_id):
                    kept.append(edge.source_id)
                    continue
                edges_repo.supersede(edge.id)
            say(f"  unlinked '{capability.name}' (the edges are retired,"
                " not deleted).")
            for evidence_id in kept:
                say(f"  kept the support from {evidence_id}: it also proves"
                    " other experiences' facts, and retiring it would break"
                    " their chains. Retract the facts you meant instead.")


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
