"""Archive export/import (.zip): the instance as one movable unit, every
bundled file hash-verified against its row's recorded hash (OC-35)."""

import hashlib
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

from adapters.storage.migrations import migrate
from adapters.storage.portability import export_archive, import_archive
from adapters.storage.sqlite_entities import SqliteEvidenceRepository
from domain.entities import Evidence


def _instance(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    migrate(root / "open-career.sqlite3")
    return root


def _seed_evidence_file(root, body=b"story body\n", with_hash=True,
                        evidence_id="ev_1", locator="files/stories/ev_1.md"):
    (root / locator).parent.mkdir(parents=True, exist_ok=True)
    (root / locator).write_bytes(body)
    conn = sqlite3.connect(root / "open-career.sqlite3")
    SqliteEvidenceRepository(conn).add(Evidence(
        id=evidence_id, evidence_type="user_statement", title=f"story: {evidence_id}",
        locator=locator,
        content_hash=hashlib.sha256(body).hexdigest() if with_hash else None))
    conn.close()
    return locator


def test_archive_roundtrip_restores_files_hash_verified(tmp_path):
    src = _instance(tmp_path, "src")
    locator = _seed_evidence_file(src)
    out = tmp_path / "bundle.zip"
    export_archive(src / "open-career.sqlite3", src, out)

    with zipfile.ZipFile(out) as archive:
        assert set(archive.namelist()) == {"dump.json", f"files/{locator}"}

    dst = _instance(tmp_path, "dst")
    import_archive(dst / "open-career.sqlite3", dst, out)
    assert (dst / locator).read_bytes() == b"story body\n"
    conn = sqlite3.connect(dst / "open-career.sqlite3")
    rows = SqliteEvidenceRepository(conn).list_all()
    conn.close()
    assert [r.locator for r in rows] == [locator]


def test_export_fails_when_a_referenced_file_is_missing(tmp_path):
    src = _instance(tmp_path, "src")
    locator = _seed_evidence_file(src)
    (src / locator).unlink()
    with pytest.raises(ValueError, match=f"referenced instance file missing: {locator}"):
        export_archive(src / "open-career.sqlite3", src, tmp_path / "bundle.zip")


def test_export_fails_when_a_file_no_longer_matches_its_hash(tmp_path):
    src = _instance(tmp_path, "src")
    locator = _seed_evidence_file(src)
    (src / locator).write_bytes(b"silently changed\n")
    with pytest.raises(ValueError, match="does not match its recorded hash"):
        export_archive(src / "open-career.sqlite3", src, tmp_path / "bundle.zip")


def _tamper(archive_path, member, data):
    with zipfile.ZipFile(archive_path) as archive:
        contents = {n: archive.read(n) for n in archive.namelist()}
    contents[member] = data
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, body in contents.items():
            archive.writestr(name, body)


def test_tampered_file_fails_import_atomically(tmp_path):
    src = _instance(tmp_path, "src")
    locator = _seed_evidence_file(src)
    out = tmp_path / "bundle.zip"
    export_archive(src / "open-career.sqlite3", src, out)
    _tamper(out, f"files/{locator}", b"tampered body\n")

    dst = _instance(tmp_path, "dst")
    with pytest.raises(ValueError, match="does not match its recorded content hash"):
        import_archive(dst / "open-career.sqlite3", dst, out)
    # Atomic: no file written, no rows loaded.
    assert not (dst / locator).exists()
    conn = sqlite3.connect(dst / "open-career.sqlite3")
    assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    conn.close()


def test_archive_missing_a_referenced_file_is_rejected(tmp_path):
    src = _instance(tmp_path, "src")
    locator = _seed_evidence_file(src)
    out = tmp_path / "bundle.zip"
    export_archive(src / "open-career.sqlite3", src, out)
    with zipfile.ZipFile(out) as archive:
        dump = archive.read("dump.json")
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("dump.json", dump)  # files/ dropped
    dst = _instance(tmp_path, "dst")
    with pytest.raises(ValueError, match="missing referenced files"):
        import_archive(dst / "open-career.sqlite3", dst, out)


def test_unreferenced_archive_file_is_rejected(tmp_path):
    src = _instance(tmp_path, "src")
    _seed_evidence_file(src)
    out = tmp_path / "bundle.zip"
    export_archive(src / "open-career.sqlite3", src, out)
    with zipfile.ZipFile(out, "a") as archive:
        archive.writestr("files/../../escape.md", b"nope")
    dst = _instance(tmp_path, "dst")
    with pytest.raises(ValueError, match="never references"):
        import_archive(dst / "open-career.sqlite3", dst, out)


def test_export_rejects_a_referenced_file_with_no_recorded_hash(tmp_path):
    """A bundle must be hash-verifiable end to end: a row referencing an
    instance file without a content hash fails the export, naming the locator
    (Codex round 1)."""
    src = _instance(tmp_path, "src")
    locator = _seed_evidence_file(src, with_hash=False)
    with pytest.raises(ValueError, match=f"'{locator}' is referenced by a row with no recorded"):
        export_archive(src / "open-career.sqlite3", src, tmp_path / "bundle.zip")


def test_import_rejects_a_dump_whose_hash_was_nulled(tmp_path):
    """Import re-checks the same invariant: nulling content_hash in dump.json
    cannot smuggle an unverifiable file past the restore."""
    src = _instance(tmp_path, "src")
    locator = _seed_evidence_file(src)
    out = tmp_path / "bundle.zip"
    export_archive(src / "open-career.sqlite3", src, out)
    with zipfile.ZipFile(out) as archive:
        dump = json.loads(archive.read("dump.json"))
    dump["tables"]["evidence"][0]["content_hash"] = None
    _tamper(out, "dump.json", json.dumps(dump).encode())
    dst = _instance(tmp_path, "dst")
    with pytest.raises(ValueError, match="no recorded content hash"):
        import_archive(dst / "open-career.sqlite3", dst, out)
    assert not (dst / locator).exists()


def test_traversal_locator_is_rejected_with_db_untouched(tmp_path):
    """A locator escaping the instance root (files/../../evil.md) is rejected
    before the database or any file is written (Codex round 1)."""
    src = _instance(tmp_path, "src")
    locator = _seed_evidence_file(src)
    out = tmp_path / "bundle.zip"
    export_archive(src / "open-career.sqlite3", src, out)
    evil = "files/../../evil.md"
    with zipfile.ZipFile(out) as archive:
        dump = json.loads(archive.read("dump.json"))
        body = archive.read(f"files/{locator}")
    dump["tables"]["evidence"][0]["locator"] = evil
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("dump.json", json.dumps(dump))
        archive.writestr(f"files/{evil}", body)

    dst = _instance(tmp_path, "dst")
    with pytest.raises(ValueError, match="escapes the instance root"):
        import_archive(dst / "open-career.sqlite3", dst, out)
    conn = sqlite3.connect(dst / "open-career.sqlite3")
    assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    conn.close()
    assert not (tmp_path / "evil.md").exists()
    assert not (dst / "evil.md").exists()


def test_uninstallable_destination_leaves_db_and_files_untouched(tmp_path):
    """Installability is proven before the database import: a directory
    squatting on a destination path fails the whole restore with nothing
    changed (Codex round 1: the db must never be replaced first)."""
    src = _instance(tmp_path, "src")
    locator = _seed_evidence_file(src)
    out = tmp_path / "bundle.zip"
    export_archive(src / "open-career.sqlite3", src, out)

    dst = _instance(tmp_path, "dst")
    (dst / locator).mkdir(parents=True)  # a directory where the file must land
    with pytest.raises(ValueError, match="a directory occupies its path"):
        import_archive(dst / "open-career.sqlite3", dst, out)
    conn = sqlite3.connect(dst / "open-career.sqlite3")
    assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
    conn.close()
    assert (dst / locator).is_dir()  # untouched
    assert not [p for p in dst.iterdir() if p.name.startswith(".import-stage-")]


def test_external_locators_travel_as_rows_only(tmp_path):
    """URL and absolute-path locators (repository/url evidence) are external
    references, never bundled; the archive still round-trips (regression:
    first live drive failed export on a GitHub URL locator)."""
    src = _instance(tmp_path, "src")
    conn = sqlite3.connect(src / "open-career.sqlite3")
    repo = SqliteEvidenceRepository(conn)
    repo.add(Evidence(id="ev_url", evidence_type="repository", title="demo",
                      locator="https://github.com/example/demo"))
    repo.add(Evidence(id="ev_abs", evidence_type="document", title="ext",
                      locator="/somewhere/outside.pdf"))
    conn.close()
    out = tmp_path / "bundle.zip"
    export_archive(src / "open-career.sqlite3", src, out)
    with zipfile.ZipFile(out) as archive:
        assert archive.namelist() == ["dump.json"]
    dst = _instance(tmp_path, "dst")
    import_archive(dst / "open-career.sqlite3", dst, out)
    conn = sqlite3.connect(dst / "open-career.sqlite3")
    locators = {r.locator for r in SqliteEvidenceRepository(conn).list_all()}
    conn.close()
    assert locators == {"https://github.com/example/demo", "/somewhere/outside.pdf"}


def test_partial_install_failure_rolls_everything_back(tmp_path):
    """Two-phase restore (Codex round 2): a failure installing the SECOND of
    two files restores the first destination from the journal and leaves the
    original database byte-identical; no staging residue."""
    src = _instance(tmp_path, "src")
    locator_a = _seed_evidence_file(src, body=b"new a\n",
                                    evidence_id="ev_1", locator="files/stories/a.md")
    _seed_evidence_file(src, body=b"new b\n",
                        evidence_id="ev_2", locator="files/stories/sub/b.md")
    out = tmp_path / "bundle.zip"
    export_archive(src / "open-career.sqlite3", src, out)

    dst = _instance(tmp_path, "dst")
    (dst / locator_a).parent.mkdir(parents=True)
    (dst / locator_a).write_bytes(b"old a\n")   # a destination being replaced
    blocked = dst / "files" / "stories" / "sub"
    blocked.mkdir()
    blocked.chmod(0o500)                        # second install must fail
    db_before = (dst / "open-career.sqlite3").read_bytes()
    try:
        with pytest.raises(ValueError, match="all changes rolled back"):
            import_archive(dst / "open-career.sqlite3", dst, out)
    finally:
        blocked.chmod(0o700)
    assert (dst / "open-career.sqlite3").read_bytes() == db_before  # byte-identical
    assert (dst / locator_a).read_bytes() == b"old a\n"             # journal restored it
    assert not (blocked / "b.md").exists()
    assert not [p for p in dst.iterdir() if p.name.startswith(".import-stage-")]


def test_aliased_destinations_are_rejected_at_preflight(tmp_path):
    """Two locators resolving to one destination (files/a.md vs
    files/sub/../a.md) would corrupt rollback ordering; the manifest is
    rejected before any write, naming both locators (Codex round 3)."""
    src = _instance(tmp_path, "src")
    _seed_evidence_file(src, body=b"new a\n",
                        evidence_id="ev_1", locator="files/a.md")
    out = tmp_path / "bundle.zip"
    export_archive(src / "open-career.sqlite3", src, out)
    alias = "files/sub/../a.md"
    with zipfile.ZipFile(out) as archive:
        dump = json.loads(archive.read("dump.json"))
        body = archive.read("files/files/a.md")
    dump["tables"]["evidence"].append({
        **dump["tables"]["evidence"][0], "id": "ev_2", "locator": alias})
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("dump.json", json.dumps(dump))
        archive.writestr("files/files/a.md", body)
        archive.writestr(f"files/{alias}", body)

    dst = _instance(tmp_path, "dst")
    db_before = (dst / "open-career.sqlite3").read_bytes()
    with pytest.raises(ValueError, match=r"'files/a\.md' and 'files/sub/\.\./a\.md'"
                                         r" resolve to the same destination"):
        import_archive(dst / "open-career.sqlite3", dst, out)
    assert (dst / "open-career.sqlite3").read_bytes() == db_before
    assert not (dst / "files").exists()


def test_db_promotion_failure_rolls_back_every_installed_file(tmp_path, monkeypatch):
    """A failure at the very last step (promoting the staged database) still
    rolls back every already-installed file via the reverse undo log
    (Codex round 3)."""
    src = _instance(tmp_path, "src")
    locator_a = _seed_evidence_file(src, body=b"new a\n",
                                    evidence_id="ev_1", locator="files/stories/a.md")
    locator_b = _seed_evidence_file(src, body=b"new b\n",
                                    evidence_id="ev_2", locator="files/stories/b.md")
    out = tmp_path / "bundle.zip"
    export_archive(src / "open-career.sqlite3", src, out)

    dst = _instance(tmp_path, "dst")
    (dst / locator_a).parent.mkdir(parents=True)
    (dst / locator_a).write_bytes(b"old a\n")   # replaced, then restored
    db_before = (dst / "open-career.sqlite3").read_bytes()

    real_replace = os.replace

    def failing_promotion(src_path, dst_path):
        if Path(dst_path).name == "open-career.sqlite3":
            raise OSError("promotion blocked")
        return real_replace(src_path, dst_path)

    monkeypatch.setattr("adapters.storage.portability.os.replace", failing_promotion)
    with pytest.raises(ValueError, match="all changes rolled back"):
        import_archive(dst / "open-career.sqlite3", dst, out)
    assert (dst / "open-career.sqlite3").read_bytes() == db_before
    assert (dst / locator_a).read_bytes() == b"old a\n"  # journal restored it
    assert not (dst / locator_b).exists()                # created file removed
    assert not [p for p in dst.iterdir() if p.name.startswith(".import-stage-")]


def test_not_a_zip_is_a_clean_error(tmp_path):
    bad = tmp_path / "bundle.zip"
    bad.write_bytes(b"not a zip")
    dst = _instance(tmp_path, "dst")
    with pytest.raises(ValueError, match="not a readable archive"):
        import_archive(dst / "open-career.sqlite3", dst, bad)


def test_plain_json_export_stays_db_only(tmp_path):
    """The stated limitation: JSON export carries rows (locators travel), never
    files; the archive form is the complete unit."""
    from adapters.storage.portability import export_db

    src = _instance(tmp_path, "src")
    locator = _seed_evidence_file(src)
    dump = export_db(src / "open-career.sqlite3")
    assert dump["tables"]["evidence"][0]["locator"] == locator
    assert "files" not in dump  # rows only
