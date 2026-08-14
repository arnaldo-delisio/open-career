"""Ports: interfaces the domain depends on, implemented by adapters."""

from abc import ABC, abstractmethod

from domain.edges import CareerEdge
from domain.entities import (
    Capability,
    CareerFact,
    CareerGoal,
    Evidence,
    Experience,
    PolicyWrite,
    ProfileFieldWrite,
    RoleFamily,
    StrategyVersion,
)


class CareerEdgeRepository(ABC):
    """Data access for career graph edges. add() enforces the edge vocabulary
    transactionally: known edge_type with matching endpoint types, both
    endpoints existing, no duplicate active logical edge."""

    @abstractmethod
    def add(self, edge: CareerEdge) -> CareerEdge: ...

    @abstractmethod
    def list_all(self) -> list[CareerEdge]: ...

    @abstractmethod
    def list_untyped(self) -> list[CareerEdge]:
        """Edges migrated from 0001 with 'unknown' endpoint types, excluded
        from traversal until re-typed."""

    @abstractmethod
    def active_edges_to(self, target_type: str, target_id: str, edge_type: str) -> list[CareerEdge]: ...

    @abstractmethod
    def active_edges_from(self, source_type: str, source_id: str, edge_type: str) -> list[CareerEdge]: ...


class ExperienceRepository(ABC):
    @abstractmethod
    def add(self, experience: Experience) -> None: ...

    @abstractmethod
    def get(self, experience_id: str) -> Experience | None: ...

    @abstractmethod
    def list_all(self) -> list[Experience]: ...

    @abstractmethod
    def update_fields(self, experience_id: str, title: str, org: str | None,
                      start_date: str | None, end_date: str | None) -> None: ...


class CareerFactRepository(ABC):
    @abstractmethod
    def add(self, fact: CareerFact) -> None: ...

    @abstractmethod
    def get(self, fact_id: str) -> CareerFact | None: ...

    @abstractmethod
    def list_all(self) -> list[CareerFact]: ...

    @abstractmethod
    def set_approval(self, fact_id: str, statement: str, verified_at: str) -> None:
        """Approve a draft fact (confirm or confirm-with-edit)."""

    @abstractmethod
    def set_status(self, fact_id: str, status: str) -> None: ...


class EvidenceRepository(ABC):
    @abstractmethod
    def add(self, evidence: Evidence) -> None: ...

    @abstractmethod
    def get(self, evidence_id: str) -> Evidence | None: ...

    @abstractmethod
    def list_all(self) -> list[Evidence]: ...

    @abstractmethod
    def mark_review_completed(self, evidence_id: str, completed_at: str) -> None:
        """Record that this evidence's review surface completed (OC-39)."""


class CapabilityRepository(ABC):
    @abstractmethod
    def add(self, capability: Capability) -> None: ...

    @abstractmethod
    def get(self, capability_id: str) -> Capability | None: ...

    @abstractmethod
    def get_by_name(self, name: str) -> Capability | None: ...

    @abstractmethod
    def list_all(self) -> list[Capability]: ...


class RoleFamilyRepository(ABC):
    @abstractmethod
    def add(self, role_family: RoleFamily) -> None: ...

    @abstractmethod
    def get(self, role_family_id: str) -> RoleFamily | None: ...

    @abstractmethod
    def list_all(self) -> list[RoleFamily]: ...


class CareerGoalRepository(ABC):
    @abstractmethod
    def add(self, goal: CareerGoal) -> None: ...

    @abstractmethod
    def get(self, goal_id: str) -> CareerGoal | None: ...

    @abstractmethod
    def list_all(self) -> list[CareerGoal]: ...


class StrategyRepository(ABC):
    """Append-only strategy versions with their relational allocations."""

    @abstractmethod
    def add_version(self, version: StrategyVersion) -> None: ...

    @abstractmethod
    def current(self) -> StrategyVersion | None:
        """Highest approved version, allocations attached."""

    @abstractmethod
    def list_versions(self) -> list[StrategyVersion]: ...


class UserProfileRepository(ABC):
    """The profile write seam: one mutation operation, every write audited in
    profile_field_writes, so a narrower-than-GLOBAL Resolution answer has no
    path into the profile (spec: decisions/career-graph-schema.md)."""

    @abstractmethod
    def get_fields(self) -> dict: ...

    @abstractmethod
    def set_field(self, field: str, value: str | None, source: str,
                  resolution_id: str | None = None) -> None:
        """Validate field (closed canonical set) and source, write the field,
        append the audit row, in one transaction. Only source='user_edit' is
        implemented now; 'resolution' is the reserved Resolution seam (OC-6)."""

    @abstractmethod
    def list_writes(self) -> list[ProfileFieldWrite]: ...


