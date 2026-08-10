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
    """Load a dump into a database. The dump is validated in full before any
    data is touched: supported version, and exactly the current table set."""
    if dump.get("format") != "open-career-export":
        raise ValueError("not an open-career export")
    if dump.get("version") != EXPORT_VERSION:
        raise ValueError(f"unsupported export version: {dump.get('version')} (supported: {EXPORT_VERSION})")
    tables = dump.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("export carries no tables mapping")
    migrate(db)
    conn = sqlite3.connect(db)
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
        with conn:
            for table, rows in tables.items():
                conn.execute(f'DELETE FROM "{table}"')
                for row in rows:
                    cols = list(row)
                    placeholders = ", ".join("?" for _ in cols)
                    col_list = ", ".join(f'"{c}"' for c in cols)
                    conn.execute(
                        f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})',
                        [row[c] for c in cols],
                    )
    finally:
        conn.close()


def export_to_file(db: Path, out: Path) -> None:
    out.write_text(json.dumps(export_db(db), indent=2))


def import_from_file(db: Path, src: Path) -> None:
    import_db(db, json.loads(src.read_text()))
