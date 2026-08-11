"""Package storage domain model (migration 0003; spec:
decisions/package-generation-design.md, "Storage, states, CLI").

Lifecycle state lives on the version. The state-transition table and the
status-dependent required fields are enforced at the repository seam, never by
convention; finalized bundle fields are write-once.
"""

from dataclasses import dataclass

# Version states this phase needs (subset of blueprint section 15.3). Later
# phases add states additively (TEXT column, no CHECK).
GENERATING = "GENERATING"
VERIFIED = "VERIFIED"
FAILED = "FAILED"
APPROVED = "APPROVED"

# The explicit state-transition table the repository seam enforces.
STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    GENERATING: frozenset({VERIFIED, FAILED}),
    VERIFIED: frozenset({APPROVED}),
    FAILED: frozenset(),
    APPROVED: frozenset(),
}

# The immutable audit bundle VERIFIED (and therefore APPROVED) requires.
VERIFIED_REQUIRED_FIELDS = (
    "content_model_json",
    "context_snapshot_locator",
    "input_context_hash",
    "verifier_report_json",
    "ats_report_json",
    "artifact_locator",
    "artifact_hash",
)

# Fields that become write-once the moment a version leaves GENERATING.
BUNDLE_FIELDS = VERIFIED_REQUIRED_FIELDS + ("failure_report_json",)


class PackageStateError(RuntimeError):
    """An operation violated the state-transition table, the write-once rule,
    or a status-dependent required-field rule."""


class LeaseLostError(RuntimeError):
    """The worker no longer holds the lease (expired, superseded generation,
    or the row left GENERATING). The worker stops without writing."""


def is_valid_transition(current: str, target: str) -> bool:
    return target in STATE_TRANSITIONS.get(current, frozenset())


@dataclass(frozen=True)
class Package:
    """One base package per role family (partial unique index); the
    opportunity phase declares its own identity rule when it arrives."""

    id: str
    role_family_id: str
    opportunity_id: str | None = None
    approved_version_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class PackageVersion:
    id: str
    package_id: str
    version: int
    status: str
    content_model_json: str | None = None
    context_snapshot_locator: str | None = None
    input_context_hash: str | None = None
    verifier_report_json: str | None = None
    ats_report_json: str | None = None
    artifact_locator: str | None = None
    artifact_hash: str | None = None
    gauntlet_report_json: str | None = None
    failure_report_json: str | None = None
    lease_owner: str | None = None
    lease_generation: int = 0
    lease_expires_at: str | None = None
    approved_at: str | None = None
    created_at: str | None = None


def object_locator(package_id: str, version: int, lease_generation: int, name: str) -> str:
    """Deterministic, version-derived object locator, recorded before object
    creation. Embeds the lease generation as a fence: a stale worker's write
    lands at a stale-generation locator, attributable on sight, never
    referenced by a finalized row."""
    return f"packages/{package_id}/v{version}/g{lease_generation}/{name}"
