"""The discovery run orchestrator (OC-37 §2-§5): one budgeted run per
invocation, under a singleton lease, budget locked from config and recorded on
the run row, exhaustion a safe stop with everything persisted (exit 0).

Stage order is the §4 funnel: probe -> poll (snapshot commit, versioning,
closure plan) -> eligibility gate (deterministic, with dependency-epoch
re-gating) -> requirement extraction -> judged fit, the two model stages
through ModelAdapter inside the untrusted-content isolation boundary. The
proposed action defaults to IGNORE/MONITOR (OC-23); nothing here pursues.
"""

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field, replace

from adapters.models.claude_code import (
    DEFAULT_TIMEOUT_SECONDS as MODEL_TIMEOUT_SECONDS,
)
from adapters.models.claude_code import ModelCallError
from adapters.sources.base import AdapterDegradedError, OversizedFeedError
from adapters.sources.http import (
    DEFAULT_MAX_RETRIES as HTTP_MAX_RETRIES,
)
from adapters.sources.http import (
    DEFAULT_TIMEOUT_S as HTTP_TIMEOUT_S,
)
from adapters.sources.http import FetchError, RefusedHostError
from adapters.storage.sqlite_discovery import (
    SqliteDependencyEpochRepository,
    SqliteDiscoveryLease,
    SqliteDiscoveryRunRepository,
    SqliteSnapshotRepository,
    SqliteSourceRegistryRepository,
)
from adapters.storage.sqlite_opportunities import (
    SqliteOpportunityRepository,
    SqlitePromotionQueueRepository,
)
from adapters.storage.sqlite_conn import retry_on_locked
from adapters.storage.sqlite_policies import SqliteUserPolicyRepository
from adapters.storage.sqlite_profile import SqliteUserProfileRepository
from domain.budget import Budget, BudgetLedger
from domain.closure import OpportunityState, plan_snapshot
from domain.discovery import (
    MATERIAL_FIELDS,
    Opportunity,
    OpportunityVersion,
    StoredGateVerdict,
    material_fingerprint,
)
from domain.gate import CompanyMetadata, GateContext, PostingFacts, evaluate_gate
from domain.ids import new_id
from domain.ports import ModelUnavailableError, StorageObjectExistsError
from domain.promotion import (
    PendingRow,
    coverage_priority_key,
    lane_rank,
    pre_extraction_priority_key,
    select_for_stage,
)
from domain.requirements import (
    BudgetExhausted,
    CONTENT_FRACTION_BP,
    JudgedFitService,
    RequirementExtractionService,
    StageOutputError,
    build_posting_json,
    coverage_bp,
    render_judged_reason,
    title_relevance_score,
)
from prompts import load_prompt
from workers.discovery.probe import probe_source

CONFIG_FILENAME = "discovery.json"
FAMILIES_FILENAME = "families.json"
FAMILIES_EXAMPLE_FILENAME = "families.example.json"

# The four keys a families.json entry carries, all required: discovery uses
# every one of them and a typo'd key must not read as an absent one.
_FAMILY_KEYS = frozenset({"name", "seniority", "search_vocabulary",
                          "adjacent_titles"})

# Persisted failure messages are our own exception text, kept bounded so one
# pathological message cannot bloat the run row.
_FAILURE_MESSAGE_MAX = 500


class LeaseLostError(RuntimeError):
    """The run lease expired or was claimed by another owner; the run stops
    before its next mutation, keeping everything already committed."""


class _AtomicConnection:
    """Connection proxy whose `with` blocks defer to an enclosing explicit
    transaction: repository code keeps its own transactional style, while one
    poll's snapshot, observations, versions, and closure effects commit (or
    roll back) together (§1/§3)."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args):
        return self._conn.execute(*args)

    def executemany(self, *args):
        return self._conn.executemany(*args)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False  # neither commits nor swallows: the outer BEGIN owns it


class _FencedTransaction:
    """BEGIN -> in-transaction owner/fence/expiry verification -> caller's
    mutations -> COMMIT; any failure (fence lost included) rolls the whole
    transition back."""

    def __init__(self, conn, lease, owner: str, fence: int):
        self._conn = conn
        self._lease = lease
        self._owner = owner
        self._fence = fence

    def __enter__(self):
        self._conn.commit()  # nothing pending crosses into this transaction
        self._conn.execute("BEGIN")
        if not self._lease.held_by(self._owner, self._fence):
            self._conn.rollback()
            raise LeaseLostError(
                "discovery lease fence lost inside a transition transaction;"
                " transition rolled back, nothing mutated")
        return _AtomicConnection(self._conn)

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        return False


@dataclass(frozen=True)
class TargetFamily:
    """One operator-authored target role family (OC-42). Only what discovery
    uses: the CV behind a family and the rationale for it live in the
    candidate's own materials, never in this config."""

    name: str
    seniority: str
    search_vocabulary: tuple[str, ...]
    adjacent_titles: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryConfig:
    """Locked before the run; the budget and these scheduler numbers are all
    recorded on the run row (§4)."""

    budget: Budget = field(default_factory=Budget)
    max_pages_per_poll: int = 200
    default_page_cost: int = 2
    poll_interval_days: int = 1
    probe_backoff_base_days: int = 1
    probe_backoff_cap_days: int = 30
    disabled_reprobe_days: int = 30
    # The coverage match threshold (§5), in basis points of a target-family
    # vocabulary term's content tokens. Calibrated, not guessed
    # (scripts/calibrate_evidence_matcher.py); config because the threshold
    # trades precision against recall and that is a judgment call.
    coverage_match_fraction_bp: int = CONTENT_FRACTION_BP
    # Lease liveness is TTL only, so the TTL is also how long an interrupted
    # run blocks the next one. The two numbers are one design: the TTL must
    # cover the renewal interval PLUS the longest gap between two checkpoints,
    # with margin, and load_config enforces the relationship so changing one
    # number forces thought about the other.
    #
    # The gaps are bounded by real timeouts, not estimates. A paginated poll
    # checkpoints per page, so the poll gap is one page attempt chain
    # (fetch_attempt_worst_s, covering the fetcher's retries and backoff) plus
    # the per-host wait, not the whole poll. The long pole is therefore one
    # model call plus its schema retry: 2 * model_call_timeout_s.
    lease_ttl_s: int = 1800
    lease_renew_interval_s: int = 60

    def to_json(self) -> str:
        record = json.loads(self.budget.to_json())
        record.update({
            "max_pages_per_poll": self.max_pages_per_poll,
            "default_page_cost": self.default_page_cost,
            "poll_interval_days": self.poll_interval_days,
            "probe_backoff_base_days": self.probe_backoff_base_days,
            "probe_backoff_cap_days": self.probe_backoff_cap_days,
            "disabled_reprobe_days": self.disabled_reprobe_days,
            "coverage_match_fraction_bp": self.coverage_match_fraction_bp,
            "lease_ttl_s": self.lease_ttl_s,
            "lease_renew_interval_s": self.lease_renew_interval_s,
            "model_call_timeout_s": MODEL_CALL_TIMEOUT_S,
            "fetch_attempt_worst_s": FETCH_ATTEMPT_WORST_S,
        })
        return json.dumps(record, sort_keys=True)

    def worst_checkpoint_gap_s(self) -> int:
        """The longest stretch between two checkpoints these numbers allow:
        the renewal throttle plus whichever bounded operation runs longest
        between checkpoints (one model call with its schema retry, or one page
        attempt chain in a poll)."""
        page_gap = self.budget.per_host_min_interval_s + FETCH_ATTEMPT_WORST_S
        model_gap = 2 * MODEL_CALL_TIMEOUT_S  # the call and its schema retry
        return self.lease_renew_interval_s + max(page_gap, model_gap)


