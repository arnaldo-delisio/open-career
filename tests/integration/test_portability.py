import sqlite3

import pytest

from adapters.storage.migrations import migrate
from adapters.storage.portability import export_to_file, import_db, import_from_file
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from domain.edges import CareerEdge


def test_export_import_roundtrip(tmp_path):
    src_db = tmp_path / "src.sqlite3"
    migrate(src_db)
    conn = sqlite3.connect(src_db)
    repo = SqliteCareerEdgeRepository(conn)
    repo.add(CareerEdge("evidence:1", "capability:python", "demonstrates", "fact", "user"))
    repo.add(CareerEdge("capability:python", "requirement:backend", "satisfies", "inference", "matcher"))
    original = repo.list_all()
    conn.close()

    dump_file = tmp_path / "dump.json"
    export_to_file(src_db, dump_file)

    dst_db = tmp_path / "dst.sqlite3"
    import_from_file(dst_db, dump_file)

    conn2 = sqlite3.connect(dst_db)
    restored = SqliteCareerEdgeRepository(conn2).list_all()
    conn2.close()
    assert restored == original


def test_export_requires_initialized_instance(tmp_path):
    with pytest.raises(ValueError, match="instance not initialized"):
        export_to_file(tmp_path / "missing.sqlite3", tmp_path / "dump.json")


def test_export_requires_current_schema_version(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    sqlite3.connect(db).close()  # exists, but no migrations applied
    with pytest.raises(ValueError, match="schema out of date"):
        export_to_file(db, tmp_path / "dump.json")


def test_every_successful_export_is_accepted_by_import(tmp_path):
    src_db = tmp_path / "src.sqlite3"
    migrate(src_db)  # fresh, empty instance: previously exported an import-rejected dump
    dump_file = tmp_path / "dump.json"
    export_to_file(src_db, dump_file)
    dst_db = tmp_path / "dst.sqlite3"
    import_from_file(dst_db, dump_file)  # must not raise


def test_import_rejects_empty_tables(tmp_path):
    db = tmp_path / "dst.sqlite3"
    with pytest.raises(ValueError, match="missing tables"):
        import_db(db, {"format": "open-career-export", "version": 1, "tables": {}})


def test_import_rejects_unsupported_version(tmp_path):
    db = tmp_path / "dst.sqlite3"
    with pytest.raises(ValueError, match="unsupported export version"):
        import_db(db, {"format": "open-career-export", "version": 99, "tables": {"career_edges": []}})


def test_import_rejects_unknown_table(tmp_path):
    db = tmp_path / "dst.sqlite3"
    dump = {
        "format": "open-career-export",
        "version": 1,
        "tables": {"career_edges": [], "surprise": []},
    }
    with pytest.raises(ValueError, match="unknown tables"):
        import_db(db, dump)


def test_import_missing_file_is_clean_error_and_exit_1(tmp_path, monkeypatch, capsys):
    with pytest.raises(ValueError, match="import file not found"):
        import_from_file(tmp_path / "db.sqlite3", tmp_path / "nope.json")
    # Through the CLI: one-line error, exit code 1.
    from apps.cli.main import main

    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        main(["import", str(tmp_path / "nope.json")])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("import failed: import file not found")
    assert "Traceback" not in err


def test_import_rejects_non_list_table_value(tmp_path):
    dump = {"format": "open-career-export", "version": 1, "tables": {"career_edges": "garbage"}}
    with pytest.raises(ValueError, match="must be a list of row objects"):
        import_db(tmp_path / "db.sqlite3", dump)


def test_import_rejects_unknown_column(tmp_path):
    dump = {
        "format": "open-career-export",
        "version": 1,
        "tables": {"career_edges": [{"source_id": "a", "surprise_col": 1}]},
    }
    with pytest.raises(ValueError, match=r"row 0: unknown columns \['surprise_col'\]"):
        import_db(tmp_path / "db.sqlite3", dump)


def _edge_row(**overrides):
    row = {
        "id": None,
        "source_id": "a",
        "target_id": "b",
        "edge_type": "demonstrates",
        "claim_kind": "fact",
        "source": "user",
        "created_at": "2026-08-10T00:00:00Z",
    }
    row.update(overrides)
    return row


def test_import_check_violation_is_clean_error(tmp_path):
    dump = {
        "format": "open-career-export",
        "version": 1,
        "tables": {"career_edges": [_edge_row(claim_kind="stated")]},
    }
    with pytest.raises(ValueError, match="loading table 'career_edges' failed: CHECK constraint"):
        import_db(tmp_path / "db.sqlite3", dump)


def test_failed_import_leaves_db_unchanged(tmp_path):
    db = tmp_path / "db.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    SqliteCareerEdgeRepository(conn).add(
        CareerEdge("keep:1", "keep:2", "demonstrates", "fact", "user")
    )
    conn.close()

    dump = {
        "format": "open-career-export",
        "version": 1,
        "tables": {
            "career_edges": [
                _edge_row(id=10),
                _edge_row(id=11, claim_kind="stated"),  # last row violates the CHECK
            ]
        },
    }
    with pytest.raises(ValueError, match="loading table 'career_edges' failed"):
        import_db(db, dump)

    conn = sqlite3.connect(db)
    rows = SqliteCareerEdgeRepository(conn).list_all()
    conn.close()
    assert len(rows) == 1
    assert rows[0].source_id == "keep:1"  # pre-import state intact, nothing deleted


