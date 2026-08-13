"""Portable export/import: JSON dump and load of the instance database, plus
the archive form (.zip) that bundles every referenced instance file so the
instance is genuinely one movable unit (OC-35). Plain-JSON export stays
available as the database-only form; its file-loss limitation is stated in
docs/data-model.md, never implied away."""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from adapters.storage.local import LocalStorageAdapter
from adapters.storage.migrations import MIGRATIONS_DIR, migrate, pending_migrations
from adapters.storage.sqlite_edges import ENTITY_TABLES
from domain.edges import EDGE_VOCABULARY, UNKNOWN_TYPE
from domain.policies import CANONICAL_POLICY_KEYS, validate_policy_value
from domain.profile import CANONICAL_PROFILE_FIELDS

EXPORT_VERSION = 1
_ARCHIVE_DUMP_NAME = "dump.json"
_ARCHIVE_FILES_PREFIX = "files/"


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


def _validate_dump_semantics(tables: dict) -> None:
    """Import is a validated restore, not a raw load: profile fields must be in
    the closed canonical set, and every active typed edge must satisfy the edge
    vocabulary with its endpoints present in the dump. 'unknown'-typed edges
    are exempt (the 0001-migration legacy lane, excluded from traversal)."""
    for i, row in enumerate(tables.get("user_profile", [])):
        try:
            fields = json.loads(row.get("fields_json") or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"user_profile row {i}: fields_json is not valid JSON: {e}") from e
        if not isinstance(fields, dict):
            raise ValueError(f"user_profile row {i}: fields_json must be a JSON object")
        unknown = set(fields) - CANONICAL_PROFILE_FIELDS
        if unknown:
            raise ValueError(f"user_profile row {i}: unknown profile fields {sorted(unknown)}")

    for i, row in enumerate(tables.get("user_policies", [])):
        try:
            policies = json.loads(row.get("policies_json") or "{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"user_policies row {i}: policies_json is not valid JSON: {e}") from e
        if not isinstance(policies, dict):
            raise ValueError(f"user_policies row {i}: policies_json must be a JSON object")
        unknown = set(policies) - CANONICAL_POLICY_KEYS
        if unknown:
            raise ValueError(f"user_policies row {i}: unknown policy keys {sorted(unknown)}")
        # Import is a validated restore: every policy value passes the same
        # per-key shape validation the write seam enforces (Codex round 1).
        for key, value in policies.items():
            try:
                validate_policy_value(key, value)
            except ValueError as e:
                raise ValueError(f"user_policies row {i}: {e}") from e

    dump_ids = {entity_type: {r.get("id") for r in tables.get(table, [])}
                for entity_type, table in ENTITY_TABLES.items()}
    for i, row in enumerate(tables.get("career_edges", [])):
        if row.get("superseded_at") is not None:
            continue
        source_type, target_type = row.get("source_type"), row.get("target_type")
        if UNKNOWN_TYPE in (source_type, target_type):
            continue
        edge_type = row.get("edge_type")
        expected = EDGE_VOCABULARY.get(edge_type)
        if expected is None:
            raise ValueError(f"career_edges row {i}: unknown edge_type '{edge_type}'")
        if (source_type, target_type) != expected:
            raise ValueError(
                f"career_edges row {i}: {edge_type} requires {expected[0]} -> {expected[1]},"
                f" got {source_type} -> {target_type}")
        for role, entity_type, entity_id in (("source", source_type, row.get("source_id")),
                                             ("target", target_type, row.get("target_id"))):
            if entity_id not in dump_ids[entity_type]:
                raise ValueError(
                    f"career_edges row {i}: {role} {entity_type} '{entity_id}' not in dump")


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
        conn.execute("PRAGMA foreign_keys = ON")
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
        _validate_dump_semantics(tables)
        current_table = None
        try:
            with conn:  # one transaction: a failure anywhere rolls the whole load back
                # Load order follows the dump, so FK checks defer to the end of
                # the transaction and are verified explicitly before commit.
                conn.execute("PRAGMA defer_foreign_keys = ON")
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
                violation = conn.execute("PRAGMA foreign_key_check").fetchone()
                if violation:
                    raise ValueError(
                        f"foreign key violation in table '{violation[0]}':"
                        f" a row references a missing {violation[2]} row")
        except sqlite3.Error as e:
            raise ValueError(f"loading table '{current_table}' failed: {e}") from e
    finally:
        conn.close()


def _referenced_files(tables: dict) -> dict[str, str | None]:
    """locator -> expected sha256 (None where the row records no hash), for
    every instance file the dump references: evidence locators, and package
    artifacts under instance/ (context snapshots and rendered PDFs, whose
    recorded hashes are sha256 of the stored bytes). Evidence locators that
    point outside the instance (URLs, absolute paths: repository/url/portfolio
    rows) are external references, not instance files; they travel as rows
    only."""
    refs: dict[str, str | None] = {}

    def add(locator, expected):
        if "://" in locator or locator.startswith("/"):
            return  # external reference; not an instance file
        current = refs.get(locator)
        if current is not None and expected is not None and current != expected:
            raise ValueError(
                f"instance file '{locator}' is referenced with two different hashes")
        refs[locator] = current if expected is None else expected

    for row in tables.get("evidence", []):
        if row.get("locator"):
            add(row["locator"], row.get("content_hash"))
    for row in tables.get("package_versions", []):
        if row.get("context_snapshot_locator"):
            add(row["context_snapshot_locator"], row.get("input_context_hash"))
        if row.get("artifact_locator"):
            add(row["artifact_locator"], row.get("artifact_hash"))
    # Gauntlet judge evidence (spec: decisions/gauntlet-design.md, principle
    # 6): every run's locator and hash pair travels and verifies exactly like
    # package artifacts; an archive that omitted judge evidence would claim
    # completeness it does not have.
    for row in tables.get("gauntlet_runs", []):
        for locator_col, hash_col in (
                ("policy_snapshot_locator", "policy_snapshot_hash"),
                ("prompt_inputs_locator", "prompt_inputs_hash"),
                ("raw_completions_locator", "raw_completions_hash")):
            if row.get(locator_col):
                add(row[locator_col], row.get(hash_col))
    return refs


def export_archive(db: Path, instance_root: Path, out: Path) -> None:
    """One movable unit: dump.json plus files/<locator> for every referenced
    instance file. A referenced file that is absent, or whose bytes no longer
    match the recorded hash, fails the export rather than shipping a bundle
    import would reject."""
    dump = export_db(db)
    storage = LocalStorageAdapter(instance_root)
    manifest: dict[str, bytes] = {}
    for locator, expected in sorted(_referenced_files(dump["tables"]).items()):
        if expected is None:
            raise ValueError(
                f"instance file '{locator}' is referenced by a row with no recorded"
                " content hash; a bundle must be hash-verifiable end to end")
        if not storage.exists(locator):
            raise ValueError(f"referenced instance file missing: {locator}")
        data = storage.read_bytes(locator)
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(
                f"instance file '{locator}' does not match its recorded hash;"
                " refusing to export a bundle import would reject")
        manifest[locator] = data
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(_ARCHIVE_DUMP_NAME, json.dumps(dump, indent=2))
        for locator, data in manifest.items():
            archive.writestr(f"{_ARCHIVE_FILES_PREFIX}{locator}", data)


def import_archive(db: Path, instance_root: Path, src: Path) -> None:
    """Restore an archive export. Order of proof before anything durable
    changes (Codex round 1): every bundled file hash-verified against the dump
    rows' recorded hashes, every locator contained inside the instance root,
    every destination installable (no directory squatting on a file path),
    and all bytes staged to a temp area; the database imports into a staged
    copy, files install journaled, and the staged database promotes last. A
    failure at any point rolls installed files back from the journal and
    leaves the original database untouched."""
    if not src.exists():
        raise ValueError(f"import file not found: {src}")
    try:
        with zipfile.ZipFile(src) as archive:
            names = set(archive.namelist())
            if _ARCHIVE_DUMP_NAME not in names:
                raise ValueError("archive carries no dump.json; not an open-career export")
            dump = json.loads(archive.read(_ARCHIVE_DUMP_NAME))
            if not isinstance(dump, dict) or not isinstance(dump.get("tables"), dict):
                raise ValueError("not an open-career export")
            refs = _referenced_files(dump["tables"])
            bundled = {n.removeprefix(_ARCHIVE_FILES_PREFIX)
                       for n in names if n.startswith(_ARCHIVE_FILES_PREFIX)}
            missing = set(refs) - bundled
            extra = bundled - set(refs)
            if missing:
                raise ValueError(f"archive is missing referenced files: {sorted(missing)}")
            if extra:
                raise ValueError(f"archive carries files the dump never references: {sorted(extra)}")
            files: dict[str, bytes] = {}
            for locator, expected in refs.items():
                data = archive.read(f"{_ARCHIVE_FILES_PREFIX}{locator}")
                if expected is None:
                    raise ValueError(
                        f"file '{locator}' is referenced by a row with no recorded"
                        " content hash; nothing imported")
                if hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError(
                        f"file '{locator}' does not match its recorded content hash;"
                        " nothing imported")
                files[locator] = data
    except zipfile.BadZipFile as e:
        raise ValueError(f"not a readable archive: {e}") from e
    except json.JSONDecodeError as e:
        raise ValueError(f"archive dump.json is not valid JSON: {e}") from e

    # Containment and installability, all read-only, before any write.
    root = instance_root.resolve()
    destinations: dict[str, Path] = {}
    seen_destinations: dict[Path, str] = {}
    for locator in files:
        dest = (root / locator).resolve()
        if not dest.is_relative_to(root):
            raise ValueError(
                f"file '{locator}' escapes the instance root; nothing imported")
        if dest in seen_destinations:
            # Aliasing has no legitimate use: locators come from evidence and
            # package rows, which never spell one file two ways (Codex round 3).
            raise ValueError(
                f"locators '{seen_destinations[dest]}' and '{locator}' resolve to"
                " the same destination; nothing imported")
        seen_destinations[dest] = locator
        if dest.is_dir():
            raise ValueError(
                f"file '{locator}' cannot be installed: a directory occupies its path;"
                " nothing imported")
        ancestor = dest.parent
        while not ancestor.exists() and ancestor != root:
            ancestor = ancestor.parent
        if ancestor.exists() and not ancestor.is_dir():
            raise ValueError(
                f"file '{locator}' cannot be installed: '{ancestor.name}' is a file"
                " where a directory is needed; nothing imported")
        destinations[locator] = dest

    # Recoverable two-phase restore (Codex round 2). Phase 1 imports into a
    # STAGED database copy, so the live database is untouched while files
    # install. Phase 2 installs archive files, journaling every destination it
    # replaces; a failure at any point rolls back every already-installed
    # destination from the journal. Phase 3, only after every file installed,
    # promotes the staged database in one os.replace; a promotion failure
    # rolls the files back too. Nothing durable changes unless everything did.
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".import-stage-", dir=root))
    try:
        staged: dict[str, Path] = {}
        for n, (locator, data) in enumerate(files.items()):
            staged_path = staging / f"f{n}"
            staged_path.write_bytes(data)
            staged[locator] = staged_path

        staged_db = staging / "staged.sqlite3"
        if db.exists():
            _copy_db(db, staged_db)  # SQLite backup API, never a file copy
        import_db(staged_db, dump)  # validated restore, all-or-nothing

        # Ordered undo log, replayed in strict reverse order on failure: each
        # entry is (destination, journaled backup or None). Undo removes the
        # installed destination, then restores that operation's backup, so
        # correctness never depends on the aliasing preflight above.
        undo: list[tuple[Path, Path | None]] = []
        journal_dir = staging / "journal"
        journal_dir.mkdir()
        try:
            for n, (locator, staged_path) in enumerate(staged.items()):
                dest = destinations[locator]
                dest.parent.mkdir(parents=True, exist_ok=True)
                backup_path: Path | None = None
                if dest.exists():
                    backup_path = journal_dir / f"j{n}"
                    os.replace(dest, backup_path)
                undo.append((dest, backup_path))
                os.replace(staged_path, dest)  # same filesystem: atomic per file
            os.replace(staged_db, db)  # promotion: the single durable db write
        except OSError as e:
            for dest, backup_path in reversed(undo):
                dest.unlink(missing_ok=True)
                if backup_path is not None:
                    os.replace(backup_path, dest)
            raise ValueError(
                f"restoring files failed ({e}); all changes rolled back") from e
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _copy_db(src: Path, dst: Path) -> None:
    """Copy a database with SQLite's own backup API (a file copy can silently
    omit rows living in the write-ahead log)."""
    src_conn = sqlite3.connect(src)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def export_to_file(db: Path, out: Path) -> None:
    """Plain JSON: database only (evidence locators travel, their files do
    not). The archive form (`.zip`) is the complete unit."""
    out.write_text(json.dumps(export_db(db), indent=2))


def import_from_file(db: Path, src: Path) -> None:
    if not src.exists():
        raise ValueError(f"import file not found: {src}")
    import_db(db, json.loads(src.read_text()))
