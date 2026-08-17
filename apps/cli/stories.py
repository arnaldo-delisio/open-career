"""`open-career stories`: the sitting-three depth interview (OC-35; spec: the
scope's decisions/onboarding-interview-design.md, "Sitting three").

Six clusters, chosen from a menu showing per-cluster completeness; one cluster
per run by default; "continue or stop" offered every five items; every item
persists the moment it is answered, and resume state is computed from the data
itself (experiences without stories, capabilities without eligible chains,
policies still unset), never a stored cursor. No new entity tables: everything
flows through the ratified schema plus the policy seam. Nothing here calls the
model; the user speaks, the system records.
"""

import sqlite3
from typing import Callable

from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteCareerFactRepository,
    SqliteEvidenceRepository,
    SqliteExperienceRepository,
)
from adapters.storage.sqlite_policies import SqliteUserPolicyRepository
from adapters.storage.sqlite_profile import SqliteUserProfileRepository
from apps.cli.interview import (
    _ask_choice,
    _ask_profile_question,
    ask_yes_no,
    offer_quantifier,
    run_evidence_intake,
    write_capability_link,
    write_stated_fact,
    store_statement_file,
)
from domain.edges import CareerEdge
from domain.entities import Evidence
from domain.ids import new_id
from domain.policies import WORK_TRACKS
from domain.ports import StorageAdapter
from domain.questions import TIER1, Question
from domain.traversal import STORY_NOTE_PREFIX

Ask = Callable[[str], str]
Say = Callable[[str], None]

NARRATIVE_NAMES = ("elevator_pitch", "differentiators", "career_change_story",
                   "gap_explanation")

# The DEPTH policy keys each cluster owns; a registry test asserts these cover
# questions(kind="policy", tier=DEPTH) exactly, so a new depth policy cannot
# silently gain no cluster.
PREFERENCE_POLICY_KEYS = ("company_stage_pref", "company_size_pref",
                          "industry_pref", "work_track", "mission_themes")
LOGISTICS_POLICY_KEYS = ("relocation_whitelist", "timezone_bounds",
                         "visa_details", "earliest_start")

# Behavioral stories come from work, not coursework: education rows are out of
# the story bank and its completeness denominator (drive finding).
STORY_EXPERIENCE_KINDS = ("role", "project", "venture", "other")


def _story_experiences(conn) -> list:
    return [e for e in SqliteExperienceRepository(conn).list_all()
            if e.kind in STORY_EXPERIENCE_KINDS]


class Pacer:
    """The per-run budget guard: 'continue or stop here' every five items.
    Stopping loses nothing; resume state is recomputed from data."""

    def __init__(self, ask: Ask, say: Say):
        self._ask, self._say, self._count = ask, say, 0

    def checkpoint(self) -> bool:
        self._count += 1
        if self._count % 5:
            return True
        answer = self._ask("Continue, or stop here? (c/s) [c]: ").strip().lower()
        if answer in ("s", "stop"):
            self._say("Stopped; everything answered so far is saved."
                      " Re-run `open-career stories` to resume.")
            return False
        return True


# --- resume state, computed from the data ------------------------------------

def _experiences_with_stories(conn) -> set[str]:
    return {e.notes.removeprefix(STORY_NOTE_PREFIX)
            for e in SqliteEvidenceRepository(conn).list_all()
            if e.notes and e.notes.startswith(STORY_NOTE_PREFIX)}


def _capabilities_with_eligible_chain(conn) -> set[str]:
    """Capabilities reachable by the ratified traversal shape: an active
    SUPPORTS edge from evidence whose PROVES edges reach an approved active
    fact. Computed, never stored (OC-22)."""
    edges = SqliteCareerEdgeRepository(conn)
    facts = SqliteCareerFactRepository(conn)
    covered: set[str] = set()
    for capability in SqliteCapabilityRepository(conn).list_all():
        for support in edges.active_edges_to("capability", capability.id, "SUPPORTS"):
            proven = edges.active_edges_from("evidence", support.source_id, "PROVES")
            if any((fact := facts.get(p.target_id)) and fact.user_approved
                   and fact.status == "active" for p in proven):
                covered.add(capability.id)
                break
    return covered


def _policies_set(conn, keys: tuple[str, ...]) -> int:
    policies = SqliteUserPolicyRepository(conn).get_policies()
    return sum(1 for k in keys if policies.get(k) is not None)


