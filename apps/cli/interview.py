"""Registry-generated interview flows (OC-35; spec: the scope's
decisions/onboarding-interview-design.md).

Sitting one's must-ask block (`run_tier1`, attached at the end of `onboard`
after the CV walk and the families step) and sitting two (`run_deepen`:
remaining canonical fields, additional evidence intake, metric catch-up).
Questions are generated from the typed registry in domain/questions.py; every
question shows its current value, blank is skip (skip is never a decline), and
answers write only through the audited profile and policy seams. Nothing here
calls the model.
"""

import hashlib
import sqlite3
import time
from typing import Callable

from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_entities import (
    SqliteCareerFactRepository,
    SqliteEvidenceRepository,
)
from adapters.storage.sqlite_policies import SqliteUserPolicyRepository
from adapters.storage.tx import transaction
from adapters.storage.sqlite_profile import SqliteUserProfileRepository
from domain import dates
from domain.edges import CareerEdge
from domain.entities import CareerFact, Evidence
from domain.ids import new_id
from domain.policies import EEO_STANCES, PERIOD_FACTORS
from domain.profile import InvalidProfileValueError, authorization_contradiction
from domain.questions import TIER1, TIER2, Question, questions

Ask = Callable[[str], str]
Say = Callable[[str], None]

# Asked by the onboarding basics walk already; the tier-1 block skips them so
# one sitting never asks the same field twice.
BASICS_ASKED_IN_CV_WALK = ("full_name", "email", "phone", "location")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def is_unquantified(statement: str) -> bool:
    """The metric-backfill detection heuristic, deliberately this simple: a
    fact with no digit anywhere in its statement is unquantified."""
    return not any(ch.isdigit() for ch in statement)


# Fact types where a scope/mechanism quantifier makes sense; 'other' (e.g.
# exclusion lists, stances) is never asked for a number (drive finding).
QUANTIFIABLE_FACT_TYPES = frozenset(
    {"achievement", "responsibility", "scope", "metric", "skill_use"})


