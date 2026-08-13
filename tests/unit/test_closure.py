"""Closure planning (OC-37 §3): absence streaks over committed snapshots, the
mass-closure suspect cohort with row-level confirmation on the immediately
next snapshot, reappearance resets, and the vendor-token single-poll collapse."""

from domain.closure import (
    OpportunityState,
    PendingCohort,
    plan_snapshot,
)


def open_state(opp_id: str, streak: int = 0, last_absence: str | None = None,
               availability: str = "open") -> OpportunityState:
    return OpportunityState(opportunity_id=opp_id, availability=availability,
                            absence_streak=streak,
                            last_absence_snapshot_id=last_absence)


# --- ordinary streak ------------------------------------------------------------

def test_first_absence_is_observed_not_closed():
    plan = plan_snapshot("snap2", present_ids=set(), known=[open_state("opp1")])
    assert plan.closures == ()
    assert plan.absences == (("opp1", 1),)


def test_second_consecutive_absence_closes_with_both_confirming_snapshots():
    plan = plan_snapshot("snap3", present_ids=set(),
                         known=[open_state("opp1", streak=1, last_absence="snap2")])
    assert plan.closures == (("opp1", ("snap2", "snap3")),)
    assert plan.absences == ()


def test_presence_resets_the_streak():
    plan = plan_snapshot("snap3", present_ids={"opp1"},
                         known=[open_state("opp1", streak=1, last_absence="snap2")])
    assert plan.present == ("opp1",)
    assert plan.absences == () and plan.closures == ()


def test_closed_posting_present_again_is_reopened():
    plan = plan_snapshot("snap9", present_ids={"opp1"},
                         known=[open_state("opp1", availability="closed")])
    assert plan.reopened == ("opp1",)


def test_closed_posting_still_absent_stays_untouched():
    plan = plan_snapshot("snap9", present_ids=set(),
                         known=[open_state("opp1", availability="closed")])
    assert plan.absences == () and plan.closures == ()


def test_vendor_version_token_collapses_closure_to_one_poll():
    plan = plan_snapshot("snap2", present_ids=set(), known=[open_state("opp1")],
                         authoritative=True)
    assert plan.closures == (("opp1", ("snap2",)),)


# --- mass-closure guard -----------------------------------------------------------

def _many(prefix: str, count: int) -> list[OpportunityState]:
    return [open_state(f"{prefix}{i}") for i in range(count)]


def test_mass_disappearance_forms_a_cohort_instead_of_streaks_toward_closure():
    known = _many("opp", 20)
    plan = plan_snapshot("snapA", present_ids=set(), known=known)
    assert plan.closures == ()
    assert set(plan.new_cohort_member_ids) == {s.opportunity_id for s in known}
    assert plan.notes and "mass-closure guard" in plan.notes[0]


def test_guard_needs_the_minimum_absolute_size():
    # 5 of 5 disappear: 100% but under the minimum of 10, so ordinary rules.
    plan = plan_snapshot("snapA", present_ids=set(), known=_many("opp", 5))
    assert plan.new_cohort_member_ids == ()
    assert len(plan.absences) == 5


def test_guard_needs_the_fraction_exceeded():
    # 10 of 30 disappear: over the minimum but only 33%, ordinary rules.
    known = _many("opp", 30)
    present = {s.opportunity_id for s in known[:20]}
    plan = plan_snapshot("snapA", present_ids=present, known=known)
    assert plan.new_cohort_member_ids == ()
    assert len(plan.absences) == 10


def test_cohort_members_close_after_exactly_one_confirming_poll():
    known = _many("opp", 20)
    cohort = PendingCohort(
        cohort_id="coh1", triggering_snapshot_id="snapA",
        pending_member_ids=frozenset(s.opportunity_id for s in known))
    plan = plan_snapshot("snapB", present_ids=set(),
                         known=[open_state(s.opportunity_id, streak=1, last_absence="snapA")
                                for s in known],
                         pending_cohort=cohort)
    assert set(plan.cohort_closed) == set(cohort.pending_member_ids)
    assert plan.resolved_cohort_id == "coh1"
    assert plan.closures == ()


def test_reappearing_cohort_member_leaves_the_cohort_unclosed():
    cohort = PendingCohort("coh1", "snapA", frozenset({"opp1", "opp2"}))
    plan = plan_snapshot(
        "snapB", present_ids={"opp1"},
        known=[open_state("opp1", streak=1, last_absence="snapA"),
               open_state("opp2", streak=1, last_absence="snapA")],
        pending_cohort=cohort)
    assert plan.cohort_reappeared == ("opp1",)
    assert plan.cohort_closed == ("opp2",)


def test_newly_absent_rows_never_join_or_reset_the_prior_cohort():
    # opp1/opp2 are the prior cohort; opp3 disappears in the confirming
    # snapshot: it starts an ordinary streak (or its own cohort if massive),
    # while the prior cohort's confirmation proceeds row-level.
    cohort = PendingCohort("coh1", "snapA", frozenset({"opp1", "opp2"}))
    plan = plan_snapshot(
        "snapB", present_ids=set(),
        known=[open_state("opp1", streak=1), open_state("opp2", streak=1),
               open_state("opp3")],
        pending_cohort=cohort)
    assert set(plan.cohort_closed) == {"opp1", "opp2"}
    assert plan.absences == (("opp3", 1),)


def test_ordinary_streak_applies_only_outside_a_cohort():
    # A cohort member at streak 1 absent again closes via cohort confirmation
    # with the trigger snapshot as first confirming id, not via the streak.
    cohort = PendingCohort("coh1", "snapA", frozenset({"opp1"}))
    plan = plan_snapshot("snapB", present_ids=set(),
                         known=[open_state("opp1", streak=1, last_absence="snapA")],
                         pending_cohort=cohort)
    assert plan.cohort_closed == ("opp1",)
    assert plan.closures == ()