def _narratives_present(conn) -> set[str]:
    titles = {e.title for e in SqliteEvidenceRepository(conn).list_all()}
    return {name for name in NARRATIVE_NAMES if f"narrative:{name}" in titles}


# --- cluster 1: story bank ----------------------------------------------------

def _run_story_bank(conn, storage: StorageAdapter, ask: Ask, say: Say) -> None:
    evidence_repo = SqliteEvidenceRepository(conn)
    facts_repo = SqliteCareerFactRepository(conn)
    edges_repo = SqliteCareerEdgeRepository(conn)
    capabilities_repo = SqliteCapabilityRepository(conn)
    covered = _experiences_with_stories(conn)
    pending = [e for e in _story_experiences(conn) if e.id not in covered]
    if not pending:
        say("Every experience already has a story. (Re-telling mints new"
            " evidence; old rows keep their hashes.)")
        return
    say("Story bank: per experience, one behavioral story (situation, what YOU"
        " did, outcome). Blank situation skips an experience.")
    pacer = Pacer(ask, say)
    for experience in pending:
        say(f"\n[{experience.kind}] {experience.title} @ {experience.org or '?'}")
        situation = ask("Situation: ").strip()
        if not situation:
            continue
        action = ask("What you did: ").strip()
        outcome = ask("Outcome: ").strip()
        evidence_id = new_id("ev")
        text = (f"# Story: {experience.title}\n\n## Situation\n{situation}\n\n"
                f"## What I did\n{action}\n\n## Outcome\n{outcome}\n")
        locator, digest = store_statement_file(storage, "stories", evidence_id, text)
        evidence_repo.add(Evidence(
            id=evidence_id, evidence_type="user_statement",
            title=f"story: {experience.title}", locator=locator, content_hash=digest,
            notes=f"{STORY_NOTE_PREFIX}{experience.id}"))
        # PROVES: which of this experience's approved facts does it substantiate?
        for fact in facts_repo.list_all():
            if fact.experience_id != experience.id or not fact.user_approved \
                    or fact.status != "active":
                continue
            if ask_yes_no(ask, say, f"  Does it substantiate '{fact.statement}'?",
                          default=False):
                edges_repo.add(CareerEdge(
                    id=new_id("edge"), source_type="evidence", source_id=evidence_id,
                    edge_type="PROVES", target_type="career_fact", target_id=fact.id,
                    claim_kind="fact", provenance="stories:story-bank",
                    created_by="user", user_verified=1))
        # SUPPORTS: which capabilities does it demonstrate?
        while True:
            name = ask("  Capability it demonstrates (blank to finish): ").strip()
            if not name:
                break
            capability = capabilities_repo.get_by_name(name)
            if capability is None:
                say(f"  (unknown capability '{name}'; add it with"
                    " `open-career capability add`, then re-run this cluster)")
                continue
            write_capability_link(conn, capability.id, evidence_id,
                                  "stories:story-bank")
        say(f"  story saved ({locator}).")
        if not pacer.checkpoint():
            return


# --- cluster 2: capability evidence deepening ---------------------------------

def _run_capability_deepening(conn, ask: Ask, say: Say) -> None:
    capabilities_repo = SqliteCapabilityRepository(conn)
    experiences = SqliteExperienceRepository(conn).list_all()
    facts_repo = SqliteCareerFactRepository(conn)
    evidence_repo = SqliteEvidenceRepository(conn)
    edges_repo = SqliteCareerEdgeRepository(conn)
    covered = _capabilities_with_eligible_chain(conn)
    gaps = [c for c in capabilities_repo.list_all() if c.id not in covered]
    if not gaps:
        say("Every capability already has an eligible evidence chain"
            " (global per-capability view; per-family gaps arrive with discovery).")
        return
    say("Capability deepening: per capability without an eligible chain, which"
        " experience demonstrates it and what concretely happened. Blank skips.")
    pacer = Pacer(ask, say)
    for capability in gaps:
        say(f"\nCapability: {capability.name} ({capability.strength}), no eligible chain yet.")
        for i, experience in enumerate(experiences, 1):
            say(f"  {i}. {experience.title} @ {experience.org or '?'}")
        raw = ask("Which experience demonstrates it? (number, blank to skip): ").strip()
        if not raw or not raw.isdigit() or not 1 <= int(raw) <= len(experiences):
            continue
        experience = experiences[int(raw) - 1]
        statement = ask("What concretely happened? ").strip()
        if not statement:
            continue
        # The full eligible chain, exactly the shape the traversal consumes:
        # approved fact on the experience, user_statement evidence, PROVES +
        # SUPPORTS edges, DEMONSTRATES as the summary edge.
        evidence = Evidence(id=new_id("ev"), evidence_type="user_statement",
                            title=f"capability evidence: {capability.name}")
        evidence_repo.add(evidence)
        fact = write_stated_fact(conn, lambda: evidence, statement, "achievement",
                                 "stories:capability-deepening", experience.id)
        write_capability_link(conn, capability.id, evidence.id,
                              "stories:capability-deepening", experience.id)
        offer_quantifier(facts_repo, fact.id, statement, fact.fact_type, ask, say)
        say("  chain minted (fact, evidence, PROVES, SUPPORTS, DEMONSTRATES).")
        if not pacer.checkpoint():
            return


