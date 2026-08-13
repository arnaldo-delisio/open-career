"""SQLite CareerEdgeRepository. Validation logic lives here in code (edge
vocabulary, endpoint existence, duplicate active edge); SQL carries only
constraints and the partial unique index backstop."""

import sqlite3

from adapters.storage.epoch import bump_dependency_epoch
from domain.edges import (
    EDGE_VOCABULARY,
    UNKNOWN_TYPE,
    CareerEdge,
    is_generation_eligible,
)
from domain.ports import CareerEdgeRepository

# Entity type -> table holding it, for endpoint existence checks (also used by
# portability import validation).
ENTITY_TABLES = {
    "experience": "experiences",
    "career_fact": "career_facts",
    "evidence": "evidence",
    "capability": "capabilities",
    "role_family": "role_families",
    "career_goal": "career_goals",
    "strategy_version": "strategy_versions",
}

_COLUMNS = ("id, source_type, source_id, edge_type, target_type, target_id,"
            " claim_kind, confidence, provenance, derived_from_fact_id,"
            " created_by, user_verified, created_at, superseded_at")


class EdgeValidationError(ValueError):
    pass


class SqliteCareerEdgeRepository(CareerEdgeRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(self, edge: CareerEdge) -> CareerEdge:
        with self._conn:  # one transaction: validation and insert together
            expected = EDGE_VOCABULARY.get(edge.edge_type)
            if expected is None:
                raise EdgeValidationError(
                    f"unknown edge_type '{edge.edge_type}'; known: {sorted(EDGE_VOCABULARY)}")
            if (edge.source_type, edge.target_type) != expected:
                raise EdgeValidationError(
                    f"{edge.edge_type} requires {expected[0]} -> {expected[1]},"
                    f" got {edge.source_type} -> {edge.target_type}")
            self._require_endpoint(edge.source_type, edge.source_id)
            self._require_endpoint(edge.target_type, edge.target_id)
            duplicate = self._conn.execute(
                "SELECT id FROM career_edges WHERE source_type = ? AND source_id = ?"
                " AND edge_type = ? AND target_type = ? AND target_id = ?"
                " AND superseded_at IS NULL",
                (edge.source_type, edge.source_id, edge.edge_type,
                 edge.target_type, edge.target_id),
            ).fetchone()
            if duplicate:
                raise EdgeValidationError(
                    f"active edge already exists ({duplicate[0]}): "
                    f"{edge.source_id} -{edge.edge_type}-> {edge.target_id}")
            self._conn.execute(
                "INSERT INTO career_edges (id, source_type, source_id, edge_type,"
                " target_type, target_id, claim_kind, confidence, provenance,"
                " derived_from_fact_id, created_by, user_verified)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (edge.id, edge.source_type, edge.source_id, edge.edge_type,
                 edge.target_type, edge.target_id, edge.claim_kind, edge.confidence,
                 edge.provenance, edge.derived_from_fact_id, edge.created_by,
                 edge.user_verified),
            )
            if is_generation_eligible(edge):
                # OC-37 §5: the eligible edge set changed; gate results go stale.
                bump_dependency_epoch(self._conn)
        return self._get(edge.id)

    def _require_endpoint(self, entity_type: str, entity_id: str) -> None:
        table = ENTITY_TABLES.get(entity_type)
        if table is None:
            raise EdgeValidationError(f"unknown endpoint type '{entity_type}'")
        row = self._conn.execute(f'SELECT 1 FROM "{table}" WHERE id = ?', (entity_id,)).fetchone()
        if row is None:
            raise EdgeValidationError(f"{entity_type} '{entity_id}' does not exist")

    def _get(self, edge_id: str) -> CareerEdge:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM career_edges WHERE id = ?", (edge_id,)
        ).fetchone()
        return _row_to_edge(row)

    def list_all(self) -> list[CareerEdge]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM career_edges ORDER BY id").fetchall()
        return [_row_to_edge(r) for r in rows]

    def list_untyped(self) -> list[CareerEdge]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM career_edges"
            " WHERE source_type = ? OR target_type = ? ORDER BY id",
            (UNKNOWN_TYPE, UNKNOWN_TYPE),
        ).fetchall()
        return [_row_to_edge(r) for r in rows]

    def active_edges_to(self, target_type: str, target_id: str, edge_type: str) -> list[CareerEdge]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM career_edges"
            " WHERE target_type = ? AND target_id = ? AND edge_type = ?"
            " AND superseded_at IS NULL ORDER BY id",
            (target_type, target_id, edge_type),
        ).fetchall()
        return [_row_to_edge(r) for r in rows]

    def active_edges_from(self, source_type: str, source_id: str, edge_type: str) -> list[CareerEdge]:
        rows = self._conn.execute(
            f"SELECT {_COLUMNS} FROM career_edges"
            " WHERE source_type = ? AND source_id = ? AND edge_type = ?"
            " AND superseded_at IS NULL ORDER BY id",
            (source_type, source_id, edge_type),
        ).fetchall()
        return [_row_to_edge(r) for r in rows]


def _row_to_edge(row: tuple) -> CareerEdge:
    return CareerEdge(
        id=row[0], source_type=row[1], source_id=row[2], edge_type=row[3],
        target_type=row[4], target_id=row[5], claim_kind=row[6], confidence=row[7],
        provenance=row[8], derived_from_fact_id=row[9], created_by=row[10],
        user_verified=row[11], created_at=row[12], superseded_at=row[13],
    )
