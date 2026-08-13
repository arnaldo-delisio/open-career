"""SQLite repositories for the career-state entity tables (migration 0002).
Plain row mapping; no business logic in SQL."""

import json
import sqlite3

from domain.entities import (
    Capability,
    CareerFact,
    CareerGoal,
    Evidence,
    Experience,
    RoleFamily,
    StrategyAllocation,
    StrategyVersion,
)
from domain.ports import (
    CapabilityRepository,
    CareerFactRepository,
    CareerGoalRepository,
    EvidenceRepository,
    ExperienceRepository,
    RoleFamilyRepository,
    StrategyRepository,
)


class SqliteExperienceRepository(ExperienceRepository):
    _SELECT = ("SELECT id, kind, title, org, start_date, end_date, summary,"
               " display_order, created_at, updated_at FROM experiences")

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(self, experience: Experience) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO experiences (id, kind, title, org, start_date, end_date,"
                " summary, display_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (experience.id, experience.kind, experience.title, experience.org,
                 experience.start_date, experience.end_date, experience.summary,
                 experience.display_order),
            )

    def get(self, experience_id: str) -> Experience | None:
        row = self._conn.execute(f"{self._SELECT} WHERE id = ?", (experience_id,)).fetchone()
        return Experience(*row) if row else None

    def list_all(self) -> list[Experience]:
        rows = self._conn.execute(f"{self._SELECT} ORDER BY display_order, id").fetchall()
        return [Experience(*r) for r in rows]


class SqliteCareerFactRepository(CareerFactRepository):
    _SELECT = ("SELECT id, fact_type, statement, source, user_approved, experience_id,"
               " status, confidence, source_location, created_at, verified_at,"
               " origin_evidence_id FROM career_facts")

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(self, fact: CareerFact) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO career_facts (id, experience_id, fact_type, statement, status,"
                " confidence, source, source_location, user_approved, verified_at,"
                " origin_evidence_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (fact.id, fact.experience_id, fact.fact_type, fact.statement, fact.status,
                 fact.confidence, fact.source, fact.source_location, fact.user_approved,
                 fact.verified_at, fact.origin_evidence_id),
            )

    def get(self, fact_id: str) -> CareerFact | None:
        row = self._conn.execute(f"{self._SELECT} WHERE id = ?", (fact_id,)).fetchone()
        return CareerFact(*row) if row else None

    def list_all(self) -> list[CareerFact]:
        return [CareerFact(*r) for r in self._conn.execute(f"{self._SELECT} ORDER BY id")]

    def set_approval(self, fact_id: str, statement: str, verified_at: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE career_facts SET statement = ?, user_approved = 1, verified_at = ?"
                " WHERE id = ?",
                (statement, verified_at, fact_id),
            )

    def set_status(self, fact_id: str, status: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE career_facts SET status = ? WHERE id = ?", (status, fact_id))


class SqliteEvidenceRepository(EvidenceRepository):
    _SELECT = ("SELECT id, evidence_type, title, locator, content_hash, notes,"
               " created_at, review_completed_at FROM evidence")

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(self, evidence: Evidence) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO evidence (id, evidence_type, title, locator, content_hash, notes)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (evidence.id, evidence.evidence_type, evidence.title, evidence.locator,
                 evidence.content_hash, evidence.notes),
            )

    def get(self, evidence_id: str) -> Evidence | None:
        row = self._conn.execute(f"{self._SELECT} WHERE id = ?", (evidence_id,)).fetchone()
        return Evidence(*row) if row else None

    def list_all(self) -> list[Evidence]:
        return [Evidence(*r) for r in self._conn.execute(f"{self._SELECT} ORDER BY id")]

    def mark_review_completed(self, evidence_id: str, completed_at: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE evidence SET review_completed_at = ? WHERE id = ?",
                (completed_at, evidence_id))


class SqliteCapabilityRepository(CapabilityRepository):
    _SELECT = ("SELECT id, name, strength, description, last_assessed_at, created_at,"
               " updated_at FROM capabilities")

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(self, capability: Capability) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO capabilities (id, name, strength, description, last_assessed_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (capability.id, capability.name, capability.strength,
                 capability.description, capability.last_assessed_at),
            )

    def get(self, capability_id: str) -> Capability | None:
        row = self._conn.execute(f"{self._SELECT} WHERE id = ?", (capability_id,)).fetchone()
        return Capability(*row) if row else None

    def get_by_name(self, name: str) -> Capability | None:
        row = self._conn.execute(f"{self._SELECT} WHERE name = ?", (name,)).fetchone()
        return Capability(*row) if row else None

    def list_all(self) -> list[Capability]:
        return [Capability(*r) for r in self._conn.execute(f"{self._SELECT} ORDER BY name")]


