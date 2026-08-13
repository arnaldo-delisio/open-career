"""Closure planning (OC-37 §3): versioning is the closure mechanism. A posting
vanishing from the poll is the only closure signal, so closure is a consecutive
absence streak over the source's committed complete snapshots, guarded against
mass disappearance by suspect cohorts.

This module is a pure planner: given the committed snapshot's presence set and
the source's current opportunity state, it returns the effects to apply. It
never sees a connection; the repository applies the plan in one transaction.
Absence only ever comes from an atomically committed complete snapshot: a
degraded source commits no snapshot, so no plan exists and nothing closes,
which is the rule that stops an outage from mass-closing the pipeline.
"""

from dataclasses import dataclass, field

# §4 defaults, config: the guard triggers when the disappearance set exceeds
# this percentage of the source's live postings, with a minimum absolute size.
MASS_CLOSURE_GUARD_PERCENT = 50
MASS_CLOSURE_GUARD_MIN = 10
CLOSURE_STREAK = 2


@dataclass(frozen=True)
class OpportunityState:
    """The slice of an opportunity the planner needs."""

    opportunity_id: str
    availability: str  # open | closed | reopened
    absence_streak: int
    last_absence_snapshot_id: str | None = None


@dataclass(frozen=True)
class PendingCohort:
    """An unresolved suspect cohort: confirmation is row-level against the
    immediately next consecutive committed snapshot, which in the per-source
    committed sequence is exactly the snapshot being planned."""

    cohort_id: str
    triggering_snapshot_id: str
    pending_member_ids: frozenset[str]


@dataclass(frozen=True)
class ClosurePlan:
    """Effects of one committed snapshot, applied transactionally by the repo."""

    snapshot_id: str
    # Present rows: streak reset to zero, last_seen advanced.
    present: tuple[str, ...] = ()
    # Present rows that were closed: availability -> reopened, history kept.
    reopened: tuple[str, ...] = ()
    # Absent rows not closing yet: (opportunity_id, new_streak); the snapshot
    # becomes their last_absence_snapshot_id when the streak starts at one.
    absences: tuple[tuple[str, int], ...] = ()
    # Rows closing: (opportunity_id, confirming snapshot ids). Two ids for the
    # streak rule, one where a vendor version token collapses it (§1).
    closures: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # Cohort resolution against the prior pending cohort.
    resolved_cohort_id: str | None = None
    cohort_closed: tuple[str, ...] = ()
    cohort_reappeared: tuple[str, ...] = ()
    # A new suspect cohort forming on this snapshot's disappearance set.
    new_cohort_member_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default=())


def plan_snapshot(
    snapshot_id: str,
    present_ids: set[str],
    known: list[OpportunityState],
    pending_cohort: PendingCohort | None = None,
    authoritative: bool = False,
    guard_percent: int = MASS_CLOSURE_GUARD_PERCENT,
    guard_min: int = MASS_CLOSURE_GUARD_MIN,
) -> ClosurePlan:
    """Plan the effects of one committed complete snapshot.

    present_ids holds opportunity ids present in the snapshot (the caller maps
    external job ids to opportunities; unknown external ids are new
    opportunities, minted outside this planner). authoritative means the
    vendor offered a snapshot/version token, collapsing the two-poll rule to
    one and bypassing the cohort guard: the vendor asserts consistency, so a
    single stated absence is decisive (§1)."""
    present: list[str] = []
    reopened: list[str] = []
    absences: list[tuple[str, int]] = []
    closures: list[tuple[str, tuple[str, ...]]] = []
    cohort_closed: list[str] = []
    cohort_reappeared: list[str] = []
    notes: list[str] = []

    by_id = {state.opportunity_id: state for state in known}
    pending_members = pending_cohort.pending_member_ids if pending_cohort else frozenset()

    # 1. Resolve the prior cohort row-level against this snapshot: members
    # still absent close (confirming ids: trigger + this snapshot); members
    # present leave the cohort. Confirmation is against the immutable first
    # cohort only; nothing this snapshot adds can reset it.
    for member_id in sorted(pending_members):
        state = by_id.get(member_id)
        if member_id in present_ids:
            cohort_reappeared.append(member_id)
        elif state is not None and state.availability in ("open", "reopened"):
            cohort_closed.append(member_id)
        # A member already closed by other means resolves silently as closed.

    # 2. Presence handling for everything not resolved above.
    live_absent: list[OpportunityState] = []
    for state in known:
        if state.opportunity_id in pending_members:
            continue
        if state.opportunity_id in present_ids:
            if state.availability == "closed":
                reopened.append(state.opportunity_id)
            else:
                present.append(state.opportunity_id)
        elif state.availability in ("open", "reopened"):
            live_absent.append(state)

    # 3. Mass-closure guard (skipped for authoritative snapshots): measured on
    # this snapshot's DISAPPEARANCE SET, the rows entering absence now (streak
    # zero), never rows already mid-streak (§3: "the guard measures the
    # disappearance set, not rows already at streak one"). Rows mid-streak
    # keep the ordinary rule and close at streak two regardless of the guard.
    fresh_absent = [s for s in live_absent if s.absence_streak == 0]
    live_count = sum(1 for s in known if s.availability in ("open", "reopened"))
    guard_triggers = (
        not authoritative
        and live_count > 0
        and len(fresh_absent) >= guard_min
        and len(fresh_absent) * 100 > live_count * guard_percent
    )
    new_cohort_member_ids: tuple[str, ...] = ()
    if guard_triggers:
        notes.append(
            f"mass-closure guard: {len(fresh_absent)} of {live_count} live postings"
            " disappeared; suspect cohort formed, closure deferred to the next"
            " consecutive snapshot")
        new_cohort_member_ids = tuple(s.opportunity_id for s in fresh_absent)
        absences.extend((s.opportunity_id, s.absence_streak + 1)
                        for s in fresh_absent)

    # 4. Ordinary absence streaks (outside any cohort). Closure fires at two
    # consecutive absences, the confirming snapshots consecutive in committed
    # order; an authoritative snapshot closes on one.
    ordinary = live_absent if not guard_triggers else \
        [s for s in live_absent if s.absence_streak > 0]
    for state in ordinary:
        new_streak = state.absence_streak + 1
        if authoritative:
            closures.append((state.opportunity_id, (snapshot_id,)))
        elif new_streak >= CLOSURE_STREAK:
            first = state.last_absence_snapshot_id
            confirming = (first, snapshot_id) if first else (snapshot_id,)
            closures.append((state.opportunity_id, confirming))
        else:
            absences.append((state.opportunity_id, new_streak))

    return ClosurePlan(
        snapshot_id=snapshot_id,
        present=tuple(present),
        reopened=tuple(reopened),
        absences=tuple(absences),
        closures=tuple(closures),
        resolved_cohort_id=pending_cohort.cohort_id if pending_cohort else None,
        cohort_closed=tuple(cohort_closed),
        cohort_reappeared=tuple(cohort_reappeared),
        new_cohort_member_ids=new_cohort_member_ids,
        notes=tuple(notes),
    )
