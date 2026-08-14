"""Promotion queue semantics (OC-37 §4): version-pinned rows, frozen
deterministic ordering keys, stage caps with half-cap aging, bounded retry
with a terminal failed state, and dependency-epoch invalidation.

Pure ordering and selection logic; the repository owns persistence and the
transactional claim checks.
"""

from dataclasses import dataclass

# §4 defaults, config and recorded on the run row.
MAX_ATTEMPTS = 3
# Backoff doubling from one run: attempt n failing defers the next attempt by
# 2**(n-1) runs (1, 2, then terminal failed).
RETRY_BACKOFF_RUNS: tuple[int, ...] = (1, 2)

# Priority lanes for the frozen pre-extraction ordering key: curated-layer
# sources first, then manual, then harvest.
LANE_RANKS: dict[str, int] = {"curated": 0, "manual": 1, "harvest": 2}


def lane_rank(origin: str) -> int:
    if origin not in LANE_RANKS:
        raise ValueError(f"unknown source origin '{origin}'")
    return LANE_RANKS[origin]


@dataclass(frozen=True)
class PendingRow:
    """The slice of a queue row selection needs. priority_key orders the top
    half of a stage's cap; enqueue_seq orders the aging half."""

    row_id: str
    enqueue_seq: int
    priority_key: tuple


def pre_extraction_priority_key(relevance: int, lane: int, first_seen: str,
                                opportunity_id: str) -> tuple:
    """Frozen at enqueue: title relevance first (most matched target-family
    vocabulary terms first), then curated-layer priority, then first_seen
    recency (newest first), then opportunity id tie-break. first_seen is an ISO
    UTC timestamp, so lexical order is chronological.

    Relevance leads because lane and recency describe where a row came from,
    not whether anyone wants it: with a large arrival-ordered backlog they
    spend the whole paid-model budget on whichever tenant was polled first. The
    score is computed once at enqueue and persisted on the queue row
    (relevance_score), so the ordering is stable across runs; the value comes
    from domain.requirements.title_relevance_score, which this module
    deliberately does not import (it takes the integer, not the vocabulary)."""
    return (-relevance, lane, _descending(first_seen), opportunity_id)


def coverage_priority_key(coverage_bp: int, enqueue_seq: int) -> tuple:
    """The judged-fit stage consumes extracted rows in coverage order (highest
    first), enqueue sequence as the tie-break."""
    return (-coverage_bp, enqueue_seq)


def _descending(text: str) -> tuple:
    """Sort helper: lexically descending text inside an ascending tuple sort."""
    return tuple(-byte for byte in text.encode())


def select_for_stage(cap: int, rows: list[PendingRow]) -> list[str]:
    """Half-cap aging (§4): half of the stage cap is reserved for the oldest
    still-current pending rows by enqueue sequence, the other half for the top
    of the priority order, so sustained arrivals cannot starve older rows.
    Underfilled halves backfill each other; the result is deterministic."""
    if cap <= 0 or not rows:
        return []
    aged_quota = cap // 2
    by_age = sorted(rows, key=lambda r: r.enqueue_seq)
    aged = by_age[:aged_quota]
    chosen = {r.row_id for r in aged}
    by_priority = sorted(
        (r for r in rows if r.row_id not in chosen),
        key=lambda r: (r.priority_key, r.enqueue_seq))
    picked = aged + by_priority[: cap - len(aged)]
    return [r.row_id for r in picked]


def retry_outcome(attempts_before: int, current_run_seq: int) -> tuple[str, int | None]:
    """One failed attempt: ('retry', next_attempt_run_seq) while attempts
    remain, else ('failed', None), the auditable terminal state excluded from
    claiming and retryable only explicitly via CLI (§4)."""
    attempts_now = attempts_before + 1
    if attempts_now >= MAX_ATTEMPTS:
        return ("failed", None)
    return ("retry", current_run_seq + RETRY_BACKOFF_RUNS[attempts_now - 1])
