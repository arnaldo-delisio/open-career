import sqlite3

import pytest

from adapters.storage.migrations import MIGRATIONS_DIR, _resolve_migrations_dir, migrate


EXPECTED_TABLES = {
    "career_edges", "experiences", "career_facts", "evidence", "capabilities",
    "role_families", "career_goals", "strategy_versions",
    "strategy_role_family_allocations", "user_profile", "profile_field_writes", "packages", "package_versions",
}

# The ledgered migration prefix this suite pins exactly, in order. Later
# migrations present in the tree (another workstream mid-flight) are
# tolerated after it, so the suite is green with or without them.
KNOWN_VERSIONS = ["0001", "0002", "0003", "0004", "0005"]


def _assert_known_prefix(applied, start="0001"):
    expected = KNOWN_VERSIONS[KNOWN_VERSIONS.index(start):]
    assert applied[:len(expected)] == expected
    tail = applied[len(expected):]
    assert tail == sorted(tail)
    assert all(version > KNOWN_VERSIONS[-1] for version in tail)


def test_fresh_init_applies_all_migrations(tmp_path):
    db = tmp_path / "test.sqlite3"
    applied = migrate(db)
    _assert_known_prefix(applied)
    conn = sqlite3.connect(db)
    try:
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
        _assert_known_prefix(versions)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert EXPECTED_TABLES <= tables
        assert "career_edges_0001" not in tables
        assert "_0002_conversion_check" not in tables
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_career_edges_active_unique", "idx_career_edges_active_by_target",
                "idx_career_edges_active_by_source", "idx_packages_one_base_per_family"} <= indexes
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path):
    db = tmp_path / "test.sqlite3"
    _assert_known_prefix(migrate(db))
    assert migrate(db) == []


def _only_0001_dir(tmp_path):
    d = tmp_path / "only-0001"
    d.mkdir()
    src = next(MIGRATIONS_DIR.glob("0001_*.sql"))
    (d / src.name).write_text(src.read_text())
    return d


def test_0002_converts_legacy_edges_preserving_data(tmp_path):
    """0001 -> 0002 on a database holding hand-imported edges: every row is
    carried into the new shape (edge_ ids, source -> provenance, endpoint types
    'unknown', created_by 'import', unverified), count-validated."""
    db = tmp_path / "legacy.sqlite3"
    assert migrate(db, migrations_dir=_only_0001_dir(tmp_path), backups_dir=tmp_path / "backups") == ["0001"]
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(
            "INSERT INTO career_edges (id, source_id, target_id, edge_type, claim_kind, source, created_at)"
            " VALUES (7, 'ev_a', 'cap_b', 'demonstrates', 'fact', 'hand-import', '2026-08-09T00:00:00Z')")
        conn.execute(
            "INSERT INTO career_edges (source_id, target_id, edge_type, claim_kind, source)"
            " VALUES ('cap_b', 'req_c', 'satisfies', 'inference', 'matcher-run-1')")
    conn.close()

    _assert_known_prefix(migrate(db, backups_dir=tmp_path / "backups"), start="0002")

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT id, source_type, source_id, edge_type, target_type, target_id,"
            " claim_kind, provenance, created_by, user_verified, created_at, superseded_at"
            " FROM career_edges ORDER BY id").fetchall()
        assert len(rows) == 2  # count preserved
        first = rows[0]
        assert first == ("edge_7", "unknown", "ev_a", "demonstrates", "unknown", "cap_b",
                         "fact", "hand-import", "import", 0, "2026-08-09T00:00:00Z", None)
        second = rows[1]
        assert second[0].startswith("edge_")
        assert (second[1], second[4]) == ("unknown", "unknown")
        assert second[6:10] == ("inference", "matcher-run-1", "import", 0)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "career_edges_0001" not in tables
    finally:
        conn.close()


def test_0002_active_edge_uniqueness_is_partial(tmp_path):
    """The unique index binds only active edges: a superseded copy may coexist
    with a new active edge on the same logical tuple."""
    db = tmp_path / "test.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    try:
        with conn:
            conn.execute("INSERT INTO evidence (id, evidence_type, title) VALUES ('ev_1', 'cv', 't')")
            conn.execute(
                "INSERT INTO capabilities (id, name, strength) VALUES ('cap_1', 'python', 'strong')")
            base = ("INSERT INTO career_edges (id, source_type, source_id, edge_type, target_type,"
                    " target_id, claim_kind, provenance, created_by, user_verified, superseded_at)"
                    " VALUES (?, 'evidence', 'ev_1', 'SUPPORTS', 'capability', 'cap_1',"
                    " 'fact', 'test', 'user', 1, ?)")
            conn.execute(base, ("edge_a", "2026-08-10T00:00:00Z"))  # superseded
            conn.execute(base, ("edge_b", None))  # active
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(base, ("edge_c", None))  # second active duplicate
    finally:
        conn.close()


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
            "INSERT INTO evidence (id, evidence_type, title) VALUES ('ev_1', 'cv', 'cv.txt')"
        )
    conn.close()

    extra_dir = tmp_path / "migrations"
    extra_dir.mkdir()
    for f in MIGRATIONS_DIR.glob("[0-9]*.sql"):
        (extra_dir / f.name).write_text(f.read_text())
    (extra_dir / "9999_noop.sql").write_text("CREATE TABLE noop_probe (id INTEGER PRIMARY KEY);")

    applied = migrate(db, migrations_dir=extra_dir, backups_dir=backups)
    assert applied == ["9999"]

    backup_files = list(backups.iterdir())
    assert len(backup_files) == 1
    # The backup is a valid SQLite database holding the pre-upgrade state.
    bconn = sqlite3.connect(backup_files[0])
    try:
        assert bconn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert bconn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
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
    _assert_known_prefix(migrate(db, backups_dir=backups))

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
