"""The generation context: assembled in code, the only data path into the
prompt (spec: decisions/package-generation-design.md, "Evidence selection").

Two named views:
- the prompt context: everything, including the non-renderable strategy
  targeting block (family row, approved objective, this family's allocation);
- the renderable grounding view: facts, experiences, profile, selected
  capability rows only. Every verifier rule runs exclusively against this
  view, so strategy text cannot leak into the CV.

The context persists as a canonical serialized snapshot under instance/ via
StorageAdapter; input_context_hash hashes that snapshot, making auditable
which strategy informed a version and letting the verifier re-check later."""

import hashlib
import json
from dataclasses import dataclass

from domain.entities import Capability, Experience, RoleFamily
from domain.grounding_spec import SPEC_VERSION
from domain.selection import SelectionReport


@dataclass(frozen=True)
class StrategySnapshot:
    """Non-renderable targeting control data: steers selection and phrasing
    emphasis; its values never enter the grounding allowlist unless
    independently present in approved facts or profile."""

    family: RoleFamily
    strategy_version: int
    objective: str
    allocation: int


@dataclass(frozen=True)
class GenerationContext:
    role_family_id: str
    selection: SelectionReport
    profile: dict
    experiences: tuple[Experience, ...]
    strategy: StrategySnapshot

    # -- renderable grounding view ----------------------------------------

    def covered_capabilities(self) -> tuple[Capability, ...]:
        return tuple(s.capability for s in self.selection.covered)

    def renderable_grounding_view(self) -> dict:
        """Facts, experiences, profile, selected capability rows only. No
        strategy block, no family row, no objective."""
        facts = {}
        for s in self.selection.selections:
            for chain in s.chains:
                for fc in chain.facts:
                    facts[fc.fact.id] = {
                        "statement": fc.fact.statement,
                        "fact_type": fc.fact.fact_type,
                        "experience_id": fc.fact.experience_id,
                    }
        return {
            "facts": facts,
            "experiences": {e.id: _experience_dict(e) for e in self.experiences},
            "profile": dict(self.profile),
            "capabilities": {c.id: {"name": c.name, "strength": c.strength}
                             for c in self.covered_capabilities()},
        }

    # -- snapshot ----------------------------------------------------------

    def snapshot_json(self) -> str:
        """The exact canonical serialized context: source ids, fact statements,
        profile fields, traversal edges, selection report, spec version."""
        selection = {
            "role_family_id": self.selection.role_family_id,
            "capabilities": [
                {
                    "capability_id": s.capability.id,
                    "capability_name": s.capability.name,
                    "covered": s.covered,
                    "chains": [
                        {
                            "supports_edge_id": chain.supports_edge.id,
                            "evidence_id": chain.evidence.id,
                            "facts": [
                                {"proves_edge_id": fc.proves_edge.id, "fact_id": fc.fact.id}
                                for fc in chain.facts
                            ],
                        }
                        for chain in s.chains
                    ],
                }
                for s in self.selection.selections
            ],
            "gaps": [c.id for c in self.selection.gaps],
        }
        return json.dumps({
            "normalization_spec_version": SPEC_VERSION,
            "role_family_id": self.role_family_id,
            "selection": selection,
            "renderable_grounding_view": self.renderable_grounding_view(),
            "strategy": {
                "family": {
                    "id": self.strategy.family.id,
                    "name": self.strategy.family.name,
                    "rationale": self.strategy.family.rationale,
                    "adjacent_titles": list(self.strategy.family.adjacent_titles),
                    "search_vocabulary": list(self.strategy.family.search_vocabulary),
                },
                "strategy_version": self.strategy.strategy_version,
                "objective": self.strategy.objective,
                "allocation": self.strategy.allocation,
            },
        }, indent=2, sort_keys=True)

    def snapshot_hash(self) -> str:
        return hashlib.sha256(self.snapshot_json().encode()).hexdigest()


def _experience_dict(e: Experience) -> dict:
    return {
        "kind": e.kind, "title": e.title, "org": e.org,
        "start_date": e.start_date, "end_date": e.end_date, "summary": e.summary,
    }
