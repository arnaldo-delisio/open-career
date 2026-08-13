"""Registry bootstrap scripts (OC-37 §2): CDX slug extraction with dedupe
across snapshots, candidate insertion with dry-run, and the curated-layer
loader (origin 'curated', never auto-pruned, reviewed metadata with
provenance). No network anywhere."""

import sqlite3

import pytest

from adapters.storage.migrations import migrate
from adapters.storage.sqlite_discovery import SqliteSourceRegistryRepository
from domain.discovery import Source
from domain.ids import new_id
from scripts.harvest_sources import URL_PATTERNS, extract_slugs, insert_candidates
from scripts.load_curated_sources import (
    CuratedFileError,
    merge_curated,
    parse_curated_yaml,
)


@pytest.fixture
def conn(tmp_path):
    migrate(tmp_path / "db.sqlite3")
    connection = sqlite3.connect(tmp_path / "db.sqlite3")
    connection.execute("PRAGMA foreign_keys = ON")
    yield connection
    connection.close()


# ------------------------------------------------------------------- harvest

def test_extract_slugs_covers_all_five_patterns():
    urls = [
        "https://boards.greenhouse.io/acme",
        "https://boards.greenhouse.io/acme/jobs/123",  # deeper path, same slug
        "http://boards.greenhouse.io/Widget?t=1",
        "https://jobs.lever.co/leverco/openings",
        "https://jobs.ashbyhq.com/Some.Client",
        "https://apply.workable.com/madrid-startup/",
        "https://careers.smartrecruiters.com/BigCo/some-job",
        "https://example.com/boards.greenhouse.io/fake",  # wrong host
    ]
    assert extract_slugs(urls, "greenhouse") == {"acme", "widget"}
    assert extract_slugs(urls, "lever") == {"leverco"}
    assert extract_slugs(urls, "ashby") == {"some.client"}
    assert extract_slugs(urls, "workable") == {"madrid-startup"}
    assert extract_slugs(urls, "smartrecruiters") == {"bigco"}
    assert set(URL_PATTERNS) == {"greenhouse", "lever", "ashby", "workable",
                                 "smartrecruiters"}


def test_greenhouse_harvests_both_board_hosts_into_one_ats_type():
    """Greenhouse migrated boards.greenhouse.io -> job-boards.greenhouse.io
    (design §2 amendment 2026-08-13). Both hosts are harvested, the blocklist
    applies to both, and a tenant on both collapses to one slug."""
    hosts = [host for host, _ in URL_PATTERNS["greenhouse"]]
    assert hosts == ["boards.greenhouse.io/*", "job-boards.greenhouse.io/*"]
    urls = [
        "https://boards.greenhouse.io/onboth",
        "https://job-boards.greenhouse.io/onboth",
        "https://job-boards.greenhouse.io/OnBoth/jobs/99",
        "https://job-boards.greenhouse.io/newonly?gh_src=x",
        "https://job-boards.greenhouse.io/embed/job_board?for=onboth",
        "https://job-boards.greenhouse.io.evil.com/fake",
    ]
    assert extract_slugs(urls, "greenhouse") == {"onboth", "newonly"}


def test_both_greenhouse_hosts_dedupe_to_one_registry_row(conn):
    """Dedup is the ats_type/slug key, so the same tenant seen on the legacy
    and the current host inserts once and re-runs as already-present."""
    slugs = extract_slugs(["https://boards.greenhouse.io/onboth",
                           "https://job-boards.greenhouse.io/onboth"], "greenhouse")
    inserted, skipped = insert_candidates(conn, {"greenhouse": slugs}, dry_run=False)
    assert (inserted, skipped) == (1, 0)
    rows = SqliteSourceRegistryRepository(conn).list_all()
    assert [(s.ats_type, s.tenant_slug) for s in rows] == [("greenhouse", "onboth")]
    assert insert_candidates(conn, {"greenhouse": slugs}, dry_run=False) == (0, 1)


def test_extract_slugs_dedupes_across_snapshots_and_skips_vendor_paths():
    urls = ["https://boards.greenhouse.io/acme"] * 50 + [
        "https://boards.greenhouse.io/embed/job_board?for=acme",
        "https://boards.greenhouse.io/api/something",
    ]
    assert extract_slugs(urls, "greenhouse") == {"acme"}


def test_insert_candidates_writes_harvest_rows_and_dry_run_writes_nothing(conn, capsys):
    slugs = {"greenhouse": {"acme"}, "lever": {"leverco"}}
    inserted, skipped = insert_candidates(conn, slugs, dry_run=True)
    assert (inserted, skipped) == (2, 0)
    assert "would insert" in capsys.readouterr().out
    assert SqliteSourceRegistryRepository(conn).list_all() == []

    inserted, skipped = insert_candidates(conn, slugs, dry_run=False)
    assert (inserted, skipped) == (2, 0)
    sources = SqliteSourceRegistryRepository(conn).list_all()
    assert {(s.ats_type, s.tenant_slug) for s in sources} == {
        ("greenhouse", "acme"), ("lever", "leverco")}
    assert all(s.origin == "harvest" and s.status == "candidate" for s in sources)

    inserted, skipped = insert_candidates(conn, slugs, dry_run=False)
    assert (inserted, skipped) == (0, 2)  # re-runnable, idempotent


# ------------------------------------------------------------------- curated

CURATED = """\
# Italy/EU-remote curated layer
- ats_type: greenhouse
  tenant_slug: acme
  company_name: Acme S.r.l.
  industry: fintech
  company_stage: series_b
- ats_type: workable
  tenant_slug: torino-tools
"""


def test_parse_curated_yaml_happy_path():
    entries = parse_curated_yaml(CURATED)
    assert entries[0]["company_name"] == "Acme S.r.l."
    assert entries[0]["industry"] == "fintech"
    assert entries[1] == {"ats_type": "workable", "tenant_slug": "torino-tools"}


@pytest.mark.parametrize("bad", [
    "- ats_type: greenhouse\n  unknown_key: x\n  tenant_slug: a",
    "- ats_type: notanats\n  tenant_slug: a",
    "- ats_type: greenhouse",  # missing tenant_slug
    "stray_line: value",  # not a list item
    "- ats_type: greenhouse\n  ats_type: lever\n  tenant_slug: a",  # duplicate
])
def test_parse_curated_yaml_rejects_out_of_subset_input(bad):
    with pytest.raises(CuratedFileError):
        parse_curated_yaml(bad)


def test_merge_curated_adds_upgrades_and_lands_metadata_with_provenance(conn):
    registry = SqliteSourceRegistryRepository(conn)
    registry.add(Source(id=new_id("src"), ats_type="greenhouse",
                        tenant_slug="acme", origin="harvest"))
    added, updated = merge_curated(conn, parse_curated_yaml(CURATED), dry_run=False)
    assert (added, updated) == (1, 1)
    by_key = {(s.ats_type, s.tenant_slug): s for s in registry.list_all()}
    upgraded = by_key[("greenhouse", "acme")]
    assert upgraded.origin == "curated"  # merged, never a rival row
    assert upgraded.industry == "fintech"
    assert upgraded.industry_origin == "curated"
    assert upgraded.company_stage == "series_b"
    assert by_key[("workable", "torino-tools")].origin == "curated"


def test_merge_curated_dry_run_writes_nothing(conn, capsys):
    merge_curated(conn, parse_curated_yaml(CURATED), dry_run=True)
    assert "would add" in capsys.readouterr().out
    assert SqliteSourceRegistryRepository(conn).list_all() == []