def ask_yes_no(ask: Ask, say: Say, prompt: str, default: bool) -> bool:
    """The one validated y/n prompt: y/yes/n/no, blank takes the default,
    anything else re-asks (garbage is never consumed as data)."""
    default_hint = "y" if default else "n"
    while True:
        raw = ask(f"{prompt} (y/n) [{default_hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        say("invalid choice, expected y/n")


def ask_choice(ask: Ask, say: Say, prompt: str, choices: tuple[str, ...],
               default: str) -> str:
    """The one closed-vocabulary prompt: blank takes the default, anything
    outside the vocabulary re-asks."""
    while True:
        answer = ask(f"{prompt} ({'/'.join(choices)}) [{default}]: ").strip().lower()
        if not answer:
            return default
        if answer in choices:
            return answer
        say(f"invalid choice, expected {'/'.join(choices)}")


def stored_date(value: str | None) -> str | None:
    """The stored form of a date label: an open-ended role stores null, which
    is the one representation the ordering and coherence rules read as
    'has not ended' (domain/dates.py)."""
    return None if dates.is_open_ended(value) else value


def ask_date(ask: Ask, say: Say, label: str, current: str | None,
             editing: bool) -> str | None:
    """A date the system can compare and order. Asked when the walk is in edit
    mode, and whenever the extracted label is one the canonical parser cannot
    read: an unreadable date silently accepted here is a package that can never
    clear the Gauntlet's date-coherence check, discovered six minutes later."""
    if not editing and dates.is_readable(current):
        return stored_date(current)
    if not editing:
        say(f"  '{current}' is not a date this system can order.")
    while True:
        raw = ask(f"{label} [{current or ''}]: ").strip()
        value = raw or current
        if dates.is_readable(value):
            return stored_date(value)
        say("  expected a month and year (e.g. '2015-09', 'September 2015',"
            " '09/2015'), a year ('2015'), or 'present' for an ongoing role")


def write_stated_fact(conn: sqlite3.Connection,
                      evidence_supplier: Callable[[], Evidence], statement: str,
                      fact_type: str, provenance: str,
                      experience_id: str | None = None) -> CareerFact:
    """The one way a user-stated fact is created: the fact row (interview
    sourced, approved by the act of stating it, active, stamped verified) and
    the PROVES edge from the evidence row backing it, in one transaction. Every
    flow that takes a fact from the human goes through here, so the shape
    cannot drift between the sittings, the state commands and package review.

    `provenance` is the one deliberate difference between the callers, passed
    in rather than fixed here: provenance records where a row actually came
    from, and flattening the flows into one value would make the graph less
    honest, not more. `evidence_supplier` is called inside the transaction, so
    a caller that mints its evidence row lazily (one row per sitting) mints it
    atomically with the first fact that needs it. The boundary is reentrant, so
    a caller writing several facts as one unit keeps its own atomicity."""
    with transaction(conn):
        fact = CareerFact(
            id=new_id("fact"), fact_type=fact_type, statement=statement,
            source="interview", user_approved=1, experience_id=experience_id,
            verified_at=_now())
        SqliteCareerFactRepository(conn).add(fact)
        SqliteCareerEdgeRepository(conn).add(CareerEdge(
            id=new_id("edge"), source_type="evidence", source_id=evidence_supplier().id,
            edge_type="PROVES", target_type="career_fact", target_id=fact.id,
            claim_kind="fact", provenance=provenance,
            created_by="user", user_verified=1))
    return fact


def write_capability_link(conn: sqlite3.Connection, capability_id: str,
                          evidence_id: str, provenance: str,
                          experience_id: str | None = None) -> None:
    """The one way an evidence row is linked to a capability: the SUPPORTS edge
    that makes the facts that evidence proves reachable by the package walk
    (family -> capability -> SUPPORTS -> PROVES -> fact), plus, when the link
    comes from an experience, the DEMONSTRATES summary edge, skipped when an
    active one already exists. One transaction, so a link never lands half
    made, and one writer, so the story bank, capability deepening and the
    experience commands cannot drift apart.

    Which capabilities an item demonstrates is always the user's answer and
    never a default: a blanket link from one item to every capability is the
    meaningless edge OC-39 deleted, so callers ask and pass only what was
    chosen."""
    edges_repo = SqliteCareerEdgeRepository(conn)
    with transaction(conn):
        supports = edges_repo.active_edges_to("capability", capability_id, "SUPPORTS")
        # Idempotent by design: a flow that writes facts one at a time asks for
        # the same link after each of them, and re-stating a link is not an
        # error, it is a no-op.
        if not any(e.source_id == evidence_id for e in supports):
            edges_repo.add(CareerEdge(
                id=new_id("edge"), source_type="evidence", source_id=evidence_id,
                edge_type="SUPPORTS", target_type="capability",
                target_id=capability_id, claim_kind="fact", provenance=provenance,
                created_by="user", user_verified=1))
        if experience_id is None:
            return
        if any(e.source_id == experience_id for e in edges_repo.active_edges_to(
                "capability", capability_id, "DEMONSTRATES")):
            return
        edges_repo.add(CareerEdge(
            id=new_id("edge"), source_type="experience", source_id=experience_id,
            edge_type="DEMONSTRATES", target_type="capability",
            target_id=capability_id, claim_kind="fact", provenance=provenance,
            created_by="user", user_verified=1))


def offer_quantifier(facts_repo: SqliteCareerFactRepository, fact_id: str,
                     statement: str, fact_type: str, ask: Ask, say: Say) -> None:
    """One optional follow-up on an unquantified confirmed fact. A supplied
    restatement is a user edit of the statement, approved by the act of
    stating it; the system never proposes a number (OC-5, spec: metric
    backfill). Skip costs one keypress."""
    if fact_type not in QUANTIFIABLE_FACT_TYPES or not is_unquantified(statement):
        return
    while True:
        restated = ask(
            "  No number in that fact. Restate it with one (e.g. 'N clients',"
            " 'X/day', 'team of Y'; blank to skip): ").strip()
        if not restated:
            return  # blank skips, exactly as the prompt promises
        if is_unquantified(restated):
            # The approved statement is never overwritten by an answer that
            # does not do what the prompt asked: a stray 'confirm' typed here
            # used to replace a real achievement and ride into the PDF.
            say("  that restatement has no number in it either; the fact is"
                " unchanged (blank to skip)")
            continue
        facts_repo.set_approval(fact_id, restated, _now())
        say("  updated (user edit, approved).")
        return


def _ask_profile_question(profile_repo: SqliteUserProfileRepository,
                          question: Question, ask: Ask, say: Say) -> None:
    current = profile_repo.get_fields().get(question.key)
    hint = f" ({'/'.join(question.choices)})" if question.choices else ""
    while True:
        raw = ask(f"{question.prompt}{hint} [{current or ''}]: ").strip()
        if not raw:
            return  # blank = skip; the field stays as it is
        try:
            profile_repo.set_field(question.key, raw, source="user_edit")
            return
        except InvalidProfileValueError as e:
            say(f"invalid value: {e}")


def _resolve_authorization_contradiction(profile_repo: SqliteUserProfileRepository,
                                         tier1: list[Question], ask: Ask, say: Say) -> None:
    """Two answers that read as opposites downstream are named here, once,
    while the human is still in the prompt. Never silently corrected: the
    combination can be genuinely true (a time-limited permit), so the human
    keeps or revises it."""
    conflict = authorization_contradiction(profile_repo.get_fields())
    if conflict is None:
        return
    say(f"\nheads up: {conflict}.")
    if ask_yes_no(ask, say, "Keep both answers as given?", True):
        return
    for question in tier1:
        if question.key in ("authorized_in_country", "needs_sponsorship"):
            _ask_profile_question(profile_repo, question, ask, say)


def _ask_int(ask: Ask, say: Say, prompt: str) -> int | None:
    """A positive integer amount, or None on blank (skip)."""
    while True:
        raw = ask(prompt).strip().replace(",", "").replace("_", "")
        if not raw:
            return None
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        say("expected a whole positive number (no decimals; OC-22)")


def _ask_choice(ask: Ask, say: Say, prompt: str, choices: tuple[str, ...],
                default: str | None = None) -> str | None:
    while True:
        raw = ask(f"{prompt} ({'/'.join(choices)}) [{default or ''}]: ").strip().lower()
        if not raw:
            return default
        if raw in choices:
            return raw
        say(f"expected one of {'/'.join(choices)}")


def _ask_compensation_floor(policies: SqliteUserPolicyRepository, ask: Ask, say: Say) -> None:
    current = policies.get_policies().get("compensation_floor")
    say(f"\nCompensation floor (postings below it auto-fail the gate; absent"
        f" floor means the gate skips the comp check). Current: {current or '(unset)'}")
    amount = _ask_int(ask, say, "Floor amount (blank to skip): ")
    if amount is None:
        return
    currency = _ask_currency(ask, say)
    period = _ask_choice(ask, say, "Period", tuple(sorted(PERIOD_FACTORS)), "annual")
    policies.set_policy("compensation_floor",
                        {"amount": amount, "currency": currency, "period": period},
                        source="user_edit")
    say("compensation_floor set.")


def _ask_currency(ask: Ask, say: Say) -> str:
    while True:
        raw = ask("Currency (3-letter code, e.g. EUR): ").strip().upper()
        if len(raw) == 3 and raw.isalpha():
            return raw
        say("expected a 3-letter currency code")


def _ask_compensation_target(policies: SqliteUserPolicyRepository, ask: Ask, say: Say) -> None:
    current = policies.get_policies().get("compensation_target")
    say(f"\nCompensation target (range forms get the range; single-number"
        f" fields get your chosen scalar). Current: {current or '(unset)'}")
    low = _ask_int(ask, say, "Target range minimum (blank to skip): ")
    if low is None:
        return
    while True:
        high = _ask_int(ask, say, "Target range maximum: ")
        if high is not None and high >= low:
            break
        say(f"the maximum must be at least {low}")
    currency = _ask_currency(ask, say)
    period = _ask_choice(ask, say, "Period", tuple(sorted(PERIOD_FACTORS)), "annual")
    scalar = _ask_choice(
        ask, say, "Which value should a single-number salary field receive?",
        ("min", "mid", "max"), "mid")
    policies.set_policy("compensation_target",
                        {"min": low, "max": high, "currency": currency,
                         "period": period, "scalar": scalar},
                        source="user_edit")
    say("compensation_target set.")


# Interactive builders for tiered policies, keyed by registry key so the
# interview dispatches from the registry: a policy moved into TIER1/TIER2
# without a handler here fails the registry test, never silently goes unasked.
_POLICY_ASKERS: dict[str, Callable] = {
    "compensation_floor": _ask_compensation_floor,
    "compensation_target": _ask_compensation_target,
    # _ask_eeo_stance is defined below; registered right after its definition.
}


def _ask_tier_policies(policies: SqliteUserPolicyRepository, tier, ask: Ask, say: Say) -> None:
    for question in questions(kind="policy", tier=tier):
        _POLICY_ASKERS[question.key](policies, ask, say)


def run_tier1(conn: sqlite3.Connection, ask: Ask, say: Say) -> None:
    """Sitting one's must-ask block: work authorization, location logistics,
    notice period, compensation floor + target. Runs after the CV walk and the
    families step (sequencing per the design; the families flow itself is
    OC-33's, untouched here)."""
    profile_repo = SqliteUserProfileRepository(conn)
    policies = SqliteUserPolicyRepository(conn)
    say("\nMust-ask questions (blank skips; a skipped item surfaces as a named"
        " manual action when an application needs it):")
    tier1 = [q for q in questions(kind="profile", tier=TIER1)
             if q.key not in BASICS_ASKED_IN_CV_WALK]
    for question in tier1:
        _ask_profile_question(profile_repo, question, ask, say)
    _resolve_authorization_contradiction(profile_repo, tier1, ask, say)
    _ask_tier_policies(policies, TIER1, ask, say)
    say("\nSitting one done. Whenever you want more depth:"
        " `open-career deepen` (remaining fields, evidence, metric catch-up),"
        " then `open-career stories` (depth interview).")


def _ask_eeo_stance(policies: SqliteUserPolicyRepository, ask: Ask, say: Say) -> None:
    current = policies.get_policies().get("eeo_stance")
    say(f"\nEEO stance (answer_honestly fills EEO blocks from your EEO fields;"
        f" always_decline selects a decline option only where the form"
        f" verifiably offers one; per_application asks each time)."
        f" Current: {current or '(unset; behaves as per_application)'}")
    stance = _ask_choice(ask, say, "EEO stance", EEO_STANCES, None)
    if stance is not None:
        policies.set_policy("eeo_stance", stance, source="user_edit")
        say("eeo_stance set.")


_POLICY_ASKERS["eeo_stance"] = _ask_eeo_stance


def _ask_never_render(policies: SqliteUserPolicyRepository, ask: Ask, say: Say) -> None:
    """The never-render list (OC-26 personal boundaries): literal strings
    that must not appear in any rendered artifact, written through the
    audited policy seam and consumed by the Gauntlet's stage-zero
    user-constraints check via the policy snapshot."""
    current = policies.get_policies().get("never_render") or []
    raw = ask("Strings that must never appear in a rendered CV (personal"
              " boundaries, comma-separated; blank keeps"
              f" [{', '.join(current)}]): ").strip()
    if not raw:
        return
    additions = [part.strip() for part in raw.split(",") if part.strip()]
    merged = list(dict.fromkeys(current + additions))
    policies.set_policy("never_render", merged, source="user_edit")
    say("never_render updated.")


_POLICY_ASKERS["never_render"] = _ask_never_render


_EVIDENCE_INTAKE_TYPES = ("repository", "portfolio", "url", "artifact", "document")


def run_evidence_intake(conn: sqlite3.Connection, ask: Ask, say: Say,
                        checkpoint: Callable[[], bool] | None = None) -> None:
    """Additional evidence intake: repositories, portfolio pieces, URLs, each
    minted as an evidence row. Optionally offers a user-stated fact per item,
    with a PROVES edge, so the new evidence can reach the graph."""
    evidence_repo = SqliteEvidenceRepository(conn)
    facts_repo = SqliteCareerFactRepository(conn)
    say("\nAdditional evidence (repos, portfolio pieces, URLs; blank type to finish):")
    while True:
        evidence_type = _ask_choice(ask, say, "Evidence type", _EVIDENCE_INTAKE_TYPES, None)
        if evidence_type is None:
            return
        title = ask("Title: ").strip()
        if not title:
            say("skipped (a title is required).")
            continue
        locator = ask("URL or path: ").strip() or None
        evidence = Evidence(id=new_id("ev"), evidence_type=evidence_type,
                            title=title, locator=locator)
        evidence_repo.add(evidence)
        say(f"  evidence added: {title}")
        statement = ask(
            "One user-stated fact it proves (blank to skip): ").strip()
        if statement:
            fact = write_stated_fact(conn, lambda: evidence, statement, "other",
                                     "interview:evidence-intake")
            offer_quantifier(facts_repo, fact.id, statement, fact.fact_type, ask, say)
        if checkpoint is not None and not checkpoint():
            return


def run_metric_catchup(conn: sqlite3.Connection, ask: Ask, say: Say) -> None:
    """The catch-up pass: revisit only the approved active facts still
    unquantified, once each (spec: metric backfill, layer 2)."""
    facts_repo = SqliteCareerFactRepository(conn)
    unquantified = [f for f in facts_repo.list_all()
                    if f.user_approved and f.status == "active"
                    and f.fact_type in QUANTIFIABLE_FACT_TYPES
                    and is_unquantified(f.statement)]
    if not unquantified:
        say("\nMetric catch-up: every quantifiable approved fact already carries a number.")
        return
    say(f"\nMetric catch-up: {len(unquantified)} approved facts carry no number."
        " Honest quantifiers only; blank skips.")
    for fact in unquantified:
        say(f"\n[{fact.fact_type}] {fact.statement}")
        offer_quantifier(facts_repo, fact.id, fact.statement, fact.fact_type, ask, say)


# Deepen items with no canonical profile field (OC-29's set is closed) and no
# policy key (OC-35's set is closed): they land as approved interview-sourced
# facts, user-stated, provenance-carrying. The design doc lists them for
# sitting two; a canonical or policy home would reopen a locked set.
_STATEMENT_FACT_ASKS = (
    ("skill_use", "Languages you speak, with levels"),
    ("other", "Travel willingness (e.g. up to 20%)"),
)


def _ask_hard_exclusions(policies: SqliteUserPolicyRepository, ask: Ask, say: Say) -> None:
    """Hard exclusions have exactly one home: `industry_pref.out`, written
    through the audited policy seam and consumed deterministically by the
    eligibility gate. Entries append to whatever the richer stories cluster
    already holds; no fact is minted (drive reconciliation)."""
    current = policies.get_policies().get("industry_pref") or {"in": [], "out": []}
    raw = ask("Hard exclusions: companies or domains you will not apply to"
              f" (comma-separated) [{', '.join(current['out'])}]: ").strip()
    if not raw:
        return
    additions = [part.strip() for part in raw.split(",") if part.strip()]
    # One ordered unique list across current entries and additions, first
    # occurrence wins: duplicates inside one answer, and any already-present
    # duplicates, both collapse (Codex round 5).
    merged = list(dict.fromkeys(current["out"] + additions))
    policies.set_policy("industry_pref", {"in": current["in"], "out": merged},
                        source="user_edit")
    say("industry_pref.out updated.")


def _ask_statement_facts(conn: sqlite3.Connection, ask: Ask, say: Say) -> None:
    evidence_repo = SqliteEvidenceRepository(conn)
    say("\nStated facts (no form field of their own; blank skips):")
    interview_evidence: Evidence | None = None

    def evidence_row() -> Evidence:
        # One row per sitting, minted with the first fact that needs it.
        nonlocal interview_evidence
        if interview_evidence is None:
            interview_evidence = Evidence(
                id=new_id("ev"), evidence_type="user_statement",
                title=f"Deepen interview {_now()[:10]}")
            evidence_repo.add(interview_evidence)
        return interview_evidence

    for fact_type, prompt in _STATEMENT_FACT_ASKS:
        statement = ask(f"{prompt}: ").strip()
        if not statement:
            continue
        write_stated_fact(conn, evidence_row, statement, fact_type,
                          "interview:deepen")


def run_deepen(conn: sqlite3.Connection, ask: Ask, say: Say) -> None:
    """Sitting two: tier-2 registry items, any tier-1 canonical field still
    unset, evidence intake, metric catch-up. Nothing blocks if it never runs."""
    profile_repo = SqliteUserProfileRepository(conn)
    policies = SqliteUserPolicyRepository(conn)
    say("Deepen: remaining profile fields (blank skips):")
    current = profile_repo.get_fields()
    for question in questions(kind="profile", tier=TIER2):
        _ask_profile_question(profile_repo, question, ask, say)
    _ask_tier_policies(policies, TIER2, ask, say)
    remaining_tier1 = [q for q in questions(kind="profile", tier=TIER1)
                       if not current.get(q.key)]
    if remaining_tier1:
        say("\nStill-unset must-ask fields:")
        for question in remaining_tier1:
            _ask_profile_question(profile_repo, question, ask, say)
    _ask_statement_facts(conn, ask, say)
    _ask_hard_exclusions(policies, ask, say)
    run_evidence_intake(conn, ask, say)
    run_metric_catchup(conn, ask, say)
    say("\nDeepen done. The depth interview is `open-career stories`.")


def store_statement_file(storage, kind: str, evidence_id: str, text: str) -> tuple[str, str]:
    """Persist an authored statement as an instance file; returns (locator,
    sha256). Locators derive from the evidence id, so re-telling never
    overwrites an earlier file out from under its evidence row's hash."""
    locator = f"files/{kind}/{evidence_id}.md"
    data = text.encode()
    storage.write_bytes(locator, data)
    return locator, hashlib.sha256(data).hexdigest()
