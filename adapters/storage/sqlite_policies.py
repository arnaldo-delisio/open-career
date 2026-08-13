"""The policy write seam (migration 0004; spec: the scope's
decisions/onboarding-interview-design.md, user_policies).

Mirrors the profile seam: one mutation operation for the single-row JSON
policy document, key validated against the closed policy set, per-key value
shape validated, source validated, audit row appended, all in one transaction.
Values are stored (and audited) JSON-encoded. Resolution-sourced writes are the
same reserved seam as the profile's (OC-6)."""

import json
import sqlite3

from adapters.storage.epoch import bump_dependency_epoch
from domain.entities import PolicyWrite
from domain.ids import new_id
from domain.policies import validate_policy_key, validate_policy_value
from domain.ports import UserPolicyRepository


class SqliteUserPolicyRepository(UserPolicyRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get_policies(self) -> dict:
        row = self._conn.execute("SELECT policies_json FROM user_policies WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else {}

    def set_policy(self, key: str, value, source: str,
                   resolution_id: str | None = None) -> None:
        validate_policy_key(key)
        validate_policy_value(key, value)
        if source == "resolution":
            raise NotImplementedError(
                "resolution-sourced policy writes arrive with the Resolution protocol"
                " (OC-6); only a confirmed-GLOBAL Resolution may ever write here")
        if source != "user_edit":
            raise ValueError(f"unknown policy write source '{source}'")
        with self._conn:  # policy write and audit row land together
            row = self._conn.execute(
                "SELECT policies_json FROM user_policies WHERE id = 1").fetchone()
            policies = json.loads(row[0]) if row else {}
            old_value_json = json.dumps(policies[key]) if key in policies else None
            policies[key] = value
            if row:
                self._conn.execute(
                    "UPDATE user_policies SET policies_json = ?,"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1",
                    (json.dumps(policies),),
                )
            else:
                self._conn.execute(
                    "INSERT INTO user_policies (id, policies_json) VALUES (1, ?)",
                    (json.dumps(policies),),
                )
            self._conn.execute(
                "INSERT INTO policy_writes (id, key, old_value, new_value,"
                " source, resolution_id) VALUES (?, ?, ?, ?, ?, ?)",
                (new_id("plw"), key, old_value_json, json.dumps(value), source, resolution_id),
            )
            bump_dependency_epoch(self._conn)  # OC-37 §5: gate results go stale

    def list_writes(self) -> list[PolicyWrite]:
        rows = self._conn.execute(
            "SELECT id, key, old_value, new_value, source, resolution_id, created_at"
            " FROM policy_writes ORDER BY created_at, id"
        ).fetchall()
        return [PolicyWrite(*r) for r in rows]
