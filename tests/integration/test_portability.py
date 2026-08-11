import json
import sqlite3

import pytest

from adapters.storage.migrations import migrate
from adapters.storage.portability import export_db, export_to_file, import_db, import_from_file
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from domain.edges import CareerEdge

ALL_TABLES = {
    "career_edges", "experiences", "career_facts", "evidence", "capabilities",
    "role_families", "career_goals", "strategy_versions",
    "strategy_role_family_allocations", "user_profile", "profile_field_writes",
    "packages", "package_versions",
}


def _seed(conn: sqlite3.Connection) -> list[CareerEdge]:
    """Endpoints plus one valid edge, through the repository boundary."""
    with conn:
        conn.execute("INSERT INTO evidence (id, evidence_type, title) VALUES ('ev_1', 'cv', 'cv.txt')")
        conn.execute("INSERT INTO capabilities (id, name, strength) VALUES ('cap_1', 'python', 'strong')")
    repo = SqliteCareerEdgeRepository(conn)
    repo.add(CareerEdge(
        id="edge_1", source_type="evidence", source_id="ev_1", edge_type="SUPPORTS",
        target_type="capability", target_id="cap_1", claim_kind="fact",
        provenance="user", created_by="user", user_verified=1,
    ))
    return repo.list_all()


def _full_dump(**table_overrides) -> dict:
    tables = {t: [] for t in ALL_TABLES}
    tables.update(table_overrides)
    return {"format": "open-career-export", "version": 1, "tables": tables}


_ENDPOINT_ROWS = dict(
    evidence=[{"id": "ev_1", "evidence_type": "cv", "title": "t", "locator": None,
               "content_hash": None, "notes": None, "created_at": "2026-08-11T00:00:00Z"}],
    capabilities=[{"id": "cap_1", "name": "python", "strength": "strong", "description": None,
                   "last_assessed_at": None, "created_at": "2026-08-11T00:00:00Z",
                   "updated_at": None}],
)


def _edge_row(**overrides):
    row = {
        "id": "edge_x", "source_type": "evidence", "source_id": "ev_1",
        "edge_type": "SUPPORTS", "target_type": "capability", "target_id": "cap_1",
        "claim_kind": "fact", "confidence": None, "provenance": "user",
        "derived_from_fact_id": None, "created_by": "user", "user_verified": 1,
        "created_at": "2026-08-10T00:00:00Z", "superseded_at": None,
    }
    row.update(overrides)
    return row


def test_export_import_roundtrip(tmp_path):
    src_db = tmp_path / "src.sqlite3"
    migrate(src_db)
    conn = sqlite3.connect(src_db)
    original = _seed(conn)
    conn.close()

    dump_file = tmp_path / "dump.json"
    export_to_file(src_db, dump_file)

    dst_db = tmp_path / "dst.sqlite3"
    import_from_file(dst_db, dump_file)

    conn2 = sqlite3.connect(dst_db)
    restored = SqliteCareerEdgeRepository(conn2).list_all()
    evidence_count = conn2.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    conn2.close()
    assert restored == original
    assert evidence_count == 1


def test_export_covers_the_exact_current_table_set(tmp_path):
    db = tmp_path / "src.sqlite3"
    migrate(db)
    dump = export_db(db)
    assert set(dump["tables"]) == ALL_TABLES


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


def test_import_rejects_partial_table_set(tmp_path):
    """The exact-table-set validation covers the 0002 tables: a dump carrying
    only career_edges is rejected, naming what is missing."""
    db = tmp_path / "dst.sqlite3"
    dump = {"format": "open-career-export", "version": 1, "tables": {"career_edges": []}}
    with pytest.raises(ValueError, match="missing tables.*capabilities"):
        import_db(db, dump)


def test_import_rejects_unsupported_version(tmp_path):
    db = tmp_path / "dst.sqlite3"
    with pytest.raises(ValueError, match="unsupported export version"):
        import_db(db, {"format": "open-career-export", "version": 99, "tables": {"career_edges": []}})


def test_import_rejects_unknown_table(tmp_path):
    db = tmp_path / "dst.sqlite3"
    with pytest.raises(ValueError, match="unknown tables"):
        import_db(db, _full_dump(surprise=[]))


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
    with pytest.raises(ValueError, match="must be a list of row objects"):
        import_db(tmp_path / "db.sqlite3", _full_dump(career_edges="garbage"))


def test_import_rejects_unknown_column(tmp_path):
    dump = _full_dump(career_edges=[{"source_id": "a", "surprise_col": 1}])
    with pytest.raises(ValueError, match=r"row 0: unknown columns \['surprise_col'\]"):
        import_db(tmp_path / "db.sqlite3", dump)