# --- clusters 3 and 6: policy asks --------------------------------------------

def _ask_str_list(ask: Ask, prompt: str) -> list[str] | None:
    raw = ask(f"{prompt} (comma-separated, blank to skip): ").strip()
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def _ask_in_out_pref(policies, key: str, label: str, ask: Ask, say: Say) -> None:
    current = policies.get_policies().get(key)
    say(f"\n{label}. Current: {current or '(unset)'} ('out' feeds the"
        " eligibility gate; 'in' is ranking context only)")
    preferred = _ask_str_list(ask, "Preferred ('in')")
    excluded = _ask_str_list(ask, "Excluded ('out')")
    if preferred is None and excluded is None:
        return
    policies.set_policy(key, {"in": preferred or [], "out": excluded or []},
                        source="user_edit")
    say(f"{key} set.")


def _run_preferences(conn, ask: Ask, say: Say) -> None:
    policies = SqliteUserPolicyRepository(conn)
    say("Preferences and dealbreakers (blank skips; each answer persists"
        " immediately through the audited policy seam).")
    _ask_in_out_pref(policies, "company_stage_pref", "Company stage preferences"
                     " (e.g. seed, growth, public)", ask, say)
    _ask_in_out_pref(policies, "company_size_pref", "Company size preferences"
                     " (e.g. 1-10, 500+)", ask, say)
    _ask_in_out_pref(policies, "industry_pref", "Industry preferences", ask, say)
    track = _ask_choice(ask, say, "\nWork track",
                        WORK_TRACKS, policies.get_policies().get("work_track"))
    if track is not None:
        policies.set_policy("work_track", track, source="user_edit")
    themes = _ask_str_list(ask, "\nMission themes that matter to you")
    if themes is not None:
        policies.set_policy("mission_themes", themes, source="user_edit")
    say("\nHard exclusions live in industry_pref's 'out' list (one home): state"
        " them above, or via the hard-exclusions question in `open-career deepen`,"
        " which appends to the same list.")


def _run_logistics(conn, ask: Ask, say: Say) -> None:
    policies = SqliteUserPolicyRepository(conn)
    profile_repo = SqliteUserProfileRepository(conn)
    current = policies.get_policies()
    say("Logistics depth (blank skips).")
    whitelist = _ask_str_list(
        ask, f"\nRelocation whitelist, cities [{current.get('relocation_whitelist') or ''}]")
    if whitelist is not None:
        policies.set_policy("relocation_whitelist", whitelist, source="user_edit")
    raw_min = ask(f"\nTimezone bounds, min UTC offset (-12..14)"
                  f" [{current.get('timezone_bounds') or ''}] (blank to skip): ").strip()
    if raw_min:
        try:
            low = int(raw_min)
            high = int(ask("Max UTC offset (-12..14): ").strip())
            policies.set_policy("timezone_bounds",
                                {"min_utc_offset": low, "max_utc_offset": high},
                                source="user_edit")
        except ValueError as e:
            say(f"skipped: {e}")
    status_note = ask(f"\nVisa status note [{current.get('visa_details') or ''}]"
                      " (blank to skip): ").strip()
    if status_note:
        details = {"status_note": status_note}
        expiry = ask("Visa expiry date (YYYY-MM-DD, blank if none): ").strip()
        if expiry:
            details["expiry_date"] = expiry
        try:
            policies.set_policy("visa_details", details, source="user_edit")
        except ValueError as e:
            say(f"skipped: {e}")
    start = ask(f"\nEarliest start date (YYYY-MM-DD)"
                f" [{current.get('earliest_start') or ''}] (blank to skip): ").strip()
    if start:
        try:
            policies.set_policy("earliest_start", start, source="user_edit")
        except ValueError as e:
            say(f"skipped: {e}")
    # Canonical fields OC-29 carries stay in the profile seam.
    _ask_profile_question(profile_repo, Question(
        "notice_period", "profile", TIER1, "Notice period (canonical field)",
        "availability questions"), ask, say)


