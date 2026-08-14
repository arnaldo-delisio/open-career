"""Discovery entities (migration 0006; spec: the scope's
decisions/discovery-design.md, OC-37).

State is separated structurally, not by convention (§3): observed availability,
gate verdict, proposed action, and human-selected action are four fields with
four owners, and polling never overwrites a human selection. Snapshots are
immutable and per-source sequenced; that committed order is what absence
streaks and suspect cohorts reference.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

AtsType = Literal["greenhouse", "lever", "ashby", "workable", "smartrecruiters"]
SourceOrigin = Literal["harvest", "curated", "manual"]
SourceStatus = Literal["candidate", "enabled", "disabled"]
MetadataOrigin = Literal["curated", "cli_edit"]
Availability = Literal["open", "closed", "reopened"]
ProposedAction = Literal["ignore", "monitor", "pursue"]
ApplySupport = Literal["extension", "none"]
BacklogState = Literal["pending", "gated", "discarded"]
QueueState = Literal[
    "pending_extraction", "extracted", "pending_judgment", "judged",
    "superseded", "failed",
]
# Operator display order for queue rows: the actionable stage states first
# (the live work), then the terminal and inert ones, so a truncated listing
# never shows only dead rows.
QUEUE_STATE_DISPLAY_ORDER: tuple[str, ...] = (
    "pending_extraction", "extracted", "pending_judgment",
    "judged", "superseded", "failed",
)
RunStatus = Literal["running", "completed", "budget_exhausted", "failed",
                    "interrupted"]
CohortOutcome = Literal["pending", "closed", "reappeared"]

# Greenhouse/Lever/Ashby are the fill-adapter set (OC-3); Workable and
# SmartRecruiters are discovery-only, so their opportunities carry
# apply_support 'none' and nothing downstream promises a fill (§1).
FILL_ADAPTER_ATS_TYPES: frozenset[str] = frozenset({"greenhouse", "lever", "ashby"})


def apply_support_for(ats_type: str) -> ApplySupport:
    return "extension" if ats_type in FILL_ADAPTER_ATS_TYPES else "none"


@dataclass(frozen=True)
class Source:
    """A registry row (§2). id is stable identity; tenant_slug is a mutable
    locator, so a slug change never mints a new source or a fake closure."""

    id: str
    ats_type: AtsType
    tenant_slug: str
    origin: SourceOrigin
    status: SourceStatus = "candidate"
    company_name: str | None = None
    last_checked: str | None = None
    last_success: str | None = None
    consecutive_failures: int = 0
    last_polled_at: str | None = None
    next_poll_at: str | None = None
    next_probe_at: str | None = None
    probe_attempts: int = 0
    last_poll_outcome: str | None = None
    policy_notes: str | None = None
    industry: str | None = None
    industry_origin: MetadataOrigin | None = None
    company_stage: str | None = None
    company_stage_origin: MetadataOrigin | None = None
    company_size_band: str | None = None
    company_size_band_origin: MetadataOrigin | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class SourceSupersession:
    """Reviewed link from an old source to its successor (ATS migration or
    tenant rename); opportunity lineage across it needs an explicit reviewed
    per-opportunity mapping, never automatic continuity (§2/§3)."""

    id: str
    old_source_id: str
    new_source_id: str
    origin: Literal["migration"]
    reviewed_at: str
    notes: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class Snapshot:
    """An immutable committed complete poll (§1): all pages or none. seq is
    the per-source committed order closure logic references."""

    id: str
    source_id: str
    seq: int
    raw_locator: str
    content_hash: str
    completion_json: str
    posting_count: int
    remote_version_token: str | None = None
    run_id: str | None = None
    committed_at: str | None = None


@dataclass(frozen=True)
class Opportunity:
    id: str
    source_id: str
    external_job_id: str
    first_seen: str
    last_seen: str
    apply_support: ApplySupport
    availability: Availability = "open"
    absence_streak: int = 0
    last_absence_snapshot_id: str | None = None
    closed_observed_at: str | None = None
    closing_snapshot_ids_json: str | None = None
    reopen_count: int = 0
    current_version_id: str | None = None
    latest_gate_verdict_id: str | None = None
    proposed_action: ProposedAction | None = None
    proposed_action_version_id: str | None = None
    proposed_action_epoch: int | None = None
    human_action: str | None = None
    backlog_state: BacklogState = "pending"
    backlog_discard_reason: str | None = None
    requirement_proposals_json: str | None = None
    judged_fit_json: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


# The material-field enumeration (§3): consumed by gate, extraction, rank,
# action proposal, or apply support. Extending it is a design change.
MATERIAL_FIELDS: tuple[str, ...] = (
    "title", "seniority", "work_authorization_json", "language_requirements_json",
    "description_hash", "location_json", "remote_policy_json", "salary_json",
    "apply_url",
)


@dataclass(frozen=True)
class OpportunityVersion:
    """Append-only material state of a posting; a changed material field mints
    a new row, and every queue row and derived result pins one of these."""

    id: str
    opportunity_id: str
    version: int
    snapshot_id: str
    fingerprint: str
    title: str | None = None
    seniority: str | None = None
    work_authorization_json: str | None = None
    language_requirements_json: str | None = None
    description_hash: str | None = None
    location_json: str | None = None
    remote_policy_json: str | None = None
    salary_json: str | None = None
    apply_url: str | None = None
    created_at: str | None = None


def material_fingerprint(fields: dict) -> str:
    """Deterministic hash over the material fields; equality means no new
    version. Unknown keys are rejected so a field added to the payload cannot
    silently bypass versioning."""
    unknown = set(fields) - set(MATERIAL_FIELDS)
    if unknown:
        raise ValueError(f"non-material fields in fingerprint input: {sorted(unknown)}")
    canonical = json.dumps({k: fields.get(k) for k in MATERIAL_FIELDS}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class StoredGateVerdict:
    """A persisted gate evaluation (§5): overall verdict, the per-dimension
    checks JSON-encoded with their reasons, and the dependency epoch it was
    computed under. Appended, never updated."""

    id: str
    opportunity_id: str
    version_id: str
    epoch: int
    verdict: Literal["pass", "fail"]
    dimensions_json: str
    created_at: str | None = None


@dataclass(frozen=True)
class QueueRow:
    """A promotion-queue row (§4), pinned to an immutable version.
    relevance_score, lane_rank, first_seen, and enqueue_seq are the frozen
    pre-extraction ordering key; coverage_bp (basis points, integer) is set at
    the extracted transition."""

    id: str
    opportunity_id: str
    version_id: str
    state: QueueState
    lane_rank: int
    first_seen: str
    enqueue_seq: int
    epoch: int
    coverage_bp: int | None = None
    relevance_score: int = 0
    attempts: int = 0
    next_attempt_run_seq: int = 0
    claimed_by: str | None = None
    claimed_fence: int | None = None
    superseded_reason: str | None = None
    failure_reason: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class DiscoveryRun:
    id: str
    run_seq: int
    status: RunStatus
    budget_json: str
    epoch: int
    exhausted_stage: str | None = None
    spend_json: str | None = None
    source_outcomes_json: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    # Set only on an aborted run: the exception type and message, plus the
    # stage and source id when known (§4).
    failure_json: str | None = None


@dataclass(frozen=True)
class SuspectCohort:
    """A mass-closure suspect cohort (§3), keyed to the first triggering
    snapshot; confirmation is row-level against the immediately next
    consecutive committed snapshot."""

    id: str
    source_id: str
    triggering_snapshot_id: str
    resolved: int = 0
    created_at: str | None = None


@dataclass(frozen=True)
class CohortMember:
    cohort_id: str
    opportunity_id: str
    outcome: CohortOutcome = "pending"
