"""Career-state entities (migration 0002; spec: decisions/career-graph-schema.md).

Epistemic classes are structural, never conventions: user_approved gates fact
use for generation, and every assertion carries provenance from birth.
"""

from dataclasses import dataclass, field
from typing import Literal

ExperienceKind = Literal["role", "project", "education", "venture", "other"]
FactType = Literal["achievement", "responsibility", "skill_use", "metric", "scope", "other"]
FactStatus = Literal["active", "superseded", "retracted"]
FactSource = Literal["cv", "interview", "user_edit", "import"]
EvidenceType = Literal[
    "cv", "document", "repository", "portfolio", "url",
    "user_statement", "reference", "artifact", "metric_record",
]
# Strength is an enum, not a float (OC-22: no fake precision).
CapabilityStrength = Literal["none", "weak", "moderate", "strong"]
RoleFamilyStatus = Literal["active", "paused", "dropped"]
GoalHorizon = Literal["near", "mid", "long"]
GoalStatus = Literal["active", "achieved", "dropped"]
StrategyCreatedBy = Literal["user", "proposal"]
ProfileWriteSource = Literal["user_edit", "resolution"]


@dataclass(frozen=True)
class Experience:
    """A container facts hang off: a role, a project, a venture."""

    id: str
    kind: ExperienceKind
    title: str
    org: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    summary: str | None = None
    display_order: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class CareerFact:
    """A normalized assertion, the atom of generation. Born unapproved when
    LLM-extracted; unusable for generation until user_approved (OC-5/OC-6)."""

    id: str
    fact_type: FactType
    statement: str
    source: FactSource
    user_approved: int
    experience_id: str | None = None
    status: FactStatus = "active"
    confidence: float | None = None
    source_location: str | None = None
    created_at: str | None = None
    verified_at: str | None = None


@dataclass(frozen=True)
class Evidence:
    """Something that backs facts and capabilities. Files live under instance/
    via StorageAdapter; the row stores the locator, never bytes."""

    id: str
    evidence_type: EvidenceType
    title: str
    locator: str | None = None
    content_hash: str | None = None
    notes: str | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class Capability:
    id: str
    name: str
    strength: CapabilityStrength
    description: str | None = None
    last_assessed_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class RoleFamily:
    """A target role family. Emphasis lives solely in the current approved
    strategy version's allocations; display_order is presentation only."""

    id: str
    name: str
    rationale: str
    display_order: int | None = None
    target_seniority: str | None = None
    geography: str | None = None
    search_vocabulary: tuple[str, ...] = ()
    adjacent_titles: tuple[str, ...] = ()
    status: RoleFamilyStatus = "active"
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class CareerGoal:
    id: str
    statement: str
    horizon: GoalHorizon
    status: GoalStatus = "active"
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class StrategyVersion:
    """Append-only: updates insert a new version. Current = highest approved.
    Agent proposals arrive created_by='proposal', user_approved=0."""

    id: str
    version: int
    objective: str
    created_by: StrategyCreatedBy
    user_approved: int
    time_horizon: str | None = None
    constraints_json: str = "{}"
    hypotheses_json: str = "[]"
    bottlenecks: str | None = None
    focus: str | None = None
    created_at: str | None = None
    allocations: tuple["StrategyAllocation", ...] = field(default=())


@dataclass(frozen=True)
class StrategyAllocation:
    """Discrete 1-to-5 emphasis per role family (OC-22: no float weights)."""

    role_family_id: str
    allocation: int


@dataclass(frozen=True)
class ProfileFieldWrite:
    """Audit row appended by every profile mutation: a changed field is always
    attributable, and narrower-than-GLOBAL answers have no path in."""

    id: str
    field: str
    old_value: str | None
    new_value: str | None
    source: ProfileWriteSource
    resolution_id: str | None = None
    created_at: str | None = None
