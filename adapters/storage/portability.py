"""Portable export/import: JSON dump and load of the instance database."""

import json
import sqlite3
from pathlib import Path

from adapters.storage.migrations import MIGRATIONS_DIR, migrate, pending_migrations

EXPORT_VERSION = 1


def _user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def export_db(db: Path) -> dict:
    """Dump the instance database. The source must exist and be at the current
    migration version, so every successful export is accepted by import."""
    if not db.exists():
        raise ValueError("instance not initialized (run: open-career init)")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        if pending_migrations(conn, MIGRATIONS_DIR):
            raise ValueError("schema out of date, run migrate before exporting")
        tables = {}
        for table in _user_tables(conn):
            tables[table] = [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
        return {"format": "open-career-export", "version": EXPORT_VERSION, "tables": tables}
    finally:
        conn.close()


def import_db(db: Path, dump: dict) -> None:
    """Load a dump into a database. A structurally invalid dump is rejected
    before the database is touched; a valid-looking dump first migrates the
    target (with the standard pre-upgrade backup), then loads rows
    all-or-nothing after schema-level validation."""
    if not isinstance(dump, dict) or dump.get("format") != "open-career-export":
        raise ValueError("not an open-career export")
    if dump.get("version") != EXPORT_VERSION:
        raise ValueError(f"unsupported export version: {dump.get('version')} (supported: {EXPORT_VERSION})")
    tables = dump.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("export carries no tables mapping")
    for table, rows in tables.items():
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise ValueError(f"table '{table}' must be a list of row objects")
    # Structural validation passed; from here on the target database is touched
    # (migrate first, with its standard pre-upgrade backup).
    try:
        migrate(db)
        conn = sqlite3.connect(db)
    except sqlite3.Error as e:
        raise ValueError(f"cannot open instance database: {e}") from e
    try:
        try:
            known = set(_user_tables(conn))
            got = set(tables)
            if got != known:
                missing, unknown = known - got, got - known
                parts = []
                if missing:
                    parts.append(f"missing tables: {sorted(missing)}")
                if unknown:
                    parts.append(f"unknown tables: {sorted(unknown)}")
                raise ValueError("export does not match the current schema; " + "; ".join(parts))
            for table, rows in tables.items():
                table_cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
                for i, row in enumerate(rows):
                    bad = set(row) - table_cols
                    if bad:
                        raise ValueError(f"table '{table}' row {i}: unknown columns {sorted(bad)}")
        except sqlite3.Error as e:
            raise ValueError(f"reading instance schema failed: {e}") from e
        current_table = None
        try:
            with conn:  # one transaction: a failure anywhere rolls the whole load back
                for table, rows in tables.items():
                    current_table = table
                    conn.execute(f'DELETE FROM "{table}"')
                    for row in rows:
                        cols = list(row)
                        placeholders = ", ".join("?" for _ in cols)
                        col_list = ", ".join(f'"{c}"' for c in cols)
                        conn.execute(
                            f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})',
                            [row[c] for c in cols],
                        )
        except sqlite3.Error as e:
            raise ValueError(f"loading table '{current_table}' failed: {e}") from e
    finally:
        conn.close()


def export_to_file(db: Path, out: Path) -> None:
    out.write_text(json.dumps(export_db(db), indent=2))


def import_from_file(db: Path, src: Path) -> None:
    if not src.exists():
        raise ValueError(f"import file not found: {src}")
    import_db(db, json.loads(src.read_text()))