class UserPolicyRepository(ABC):
    """The policy write seam (migration 0004), mirroring the profile seam: one
    mutation operation, closed key set validated in the domain
    (domain/policies.py), every write audited in policy_writes."""

    @abstractmethod
    def get_policies(self) -> dict: ...

    @abstractmethod
    def set_policy(self, key: str, value, source: str,
                   resolution_id: str | None = None) -> None:
        """Validate key (closed policy set) and per-key value shape, write the
        policy, append the audit row, in one transaction. Only
        source='user_edit' is implemented now; 'resolution' is the reserved
        Resolution seam (OC-6)."""

    @abstractmethod
    def list_writes(self) -> list[PolicyWrite]: ...


class PackageRepository(ABC):
    """The package lifecycle seam (spec: decisions/package-generation-design.md).
    Enforces, transactionally: the state-transition table, status-dependent
    required fields, write-once finalized bundle fields, and the generation
    lease (owner token, generation fence, expiry at the database clock)."""

    @abstractmethod
    def get_or_create_base_package(self, role_family_id: str): ...

    @abstractmethod
    def get_package(self, package_id: str): ...

    @abstractmethod
    def get_base_package_for_family(self, role_family_id: str): ...

    @abstractmethod
    def get_version(self, version_id: str): ...

    @abstractmethod
    def list_versions(self, package_id: str) -> list: ...

    @abstractmethod
    def reserve_version(self, package_id: str, owner_token: str, lease_seconds: int):
        """Reserve the next version number in a short write transaction as a
        GENERATING row holding a fresh lease. A concurrent generate loses on
        the UNIQUE constraint and retries cleanly."""

    @abstractmethod
    def renew_lease(self, version_id: str, owner_token: str, lease_generation: int,
                    lease_seconds: int) -> bool:
        """One atomic conditional update: GENERATING status, matching owner and
        generation, unexpired lease at the db clock. Zero rows renewed means
        the worker is permanently stopped."""

    @abstractmethod
    def check_lease(self, version_id: str, owner_token: str, lease_generation: int) -> bool:
        """True only while this owner holds an unexpired lease on a GENERATING
        row (checked immediately before each object write)."""

    @abstractmethod
    def record_progress(self, version_id: str, owner_token: str, lease_generation: int,
                        **fields) -> None:
        """Persist stage progress on the owned GENERATING row (lease-fenced)."""

    @abstractmethod
    def finalize_verified(self, version_id: str, owner_token: str, lease_generation: int,
                          **bundle) -> None:
        """GENERATING -> VERIFIED, validating lease ownership, token, and
        generation atomically, plus the full required audit bundle."""

    @abstractmethod
    def fail(self, version_id: str, owner_token: str, lease_generation: int,
             failure_report_json: str) -> None:
        """GENERATING -> FAILED by the lease owner; always requires a
        structured failure report; retains recorded partial artifacts."""

    @abstractmethod
    def claim_expired_and_fail(self, version_id: str, failure_report_json: str) -> bool:
        """Recovery: claim a GENERATING row whose lease has expired, via
        compare-and-set on status and lease generation, and mark it FAILED
        with a structured interruption report. A live generation can never be
        failed from under its owner."""

    @abstractmethod
    def approve(self, version_id: str, approved_at: str, override: bool = False,
                override_reason: str | None = None) -> None:
        """VERIFIED -> APPROVED; sets packages.approved_version_id in the same
        repository operation, validating same-package ownership. The approval
        gate (spec: the scope's decisions/gauntlet-design.md): the repository
        owns the current suite_version and resolves that suite's effective
        Gauntlet run inside its own transaction; an effective terminal
        current-suite run with verdict PASS is required, or override with a
        mandatory non-empty reason waiving only a recorded FAIL or ATTENTION,
        never missing adjudication. Every approval appends an
        approval_decisions record."""

    # -- Gauntlet (spec: the scope's decisions/gauntlet-design.md) ---------

    @abstractmethod
    def claim_gauntlet_reservation(self, version_id: str, suite_version: str,
                                   owner_token: str, ttl_seconds: int) -> bool:
        """Atomic admission for one run attempt: insert, or take over an
        existing reservation only when its expiry is past the database clock.
        Raises when a complete attempt already exists for the suite (further
        same-suite attempts are rejected); returns False when a live worker
        holds the reservation."""

    @abstractmethod
    def renew_gauntlet_reservation(self, version_id: str, suite_version: str,
                                   owner_token: str, ttl_seconds: int) -> bool:
        """One atomic conditional renewal (matching owner token and unexpired
        expiry at the database clock); zero rows renewed permanently stops
        the worker."""

    @abstractmethod
    def release_gauntlet_reservation(self, version_id: str, suite_version: str,
                                     owner_token: str) -> bool:
        """One atomic conditional release (matching owner token and unexpired
        expiry), for a claimed attempt that failed before it could record a
        run: the suite becomes immediately re-runnable instead of waiting out
        the reservation. Fenced like the consume, so a stale worker can never
        drop a successor's reservation."""

    @abstractmethod
    def insert_gauntlet_run(self, version_id: str, suite_version: str,
                            owner_token: str, **fields):
        """Append one write-once run row inside the same transaction that
        conditionally consumes the reservation (matching owner token and
        unexpired expiry); a zero-row consume discards the result entirely
        and inserts nothing. The attempt number is allocated here. Append
        only: no update, no delete."""

    @abstractmethod
    def get_gauntlet_run(self, run_id: str): ...

    @abstractmethod
    def list_gauntlet_runs(self, version_id: str) -> list:
        """All runs for a version, newest first by seq."""

    @abstractmethod
    def effective_gauntlet_run(self, version_id: str, suite_version: str):
        """The suite's effective run: its complete run with the greatest seq
        (seq is the sole ordering authority), or None."""

    @abstractmethod
    def list_approval_decisions(self, version_id: str) -> list: ...


