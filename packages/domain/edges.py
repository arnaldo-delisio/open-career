"""Career graph edge layer (migration 0002 shape; spec: decisions/career-graph-schema.md).

One generic edge table carries every relationship with claim_kind, provenance,
and verification. The edge vocabulary lives here, not in a CHECK constraint, so
later phases extend it without a migration; the repository boundary enforces it
transactionally.
"""

from dataclasses import dataclass
from typing import Literal

ClaimKind = Literal["fact", "inference"]
EdgeCreatedBy = Literal["user", "import", "matcher"]

# Endpoint type for edges migrated from 0001, whose ids were untyped. Excluded
# from traversal until the user re-types them (open-career edges list --untyped).
UNKNOWN_TYPE = "unknown"

# edge_type -> (source entity type, target entity type). Role families are
# deliberately not a PRIORITIZES endpoint: the strategy allocations table is the
# single expression of role-family emphasis.
EDGE_VOCABULARY: dict[str, tuple[str, str]] = {
    "PROVES": ("evidence", "career_fact"),
    "SUPPORTS": ("evidence", "capability"),
    "DEMONSTRATES": ("experience", "capability"),
    "REQUIRES": ("career_goal", "capability"),
    "PRIORITIZES": ("strategy_version", "capability"),
    # Package-generation phase (OC-33): which capabilities matter inside one
    # family. Does not rival the allocations table, which ranks families
    # against each other; TARGETS states relevance, not possession.
    "TARGETS": ("role_family", "capability"),
}


@dataclass(frozen=True)
class CareerEdge:
    """A directed edge. Edges retire by superseded_at, never delete."""

    id: str
    source_type: str
    source_id: str
    edge_type: str
    target_type: str
    target_id: str
    claim_kind: ClaimKind
    provenance: str
    created_by: EdgeCreatedBy
    user_verified: int
    confidence: float | None = None
    derived_from_fact_id: str | None = None
    created_at: str | None = None
    superseded_at: str | None = None


def is_generation_eligible(edge: CareerEdge) -> bool:
    """The generation-eligibility gate: user-verified, or born as a fact from a
    user or import. Matcher-inferred unverified edges are proposals, visible in
    review surfaces but never traversed for generation until verified."""
    if edge.user_verified:
        return True
    return edge.created_by in ("user", "import") and edge.claim_kind == "fact"


def eligibility_order_key(edge: CareerEdge) -> tuple:
    """Sort key for eligible results: user_verified DESC, confidence DESC NULLS
    LAST (use with reverse=True)."""
    return (edge.user_verified, edge.confidence is not None, edge.confidence or 0.0)
