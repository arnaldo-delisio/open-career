"""Career graph edge entity (OC-21: provenance per edge, fact/inference separation)."""

from dataclasses import dataclass
from typing import Literal

ClaimKind = Literal["fact", "inference"]


@dataclass(frozen=True)
class CareerEdge:
    """A directed edge in the career graph.

    claim_kind separates observed facts from inferences; source records where
    the edge came from (user input, document, inference run), per OC-21.
    """

    source_id: str
    target_id: str
    edge_type: str
    claim_kind: ClaimKind
    source: str
    id: int | None = None
    created_at: str | None = None