class StorageObjectExistsError(RuntimeError):
    """A write-once object already exists at the locator; nothing overwrites."""


class StorageAdapter(ABC):
    """Filesystem access boundary. Implementations root all paths at the
    instance directory; domain and app code never touch the filesystem directly."""

    @abstractmethod
    def read_text(self, relative_path: str) -> str: ...

    @abstractmethod
    def read_bytes(self, relative_path: str) -> bytes: ...

    @abstractmethod
    def write_text(self, relative_path: str, content: str) -> None: ...

    @abstractmethod
    def write_bytes(self, relative_path: str, content: bytes) -> None: ...

    @abstractmethod
    def write_text_new(self, relative_path: str, content: str) -> None:
        """Write-once: raises StorageObjectExistsError if the object exists
        (package snapshots and artifacts are stored non-overwritably)."""

    @abstractmethod
    def write_bytes_new(self, relative_path: str, content: bytes) -> None:
        """Write-once variant of write_bytes; see write_text_new."""

    @abstractmethod
    def exists(self, relative_path: str) -> bool: ...


class CvRenderer(ABC):
    """Content model -> PDF bytes (single-column ATS-safe template, headless
    Chromium). Asserts the fixed section order; drift is a build failure."""

    @abstractmethod
    def render_pdf(self, cv) -> bytes: ...


class PdfTextExtractor(ABC):
    """Runs pdftotext -layout on artifact bytes for the mandatory separate
    ATS-parseability step (never folded into render, never skipped)."""

    @abstractmethod
    def extract_layout(self, pdf_bytes: bytes) -> str: ...


class SourceAdapter(ABC):
    """One port for all five public-API sources (OC-37 §1; blueprint §10.3
    shape). Implementations live in adapters/sources/ and may call only the
    whitelisted public API hosts (OC-1, a tested constant); the domain never
    imports an HTTP library."""

    @abstractmethod
    def list_jobs(self, tenant_slug: str) -> list: ...

    @abstractmethod
    def fetch_job(self, tenant_slug: str, external_job_id: str): ...

    @abstractmethod
    def normalize(self, raw_job) -> dict: ...

    @abstractmethod
    def healthcheck(self, tenant_slug: str) -> bool: ...