# --- cluster 4: non-CV inventory ----------------------------------------------

def _run_inventory(conn, ask: Ask, say: Say) -> None:
    say("Non-CV inventory: side projects, OSS, talks, certifications,"
        " publications, communities. Each becomes an evidence row plus"
        " user-stated facts, exactly like interview facts.")
    pacer = Pacer(ask, say)
    run_evidence_intake(conn, ask, say, checkpoint=pacer.checkpoint)


# --- cluster 5: narrative and positioning --------------------------------------

def _run_narratives(conn, storage: StorageAdapter, ask: Ask, say: Say) -> None:
    evidence_repo = SqliteEvidenceRepository(conn)
    present = _narratives_present(conn)
    say("Narrative and positioning: authored source material packaging may"
        " draw tone and framing from; every factual claim in output still"
        " traces to approved facts, never to these files. You speak, the"
        " system records; nothing is model-drafted.")
    pacer = Pacer(ask, say)
    for name in NARRATIVE_NAMES:
        label = name.replace("_", " ")
        if name in present:
            say(f"\n{label}: already recorded (re-telling mints new evidence).")
        text = ask(f"\nYour {label} (blank to skip): ").strip()
        if not text:
            continue
        evidence_id = new_id("ev")
        locator, digest = store_statement_file(storage, "narratives", evidence_id,
                                               f"# {label}\n\n{text}\n")
        evidence_repo.add(Evidence(
            id=evidence_id, evidence_type="user_statement",
            title=f"narrative:{name}", locator=locator, content_hash=digest))
        say(f"  saved ({locator}); packaging selects it by name 'narrative:{name}'.")
        if not pacer.checkpoint():
            return


# --- the menu -------------------------------------------------------------------

def _completeness(conn) -> list[str]:
    experiences = _story_experiences(conn)
    capabilities = SqliteCapabilityRepository(conn).list_all()
    with_stories = len(_experiences_with_stories(conn) & {e.id for e in experiences})
    covered = len(_capabilities_with_eligible_chain(conn))
    pref_keys, logi_keys = PREFERENCE_POLICY_KEYS, LOGISTICS_POLICY_KEYS
    inventory = sum(1 for e in SqliteEvidenceRepository(conn).list_all()
                    if e.evidence_type in ("repository", "portfolio", "url",
                                           "artifact", "document"))
    return [
        f"story bank: {with_stories}/{len(experiences)} experiences have stories",
        f"capability deepening: {covered}/{len(capabilities)} capabilities have"
        " eligible chains (global per-capability; per-family gaps arrive with discovery)",
        f"preferences and dealbreakers: {_policies_set(conn, pref_keys)}/{len(pref_keys)}"
        " policies set",
        f"non-CV inventory: {inventory} non-CV evidence rows",
        f"narratives: {len(_narratives_present(conn))}/{len(NARRATIVE_NAMES)} recorded",
        f"logistics depth: {_policies_set(conn, logi_keys)}/{len(logi_keys)} policies set",
    ]


def run_stories(conn: sqlite3.Connection, storage: StorageAdapter,
                ask: Ask, say: Say) -> None:
    clusters = (
        lambda: _run_story_bank(conn, storage, ask, say),
        lambda: _run_capability_deepening(conn, ask, say),
        lambda: _run_preferences(conn, ask, say),
        lambda: _run_inventory(conn, ask, say),
        lambda: _run_narratives(conn, storage, ask, say),
        lambda: _run_logistics(conn, ask, say),
    )
    while True:
        say("\nDepth interview clusters (one per run keeps a run under ~20"
            " minutes; stop anywhere, nothing is lost):")
        for i, line in enumerate(_completeness(conn), 1):
            say(f"  {i}. {line}")
        raw = ask("Cluster to run (1-6, blank to finish): ").strip()
        if not raw:
            say("Done for now; resume state is computed from the data itself.")
            return
        if not raw.isdigit() or not 1 <= int(raw) <= len(clusters):
            say("expected a number 1 to 6")
            continue
        say("")
        clusters[int(raw) - 1]()
        if not ask_yes_no(ask, say, "\nRun another cluster?", default=False):
            say("Done for now; resume state is computed from the data itself.")
            return
