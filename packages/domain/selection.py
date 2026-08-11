"""Family evidence selection: the walk and the input gate (spec:
decisions/package-generation-design.md, "Evidence selection").

role_family -> TARGETS -> capability -> SUPPORTS -> evidence -> PROVES -> fact
-> experience. Only generation-eligible edges are traversed; only approved,
active facts survive. The selection report lists, per targeted capability, the
eligible evidence chains, plus uncovered targeted capabilities explicitly (a
gap is surfaced, never papered over)."""

from dataclasses import dataclass

from domain.edges import eligibility_order_key, is_generation_eligible
from domain.entities import Capability
from domain.ports import CapabilityRepository, CareerEdgeRepository
from domain.traversal import EvidenceChain, EvidenceTraversal


@dataclass(frozen=True)
class CapabilitySelection:
    capability: Capability
    chains: tuple[EvidenceChain, ...]

    @property
    def covered(self) -> bool:
        """Covered = at least one eligible chain ends in an approved active fact."""
        return any(chain.facts for chain in self.chains)


@dataclass(frozen=True)
class SelectionReport:
    role_family_id: str
    selections: tuple[CapabilitySelection, ...]

    @property
    def covered(self) -> tuple[CapabilitySelection, ...]:
        return tuple(s for s in self.selections if s.covered)

    @property
    def gaps(self) -> tuple[Capability, ...]:
        """Targeted-but-uncovered capabilities: gap report only, never a skill."""
        return tuple(s.capability for s in self.selections if not s.covered)

    def fact_ids(self) -> frozenset[str]:
        """Every approved active fact reachable by this family's walk."""
        return frozenset(
            fc.fact.id for s in self.selections for chain in s.chains for fc in chain.facts)

    def facts_for_capability(self, capability_id: str) -> frozenset[str]:
        return frozenset(
            fc.fact.id for s in self.selections if s.capability.id == capability_id
            for chain in s.chains for fc in chain.facts)


class FamilyEvidenceSelection:
    """Extends the shipped EvidenceTraversal by the TARGETS hop."""

    def __init__(self, edges: CareerEdgeRepository, capabilities: CapabilityRepository,
                 traversal: EvidenceTraversal):
        self._edges = edges
        self._capabilities = capabilities
        self._traversal = traversal

    def select(self, role_family_id: str) -> SelectionReport:
        targets = [
            e for e in self._edges.active_edges_from("role_family", role_family_id, "TARGETS")
            if e.target_type == "capability" and is_generation_eligible(e)
        ]
        targets.sort(key=eligibility_order_key, reverse=True)
        selections = []
        for edge in targets:
            capability = self._capabilities.get(edge.target_id)
            if capability is None:
                continue
            chains = tuple(self._traversal.evidence_for_capability(capability.id))
            selections.append(CapabilitySelection(capability=capability, chains=chains))
        return SelectionReport(role_family_id=role_family_id, selections=tuple(selections))
