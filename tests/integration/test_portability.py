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