class SourceRegistryRepository(ABC):
    """The §2 registry: stable source identity, mutable tenant locator,
    scheduler state from stored DB timestamps, reviewed company metadata with
    per-field provenance, supersession records reviewed via CLI."""

    @abstractmethod
    def add(self, source) -> None: ...

    @abstractmethod
    def get(self, source_id: str): ...

    @abstractmethod
    def list_all(self) -> list: ...

    @abstractmethod
    def set_status(self, source_id: str, status: str) -> None: ...

    @abstractmethod
    def set_reviewed_metadata(self, source_id: str, field: str, value: str | None,
                              origin: str) -> None:
        """Write one reviewed metadata field with its provenance; the only
        write path (curation or explicit CLI edit), never a classifier."""

    @abstractmethod
    def record_supersession(self, supersession) -> None: ...

    @abstractmethod
    def list_supersessions(self) -> list: ...

    @abstractmethod
    def record_probe_outcome(self, source_id: str, success: bool,
                             next_probe_at: str | None) -> None:
        """One probe's scheduler effects: success enables and resets attempt
        state; failure records the caller-computed backoff time."""

    @abstractmethod
    def record_poll_outcome(self, source_id: str, outcome: str,
                            next_poll_at: str | None = None,
                            rot_threshold: int = 5,
                            next_probe_at: str | None = None) -> None:
        """One poll's scheduler effects: success/degraded/oversized/deferred.
        Degraded increments consecutive failures and disables at the rot
        threshold; oversized and deferred touch no health or rot state."""


class SnapshotRepository(ABC):
    """Immutable committed snapshots (§1): commit assigns the next per-source
    sequence number transactionally; nothing updates or deletes a row."""

    @abstractmethod
    def commit(self, source_id: str, raw_locator: str, content_hash: str,
               completion_json: str, posting_count: int,
               remote_version_token: str | None = None,
               run_id: str | None = None): ...

    @abstractmethod
    def get(self, snapshot_id: str): ...

    @abstractmethod
    def list_for_source(self, source_id: str) -> list: ...

    @abstractmethod
    def latest_for_source(self, source_id: str): ...


class OpportunityRepository(ABC):
    """Opportunities and their append-only versions (§3). Identity is
    (source_id, external_job_id); the four state fields have four owners and
    apply_closure_plan never touches human_action."""

    @abstractmethod
    def add(self, opportunity) -> None: ...

    @abstractmethod
    def get(self, opportunity_id: str): ...

    @abstractmethod
    def get_by_key(self, source_id: str, external_job_id: str): ...

    @abstractmethod
    def list_for_source(self, source_id: str) -> list: ...

    @abstractmethod
    def add_version(self, version) -> None:
        """Append a version row and point current_version_id at it."""

    @abstractmethod
    def list_versions(self, opportunity_id: str) -> list: ...

    @abstractmethod
    def apply_closure_plan(self, plan, observed_at: str) -> None:
        """Apply one snapshot's ClosurePlan in a single transaction: presence
        resets and reopens, streak increments, closures with their confirming
        snapshot ids, cohort resolution, and new-cohort creation."""

    @abstractmethod
    def pending_cohort_for_source(self, source_id: str):
        """The unresolved suspect cohort as a closure.PendingCohort, or None."""

    @abstractmethod
    def record_gate_verdict(self, verdict) -> None:
        """Append the auditable gate verdict and point latest_gate_verdict_id
        at it; a gated-out opportunity is stored, never dropped."""

    @abstractmethod
    def get_gate_verdict(self, verdict_id: str): ...

    @abstractmethod
    def set_proposed_action(self, opportunity_id: str, action: str,
                            version_id: str | None = None,
                            epoch: int | None = None) -> None:
        """Persist the machine proposal with its version and epoch pin;
        readers must never present a stale-pinned proposal as current."""

    @abstractmethod
    def set_human_action(self, opportunity_id: str, action: str | None) -> None: ...

    @abstractmethod
    def set_requirement_proposals(self, opportunity_id: str, proposals_json: str) -> None:
        """Persist extracted requirement phrases as proposals pinned to their
        version, never verified graph edges (§5)."""

    @abstractmethod
    def set_judged_fit(self, opportunity_id: str, judged_fit_json: str) -> None: ...

    @abstractmethod
    def list_backlog_pending(self) -> list: ...

    @abstractmethod
    def list_open_with_stale_gate(self, current_epoch: int) -> list:
        """Open opportunities whose latest gate verdict predates the current
        dependency epoch, oldest verdicts first (§5 re-gating)."""

    @abstractmethod
    def list_filtered(self, availability: str | None = None,
                      gate: str | None = None,
                      source_id: str | None = None) -> list: ...

    @abstractmethod
    def promote_from_backlog(self, opportunity_id: str):
        """Transactional promotion out of the observed-ungated backlog (§4):
        returns the current open version to gate, marking the row gated; a
        closed or superseded observation is discarded with its reason
        recorded and None returned."""