def test_invalid_dump_leaves_legacy_db_untouched(tmp_path):
    """A structurally invalid dump is rejected before the database is touched:
    a legacy db with pending migrations stays byte-identical, no schema_migrations
    created, no backup written."""
    db = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("CREATE TABLE legacy_notes (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO legacy_notes (body) VALUES ('kept')")
    conn.close()
    before = db.read_bytes()

    with pytest.raises(ValueError, match="not an open-career export"):
        import_db(db, {"format": "something-else", "version": 1, "tables": {}})

    assert db.read_bytes() == before  # byte-identical
    assert not (tmp_path / "backups").exists()
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "schema_migrations" not in tables
    finally:
        conn.close()


def test_corrupt_target_db_is_clean_cli_error(tmp_path, monkeypatch, capsys):
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / "open-career.sqlite3").write_bytes(b"this is not a sqlite database")
    dump_file = tmp_path / "dump.json"
    dump_file.write_text('{"format": "open-career-export", "version": 1, "tables": {"career_edges": []}}')

    from apps.cli.main import main

    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    with pytest.raises(SystemExit) as exc:
        main(["import", str(dump_file)])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("import failed: cannot open instance database:")
    assert "Traceback" not in err


def test_non_object_top_level_is_clean_cli_error(tmp_path, monkeypatch, capsys):
    dump_file = tmp_path / "list.json"
    dump_file.write_text("[]")

    from apps.cli.main import main

    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        main(["import", str(dump_file)])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("import failed: not an open-career export")
    assert "Traceback" not in err


def test_import_into_populated_db_fully_replaces(tmp_path):
    src_db = tmp_path / "src.sqlite3"
    migrate(src_db)
    conn = sqlite3.connect(src_db)
    SqliteCareerEdgeRepository(conn).add(
        CareerEdge("evidence:1", "capability:python", "demonstrates", "fact", "user")
    )
    conn.close()
    dump_file = tmp_path / "dump.json"
    export_to_file(src_db, dump_file)

    dst_db = tmp_path / "dst.sqlite3"
    migrate(dst_db)
    conn = sqlite3.connect(dst_db)
    repo = SqliteCareerEdgeRepository(conn)
    repo.add(CareerEdge("old:1", "old:2", "stale", "inference", "old-run"))
    repo.add(CareerEdge("old:3", "old:4", "stale", "inference", "old-run"))
    conn.close()

    import_from_file(dst_db, dump_file)

    conn = sqlite3.connect(dst_db)
    restored = SqliteCareerEdgeRepository(conn).list_all()
    conn.close()
    assert len(restored) == 1
    assert restored[0].source_id == "evidence:1"
