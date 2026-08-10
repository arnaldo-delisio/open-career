"""SQLite implementation of CareerEdgeRepository. No business logic in SQL."""

import sqlite3

from domain.edges import CareerEdge
from domain.ports import CareerEdgeRepository


class SqliteCareerEdgeRepository(CareerEdgeRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(self, edge: CareerEdge) -> CareerEdge:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO career_edges (source_id, target_id, edge_type, claim_kind, source)"
                " VALUES (?, ?, ?, ?, ?)",
                (edge.source_id, edge.target_id, edge.edge_type, edge.claim_kind, edge.source),
            )
        row = self._conn.execute(
            "SELECT id, source_id, target_id, edge_type, claim_kind, source, created_at"
            " FROM career_edges WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return self._row_to_edge(row)

    def list_all(self) -> list[CareerEdge]:
        rows = self._conn.execute(
            "SELECT id, source_id, target_id, edge_type, claim_kind, source, created_at"
            " FROM career_edges ORDER BY id"
        ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    @staticmethod
    def _row_to_edge(row: tuple) -> CareerEdge:
        return CareerEdge(
            id=row[0],
            source_id=row[1],
            target_id=row[2],
            edge_type=row[3],
            claim_kind=row[4],
            source=row[5],
            created_at=row[6],
        )