class PromotionQueueRepository(ABC):
    """Version-pinned queue rows with durable states (§4). Claiming re-checks
    the pinned version is still the current open version, else releases the
    row as superseded; supersession cancels unfinished rows atomically."""

    @abstractmethod
    def enqueue(self, opportunity_id: str, version_id: str, lane_rank: int,
                first_seen: str, epoch: int):
        """Insert with the frozen ordering key and the next enqueue sequence;
        merging by (opportunity, version) key returns the existing row."""

    @abstractmethod
    def get(self, row_id: str): ...

    @abstractmethod
    def list_rows(self, state: str | None = None) -> list: ...

    @abstractmethod
    def pending_for_stage(self, state: str, current_run_seq: int) -> list:
        """Claimable rows in a stage state: retry backoff respected, terminal
        and superseded rows excluded."""

    @abstractmethod
    def claim(self, row_id: str, current_epoch: int,
              owner_token: str | None = None, fence: int | None = None):
        """Claim a row for work: verifies the pinned version is still the
        current open version and the epoch is current, else marks the row
        superseded and returns None. With owner_token and fence, the claim is
        a durable exclusive marker set by one conditional update before any
        model call; a row claimed under a fence that is no longer the live
        lease is claimable again."""

    @abstractmethod
    def transition(self, row_id: str, new_state: str, coverage_bp: int | None = None) -> None: ...

    @abstractmethod
    def release_failed(self, row_id: str, reason: str, current_run_seq: int) -> None:
        """Bounded retry (§4): re-release with backoff while attempts remain,
        else the terminal auditable failed state."""

    @abstractmethod
    def retry_failed(self, row_id: str, current_run_seq: int) -> None:
        """Explicit CLI retry of a terminal failed row after remediation."""

    @abstractmethod
    def supersede_for_opportunity(self, opportunity_id: str, current_version_id: str | None,
                                  reason: str) -> None:
        """Cancel unfinished rows not pinned to the current open version (all
        rows when the opportunity closed, current_version_id None)."""

    @abstractmethod
    def supersede_stale_epochs(self, current_epoch: int, reason: str) -> None: ...


class DiscoveryRunRepository(ABC):
    """Run records (§4): budget locked and recorded at start, spend and
    per-source outcomes at finish, monotonic run_seq for retry backoff."""

    @abstractmethod
    def start(self, budget_json: str, epoch: int, lease_owner: str | None = None,
              lease_fence: int | None = None): ...

    @abstractmethod
    def finish(self, run_id: str, status: str, spend_json: str,
               source_outcomes_json: str, exhausted_stage: str | None = None,
               failure_json: str | None = None) -> None: ...

    @abstractmethod
    def reconcile_abandoned(self) -> list[str]:
        """Run rows still 'running' whose owning lease is no longer live are
        reconciled to the terminal 'interrupted' status, spend retained."""

    @abstractmethod
    def get(self, run_id: str): ...

    @abstractmethod
    def list_all(self) -> list: ...


class DependencyEpochRepository(ABC):
    """The single dependency-epoch integer (§5), bumped by any audited write
    to user policies, profile, active role families/strategy, or the eligible
    edge set; gate and rank results record the epoch they ran under."""

    @abstractmethod
    def current(self) -> int: ...

    @abstractmethod
    def bump(self) -> int: ...


class ModelUnavailableError(RuntimeError):
    """The model backend cannot run at all (e.g. claude CLI absent)."""


class ModelAdapter(ABC):
    """Model call boundary (OC-32: subscription-backed CLI adapters). The model
    maps structure, never values (OC-5): callers validate output against a
    closed schema in code and never let it decide a canonical answer."""

    @abstractmethod
    def complete(self, prompt: str) -> str:
        """Run one prompt, return the model's text result."""

    def complete_with_meta(self, prompt: str) -> tuple[str, dict]:
        """Run one prompt, return (text, metadata). Metadata carries the
        provider-reported resolved model identity under 'model' when the
        backend reports one. Deliberately a concrete default, not a second
        abstract method (the Gauntlet design's model-identity rule): existing
        complete-only implementations and test doubles stay instantiable and
        report the identity honestly as unreported."""
        return self.complete(prompt), {"model": "unreported"}

    def provider_version(self) -> str:
        """The backend's own version string, verbatim, observed once per run.
        This is PROVIDER identity, not model identity: where a backend reports
        no resolved model (the Codex CLI does not), the recorded provider
        version is what bounds a demonstrated claim, and a change in it voids
        a demonstration table exactly as a model-set change does. Never
        inferred: a backend that cannot be asked reports 'unavailable'."""
        return "unavailable"
