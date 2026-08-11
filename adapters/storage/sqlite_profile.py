"""The profile write seam (spec: decisions/career-graph-schema.md, user_profile).

One mutation operation for the single-row JSON profile: field validated against
the closed canonical set, source validated, audit row appended, all in one
transaction. Resolution-sourced writes are a reserved seam (OC-6): the source
value exists in the schema, but only 'user_edit' is implemented until the
Resolution protocol lands and can prove a confirmed GLOBAL scope.
"""

import json
import sqlite3

from domain.entities import ProfileFieldWrite
from domain.ids import new_id
from domain.ports import UserProfileRepository
from domain.profile import validate_profile_field, validate_profile_value


class SqliteUserProfileRepository(UserProfileRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get_fields(self) -> dict:
        row = self._conn.execute("SELECT fields_json FROM user_profile WHERE id = 1").fetchone()
        return json.loads(row[0]) if row else {}

    def set_field(self, field: str, value: str | None, source: str,
                  resolution_id: str | None = None) -> None:
        validate_profile_field(field)
        validate_profile_value(field, value)
        if source == "resolution":
            raise NotImplementedError(
                "resolution-sourced profile writes arrive with the Resolution protocol"
                " (OC-6); only a confirmed-GLOBAL Resolution may ever write here")
        if source != "user_edit":
            raise ValueError(f"unknown profile write source '{source}'")
        with self._conn:  # field write and audit row land together
            row = self._conn.execute(
                "SELECT fields_json FROM user_profile WHERE id = 1").fetchone()
            fields = json.loads(row[0]) if row else {}
            old_value = fields.get(field)
            fields[field] = value
            if row:
                self._conn.execute(
                    "UPDATE user_profile SET fields_json = ?,"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1",
                    (json.dumps(fields),),
                )
            else:
                self._conn.execute(
                    "INSERT INTO user_profile (id, fields_json) VALUES (1, ?)",
                    (json.dumps(fields),),
                )
            self._conn.execute(
                "INSERT INTO profile_field_writes (id, field, old_value, new_value,"
                " source, resolution_id) VALUES (?, ?, ?, ?, ?, ?)",
                (new_id("pfw"), field, old_value, value, source, resolution_id),
            )

    def list_writes(self) -> list[ProfileFieldWrite]:
        rows = self._conn.execute(
            "SELECT id, field, old_value, new_value, source, resolution_id, created_at"
            " FROM profile_field_writes ORDER BY created_at, id"
        ).fetchall()
        return [ProfileFieldWrite(*r) for r in rows]
