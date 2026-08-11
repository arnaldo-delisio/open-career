"""Ports: interfaces the domain depends on, implemented by adapters."""

from abc import ABC, abstractmethod

from domain.edges import CareerEdge
from domain.entities import (
    Capability,
    CareerFact,
    CareerGoal,
    Evidence,
    Experience,
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


class StorageAdapter(ABC):
    """Filesystem access boundary. Implementations root all paths at the
    instance directory; domain and app code never touch the filesystem directly."""

    @abstractmethod
    def read_text(self, relative_path: str) -> str: ...

    @abstractmethod
    def write_text(self, relative_path: str, content: str) -> None: ...

    @abstractmethod
    def exists(self, relative_path: str) -> bool: ...


class ModelUnavailableError(RuntimeError):
    """The model backend cannot run at all (e.g. claude CLI absent)."""


class ModelAdapter(ABC):
    """Model call boundary (OC-32: subscription-backed CLI adapters). The model
    maps structure, never values (OC-5): callers validate output against a
    closed schema in code and never let it decide a canonical answer."""

    @abstractmethod
    def complete(self, prompt: str) -> str:
        """Run one prompt, return the model's text result."""