# The two bounded operations a run holds between checkpoints, taken from the
# code that enforces them rather than restated as config a caller could set
# without effect: one model call (adapters/models/claude_code.py) and one page
# attempt chain (adapters/sources/http.py: max_retries + 1 requests at the
# request timeout, plus the 2/4/8s backoff between them).
MODEL_CALL_TIMEOUT_S = MODEL_TIMEOUT_SECONDS
FETCH_ATTEMPT_WORST_S = ((HTTP_MAX_RETRIES + 1) * int(HTTP_TIMEOUT_S)
                         + sum(2 ** i for i in range(1, HTTP_MAX_RETRIES + 1)))

# The TTL must be at least this multiple of the worst checkpoint gap. The gap
# is built from hard timeouts rather than estimates, so the margin covers
# scheduling slop, not uncertainty about the work.
LEASE_MARGIN_FACTOR = 1.25

# A run row pins ONE exhausted stage (the first refusal). When more than one
# stage exhausted, the full set is recorded as a run note under this prefix,
# which the operator surface reads back so no capped stage is hidden.
EXHAUSTED_STAGES_NOTE = "budget exhausted at stages: "

# A stopped model stage has two distinct causes and the run says which: the
# budget ran out (recorded as the budget_exhausted status plus the note above),
# or the backend itself was unavailable (this note, with nothing exhausted).
MODEL_UNAVAILABLE_NOTE = ("model stages stopped: the model backend is"
                          " unavailable, not the rows; ")


def load_config(storage) -> DiscoveryConfig:
    """Config overrides from the instance's discovery.json (optional); unknown
    keys are refused so a typo never silently runs defaults."""
    # The read itself is the preflight: exists() answers False on a stat
    # error (permission denial included), which would silently fall back to
    # the default budgets and spend real model calls. Only a definite
    # missing file returns defaults; every other read failure is an error.
    try:
        text = storage.read_text(CONFIG_FILENAME)
    except FileNotFoundError:
        return DiscoveryConfig()
    except (UnicodeDecodeError, OSError) as e:
        raise ValueError(
            f"{CONFIG_FILENAME} (in the instance directory) could not be"
            f" read: {e}") from e
    try:
        overrides = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"{CONFIG_FILENAME} (in the instance directory) is not valid"
            f" JSON: {e}") from e
    if not isinstance(overrides, dict):
        raise ValueError(f"{CONFIG_FILENAME} must be a JSON object")
    budget_fields = set(Budget.__dataclass_fields__)
    config_fields = set(DiscoveryConfig.__dataclass_fields__) - {"budget"}
    unknown = set(overrides) - budget_fields - config_fields
    if unknown:
        raise ValueError(
            f"unknown {CONFIG_FILENAME} keys: {sorted(unknown)};"
            f" allowed keys: {sorted(budget_fields | config_fields)}")
    for key, value in overrides.items():
        # Every field is a nonnegative integer (bool is not an integer here);
        # a bad value is the same clean one-line config error as a bad key.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"{CONFIG_FILENAME} key '{key}' must be an integer,"
                f" got {type(value).__name__}")
        if value < 0:
            raise ValueError(
                f"{CONFIG_FILENAME} key '{key}' must be nonnegative, got {value}")
        if key == "mass_closure_guard_percent" and not 0 <= value <= 100:
            raise ValueError(
                f"{CONFIG_FILENAME} key '{key}' must be between 0 and 100,"
                f" got {value}")
        if key in ("default_page_cost", "max_pages_per_poll") and value < 1:
            # Admission estimates and per-poll hard limits below 1 would let
            # a poll admit against a zero fetch budget (or forbid every poll).
            raise ValueError(
                f"{CONFIG_FILENAME} key '{key}' must be at least 1, got {value}")
        if key == "coverage_match_fraction_bp" and not 1 <= value <= 10000:
            # 0 would match every vocabulary term against every phrase (coverage
            # saturated at 100%, the signal destroyed in the other direction).
            raise ValueError(
                f"{CONFIG_FILENAME} key '{key}' must be between 1 and 10000"
                f" basis points, got {value}")
        if key in ("lease_ttl_s", "lease_renew_interval_s") and value < 1:
            raise ValueError(
                f"{CONFIG_FILENAME} key '{key}' must be at least 1, got {value}")
    budget = Budget(**{k: v for k, v in overrides.items() if k in budget_fields})
    config = DiscoveryConfig(budget=budget, **{
        k: v for k, v in overrides.items() if k in config_fields})
    required = int(LEASE_MARGIN_FACTOR * config.worst_checkpoint_gap_s())
    if config.lease_ttl_s < required:
        # Stated as the relationship, not as a magic number: a shorter TTL is
        # the point (an interrupted run blocks the next one for exactly this
        # long), but it must still outlast the longest gap between two
        # checkpoints by the stated margin.
        raise ValueError(
            f"{CONFIG_FILENAME} key 'lease_ttl_s' must be at least"
            f" {required} with these numbers ({LEASE_MARGIN_FACTOR}x the worst"
            f" gap between lease renewals: lease_renew_interval_s"
            f" {config.lease_renew_interval_s} plus the longest bounded"
            f" operation between checkpoints, either a model call and its"
            f" retry (2 x the model call timeout {MODEL_CALL_TIMEOUT_S}s) or"
            f" one page attempt chain (per_host_min_interval_s"
            f" {config.budget.per_host_min_interval_s} plus the fetcher's"
            f" bounded attempts, {FETCH_ATTEMPT_WORST_S}s)),"
            f" got {config.lease_ttl_s}")
    return config


def load_families(storage) -> list[TargetFamily]:
    """The target role families from the instance's families.json (OC-42: the
    families are operator-authored config, no longer walked out of the career
    graph). Required, never defaulted: an empty family set would compute an
    empty coverage vocabulary and skip the gate's seniority dimension while the
    run still reported success, which is silent degradation, not a default."""
    # Same read discipline as load_config: only a definite missing file is
    # special-cased, every other read failure is an error rather than a
    # fallback that quietly changes what the run does.
    try:
        text = storage.read_text(FAMILIES_FILENAME)
    except FileNotFoundError as e:
        raise ValueError(
            f"{FAMILIES_FILENAME} (in the instance directory) is required:"
            " discovery matches postings against your target role families."
            f" Copy {FAMILIES_EXAMPLE_FILENAME} from the repo root and fill"
            " it in") from e
    except (UnicodeDecodeError, OSError) as e:
        raise ValueError(
            f"{FAMILIES_FILENAME} (in the instance directory) could not be"
            f" read: {e}") from e
    try:
        document = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"{FAMILIES_FILENAME} (in the instance directory) is not valid"
            f" JSON: {e}") from e
    if not isinstance(document, dict):
        raise ValueError(f"{FAMILIES_FILENAME} must be a JSON object")
    unknown = set(document) - {"families"}
    if unknown:
        raise ValueError(
            f"unknown {FAMILIES_FILENAME} keys: {sorted(unknown)};"
            " allowed keys: ['families']")
    entries = document.get("families")
    if not isinstance(entries, list) or not entries:
        raise ValueError(
            f"{FAMILIES_FILENAME} key 'families' must be a non-empty list")
    families = []
    for index, entry in enumerate(entries):
        where = f"{FAMILIES_FILENAME} families[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be a JSON object")
        unknown = set(entry) - _FAMILY_KEYS
        if unknown:
            raise ValueError(
                f"unknown {where} keys: {sorted(unknown)};"
                f" allowed keys: {sorted(_FAMILY_KEYS)}")
        missing = _FAMILY_KEYS - set(entry)
        if missing:
            raise ValueError(f"{where} is missing keys: {sorted(missing)}")
        families.append(TargetFamily(
            name=_nonempty_string(entry["name"], f"{where} 'name'"),
            seniority=_nonempty_string(entry["seniority"],
                                       f"{where} 'seniority'"),
            search_vocabulary=_string_tuple(entry["search_vocabulary"],
                                            f"{where} 'search_vocabulary'"),
            adjacent_titles=_string_tuple(entry["adjacent_titles"],
                                          f"{where} 'adjacent_titles'")))
    return families


