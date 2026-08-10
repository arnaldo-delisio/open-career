import sqlite3

import pytest

from adapters.storage.migrations import MIGRATIONS_DIR, _resolve_migrations_dir, migrate


def test_migrate_applies_0001_and_records_version(tmp_path):
    db = tmp_path / "test.sqlite3"
    applied = migrate(db)
    assert applied == ["0001"]
    conn = sqlite3.connect(db)
    try:
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
        assert versions == ["0001"]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "career_edges" in tables
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path):
    db = tmp_path / "test.sqlite3"
    assert migrate(db) == ["0001"]
    assert migrate(db) == []


def test_no_backup_on_fresh_database(tmp_path):
    db = tmp_path / "test.sqlite3"
    migrate(db, backups_dir=tmp_path / "backups")
    assert not (tmp_path / "backups").exists()


def test_backup_taken_before_upgrading_existing_db(tmp_path):
    db = tmp_path / "test.sqlite3"
    backups = tmp_path / "backups"
    migrate(db, backups_dir=backups)

    # Add data, then a new pending migration, and migrate again.
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO career_edges (source_id, target_id, edge_type, claim_kind, source)"
            " VALUES ('a', 'b', 'demonstrates', 'fact', 'test')"
        )
    conn.close()

    extra_dir = tmp_path / "migrations"
    extra_dir.mkdir()
    for f in MIGRATIONS_DIR.glob("[0-9]*.sql"):
        (extra_dir / f.name).write_text(f.read_text())
    (extra_dir / "0002_noop.sql").write_text("CREATE TABLE noop_probe (id INTEGER PRIMARY KEY);")

    applied = migrate(db, migrations_dir=extra_dir, backups_dir=backups)
    assert applied == ["0002"]

    backup_files = list(backups.iterdir())
    assert len(backup_files) == 1
    # The backup is a valid SQLite database holding the pre-upgrade state.
    bconn = sqlite3.connect(backup_files[0])
    try:
        assert bconn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert bconn.execute("SELECT COUNT(*) FROM career_edges").fetchone()[0] == 1
        tables = {r[0] for r in bconn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "noop_probe" not in tables
    finally:
        bconn.close()


def test_failed_migration_is_atomic_and_retryable(tmp_path):
    """A migration whose second statement fails leaves no partial DDL and no
    version row; re-running after fixing the script succeeds."""
    db = tmp_path / "test.sqlite3"
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    bad = mdir / "0001_two_tables.sql"
    bad.write_text(
        "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n"  # duplicate: fails
    )
    with pytest.raises(sqlite3.OperationalError):
        migrate(db, migrations_dir=mdir, backups_dir=tmp_path / "backups")

    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "t1" not in tables  # no partial DDL
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
        assert versions == []  # no version row
    finally:
        conn.close()

    bad.write_text(
        "CREATE TABLE t1 (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE t2 (id INTEGER PRIMARY KEY);\n"
    )
    assert migrate(db, migrations_dir=mdir, backups_dir=tmp_path / "backups") == ["0001"]


def test_migration_with_own_transaction_control_is_rejected(tmp_path):
    db = tmp_path / "test.sqlite3"
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "0001_txn.sql").write_text("BEGIN; CREATE TABLE t1 (id INTEGER); COMMIT;")
    with pytest.raises(ValueError, match="transaction control"):
        migrate(db, migrations_dir=mdir, backups_dir=tmp_path / "backups")


def test_migration_with_end_is_rejected_and_applies_nothing(tmp_path):
    """END is a SQLite synonym for COMMIT; a migration using it would commit the
    runner-owned transaction early."""
    db = tmp_path / "test.sqlite3"
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "0001_end.sql").write_text("CREATE TABLE t1 (id INTEGER);\nEND;\nCREATE TABLE t2 (id INTEGER);")
    with pytest.raises(ValueError, match="transaction control"):
        migrate(db, migrations_dir=mdir, backups_dir=tmp_path / "backups")
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "t1" not in tables and "t2" not in tables
    finally:
        conn.close()


def test_duplicate_migration_versions_rejected_before_any_write(tmp_path):
    db = tmp_path / "test.sqlite3"
    mdir = tmp_path / "migrations"
    mdir.mkdir()
    (mdir / "0001_a.sql").write_text("CREATE TABLE ta (id INTEGER);")
    (mdir / "0001_b.sql").write_text("CREATE TABLE tb (id INTEGER);")
    backups = tmp_path / "backups"
    with pytest.raises(ValueError, match="duplicate migration version 0001"):
        migrate(db, migrations_dir=mdir, backups_dir=backups)
    assert not backups.exists()  # preflight fired before backup or write
    if db.exists():
        conn = sqlite3.connect(db)
        try:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            assert tables == []  # no migration body applied
        finally:
            conn.close()


def test_legacy_db_backup_is_a_pre_write_snapshot(tmp_path):
    """Migrating a legacy db (has data, no schema_migrations) produces a backup
    that does not contain a schema_migrations table."""
    db = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("CREATE TABLE legacy_notes (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO legacy_notes (body) VALUES ('kept')")
    conn.close()

    backups = tmp_path / "backups"
    assert migrate(db, backups_dir=backups) == ["0001"]

    backup_files = list(backups.iterdir())
    assert len(backup_files) == 1
    bconn = sqlite3.connect(backup_files[0])
    try:
        tables = {r[0] for r in bconn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "schema_migrations" not in tables
        assert "career_edges" not in tables
        assert bconn.execute("SELECT body FROM legacy_notes").fetchone() == ("kept",)
    finally:
        bconn.close()


def test_migrations_dir_resolution_prefers_repo_then_packaged(tmp_path):
    repo = tmp_path / "repo-migrations"
    packaged = tmp_path / "packaged-migrations"
    packaged.mkdir()
    (packaged / "0001_x.sql").write_text("CREATE TABLE x (id INTEGER);")
    # No repo checkout: falls back to the packaged copy, which holds the SQL files.
    resolved = _resolve_migrations_dir(repo, packaged)
    assert resolved == packaged
    assert list(resolved.glob("[0-9]*.sql"))
    # Repo checkout present: it wins.
    repo.mkdir()
    assert _resolve_migrations_dir(repo, packaged) == repo