def test_import_check_violation_is_clean_error(tmp_path):
    dump = _full_dump(career_edges=[_edge_row(claim_kind="stated")], **_ENDPOINT_ROWS)
    with pytest.raises(ValueError, match="loading table 'career_edges' failed: CHECK constraint"):
        import_db(tmp_path / "db.sqlite3", dump)


def test_failed_import_leaves_db_unchanged(tmp_path):
    db = tmp_path / "db.sqlite3"
    migrate(db)
    conn = sqlite3.connect(db)
    _seed(conn)
    conn.close()

    dump = _full_dump(career_edges=[
        _edge_row(id="edge_10"),
        _edge_row(id="edge_11", claim_kind="stated"),  # last row violates the CHECK
    ], **_ENDPOINT_ROWS)
    with pytest.raises(ValueError, match="loading table 'career_edges' failed"):
        import_db(db, dump)

    conn = sqlite3.connect(db)
    rows = SqliteCareerEdgeRepository(conn).list_all()
    conn.close()
    assert len(rows) == 1
    assert rows[0].id == "edge_1"  # pre-import state intact, nothing deleted


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
    dump_file.write_text(json.dumps(_full_dump()))

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


def test_import_rejects_unknown_profile_field(tmp_path):
    """Import is a validated restore: a profile document carrying a field
    outside the closed canonical set (OC-29) is rejected."""
    dump = _full_dump(user_profile=[
        {"id": 1, "fields_json": json.dumps({"full_name": "Jane", "favourite_color": "blue"}),
         "updated_at": "2026-08-11T00:00:00Z"}])
    with pytest.raises(ValueError, match=r"unknown profile fields \['favourite_color'\]"):
        import_db(tmp_path / "db.sqlite3", dump)


def test_import_rejects_bogus_edge_type(tmp_path):
    dump = _full_dump(career_edges=[_edge_row(edge_type="FABRICATES")], **_ENDPOINT_ROWS)
    with pytest.raises(ValueError, match="unknown edge_type 'FABRICATES'"):
        import_db(tmp_path / "db.sqlite3", dump)


def test_import_rejects_edge_with_endpoint_missing_from_dump(tmp_path):
    dump = _full_dump(career_edges=[_edge_row()])  # ev_1/cap_1 rows absent
    with pytest.raises(ValueError, match="source evidence 'ev_1' not in dump"):
        import_db(tmp_path / "db.sqlite3", dump)


def test_import_exempts_unknown_typed_legacy_edges(tmp_path):
    """'unknown'-typed rows are the 0001-migration legacy lane: they load even
    though their endpoints are untyped ids with no entity rows."""
    dump = _full_dump(career_edges=[_edge_row(
        source_type="unknown", target_type="unknown", edge_type="demonstrates",
        created_by="import", user_verified=0)])
    db = tmp_path / "db.sqlite3"
    import_db(db, dump)  # must not raise
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM career_edges").fetchone()[0] == 1
    conn.close()


def test_import_rejects_dangling_experience_fk(tmp_path):
    """Foreign keys are checked before commit: a career_facts row referencing a
    missing experience is a one-line error, nothing loaded."""
    dump = _full_dump(career_facts=[{
        "id": "fact_1", "experience_id": "exp_missing", "fact_type": "achievement",
        "statement": "s", "status": "active", "confidence": None, "source": "import",
        "source_location": None, "user_approved": 1,
        "created_at": "2026-08-11T00:00:00Z", "verified_at": None}])
    db = tmp_path / "db.sqlite3"
    with pytest.raises(ValueError, match="foreign key violation in table 'career_facts'"):
        import_db(db, dump)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM career_facts").fetchone()[0] == 0
    conn.close()


def test_import_into_populated_db_fully_replaces(tmp_path):
    src_db = tmp_path / "src.sqlite3"
    migrate(src_db)
    conn = sqlite3.connect(src_db)
    _seed(conn)
    conn.close()
    dump_file = tmp_path / "dump.json"
    export_to_file(src_db, dump_file)

    dst_db = tmp_path / "dst.sqlite3"
    migrate(dst_db)
    conn = sqlite3.connect(dst_db)
    with conn:
        conn.execute("INSERT INTO evidence (id, evidence_type, title) VALUES ('ev_old', 'cv', 'old.txt')")
        conn.execute("INSERT INTO career_goals (id, statement, horizon) VALUES ('goal_old', 'stale', 'near')")
    conn.close()

    import_from_file(dst_db, dump_file)

    conn = sqlite3.connect(dst_db)
    evidence_ids = [r[0] for r in conn.execute("SELECT id FROM evidence")]
    goal_count = conn.execute("SELECT COUNT(*) FROM career_goals").fetchone()[0]
    conn.close()
    assert evidence_ids == ["ev_1"]  # old contents fully replaced
    assert goal_count == 0
