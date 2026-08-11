"""The load-bearing traversal (OC-21; contract in decisions/career-graph-schema.md).

Given a capability, follow active SUPPORTS (evidence -> capability) edges to
evidence, then each evidence item's active PROVES (evidence -> career_fact)
edges to approved, active facts and their experiences. Only generation-eligible
edges are traversed; matcher-created unverified edges and 'unknown'-typed
migrated edges never reach generation.
"""

from dataclasses import dataclass

from domain.edges import CareerEdge, eligibility_order_key, is_generation_eligible
from domain.entities import CareerFact, Evidence, Experience
from domain.ports import (
    CareerEdgeRepository,
    CareerFactRepository,
    EvidenceRepository,
    ExperienceRepository,
)


@dataclass(frozen=True)
class FactChain:
    proves_edge: CareerEdge
    fact: CareerFact
    experience: Experience | None


@dataclass(frozen=True)
class EvidenceChain:
    supports_edge: CareerEdge
    evidence: Evidence
    facts: tuple[FactChain, ...]


class EvidenceTraversal:
    def __init__(self, edges: CareerEdgeRepository, evidence: EvidenceRepository,
                 facts: CareerFactRepository, experiences: ExperienceRepository):
        self._edges = edges
        self._evidence = evidence
        self._facts = facts
        self._experiences = experiences

    def evidence_for_capability(self, capability_id: str) -> list[EvidenceChain]:
        supports = [
            e for e in self._edges.active_edges_to("capability", capability_id, "SUPPORTS")
            if e.source_type == "evidence" and is_generation_eligible(e)
        ]
        supports.sort(key=eligibility_order_key, reverse=True)
        chains = []
        for supports_edge in supports:
            evidence = self._evidence.get(supports_edge.source_id)
            if evidence is None:
                continue
            chains.append(EvidenceChain(
                supports_edge=supports_edge,
                evidence=evidence,
                facts=tuple(self._facts_for_evidence(evidence.id)),
            ))
        return chains

    def _facts_for_evidence(self, evidence_id: str) -> list[FactChain]:
        proves = [
            e for e in self._edges.active_edges_from("evidence", evidence_id, "PROVES")
            if e.target_type == "career_fact" and is_generation_eligible(e)
        ]
        proves.sort(key=eligibility_order_key, reverse=True)
        fact_chains = []
        for proves_edge in proves:
            fact = self._facts.get(proves_edge.target_id)
            if fact is None or fact.status != "active" or not fact.user_approved:
                continue
            experience = self._experiences.get(fact.experience_id) if fact.experience_id else None
            fact_chains.append(FactChain(proves_edge=proves_edge, fact=fact, experience=experience))
        return fact_chains
