"""The per-run budget (OC-37 §4): locked in config before the run, recorded on
the run row, spent per stage, and exhausted safely. Exhaustion is a stop
signal the run loop consults, never an exception and never a retry loop: the
run persists everything completed, marks itself budget_exhausted with the
stage and counts, and exits 0. Integer counters only.
"""

import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Budget:
    """The locked per-run budget with the §4 numeric defaults, all config and
    all recorded on the run row."""

    per_host_min_interval_s: int = 2
    max_fetches: int = 2000
    max_probes: int = 2000
    rot_threshold: int = 5
    mass_closure_guard_percent: int = 50
    mass_closure_guard_min: int = 10
    max_new_opportunities_gated: int = 500
    max_extraction_calls: int = 30
    judged_fit_k: int = 10
    max_total_model_calls: int = 40

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


# The stages spend is tracked under, in funnel order.
STAGES: tuple[str, ...] = ("fetch", "probe", "gate", "extraction", "judgment")

# Public: stage name -> the Budget field naming its cap (CLI wording and
# spend displays consume this).
STAGE_LIMITS: dict[str, str] = {
    "fetch": "max_fetches",
    "probe": "max_probes",
    "gate": "max_new_opportunities_gated",
    "extraction": "max_extraction_calls",
    "judgment": "judged_fit_k",
}

_MODEL_STAGES: frozenset[str] = frozenset({"extraction", "judgment"})


@dataclass(frozen=True)
class Exhaustion:
    """The safe-stop signal: which stage ran out and the counts at that
    moment. A value the loop returns and records, never an exception."""

    stage: str
    spent: int
    limit: int


class BudgetLedger:
    """Spend tracking against a locked Budget. try_spend answers whether one
    unit of a stage fits and records it if so; the first refusal is captured
    as the run's exhaustion record."""

    def __init__(self, budget: Budget):
        self._budget = budget
        self._spend: dict[str, int] = {stage: 0 for stage in STAGES}
        self._model_calls = 0
        self._exhaustion: Exhaustion | None = None
        self._exhausted: set[str] = set()

    @property
    def exhaustion(self) -> Exhaustion | None:
        return self._exhaustion

    @property
    def exhausted_stages(self) -> frozenset[str]:
        """Every stage that ran out this run, not only the first refusal.
        The stage budgets are separate, so a caller deciding whether a LATER
        stage may run asks about that stage's own chain, never about the
        first exhaustion recorded (§4)."""
        return frozenset(self._exhausted)

    def exhausted(self, stage: str) -> bool:
        return stage in self._exhausted

    def spent(self, stage: str) -> int:
        return self._spend[stage]

    def try_spend(self, stage: str) -> bool:
        """Record one unit of stage spend if the stage cap (and, for model
        stages, the total model-call cap) allows it; else record exhaustion
        and answer False. Never raises on exhaustion."""
        if stage not in self._spend:
            raise ValueError(f"unknown budget stage '{stage}'")
        limit = getattr(self._budget, STAGE_LIMITS[stage])
        if self._spend[stage] >= limit:
            self._note_exhaustion(stage, self._spend[stage], limit)
            return False
        if stage in _MODEL_STAGES and self._model_calls >= self._budget.max_total_model_calls:
            self._note_exhaustion(stage, self._model_calls, self._budget.max_total_model_calls)
            return False
        self._spend[stage] += 1
        if stage in _MODEL_STAGES:
            self._model_calls += 1
        return True

    def note_exhausted(self, stage: str) -> None:
        """Record exhaustion for a stage without spending: used when work is
        refused admission because the remaining budget cannot cover its
        estimated cost (e.g. a poll deferred for fetch budget)."""
        if stage not in self._spend:
            raise ValueError(f"unknown budget stage '{stage}'")
        limit = getattr(self._budget, STAGE_LIMITS[stage])
        self._note_exhaustion(stage, self._spend[stage], limit)

    def _note_exhaustion(self, stage: str, spent: int, limit: int) -> None:
        self._exhausted.add(stage)
        if self._exhaustion is None:  # the first refusal names the run's stop
            self._exhaustion = Exhaustion(stage=stage, spent=spent, limit=limit)

    def spend_json(self) -> str:
        return json.dumps({**self._spend, "model_calls_total": self._model_calls},
                          sort_keys=True)
