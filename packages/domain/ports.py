"""Ports: interfaces the domain depends on, implemented by adapters."""

from abc import ABC, abstractmethod

from domain.edges import CareerEdge


class CareerEdgeRepository(ABC):
    """Data access for career graph edges. Implementations live in adapters."""

    @abstractmethod
    def add(self, edge: CareerEdge) -> CareerEdge:
        """Persist an edge and return it with id and created_at set."""

    @abstractmethod
    def list_all(self) -> list[CareerEdge]:
        """Return every edge."""


class StorageAdapter(ABC):
    """Filesystem access boundary. Implementations root all paths at the
    instance directory; domain and app code never touch the filesystem directly."""

    @abstractmethod
    def read_text(self, relative_path: str) -> str: ...

    @abstractmethod
    def write_text(self, relative_path: str, content: str) -> None: ...

    @abstractmethod
    def exists(self, relative_path: str) -> bool: ...
