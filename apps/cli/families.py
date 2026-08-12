"""`open-career families init|list|add|edit|pause` (spec:
decisions/package-generation-design.md, "Role-family onboarding step").

The model proposes candidate families from approved state (structure only,
OC-5); nothing persists unconfirmed. Confirmed families land active with a
1-to-5 emphasis and a user-stated strategy objective, minting an approved
strategy version; every later allocation-affecting change mints a complete
new version atomically. TARGETS edges are user-confirmed."""

import json
import sqlite3
from typing import Callable

from adapters.storage.family_strategy import FamilyStrategyService, StrategyError
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteCareerFactRepository,
    SqliteCareerGoalRepository,
    SqliteExperienceRepository,
    SqliteRoleFamilyRepository,
)
from domain.edges import CareerEdge
from domain.entities import RoleFamily
from domain.family_proposal import DraftFamily, FamilyProposalService
from domain.ids import new_id
from domain.ports import ModelAdapter
from prompts import load_prompt

Ask = Callable[[str], str]
Say = Callable[[str], None]


def _ask_int(ask: Ask, say: Say, prompt: str, low: int, high: int, default: int) -> int:
    while True:
        answer = ask(f"{prompt} ({low}-{high}) [{default}]: ").strip()
        if not answer:
            return default
        if answer.isdigit() and low <= int(answer) <= high:
            return int(answer)
        say(f"expected an integer {low} to {high}")


def _approved_state_json(conn: sqlite3.Connection) -> str:
    """The proposal input: approved experiences, facts, and capabilities."""
    experiences = SqliteExperienceRepository(conn).list_all()
    facts = [f for f in SqliteCareerFactRepository(conn).list_all()
             if f.user_approved and f.status == "active"]
    capabilities = SqliteCapabilityRepository(conn).list_all()
    return json.dumps({
        "experiences": [{"kind": e.kind, "title": e.title, "org": e.org,
                         "start_date": e.start_date, "end_date": e.end_date}
                        for e in experiences],
        "facts": [{"type": f.fact_type, "statement": f.statement} for f in facts],
        "capabilities": [{"name": c.name, "strength": c.strength} for c in capabilities],
    }, indent=2)


def _confirm_targets(conn: sqlite3.Connection, family_id: str,
                     proposed_names: tuple[str, ...], ask: Ask, say: Say) -> None:
    """User-confirmed TARGETS edges: claim_kind fact, created_by user,
    user_verified 1."""
    capabilities = SqliteCapabilityRepository(conn)
    edges = SqliteCareerEdgeRepository(conn)
    for name in proposed_names:
        capability = capabilities.get_by_name(name)
        if capability is None:
            say(f"  (skipping unknown capability '{name}')")
            continue
        answer = ask(f"  Target capability '{name}'? (y/n) [y]: ").strip().lower()
        if answer in ("", "y", "yes"):
            edges.add(CareerEdge(
                id=new_id("edge"), source_type="role_family", source_id=family_id,
                edge_type="TARGETS", target_type="capability", target_id=capability.id,
                claim_kind="fact", provenance="families:user-confirmation",
                created_by="user", user_verified=1))


def _edit_draft(draft: DraftFamily, ask: Ask) -> DraftFamily:
    name = ask(f"Name [{draft.name}]: ").strip() or draft.name
    rationale = ask(f"Rationale [{draft.rationale}]: ").strip() or draft.rationale
    seniority = ask(f"Target seniority [{draft.target_seniority or ''}]: ").strip() \
        or draft.target_seniority
    return DraftFamily(name=name, rationale=rationale, target_seniority=seniority,
                       adjacent_titles=draft.adjacent_titles,
                       search_vocabulary=draft.search_vocabulary,
                       target_capability_names=draft.target_capability_names)


def _default_objective(conn: sqlite3.Connection) -> str:
    goals = [g for g in SqliteCareerGoalRepository(conn).list_all() if g.status == "active"]
    return goals[0].statement if goals else ""


def run_families_init(conn: sqlite3.Connection, model: ModelAdapter,
                      ask: Ask, say: Say) -> None:
    families_repo = SqliteRoleFamilyRepository(conn)
    if any(f.status == "active" for f in families_repo.list_all()):
        say("Active role families already exist; use families list|add|edit|pause.")
        return
    known = {c.name for c in SqliteCapabilityRepository(conn).list_all()}
    say("Proposing candidate role families from approved state"
        " (model proposes structure, you decide)...")
    service = FamilyProposalService(model, load_prompt("family_proposal.md"))
    drafts = service.propose(_approved_state_json(conn), known)

    confirmed: list[tuple[RoleFamily, DraftFamily]] = []
    for draft in drafts:
        say(f"\nProposed family: {draft.name}\n  rationale: {draft.rationale}"
            f"\n  adjacent titles: {', '.join(draft.adjacent_titles) or '(none)'}"
            f"\n  search vocabulary: {', '.join(draft.search_vocabulary) or '(none)'}"
            f"\n  targets: {', '.join(draft.target_capability_names) or '(none)'}")
        while True:
            action = ask("confirm/edit/reject (c/e/r) [c]: ").strip().lower() or "c"
            if action in ("c", "confirm", "r", "reject"):
                break
            if action in ("e", "edit"):
                draft = _edit_draft(draft, ask)
                break
            say("expected c, e, or r")
        if action in ("r", "reject"):
            continue
        confirmed.append((RoleFamily(
            id=new_id("rf"), name=draft.name, rationale=draft.rationale,
            target_seniority=draft.target_seniority,
            search_vocabulary=draft.search_vocabulary,
            adjacent_titles=draft.adjacent_titles), draft))

    if not confirmed:
        say("No families confirmed; nothing persisted.")
        return

    allocations = {}
    for family, _draft in confirmed:
        allocations[family.id] = _ask_int(ask, say, f"Emphasis for '{family.name}'", 1, 5, 3)
    default = _default_objective(conn)
    objective = ask(f"Strategy objective [{default}]: ").strip() or default
    while not objective:
        say("The schema requires an objective; state one line.")
        objective = ask("Strategy objective: ").strip()

    version = FamilyStrategyService(conn).mint_initial(
        [f for f, _ in confirmed], allocations, objective)
    say(f"Minted approved strategy version {version}.")
    for family, draft in confirmed:
        say(f"\nConfirm targeted capabilities for '{family.name}':")
        _confirm_targets(conn, family.id, draft.target_capability_names, ask, say)
    say("\nRole families ready. Next: open-career package generate <family>.")