def _nonempty_string(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a non-empty string")
    return value


def _string_tuple(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list of non-empty strings")
    return tuple(_nonempty_string(item, f"{where} entry")
                 for item in value)


def families_fingerprint(families: list[TargetFamily]) -> str:
    """A deterministic fingerprint of the VALIDATED families, not the file
    bytes: reformatting, key reordering or a comment-only edit must not read as
    a change, and any change to what discovery actually uses must. Order within
    the config is kept significant because the coverage vocabulary is built in
    first-seen order and the reported term lists follow it."""
    canonical = json.dumps(
        [{"name": f.name, "seniority": f.seniority,
          "search_vocabulary": list(f.search_vocabulary),
          "adjacent_titles": list(f.adjacent_titles)} for f in families],
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def vocabulary_terms(families: list[TargetFamily]) -> list[str]:
    """The deterministic coverage vocabulary: every family's search terms,
    adjacent titles and its own name, deduplicated in first-seen order. These
    are multi-token phrases, which is exactly what the content-fraction
    matcher scores (domain/requirements.py)."""
    terms: dict[str, None] = {}
    for family in families:
        for term in (*family.search_vocabulary, *family.adjacent_titles,
                     family.name):
            terms.setdefault(term, None)
    return list(terms)


def _db_now(conn, days_ahead: int = 0) -> str:
    return conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+' || ? || ' days')",
        (days_ahead,)).fetchone()[0]


class DiscoveryRunner:
    def __init__(self, conn, storage, model, adapters, config: DiscoveryConfig,
                 say=print):
        self._conn = conn
        self._storage = storage
        self._model = model
        self._adapters = adapters
        self._config = config
        self._say = say
        self._registry = SqliteSourceRegistryRepository(conn)
        self._snapshots = SqliteSnapshotRepository(conn)
        self._opps = SqliteOpportunityRepository(conn)
        self._queue = SqlitePromotionQueueRepository(conn)
        self._runs = SqliteDiscoveryRunRepository(conn)
        self._epoch_repo = SqliteDependencyEpochRepository(conn)
        self._outcomes: dict = {}
        self._notes: list[str] = []
        self._last_renew_at: float | None = None
        # Where the run is, recorded so an abort names the stage and (when a
        # source is being worked) the source it died on.
        self._stage: str | None = None
        self._current_source_id: str | None = None
        self._model_available = True
        # Opportunities whose material version changed this run: their gate
        # verdict re-evaluates like an epoch bump does (§3/§5).
        self._version_changed: list[str] = []
        # The target families, read from the instance config at run start.
        self._families: list[TargetFamily] = []

    # ------------------------------------------------------------------ run

    def run(self):
        """One budgeted run. Returns the finished DiscoveryRun, or None when
        the singleton lease is held by another run."""
        # Checked before anything is claimed: a refused configuration must not
        # leave a lease held behind it.
        self._check_model_timeout_fits_the_lease()
        # Same posture as the config checks: a missing or malformed
        # families.json refuses the run here, before a lease is held and
        # before a single fetch, rather than degrading into a run with no
        # coverage vocabulary and no seniority gate.
        self._families = load_families(self._storage)
        self._lease = SqliteDiscoveryLease(self._conn)
        self._owner = new_id("dlease")
        self._fence = retry_on_locked(
            lambda: self._lease.acquire(self._owner, self._config.lease_ttl_s))
        if self._fence is None:
            self._say("another discovery run holds the lease; nothing ran")
            return None
        # Everything past a successful acquisition runs under the release:
        # a failure while reading the epoch, reconciling, or starting the run
        # row would otherwise strand the lease until its TTL expired, blocking
        # every later run for no reason.
        try:
            # families.json is a candidate-side dependency with no repository
            # write behind it (OC-42), so the run itself detects an edit: the
            # bump lands before the epoch is read, and the existing stale-epoch
            # sweep does the re-gating and coverage recomputation.
            fingerprint = families_fingerprint(self._families)
            epoch = retry_on_locked(
                lambda: self._epoch_repo.sync_families_fingerprint(fingerprint))
            # Holding the lease, any run row still 'running' whose own lease
            # ownership no longer holds is provably abandoned (§4): reconcile
            # it before starting, so run history stops reporting phantom live
            # runs.
            retry_on_locked(self._runs.reconcile_abandoned)
            ledger = BudgetLedger(self._config.budget)
            run = retry_on_locked(lambda: self._runs.start(
                self._config.to_json(), epoch,
                lease_owner=self._owner, lease_fence=self._fence))
        except Exception:
            try:  # the original failure is what propagates, released or not
                retry_on_locked(lambda: self._lease.release(self._owner))
            except sqlite3.Error:
                self._say("discovery lease could not be released after a failed"
                          " start; it expires with its TTL")
            raise
        try:
            # A stage runs when ITS OWN budget permits (§4). Exhausting one
            # stage's budget says nothing about another's, so it never stops
            # another stage: probe exhaustion stops probing, fetch exhaustion
            # stops polling, and the deterministic free gate still runs on
            # whatever backlog is already observed. The model stages are
            # bounded by their own caps (max_extraction_calls, judged_fit_k,
            # max_total_model_calls), which is what protects real spend. What
            # remains between stages is the data dependency, and it resolves
            # itself: extraction consumes gate survivors, judgment consumes
            # extracted rows.
            self._stage = "probe"
            self._probe(ledger)
            self._stage = "poll"
            self._poll(ledger, run)
            self._stage = "gate"
            self._current_source_id = None
            self._gate(ledger, epoch, run)
            self._stage = "promote"
            self._promote(ledger, epoch, run)
            self._stage = None
            exhausted = sorted(ledger.exhausted_stages)
            if len(exhausted) > 1:
                self._notes.append(
                    EXHAUSTED_STAGES_NOTE + ", ".join(exhausted))
            status = "budget_exhausted" if ledger.exhaustion else "completed"
            stage = ledger.exhaustion.stage if ledger.exhaustion else None
            self._finish(run.id, status, ledger, exhausted_stage=stage)
        except LeaseLostError as e:
            # A clean stop: everything already committed stays; nothing more
            # mutates under a lease another owner may hold.
            self._notes.append(str(e))
            self._finish(run.id, "failed", ledger, failure=e)
        except Exception as e:
            # The persisted record says what the operator's terminal said: a
            # bare "aborted" cannot tell a transient lock from a bug.
            self._notes.append(f"run aborted: {_failure_summary(e)}")
            self._finish(run.id, "failed", ledger, failure=e)
            raise
        finally:
            retry_on_locked(lambda: self._lease.release(self._owner))
        return self._runs.get(run.id)

    def _check_model_timeout_fits_the_lease(self) -> None:
        """The lease relationship is validated against the model adapter's
        default timeout, so an adapter injected with a longer one would hold
        the lease past a gap the TTL was never approved for. An adapter that
        declares its bound is checked against the numbers actually in force."""
        declared = getattr(self._model, "call_timeout_s", MODEL_CALL_TIMEOUT_S)
        gap = self._config.lease_renew_interval_s + 2 * int(declared)
        if self._config.lease_ttl_s < LEASE_MARGIN_FACTOR * gap:
            raise ValueError(
                f"the model adapter's call timeout ({declared}s) needs a"
                f" lease_ttl_s of at least {int(LEASE_MARGIN_FACTOR * gap)},"
                f" but the locked config has {self._config.lease_ttl_s};"
                " raise the TTL or lower the adapter timeout")

    def _finish(self, run_id: str, status: str, ledger: BudgetLedger,
                exhausted_stage: str | None = None,
                failure: BaseException | None = None) -> None:
        """The terminal write, retried on contention: a lock at the very end
        must not be the reason a finished run keeps reporting 'running'. The
        conditional update inside finish still refuses to overwrite a row a
        successor already reconciled."""
        retry_on_locked(lambda: self._runs.finish(
            run_id, status, ledger.spend_json(), self._outcomes_json(),
            exhausted_stage=exhausted_stage,
            failure_json=None if failure is None else self._failure_json(failure)))

    def _checkpoint(self) -> None:
        """Re-check owner and fence before each persistent transition (the
        package pipeline's discipline), renewing the lease at most once per
        lease_renew_interval_s; a lost lease terminates the run cleanly before
        the next mutation. Inside the renewal interval the ownership check is
        the same test, read-only, so the ownership guarantee is unchanged and
        only the renewal write is throttled."""
        now = time.monotonic()
        within_interval = (self._last_renew_at is not None
                           and now - self._last_renew_at
                           < self._config.lease_renew_interval_s)
        if within_interval and self._lease.held_by(self._owner, self._fence):
            return
        if not retry_on_locked(lambda: self._lease.renew(
                self._owner, self._fence, self._config.lease_ttl_s)):
            raise LeaseLostError(
                "discovery lease lost (expired or claimed by another owner);"
                " run stopped before its next mutation")
        self._last_renew_at = time.monotonic()

    def _failure_json(self, error: BaseException) -> str:
        """The persisted diagnostic for an aborted run: the exception type and
        a message that is safe to persist, plus where it happened."""
        return json.dumps({
            "error_type": type(error).__name__,
            "error_message": _persisted_failure_message(error),
            "stage": self._stage,
            "source_id": self._current_source_id,
        }, sort_keys=True)

    def _fenced(self):
        """An explicit transaction whose FIRST read re-verifies owner, fence,
        and expiry inside the transaction itself: a paused run whose lease was
        re-acquired rolls back whole and mutates nothing. Yields the proxy
        connection repositories run over."""
        return _FencedTransaction(self._conn, self._lease, self._owner,
                                  self._fence)

    def _fenced_write(self, body):
        """One fenced transaction, retried whole while another connection
        holds the database locked (§7). The transaction rolls back completely
        on any failure, so a retry re-runs body from a clean state; body must
        therefore do database writes only, never mutate run state."""
        def attempt():
            with self._fenced() as proxy:
                return body(proxy)
        return retry_on_locked(attempt)

    def _outcomes_json(self) -> str:
        return json.dumps({"sources": self._outcomes, "notes": self._notes},
                          sort_keys=True)

    # ---------------------------------------------------------------- probe

    def _probe(self, ledger: BudgetLedger) -> None:
        """§2: verification before enablement. Deterministic priority (curated,
        manual, harvest, stable order); the cursor is the persisted per-source
        next_probe_at/attempt state, so probing resumes where it stopped."""
        now = _db_now(self._conn)
        due = [s for s in self._registry.list_all()
               if s.status in ("candidate", "disabled")
               and (s.next_probe_at is None or s.next_probe_at <= now)]
        due.sort(key=lambda s: (lane_rank(s.origin), s.next_probe_at or "", s.id))
        for source in due:
            if not ledger.try_spend("probe"):
                return
            self._current_source_id = source.id
            self._checkpoint()  # lease re-checked before each probe lands
            adapter = self._adapters[source.ats_type]
            # The shared probe service (workers/discovery/probe.py): every
            # received body persists before parsing, evidence for enabled and
            # failed probes alike; the CLI enable path uses the same service.
            ok, captured = probe_source(self._storage, adapter, source.id,
                                        source.tenant_slug,
                                        checkpoint=self._checkpoint)
            backoff_days = min(
                self._config.probe_backoff_cap_days,
                self._config.probe_backoff_base_days * (2 ** source.probe_attempts))
            self._fenced_write(
                lambda proxy: SqliteSourceRegistryRepository(proxy)
                .record_probe_outcome(
                    source.id, ok,
                    next_probe_at=None if ok else _db_now(self._conn, backoff_days)))
            entry = self._outcomes.setdefault(source.id, {})
            entry["probe"] = "enabled" if ok else "failed"
            if captured:
                entry["probe_raw_pages"] = list(captured)

    # ----------------------------------------------------------------- poll

    def _poll(self, ledger: BudgetLedger, run) -> None:
        """§1/§2: due enabled sources, oldest last_polled_at first within
        priority lanes; the fetch budget splits between lanes so harvest
        cannot starve curated nor vice versa, with backfill; admission by last
        known page cost; a deferred poll is recorded and touches no health."""
        now = _db_now(self._conn)
        due = [s for s in self._registry.list_all()
               if s.status == "enabled"
               and (s.next_poll_at is None or s.next_poll_at <= now)]
        lanes: dict[int, list] = {0: [], 1: [], 2: []}
        for source in due:
            lanes[lane_rank(source.origin)].append(source)
        for lane in lanes.values():
            lane.sort(key=lambda s: (s.last_polled_at or "", s.id))
        max_fetches = self._config.budget.max_fetches
        share = max_fetches // 3
        quotas = {0: max_fetches - 2 * share, 1: share, 2: share}
        spent = {0: 0, 1: 0, 2: 0}
        deferred: list = []
        for lane_index in (0, 1, 2):  # first pass: within lane quotas
            for source in lanes[lane_index]:
                cost = self._estimated_cost(source)
                if spent[lane_index] + cost > quotas[lane_index] \
                        or ledger.spent("fetch") + cost > max_fetches:
                    deferred.append(source)
                    continue
                spent[lane_index] += self._poll_source(source, ledger, run)
        still_deferred = []
        for source in deferred:  # backfill: leftover budget, lane-free
            cost = self._estimated_cost(source)
            if ledger.spent("fetch") + cost > max_fetches:
                still_deferred.append(source)
                continue
            self._poll_source(source, ledger, run)
        for source in still_deferred:
            self._fenced_write(
                lambda proxy, source=source: SqliteSourceRegistryRepository(proxy)
                .record_poll_outcome(source.id, "deferred"))
            self._outcomes.setdefault(source.id, {})["poll"] = "deferred"
        if still_deferred:
            # A due source refused admission for budget IS fetch exhaustion:
            # the run records the stage and counts and finishes
            # budget_exhausted, with the deferrals kept (§4).
            ledger.note_exhausted("fetch")

    def _estimated_cost(self, source) -> int:
        latest = self._snapshots.latest_for_source(source.id)
        if latest is None:
            return self._config.default_page_cost
        return max(1, json.loads(latest.completion_json).get("pages", 1))

    def _poll_source(self, source, ledger: BudgetLedger, run) -> int:
        """One admitted poll: fetch all pages (an in-flight poll may finish
        past the run cap, §1), commit the snapshot atomically, mint and
        version opportunities, apply the closure plan. Returns pages spent."""
        self._current_source_id = source.id
        self._checkpoint()  # lease re-checked before this poll's mutations
        adapter = self._adapters[source.ats_type]
        outcome = self._outcomes.setdefault(source.id, {})
        fetcher = getattr(adapter, "_fetcher", None)
        requests_before = fetcher.request_count if fetcher is not None else 0

        charged = 0

        def spend_requests() -> int:
            """Charge spend at the request boundary for every outcome: a
            failed or oversized poll consumed real requests; an admitted
            in-flight poll may still finish past the run cap (§1). Idempotent
            per poll: only requests not yet charged are charged, so a failure
            after a successful fetch never double-charges."""
            nonlocal charged
            total = (fetcher.request_count - requests_before) \
                if fetcher is not None else 0
            for _ in range(total - charged):
                ledger.try_spend("fetch")
            charged = total
            return total

        # Raw capture precedes parsing (§1): each page's raw bytes stream to
        # durable storage as fetched, BEFORE JSON decoding or adapter
        # validation, so a later failure still leaves replayable evidence.
        attempt_id = new_id("att")
        captured_locators: list[str] = []  # every received body, any status
        page_locators: list[str] = []  # the 2xx feed pages only

        def capture(body: bytes, status) -> None:
            # Every received page renews the lease, so a large paginated poll
            # is a sequence of short gaps rather than one gap the TTL has to
            # cover whole. A lost lease raises here and stops the poll.
            self._checkpoint()
            locator = (f"discovery/raw/{source.id}/{attempt_id}"
                       f"/response-{len(captured_locators) + 1:04d}.json")
            self._storage.write_bytes_new(locator, body)
            captured_locators.append(locator)
            if status is not None and 200 <= status < 300:
                # Only real pages enter the snapshot manifest; an error body
                # is preserved evidence, never a replayable feed page.
                page_locators.append(locator)

        def degrade(error) -> int:
            spent = spend_requests()
            self._fenced_write(
                lambda proxy: SqliteSourceRegistryRepository(proxy)
                .record_poll_outcome(
                    source.id, "degraded",
                    next_poll_at=_db_now(self._conn, self._config.poll_interval_days),
                    rot_threshold=self._config.budget.rot_threshold,
                    next_probe_at=_db_now(self._conn,
                                          self._config.disabled_reprobe_days)))
            outcome["poll"] = "degraded"
            outcome["poll_error"] = str(error)
            outcome["requests"] = spent
            if captured_locators:
                # Auditable and replayable: the degraded record references
                # every preserved raw body while committing no snapshot.
                outcome["raw_pages"] = list(captured_locators)
            return spent

        if fetcher is not None:
            fetcher.capture = capture
        try:
            payload = adapter.poll(source.tenant_slug)
        except OversizedFeedError as e:
            spent = spend_requests()
            self._fenced_write(
                lambda proxy: SqliteSourceRegistryRepository(proxy)
                .record_poll_outcome(source.id, "oversized"))
            outcome["poll"] = "oversized"
            outcome["requests"] = spent
            if captured_locators:
                outcome["raw_pages"] = list(captured_locators)
            self._notes.append(str(e))
            return spent
        except (AdapterDegradedError, FetchError, RefusedHostError,
                ValueError) as e:
            # A refused host (e.g. a redirect to an unlisted target) or a
            # malformed adapter URL degrades THIS source with the refusal
            # reason preserved; the run continues with other sources and
            # queued work, never failing run-level.
            return degrade(e)
        finally:
            if fetcher is not None:
                fetcher.capture = None
        requests_spent = spend_requests()

        # Full validation and normalization BEFORE anything commits (§1): a
        # single shape-invalid job degrades the source and commits nothing.
        try:
            prepared = []
            for raw in payload.jobs:
                fields = adapter.material_fields(raw)
                _validate_material_fields(fields)
                prepared.append((adapter.external_id(raw), fields,
                                 material_fingerprint(fields)))
        except (AdapterDegradedError, TypeError, ValueError) as e:
            return degrade(AdapterDegradedError(
                f"posting failed material-field validation: {e}"))

        # The committed snapshot references the SAME stored raw objects the
        # fetch layer captured (no double write): the manifest lists them.
        if page_locators:
            raw_locator = f"discovery/raw/{source.id}/{attempt_id}/manifest.json"
            self._storage.write_text_new(raw_locator, json.dumps(
                {"page_locators": page_locators}))
        else:
            # Adapters over transports without the capture seam (tests) fall
            # back to storing the combined parsed payload content-addressed.
            raw_locator = (f"discovery/snapshots/{source.id}"
                           f"/{payload.content_hash}.json")
            try:
                self._storage.write_text_new(raw_locator, payload.raw_text)
            except StorageObjectExistsError:
                pass  # content-addressed: identical payload already stored

        # One fenced transaction for the snapshot, observations, versions, and
        # closure effects: repositories run over a proxy whose `with` blocks
        # defer to this explicit BEGIN, the lease fence is re-verified INSIDE
        # the transaction, and it all lands or rolls back whole.
        self._checkpoint()  # the poll may have outlived the lease; re-check
        version_changed_before = len(self._version_changed)
        notes_before = len(self._notes)

        def apply(proxy):
            # A retried transaction re-runs this body whole, so the run-state
            # appends _apply_poll makes are rewound to their pre-attempt point
            # first: no duplicated note, no duplicated re-gate id.
            del self._version_changed[version_changed_before:]
            del self._notes[notes_before:]
            return self._apply_poll(
                source, payload, prepared, raw_locator, run,
                SqliteSnapshotRepository(proxy),
                SqliteOpportunityRepository(proxy),
                SqlitePromotionQueueRepository(proxy),
                SqliteSourceRegistryRepository(proxy))

        counts = self._fenced_write(apply)
        outcome["poll"] = "success"
        outcome.update(counts)
        outcome["requests"] = requests_spent
        return requests_spent

    def _apply_poll(self, source, payload, prepared, raw_locator, run,
                    snapshots, opps, queue, registry) -> dict:
        """All DB effects of one validated poll; runs inside the caller's
        explicit transaction."""
        snapshot = snapshots.commit(
            source.id, raw_locator, payload.content_hash,
            payload.completion_json(), len(prepared),
            remote_version_token=payload.remote_version_token, run_id=run.id)
        now = _db_now(self._conn)
        present_ids: set[str] = set()
        fields_by_opp: dict[str, tuple[dict, str]] = {}
        new_count = versioned_count = 0
        for external_id, fields, fingerprint in prepared:
            existing = opps.get_by_key(source.id, external_id)
            if existing is None:
                opportunity = Opportunity(
                    id=new_id("opp"), source_id=source.id,
                    external_job_id=external_id, first_seen=now, last_seen=now,
                    apply_support=self._adapters[source.ats_type].apply_support())
                opps.add(opportunity)
                self._add_version(opps, opportunity.id, 1, snapshot.id,
                                  fingerprint, fields)
                present_ids.add(opportunity.id)
                new_count += 1
                continue
            present_ids.add(existing.id)
            fields_by_opp[existing.id] = (fields, fingerprint)
            if existing.availability == "closed":
                continue  # a reopen re-versions after the closure plan below
            current = self._current_version(existing, opps)
            if current is None or current.fingerprint != fingerprint:
                next_number = (current.version + 1) if current else 1
                version = self._add_version(opps, existing.id, next_number,
                                            snapshot.id, fingerprint, fields)
                # §4: unfinished rows for older versions cancel atomically;
                # derived results stay pinned to their version (stale by pin).
                queue.supersede_for_opportunity(
                    existing.id, version.id, "material version change")
                if existing.backlog_state == "gated":
                    self._version_changed.append(existing.id)
                versioned_count += 1

        known = opps.list_for_source(source.id)
        states = [OpportunityState(o.id, o.availability, o.absence_streak,
                                   o.last_absence_snapshot_id) for o in known]
        plan = plan_snapshot(
            snapshot.id, present_ids, states,
            pending_cohort=opps.pending_cohort_for_source(source.id),
            authoritative=payload.remote_version_token is not None,
            guard_percent=self._config.budget.mass_closure_guard_percent,
            guard_min=self._config.budget.mass_closure_guard_min)
        opps.apply_closure_plan(plan, now)
        closed_ids = [opp_id for opp_id, _ in plan.closures] + list(plan.cohort_closed)
        for opp_id in closed_ids:
            queue.supersede_for_opportunity(opp_id, None, "opportunity closed")
        # §3: a reopen appends a version even when material fields are
        # unchanged, so prior derived results (pinned to the old version)
        # never count as current, and the opportunity re-gates and re-enqueues.
        for opp_id in plan.reopened:
            fields, fingerprint = fields_by_opp[opp_id]
            current = self._current_version(opps.get(opp_id), opps)
            version = self._add_version(opps, opp_id, current.version + 1,
                                        snapshot.id, fingerprint, fields)
            queue.supersede_for_opportunity(opp_id, version.id, "reopened")
            self._version_changed.append(opp_id)
        self._notes.extend(plan.notes)

        registry.record_poll_outcome(
            source.id, "success",
            next_poll_at=_db_now(self._conn, self._config.poll_interval_days))
        return {"pages": payload.page_count, "postings": len(prepared),
                "new": new_count, "changed": versioned_count,
                "closed": len(closed_ids), "reopened": len(plan.reopened)}

    def _add_version(self, repo, opportunity_id: str, number: int,
                     snapshot_id: str, fingerprint: str,
                     fields: dict) -> OpportunityVersion:
        version = OpportunityVersion(
            id=new_id("oppv"), opportunity_id=opportunity_id, version=number,
            snapshot_id=snapshot_id, fingerprint=fingerprint,
            **{k: fields.get(k) for k in MATERIAL_FIELDS})
        repo.add_version(version)
        return version

    def _current_version(self, opportunity, repo=None) -> OpportunityVersion | None:
        versions = (repo or self._opps).list_versions(opportunity.id)
        return versions[-1] if versions else None

    # ----------------------------------------------------------------- gate

    def _gate(self, ledger: BudgetLedger, epoch: int, run) -> None:
        """§5: deterministic gate over the promotion cap, half reserved for
        dependency-epoch re-gates (oldest verdicts first), half for the new
        backlog in priority order, backfilled both ways."""
        # The stale-epoch sweep only runs when this stage can actually
        # re-gate: with the gate cap at zero (or already spent), superseding
        # cancels queued work and enqueues nothing to replace it, which
        # silently empties the queue. Claiming re-checks the epoch anyway
        # (§4), so skipping the sweep costs no safety, and when it does run
        # the run reports how many rows it cancelled rather than leaving the
        # loss invisible.
        if not ledger.admits("gate"):
            ledger.note_exhausted("gate")  # the stop is still recorded
            self._notes.append(
                "gate stage has no capacity this run; the stale-epoch queue"
                " sweep was skipped and no queued row was superseded")
            return
        superseded = self._fenced_write(
            lambda proxy: SqlitePromotionQueueRepository(proxy)
            .supersede_stale_epochs(epoch, "dependency epoch advanced"))
        if superseded:
            self._notes.append(
                f"stale-epoch queue sweep superseded {superseded} queued"
                f" row(s) for re-gating under epoch {epoch}")
        context = self._gate_context()
        vocabulary = vocabulary_terms(self._families)
        sources = {s.id: s for s in self._registry.list_all()}

        regates = self._opps.list_open_with_stale_gate(epoch)
        seen = {o.id for o in regates}
        for opp_id in self._version_changed:  # §3: material change re-gates
            if opp_id not in seen:
                regates.append(self._opps.get(opp_id))
                seen.add(opp_id)
        backlog = self._opps.list_backlog_pending()
        backlog.sort(key=lambda o: (
            lane_rank(sources[o.source_id].origin),
            _descending_text(o.first_seen), o.id))
        # Order: the aging half (re-gates, oldest verdicts first), then the
        # new backlog in priority order, then remaining re-gates. The ledger
        # is the cap: the first refusal past it records the run's exhaustion.
        regate_quota = self._config.budget.max_new_opportunities_gated // 2
        chosen: list[tuple[str, object]] = \
            [("regate", o) for o in regates[:regate_quota]]
        chosen += [("new", o) for o in backlog]
        chosen += [("regate", o) for o in regates[regate_quota:]]

        for kind, opportunity in chosen:
            if not ledger.try_spend("gate"):
                return
            self._checkpoint()  # lease re-checked before each verdict lands
            source = sources[opportunity.source_id]
            # Backlog promotion, gate verdict, proposed action, and enqueue
            # land as ONE fenced transaction: a crash cannot strand a gated
            # opportunity between verdict and queue row.
            def gate(proxy, kind=kind, opportunity=opportunity, source=source):
                opps = SqliteOpportunityRepository(proxy)
                queue = SqlitePromotionQueueRepository(proxy)
                if kind == "new":
                    version = opps.promote_from_backlog(opportunity.id)
                    # None: discarded with its recorded reason (§4), which
                    # itself belongs in this transaction.
                else:
                    current = opps.get(opportunity.id)
                    version = None \
                        if (current.availability == "closed"
                            or current.current_version_id is None) \
                        else self._current_version(current, opps)
                if version is not None:
                    self._gate_one(opps, queue, opportunity.id, version,
                                   source, context, epoch, vocabulary)

            self._fenced_write(gate)

    def _gate_one(self, opps, queue, opportunity_id: str,
                  version: OpportunityVersion, source, context: GateContext,
                  epoch: int, vocabulary: list[str]) -> None:
        """One gate evaluation's writes, on the caller's transactional repos."""
        facts = _posting_facts(version)
        result = evaluate_gate(facts, replace(context, company=CompanyMetadata(
            industry=source.industry, company_stage=source.company_stage,
            company_size_band=source.company_size_band)))
        opps.record_gate_verdict(StoredGateVerdict(
            id=new_id("gate"), opportunity_id=opportunity_id,
            version_id=version.id, epoch=epoch, verdict=result.verdict,
            dimensions_json=json.dumps([{
                "dimension": d.dimension, "verdict": d.verdict,
                "reason": d.reason, "note": d.note,
            } for d in result.dimensions])))
        if result.verdict == "pass":
            # Relevance decides PROMOTION, never the verdict. OC-37 locks the
            # gate as a deterministic exclusion check that fails only on
            # decisive structured conflicts; a job being off-target is not one,
            # so a zero-relevance row stays a 'pass' that simply did not earn a
            # paid model call. Do NOT be tempted to turn this into a gate
            # failure: that would put a soft, vocabulary-shaped judgment inside
            # the audited exclusion record.
            relevance = title_relevance_score(
                version.title, vocabulary,
                self._config.coverage_match_fraction_bp)
            if relevance == 0:
                # No target-family term in the title at all. The reason is
                # recorded on the opportunity so the operator can see why a
                # passing row is not queued, and a families.json edit bumps the
                # dependency epoch (migration 0012), which re-gates this row
                # and re-scores it here: the vocabulary is the only knob, and
                # it needs no second invalidation path.
                opps.set_proposed_action(
                    opportunity_id, "ignore", version_id=version.id,
                    epoch=epoch)
                opps.set_promotion_skip_reason(
                    opportunity_id,
                    "gate passed but no target-family term matched the title;"
                    " not promoted to the model stages")
                return
            # OC-23: the proposal defaults to MONITOR; nothing here pursues.
            opps.set_proposed_action(opportunity_id, "monitor",
                                     version_id=version.id, epoch=epoch)
            # This gate decision re-decided promotion, so any skip recorded by
            # an earlier one is history, not state.
            opps.set_promotion_skip_reason(opportunity_id, None)
            queue.enqueue(
                opportunity_id, version.id, lane_rank(source.origin),
                opps.get(opportunity_id).first_seen, epoch,
                relevance_score=relevance)
        else:
            opps.set_proposed_action(opportunity_id, "ignore",
                                     version_id=version.id, epoch=epoch)
            # A gated-out row is excluded, not skipped for promotion: the
            # verdict record carries the reason, so the skip field must not
            # keep an older one alongside it.
            opps.set_promotion_skip_reason(opportunity_id, None)

    def _gate_context(self) -> GateContext:
        policies = SqliteUserPolicyRepository(self._conn).get_policies()
        fields = SqliteUserProfileRepository(self._conn).get_fields()
        return GateContext(
            policies=policies,
            residence_country=fields.get("country") or fields.get("location"),
            # Every configured family is a target: families.json has no status
            # concept, so a family you no longer want is one you remove.
            active_family_target_seniorities=tuple(
                f.seniority for f in self._families))

    # ------------------------------------------------- extraction + judgment

    def _promote(self, ledger: BudgetLedger, epoch: int, run) -> None:
        run_seq = run.run_seq
        terms = vocabulary_terms(self._families)
        self._extract(ledger, epoch, run_seq, terms)
        # No cross-stage stop (§4): judgment is bounded by judged_fit_k and
        # max_total_model_calls, and by the extracted rows that exist. Rows
        # extraction could not reach this run simply wait for the next one.
        self._judge(ledger, epoch, run_seq)

    def _extract(self, ledger: BudgetLedger, epoch: int, run_seq: int,
                 vocabulary: list[str]) -> None:
        if not self._model_available:
            return
        # No capacity means no claim: claiming a row re-checks its epoch and
        # supersedes a stale one (§4), so touching rows a capped-out stage can
        # never work would cancel queued work for nothing.
        if not ledger.admits("extraction"):
            ledger.note_exhausted("extraction")
            return
        service = RequirementExtractionService(
            self._model, load_prompt("requirement_extraction.md"))
        pending = self._queue.pending_for_stage("pending_extraction", run_seq)
        rows = [PendingRow(r.id, r.enqueue_seq, pre_extraction_priority_key(
            r.relevance_score, r.lane_rank, r.first_seen, r.opportunity_id))
            for r in pending]
        reused = 0
        for row_id in _staged_order(self._config.budget.max_extraction_calls, rows):
            self._checkpoint()  # lease re-checked before each claim/transition
            claimed = self._claim_row(row_id, epoch)
            if claimed is None:
                continue  # released as superseded, never worked (§4)
            try:
                # Includes the stored-artifact read: raw payloads are prunable
                # operational data, so a missing or unreadable one is a bad ROW,
                # not a bad run. It releases neutrally like any other row
                # failure (bounded retry, terminal failed state) and the run
                # continues with the remaining rows.
                posting_json, stored = self._posting_payload(
                    claimed, require_epoch=False)
                # Extraction reads the posting only (OC-5: structure from the
                # posting, nothing from the user's state), so a result pinned
                # to this same version stays valid when the dependency epoch
                # advances: it is reused and re-stamped, and only coverage,
                # which does depend on the candidate side, is recomputed.
                # Without this every dependency bump would buy the whole
                # extracted backlog a second time.
                stored_phrases = stored.get("requirements")
                if stored_phrases is not None and not stored.get("stale"):
                    requirements = tuple(r["phrase"] for r in stored_phrases)
                    reused += 1
                else:
                    # Every model call, the schema retry included, is charged
                    # against the budget before it is made.
                    requirements = service.extract(
                        posting_json,
                        charge=lambda: ledger.try_spend("extraction"))
            except BudgetExhausted:
                return  # exhaustion recorded; the row stays claimable next run
            except ModelUnavailableError as e:
                # The backend, not the row: the claimed row is left exactly as
                # it was (no attempt charged, no failure recorded), the same
                # posture as BudgetExhausted above. Its claim marker is stale
                # by the next run's higher fence, so it is claimable again.
                self._stop_model_stages(e)
                break
            except (ModelCallError, StageOutputError, ValueError, OSError) as e:
                self._release_failed(row_id, _neutral_reason(e), run_seq)
                continue
            self._checkpoint()  # the model call may have outlived the lease
            coverage = coverage_bp(
                requirements, vocabulary,
                self._config.coverage_match_fraction_bp)
            # The proposal write and the extracted transition land as one
            # fenced transaction: a crash cannot persist a result the queue
            # does not know about (which would cost a second charged call).
            def record_extraction(proxy):
                SqliteOpportunityRepository(proxy).set_requirement_proposals(
                    claimed.opportunity_id, json.dumps({
                        "version_id": claimed.version_id,
                        "epoch": claimed.epoch,
                        "requirements": [
                            {"id": f"r{i + 1}", "phrase": phrase}
                            for i, phrase in enumerate(requirements)],
                        "coverage_bp": coverage,
                    }, ensure_ascii=False))
                SqlitePromotionQueueRepository(proxy).transition(
                    row_id, "extracted", coverage_bp=coverage)

            self._fenced_write(record_extraction)
        if reused:
            self._notes.append(
                f"reused stored requirement proposals for {reused} row(s) whose"
                " opportunity version was unchanged; no extraction call made"
                " for them")

    def _judge(self, ledger: BudgetLedger, epoch: int, run_seq: int) -> None:
        if not self._model_available:
            return
        if not ledger.admits("judgment"):
            ledger.note_exhausted("judgment")  # same rule as extraction
            return
        service = JudgedFitService(self._model, load_prompt("judged_fit.md"))
        # Crash recovery: a pending_judgment row with no committed result can
        # only exist from an interrupted older run (the result write is
        # atomic with the transitions), so it is claimable like an extracted
        # row rather than invisible.
        extracted = (self._queue.pending_for_stage("extracted", run_seq)
                     + self._queue.pending_for_stage("pending_judgment", run_seq))
        if not extracted:
            return
        cold_start = all((r.coverage_bp or 0) == 0 for r in extracted)
        if cold_start:
            # §5 cold start: coverage is uniformly zero, so the judged-fit
            # order falls back to the stated deterministic order (curated
            # lane first, then first_seen recency); recorded on the run.
            self._notes.append("cold_start_fallback: judged-fit order used the"
                               " deterministic lane/recency fallback")
            rows = [PendingRow(r.id, r.enqueue_seq, pre_extraction_priority_key(
                r.relevance_score, r.lane_rank, r.first_seen,
                r.opportunity_id)) for r in extracted]
        else:
            rows = [PendingRow(r.id, r.enqueue_seq, coverage_priority_key(
                r.coverage_bp or 0, r.enqueue_seq)) for r in extracted]
        candidate = {
            "target_families": [
                {"name": f.name, "seniority": f.seniority,
                 "vocabulary": list(f.search_vocabulary) + list(f.adjacent_titles)}
                for f in self._families],
        }
        for row_id in _staged_order(self._config.budget.judged_fit_k, rows):
            self._checkpoint()  # lease re-checked before each claim/transition
            claimed = self._claim_row(row_id, epoch)
            if claimed is None:
                continue
            try:
                # Same stored-artifact read as extraction: an unreadable raw
                # payload releases this row neutrally, never the whole run.
                posting_json, proposals = self._posting_payload(claimed)
                requirements = tuple(proposals.get("requirements", []))
                # Charged per call, retry included; the row transitions only
                # after a judged result exists, so a stop leaves it claimable.
                judged = service.judge(
                    posting_json, requirements,
                    dict(candidate, coverage_bp=claimed.coverage_bp or 0),
                    charge=lambda: ledger.try_spend("judgment"))
            except BudgetExhausted:
                return  # exhaustion recorded; the row stays extracted
            except ModelUnavailableError as e:
                # Same posture as extraction: the row keeps its attempt count
                # and its state; only the backend failed.
                self._stop_model_stages(e)
                return
            except (ModelCallError, StageOutputError, ValueError, OSError) as e:
                self._release_failed(row_id, _neutral_reason(e), run_seq)
                continue
            self._checkpoint()  # the model call may have outlived the lease
            # The displayed reason is rendered in code from the stored
            # phrases behind the validated ids: no model prose persists.
            phrases_by_id = {r["id"]: r["phrase"] for r in requirements}
            reason = render_judged_reason(judged, phrases_by_id)
            # The result write and the state transitions land as one fenced
            # transaction: no externally visible intermediate, no commit under
            # a re-acquired lease, and a crash cannot strand a result-less
            # pending_judgment row.
            def record_judgment(proxy):
                atomic_queue = SqlitePromotionQueueRepository(proxy)
                if claimed.state == "extracted":
                    atomic_queue.transition(row_id, "pending_judgment")
                SqliteOpportunityRepository(proxy).set_judged_fit(
                    claimed.opportunity_id, json.dumps({
                        "version_id": claimed.version_id,
                        "epoch": claimed.epoch,
                        "fit": judged.fit,
                        "matched_requirement_ids":
                            list(judged.matched_requirement_ids),
                        "gap_requirement_ids": list(judged.gap_requirement_ids),
                        "reason": reason,
                        "coverage_bp": claimed.coverage_bp or 0,
                    }, ensure_ascii=False))
                atomic_queue.transition(row_id, "judged")

            self._fenced_write(record_judgment)

    def _stop_model_stages(self, error: ModelUnavailableError) -> None:
        """Both model stages stand down for the rest of the run: nothing
        further is charged, no row is blamed. The note names the condition and
        its provider status, which is our own and the provider's metadata."""
        self._model_available = False
        self._notes.append(MODEL_UNAVAILABLE_NOTE + _diagnostic(error))

    def _claim_row(self, row_id: str, epoch: int):
        """Exclusive fenced claim (§4): the durable claimed marker lands by
        one conditional update BEFORE any model call, so a second runner's
        claim on the same row fails and never spends a model call; a claim
        left by a no-longer-live fence is recoverable."""
        return self._fenced_write(
            lambda proxy: SqlitePromotionQueueRepository(proxy).claim(
                row_id, epoch, owner_token=self._owner, fence=self._fence))

    def _release_failed(self, row_id: str, reason: str, run_seq: int) -> None:
        """Fenced release: the retry bookkeeping is a persistent transition
        too. reason must already be neutral (never model text)."""
        self._fenced_write(
            lambda proxy: SqlitePromotionQueueRepository(proxy).release_failed(
                row_id, reason, run_seq))

    def _posting_payload(self, queue_row,
                         require_epoch: bool = True) -> tuple[str, dict]:
        """Replay the pinned version's posting from its committed snapshot's
        raw payload (§1: raw responses are the replayable input) and build the
        isolation payload. Also returns the stored requirement proposals."""
        versions = self._opps.list_versions(queue_row.opportunity_id)
        version = next(v for v in versions if v.id == queue_row.version_id)
        opportunity = self._opps.get(queue_row.opportunity_id)
        source = self._registry.get(opportunity.source_id)
        adapter = self._adapters[source.ats_type]
        snapshot = self._snapshots.get(version.snapshot_id)
        stored = json.loads(self._storage.read_text(snapshot.raw_locator))
        if "page_locators" in stored:
            # Manifest form: the snapshot references the raw page objects the
            # fetch layer captured (§1); each page decodes at replay time.
            pages = [json.loads(self._storage.read_text(locator))
                     for locator in stored["page_locators"]]
        else:
            pages = stored["pages"]
        raw_job = next(
            (r for r in adapter.jobs_from_pages(pages)
             if adapter.external_id(r) == opportunity.external_job_id), None)
        if raw_job is None:
            # The external id is fetched data: attributed quoted value.
            raise ValueError(
                f'posting-supplied external id "{opportunity.external_job_id}"'
                f" not found in its own snapshot {snapshot.id}; raw payload"
                " inconsistent")
        normalized = adapter.normalize(raw_job)
        salary_view = json.loads(version.salary_json)["salary"] \
            if version.salary_json else None
        posting_json = build_posting_json(
            title=version.title, description=normalized.get("description"),
            locations=json.loads(version.location_json).get("locations", [])
            if version.location_json else [],
            salary=salary_view)
        proposals = json.loads(opportunity.requirement_proposals_json or "{}")
        stale = (proposals.get("version_id") != version.id
                 or proposals.get("stale")
                 or (require_epoch and proposals.get("epoch") != queue_row.epoch))
        if stale:
            # Stale by pin, closure, or epoch: never silently reused (§4/§5).
            # Extraction asks with require_epoch False: its result depends on
            # the pinned version alone, so an epoch bump does not invalidate
            # it (judgment consumes user state and keeps the epoch pin).
            proposals = {}
        return posting_json, proposals


def _posting_facts(version: OpportunityVersion) -> PostingFacts:
    location = json.loads(version.location_json) if version.location_json else {}
    remote = json.loads(version.remote_policy_json) if version.remote_policy_json else {}
    salary = json.loads(version.salary_json)["salary"] if version.salary_json else None
    restriction = remote.get("restriction_countries")
    return PostingFacts(
        locations=tuple(location.get("locations") or ()),
        remote_mode=remote.get("mode"),
        remote_restriction_countries=tuple(restriction) if restriction else None,
        timezone_requirement=None,  # no vendor states one structurally (§5)
        salary=salary,
        seniority=version.seniority,
    )


def _descending_text(text: str) -> tuple:
    return tuple(-byte for byte in text.encode())


# Exception types whose message is written by us, the database engine, or the
# operating system, never by a fetched posting: only these carry their message
# into the persisted diagnostic (a fetch or adapter error quotes vendor
# content, so it is recorded by type alone).
_MESSAGE_SAFE_ERRORS = (sqlite3.Error, OSError, LeaseLostError,
                        ModelUnavailableError, StorageObjectExistsError,
                        StageOutputError)


def _persisted_failure_message(error: BaseException) -> str:
    """The message an aborted run persists. Safe by construction rather than
    by inspection: an exception type that could carry fetched posting text is
    recorded by type alone, which is still enough to tell a transient
    infrastructure blip (`database is locked`) from a bug."""
    if isinstance(error, ModelCallError):
        return "model call failed operationally " + _diagnostic(error)
    if isinstance(error, _MESSAGE_SAFE_ERRORS):
        return str(error)[:_FAILURE_MESSAGE_MAX]
    return ("message withheld: this error type may carry fetched content;"
            " the record's error_type and stage are the diagnostic")


def _failure_summary(error: BaseException) -> str:
    """Type and safe message, for the run note beside the persisted failure
    record."""
    return f"{type(error).__name__}: {_persisted_failure_message(error)}"


def _diagnostic(error: BaseException) -> str:
    """The bounded diagnostic a failure record carries: the error class we
    raised and the provider's own HTTP status where the envelope reported one.
    Both are our metadata and the provider's, never anything derived from a
    fetched posting, so the OC-13 neutrality rule (which governs
    posting-derived text) applies to nothing here and stays intact. Without
    this an operator sees only 'failed operationally' and has to probe the CLI
    by hand to learn it was a quota error.

    Bounded at this boundary, not only where the envelope is parsed: anything
    that is not a plain HTTP status reads as unknown, so no caller can widen
    what gets written."""
    status = getattr(error, "provider_status", None)
    known = (isinstance(status, int) and not isinstance(status, bool)
             and 100 <= status <= 599)
    suffix = f", provider status {status}" if known else ""
    return f"[{type(error).__name__}{suffix}]"


def _neutral_reason(error) -> str:
    """Stored failure reasons never reproduce rejected model output or model
    text: StageOutputError messages are neutral by construction (they name
    the violated rule, never the text); anything else is categorized."""
    if isinstance(error, StageOutputError):
        return f"output failed validation: {error}"
    if isinstance(error, ModelCallError):
        return "model call failed operationally " + _diagnostic(error)
    if isinstance(error, OSError):
        # OS-written message (errno plus a path we minted), never posting text.
        return f"stored payload unreadable: {error}"
    return f"stage failed: {error}"


def _validate_material_fields(fields: dict) -> None:
    """Every persisted material field is type-checked before anything commits
    (§1): each is a string or None (the JSON fields arrive pre-serialized)."""
    for key in MATERIAL_FIELDS:
        value = fields.get(key)
        if value is not None and not isinstance(value, str):
            raise AdapterDegradedError(
                f"material field '{key}' is {type(value).__name__}, not a string")


def _staged_order(cap: int, rows: list[PendingRow]) -> list[str]:
    """The §4 half-cap aging order for the whole candidate set: the capped
    selection first, overflow after it in priority order. The BudgetLedger is
    the cap; iterating past it is what records the run's exhaustion honestly
    instead of silently truncating."""
    selected = select_for_stage(cap, rows)
    chosen = set(selected)
    overflow = sorted((r for r in rows if r.row_id not in chosen),
                      key=lambda r: (r.priority_key, r.enqueue_seq))
    return selected + [r.row_id for r in overflow]


def run_discovery(conn, storage, model, adapters, config: DiscoveryConfig | None = None,
                  say=print):
    config = config or load_config(storage)
    return DiscoveryRunner(conn, storage, model, adapters, config, say=say).run()
