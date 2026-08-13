"""Hand-rolled migration runner: numbered SQL files applied in order, versions
tracked in schema_migrations. Before writing anything to an existing database it
takes a backup via SQLite's own backup API (never a file copy: rows can live in
the WAL and a copy silently omits them). Each migration and its version row are
applied in one transaction the runner controls, so a mid-migration failure
leaves no partial DDL and no version row."""

import re
import sqlite3
import time
from pathlib import Path

_REPO_ROOT_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
_PACKAGED_MIGRATIONS = Path(__file__).resolve().parent / "_migrations"

# Migration files may not carry their own transaction control: the runner owns it.
_TXN_CONTROL = re.compile(r"\b(BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", re.IGNORECASE)

# Matches, in order of first alternative that fits: a single- or double-quoted string
# literal (kept verbatim, including any '--' or '/*' inside it), a line comment, or a
# block comment. Applied left to right so quoting always wins over comment markers.
_SQL_COMMENT_OR_STRING = re.compile(
    r"'(?:[^']|'')*'"       # single-quoted string, with '' as an escaped quote
    r'|"(?:[^"]|"")*"'      # double-quoted identifier/string, same escaping
    r"|--[^\n]*"            # line comment to end of line
    r"|/\*.*?\*/",          # block comment, non-greedy
    re.DOTALL,
)


def _strip_sql_comments(sql: str) -> str:
    """Remove line and block comments while leaving string literals untouched, so a
    comment mentioning transaction words (or a stray '--' inside quoted data) never
    reads as SQL to the transaction-control guard."""
    def replace(match: re.Match) -> str:
        text = match.group(0)
        if text.startswith("--") or text.startswith("/*"):
            return ""
        return text

    return _SQL_COMMENT_OR_STRING.sub(replace, sql)


def _resolve_migrations_dir(repo_candidate: Path = _REPO_ROOT_MIGRATIONS,
                            packaged_candidate: Path = _PACKAGED_MIGRATIONS) -> Path:
    """Repo-root migrations/ when running from a checkout; the copy force-included
    into the wheel when installed as a package."""
    return repo_candidate if repo_candidate.is_dir() else packaged_candidate


MIGRATIONS_DIR = _resolve_migrations_dir()


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db, isolation_level=None)  # autocommit: the runner issues BEGIN/COMMIT itself
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def applied_versions(conn: sqlite3.Connection) -> list[str]:
    """Read-only: versions already applied ([] when schema_migrations does not exist)."""
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if not exists:
        return []
    return [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def _migration_files(migrations_dir: Path) -> list[Path]:
    """All migration files, preflighted: duplicate version stems are rejected
    before any backup or write happens."""
    files = sorted(migrations_dir.glob("[0-9]*.sql"))
    seen: dict[str, str] = {}
    for f in files:
        version = f.stem.split("_")[0]
        if version in seen:
            raise ValueError(f"duplicate migration version {version}: {seen[version]} and {f.name}")
        seen[version] = f.name
    return files


def pending_migrations(conn: sqlite3.Connection, migrations_dir: Path) -> list[Path]:
    applied = set(applied_versions(conn))
    return [f for f in _migration_files(migrations_dir) if f.stem.split("_")[0] not in applied]


def backup(db: Path, backups_dir: Path) -> Path:
    """Back up an existing database using the SQLite backup API."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    dest = backups_dir / f"pre-migrate-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{db.name}"
    src = sqlite3.connect(db)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def _apply_one(conn: sqlite3.Connection, migration: Path) -> str:
    version = migration.stem.split("_")[0]
    if not version.isdigit():
        raise ValueError(f"migration file name must start with a numeric version: {migration.name}")
    body = migration.read_text()
    if _TXN_CONTROL.search(_strip_sql_comments(body)):
        raise ValueError(
            f"migration {migration.name} contains its own transaction control; the runner owns transactions"
        )
    script = (
        "BEGIN;\n"
        f"{body}\n"
        f"INSERT INTO schema_migrations (version) VALUES ('{version}');\n"
        "COMMIT;"
    )
    try:
        conn.executescript(script)
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return version


def migrate(db: Path, migrations_dir: Path = MIGRATIONS_DIR, backups_dir: Path | None = None) -> list[str]:
    """Apply pending migrations in order; returns the versions applied.

    If the database file already exists and there is work to do, a backup is
    taken before any write (backups_dir defaults to <db dir>/backups)."""
    db.parent.mkdir(parents=True, exist_ok=True)
    existed = db.exists()
    conn = _connect(db)
    try:
        pending = pending_migrations(conn, migrations_dir)  # read-only check
        if not pending:
            return []
        if existed:
            backup(db, backups_dir or db.parent / "backups")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY,"
            " applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))"
        )
        return [_apply_one(conn, f) for f in pending]
    finally:
        conn.close()