def run_families_list(conn: sqlite3.Connection, as_json: bool, say: Say) -> None:
    families = SqliteRoleFamilyRepository(conn).list_all()
    objective, allocations = FamilyStrategyService(conn).current_allocations()
    edges = SqliteCareerEdgeRepository(conn)
    rows = []
    for family in families:
        targets = [e.target_id for e in
                   edges.active_edges_from("role_family", family.id, "TARGETS")]
        rows.append({"id": family.id, "name": family.name, "status": family.status,
                     "allocation": allocations.get(family.id),
                     "targets": targets, "rationale": family.rationale})
    if as_json:
        say(json.dumps({"objective": objective, "families": rows}, indent=2))
        return
    say(f"Strategy objective: {objective or '(none; run families init)'}")
    if not rows:
        say("No role families (run: open-career families init).")
    for row in rows:
        allocation = f"emphasis {row['allocation']}" if row["allocation"] else "no allocation"
        say(f"  {row['id']}  {row['name']} [{row['status']}] {allocation},"
            f" {len(row['targets'])} targeted capabilities")


def resolve_family(conn: sqlite3.Connection, ref: str) -> RoleFamily | None:
    repo = SqliteRoleFamilyRepository(conn)
    family = repo.get(ref)
    if family:
        return family
    matches = [f for f in repo.list_all() if f.name.lower() == ref.lower()]
    return matches[0] if matches else None


def run_families_add(conn: sqlite3.Connection, ask: Ask, say: Say) -> None:
    """Every input is collected before anything persists: interrupted or ended
    input aborts with nothing written (the persist phase asks nothing), so a
    retry never collides with a half-committed family."""
    capabilities = SqliteCapabilityRepository(conn)
    try:
        name = ask("Family name: ").strip()
        if not name:
            say("aborted (empty name); nothing persisted")
            return
        if any(f.name.lower() == name.lower()
               for f in SqliteRoleFamilyRepository(conn).list_all()):
            say(f"a family named '{name}' already exists; nothing persisted")
            raise SystemExit(1)
        rationale = ask("Rationale: ").strip() or name
        seniority = ask("Target seniority (blank to skip): ").strip() or None
        allocation = _ask_int(ask, say, "Emphasis", 1, 5, 3)
        targets = []
        while True:
            capability_name = ask("Target capability name (blank to finish): ").strip()
            if not capability_name:
                break
            capability = capabilities.get_by_name(capability_name)
            if capability is None:
                say(f"  (skipping unknown capability '{capability_name}')")
                continue
            targets.append(capability)
    except (EOFError, KeyboardInterrupt):
        say("aborted (input ended); nothing persisted")
        raise SystemExit(1) from None
    family = RoleFamily(id=new_id("rf"), name=name, rationale=rationale,
                        target_seniority=seniority)
    version = FamilyStrategyService(conn).add_family(family, allocation)
    edges = SqliteCareerEdgeRepository(conn)
    for capability in targets:
        edges.add(CareerEdge(
            id=new_id("edge"), source_type="role_family", source_id=family.id,
            edge_type="TARGETS", target_type="capability", target_id=capability.id,
            claim_kind="fact", provenance="families:user-confirmation",
            created_by="user", user_verified=1))
    say(f"Added '{name}' ({family.id}); minted strategy version {version};"
        f" {len(targets)} targeted capabilities.")


def run_families_edit(conn: sqlite3.Connection, ref: str, ask: Ask, say: Say) -> None:
    family = resolve_family(conn, ref)
    if family is None:
        say(f"unknown family '{ref}'")
        raise SystemExit(1)
    service = FamilyStrategyService(conn)
    _objective, allocations = service.current_allocations()
    current = allocations.get(family.id)
    say(f"Editing '{family.name}' (status {family.status},"
        f" emphasis {current if current is not None else 'none'})")
    answer = ask(f"New emphasis (1-5, blank to keep): ").strip()
    if answer:
        if not (answer.isdigit() and 1 <= int(answer) <= 5):
            say("expected an integer 1 to 5")
            raise SystemExit(1)
        version = service.set_emphasis(family.id, int(answer))
        say(f"Minted strategy version {version}.")
    while True:
        capability_name = ask("Add target capability (blank to finish): ").strip()
        if not capability_name:
            break
        _confirm_targets(conn, family.id, (capability_name,), ask, say)


def run_families_pause(conn: sqlite3.Connection, ref: str, say: Say) -> None:
    family = resolve_family(conn, ref)
    if family is None:
        say(f"unknown family '{ref}'")
        raise SystemExit(1)
    version = FamilyStrategyService(conn).set_status(family.id, "paused")
    say(f"Paused '{family.name}'; minted strategy version {version}"
        " (allocation removed, copy-forward).")
