"""Promotion ordering and the locked run budget (OC-37 §4): frozen ordering
keys, half-cap aging, bounded retry, and exhaustion as a safe stop signal."""

import json

import pytest

from domain.budget import Budget, BudgetLedger
from domain.promotion import (
    PendingRow,
    coverage_priority_key,
    lane_rank,
    pre_extraction_priority_key,
    retry_outcome,
    select_for_stage,
)


# --- ordering keys ---------------------------------------------------------------

def test_lane_ranks_put_curated_first():
    assert lane_rank("curated") < lane_rank("manual") < lane_rank("harvest")
    with pytest.raises(ValueError):
        lane_rank("scraped")


def test_pre_extraction_key_orders_lane_then_recency_then_id():
    older_curated = pre_extraction_priority_key(1, 0, "2026-08-01T00:00:00Z", "opp_a")
    newer_curated = pre_extraction_priority_key(1, 0, "2026-08-10T00:00:00Z", "opp_b")
    harvest = pre_extraction_priority_key(1, 2, "2026-08-12T00:00:00Z", "opp_c")
    assert newer_curated < older_curated < harvest


def test_relevance_leads_the_pre_extraction_key():
    # The exact inversion that caused the bug: one company's off-target
    # postings arrived on the best lane and monopolised the paid stages, while
    # on-target harvest-lane roles waited behind them.
    relevant_harvest = pre_extraction_priority_key(
        3, 2, "2026-08-01T00:00:00Z", "opp_relevant")
    irrelevant_curated = pre_extraction_priority_key(
        1, 0, "2026-08-12T00:00:00Z", "opp_irrelevant")
    assert relevant_harvest < irrelevant_curated
    # Within one relevance level the existing terms are untouched.
    assert (pre_extraction_priority_key(3, 0, "2026-08-01T00:00:00Z", "opp_a")
            < pre_extraction_priority_key(3, 2, "2026-08-12T00:00:00Z", "opp_b"))


def test_coverage_key_orders_highest_coverage_first():
    assert coverage_priority_key(9000, 5) < coverage_priority_key(2500, 1)


# --- half-cap aging selection -------------------------------------------------------

def rows_for(specs):
    return [PendingRow(row_id=r, enqueue_seq=seq, priority_key=key)
            for r, seq, key in specs]


def test_selection_reserves_half_the_cap_for_the_oldest_rows():
    # Old harvest rows (worst priority) versus a stream of new curated rows:
    # aging guarantees the old rows half the cap.
    rows = rows_for(
        [(f"old{i}", i, (2, i)) for i in range(4)]
        + [(f"new{i}", 100 + i, (0, i)) for i in range(10)])
    picked = select_for_stage(4, rows)
    assert picked[:2] == ["old0", "old1"]
    assert picked[2:] == ["new0", "new1"]


def test_priority_half_backfills_when_few_old_rows_exist():
    rows = rows_for([("only_old", 1, (2, 0))] + [(f"new{i}", 10 + i, (0, i)) for i in range(5)])
    picked = select_for_stage(6, rows)
    assert len(picked) == 6 and "only_old" in picked


def test_aging_is_relevance_blind_so_the_queue_filter_is_the_protection():
    """Stated rather than papered over: select_for_stage's aging half orders by
    enqueue sequence alone, so an old row wins its half whatever its relevance.
    That is correct here only because a zero-relevance row is never enqueued
    (workers/discovery/run.py), which is where the protection lives; aging then
    ages WITHIN the relevant set, exactly as §4 intends."""
    old_low_relevance = rows_for([(f"old{i}", i, (0, 2)) for i in range(4)])
    new_high_relevance = rows_for([(f"new{i}", 100 + i, (-9, 0)) for i in range(10)])
    picked = select_for_stage(4, old_low_relevance + new_high_relevance)
    assert picked[:2] == ["old0", "old1"]  # the aging half, relevance ignored
    assert picked[2:] == ["new0", "new1"]  # the priority half, relevance-led


def test_selection_is_deterministic_and_capped():
    rows = rows_for([(f"r{i}", i, (0, i)) for i in range(10)])
    assert select_for_stage(3, rows) == select_for_stage(3, rows)
    assert len(select_for_stage(3, rows)) == 3
    assert select_for_stage(0, rows) == []


# --- retry policy -----------------------------------------------------------------

def test_retry_backs_off_doubling_from_one_run_then_terminal():
    assert retry_outcome(0, current_run_seq=10) == ("retry", 11)
    assert retry_outcome(1, current_run_seq=11) == ("retry", 13)
    assert retry_outcome(2, current_run_seq=13) == ("failed", None)


# --- budget -----------------------------------------------------------------------

def test_budget_defaults_match_the_ratified_numbers():
    budget = Budget()
    assert (budget.per_host_min_interval_s, budget.max_fetches, budget.max_probes,
            budget.rot_threshold, budget.mass_closure_guard_percent,
            budget.mass_closure_guard_min, budget.max_new_opportunities_gated,
            budget.max_extraction_calls, budget.judged_fit_k) == (
        2, 2000, 2000, 5, 50, 10, 500, 30, 10)


def test_budget_json_round_trips_for_the_run_record():
    assert json.loads(Budget().to_json())["max_extraction_calls"] == 30


def test_exhaustion_is_a_recorded_signal_never_an_exception():
    ledger = BudgetLedger(Budget(max_extraction_calls=2))
    assert ledger.try_spend("extraction")
    assert ledger.try_spend("extraction")
    assert not ledger.try_spend("extraction")  # returns False, never raises
    assert ledger.exhaustion.stage == "extraction"
    assert (ledger.exhaustion.spent, ledger.exhaustion.limit) == (2, 2)
    # The first refusal names the run's stop; later refusals do not overwrite.
    assert not ledger.try_spend("extraction")
    assert ledger.exhaustion.stage == "extraction"


def test_total_model_call_cap_binds_across_stages():
    ledger = BudgetLedger(Budget(max_extraction_calls=5, judged_fit_k=5,
                                 max_total_model_calls=3))
    assert ledger.try_spend("extraction") and ledger.try_spend("extraction")
    assert ledger.try_spend("judgment")
    assert not ledger.try_spend("judgment")
    assert ledger.exhaustion.stage == "judgment"


def test_deterministic_stages_never_touch_the_model_cap():
    ledger = BudgetLedger(Budget(max_total_model_calls=0))
    assert ledger.try_spend("fetch") and ledger.try_spend("gate")
    assert json.loads(ledger.spend_json())["model_calls_total"] == 0


def test_unknown_stage_is_a_programming_error():
    with pytest.raises(ValueError):
        BudgetLedger(Budget()).try_spend("rank")