class SqliteRoleFamilyRepository(RoleFamilyRepository):
    _SELECT = ("SELECT id, name, rationale, display_order, target_seniority, geography,"
               " search_vocabulary, adjacent_titles, status, created_at, updated_at"
               " FROM role_families")

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(self, role_family: RoleFamily) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO role_families (id, name, rationale, display_order,"
                " target_seniority, geography, search_vocabulary, adjacent_titles, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (role_family.id, role_family.name, role_family.rationale,
                 role_family.display_order, role_family.target_seniority,
                 role_family.geography, json.dumps(list(role_family.search_vocabulary)),
                 json.dumps(list(role_family.adjacent_titles)), role_family.status),
            )

    def get(self, role_family_id: str) -> RoleFamily | None:
        row = self._conn.execute(f"{self._SELECT} WHERE id = ?", (role_family_id,)).fetchone()
        return self._row_to_family(row) if row else None

    def list_all(self) -> list[RoleFamily]:
        rows = self._conn.execute(f"{self._SELECT} ORDER BY display_order, id").fetchall()
        return [self._row_to_family(r) for r in rows]

    @staticmethod
    def _row_to_family(row: tuple) -> RoleFamily:
        return RoleFamily(
            id=row[0], name=row[1], rationale=row[2], display_order=row[3],
            target_seniority=row[4], geography=row[5],
            search_vocabulary=tuple(json.loads(row[6])),
            adjacent_titles=tuple(json.loads(row[7])),
            status=row[8], created_at=row[9], updated_at=row[10],
        )


class SqliteCareerGoalRepository(CareerGoalRepository):
    _SELECT = "SELECT id, statement, horizon, status, created_at, updated_at FROM career_goals"

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(self, goal: CareerGoal) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO career_goals (id, statement, horizon, status) VALUES (?, ?, ?, ?)",
                (goal.id, goal.statement, goal.horizon, goal.status),
            )

    def get(self, goal_id: str) -> CareerGoal | None:
        row = self._conn.execute(f"{self._SELECT} WHERE id = ?", (goal_id,)).fetchone()
        return CareerGoal(*row) if row else None

    def list_all(self) -> list[CareerGoal]:
        return [CareerGoal(*r) for r in self._conn.execute(f"{self._SELECT} ORDER BY id")]


class SqliteStrategyRepository(StrategyRepository):
    _SELECT = ("SELECT id, version, objective, created_by, user_approved, time_horizon,"
               " constraints_json, hypotheses_json, bottlenecks, focus, created_at"
               " FROM strategy_versions")

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add_version(self, version: StrategyVersion) -> None:
        with self._conn:  # version row and allocations land together
            self._conn.execute(
                "INSERT INTO strategy_versions (id, version, objective, time_horizon,"
                " constraints_json, hypotheses_json, bottlenecks, focus, created_by,"
                " user_approved) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (version.id, version.version, version.objective, version.time_horizon,
                 version.constraints_json, version.hypotheses_json, version.bottlenecks,
                 version.focus, version.created_by, version.user_approved),
            )
            for allocation in version.allocations:
                self._conn.execute(
                    "INSERT INTO strategy_role_family_allocations"
                    " (strategy_version_id, role_family_id, allocation) VALUES (?, ?, ?)",
                    (version.id, allocation.role_family_id, allocation.allocation),
                )

    def current(self) -> StrategyVersion | None:
        row = self._conn.execute(
            f"{self._SELECT} WHERE user_approved = 1 ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return self._attach_allocations(StrategyVersion(*row)) if row else None

    def list_versions(self) -> list[StrategyVersion]:
        rows = self._conn.execute(f"{self._SELECT} ORDER BY version").fetchall()
        return [self._attach_allocations(StrategyVersion(*r)) for r in rows]

    def _attach_allocations(self, version: StrategyVersion) -> StrategyVersion:
        rows = self._conn.execute(
            "SELECT role_family_id, allocation FROM strategy_role_family_allocations"
            " WHERE strategy_version_id = ? ORDER BY role_family_id",
            (version.id,),
        ).fetchall()
        allocations = tuple(StrategyAllocation(role_family_id=r[0], allocation=r[1]) for r in rows)
        return StrategyVersion(**{**version.__dict__, "allocations": allocations})
