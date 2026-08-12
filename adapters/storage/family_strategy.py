"""Atomic family and strategy mutations (spec:
decisions/package-generation-design.md, "Role-family onboarding step").

Every allocation-affecting change (family add, pause, drop, emphasis edit)
mints a complete new approved strategy version atomically: objective and all
active-family allocations copy forward with the edit applied, in one
transaction; nothing mutates an existing version. Raw SQL here on purpose:
the entity repositories each own their own transaction, so the combined
family-mutation-plus-version mint runs in this one seam instead."""

import json
import sqlite3

from domain.entities import RoleFamily
from domain.ids import new_id

from adapters.storage.sqlite_entities import (
    SqliteRoleFamilyRepository,
    SqliteStrategyRepository,
)


class StrategyError(ValueError):
    pass


def _validate_allocations(allocations: dict[str, int]) -> None:
    for family_id, allocation in allocations.items():
        if not isinstance(allocation, int) or not 1 <= allocation <= 5:
            raise StrategyError(
                f"allocation for '{family_id}' must be an integer 1 to 5, got {allocation}")


def _insert_version(conn: sqlite3.Connection, objective: str,
                    allocations: dict[str, int]) -> int:
    if not objective.strip():
        raise StrategyError("a strategy version requires an objective")
    _validate_allocations(allocations)
    (version,) = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM strategy_versions").fetchone()
    version_id = new_id("strat")
    conn.execute(
        "INSERT INTO strategy_versions (id, version, objective, created_by, user_approved)"
        " VALUES (?, ?, ?, 'user', 1)",
        (version_id, version, objective))
    for family_id, allocation in allocations.items():
        conn.execute(
            "INSERT INTO strategy_role_family_allocations"
            " (strategy_version_id, role_family_id, allocation) VALUES (?, ?, ?)",
            (version_id, family_id, allocation))
    return version


class FamilyStrategyService:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._families = SqliteRoleFamilyRepository(conn)
        self._strategy = SqliteStrategyRepository(conn)

    def current_allocations(self) -> tuple[str | None, dict[str, int]]:
        """(objective, family_id -> allocation) of the current approved
        version, or (None, {}) before families init."""
        current = self._strategy.current()
        if current is None:
            return None, {}
        return current.objective, {a.role_family_id: a.allocation
                                   for a in current.allocations}

    def mint_initial(self, families: list[RoleFamily], allocations: dict[str, int],
                     objective: str) -> int:
        """families init: confirmed family rows plus strategy version 1, one
        transaction."""
        with self._conn:
            for i, family in enumerate(families):
                self._conn.execute(
                    "INSERT INTO role_families (id, name, rationale, display_order,"
                    " target_seniority, geography, search_vocabulary, adjacent_titles,"
                    " status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')",
                    (family.id, family.name, family.rationale, i,
                     family.target_seniority, family.geography,
                     _json_list(family.search_vocabulary), _json_list(family.adjacent_titles)))
            return _insert_version(self._conn, objective, allocations)

    def add_family(self, family: RoleFamily, allocation: int,
                   objective: str | None = None) -> int:
        """Copy-forward: current objective and allocations, plus the new family."""
        current_objective, allocations = self._require_current(objective)
        allocations[family.id] = allocation
        with self._conn:
            self._conn.execute(
                "INSERT INTO role_families (id, name, rationale, target_seniority,"
                " geography, search_vocabulary, adjacent_titles, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
                (family.id, family.name, family.rationale, family.target_seniority,
                 family.geography, _json_list(family.search_vocabulary),
                 _json_list(family.adjacent_titles)))
            return _insert_version(self._conn, current_objective, allocations)

    def set_emphasis(self, family_id: str, allocation: int,
                     objective: str | None = None) -> int:
        current_objective, allocations = self._require_current(objective)
        if family_id not in allocations:
            raise StrategyError(f"family '{family_id}' has no allocation in the"
                                " current strategy version")
        allocations[family_id] = allocation
        with self._conn:
            return _insert_version(self._conn, current_objective, allocations)

    def set_status(self, family_id: str, status: str,
                   objective: str | None = None) -> int:
        """Pause/drop/reactivate; the allocation set copies forward with the
        family included only while active. Reactivation needs an allocation
        via set_emphasis-like re-add, so it takes the family's prior value 3
        as a neutral default."""
        if status not in ("active", "paused", "dropped"):
            raise StrategyError(f"unknown family status '{status}'")
        if self._families.get(family_id) is None:
            raise StrategyError(f"family '{family_id}' does not exist")
        current_objective, allocations = self._require_current(objective)
        if status == "active":
            allocations.setdefault(family_id, 3)
        else:
            allocations.pop(family_id, None)
        with self._conn:
            self._conn.execute(
                "UPDATE role_families SET status = ?,"
                " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (status, family_id))
            return _insert_version(self._conn, current_objective, allocations)

    def _require_current(self, objective: str | None) -> tuple[str, dict[str, int]]:
        current_objective, allocations = self.current_allocations()
        if current_objective is None:
            raise StrategyError("no approved strategy version exists; run"
                                " `open-career families init` first")
        return objective or current_objective, allocations


def _json_list(values: tuple[str, ...]) -> str:
    return json.dumps(list(values))
