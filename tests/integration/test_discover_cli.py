"""`discover` CLI smoke (OC-37 §7): sources management, opportunity listing
with filters, show with gate reasons and staleness signals as disclosed
observations, queue review/retry, and a full `discover run` over a canned
transport. All surfaces support --json (OC-19); no output anywhere carries a
ghost verdict (OC-13)."""

import json
import sqlite3

import pytest

import adapters.sources as sources_pkg
from adapters.sources.greenhouse import GreenhouseAdapter
from adapters.sources.http import HttpFetcher
from adapters.storage.migrations import migrate
from apps.cli.main import main
from domain.ports import ModelAdapter


@pytest.fixture
def instance(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(tmp_path))
    migrate(tmp_path / "open-career.sqlite3")
    return tmp_path


def connect(instance):
    conn = sqlite3.connect(instance / "open-career.sqlite3")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


BOARD = {"jobs": [{"id": 42, "title": "Senior Backend Engineer",
                   "content": "Requirements: Python.",
                   "location": {"name": "Milan, Italy"},
                   "absolute_url": "https://boards.greenhouse.io/acme/jobs/42"}],
         "meta": {"total": 1}}


class FakeModel(ModelAdapter):
    def complete(self, prompt: str) -> str:
        if "The extracted requirement proposals" in prompt:
            return json.dumps({"fit": "medium",
                               "matched_requirement_ids": ["r1"],
                               "gap_requirement_ids": []})
        return json.dumps({"requirements": ["Python"]})


def patch_run_dependencies(monkeypatch):
    """`discover run` with a canned transport and a fake model: no network,
    no claude CLI."""
    def transport(url, headers, timeout):
        return 200, json.dumps(BOARD).encode()

    canned = HttpFetcher(transport=transport, sleep=lambda _s: None,
                         clock=lambda: 0.0, min_interval_s=0)
    monkeypatch.setattr(
        sources_pkg, "build_adapters",
        lambda fetcher=None, max_pages_per_poll=None:
        {"greenhouse": GreenhouseAdapter(canned)})
    monkeypatch.setattr("apps.cli.main.ClaudeCodeAdapter", lambda: FakeModel())


def test_sources_lifecycle_and_json(instance, capsys, monkeypatch):
    patch_run_dependencies(monkeypatch)  # enable probes through the adapter
    main(["discover", "sources", "add", "greenhouse", "acme", "--company", "Acme"])
    out = capsys.readouterr().out
    assert "added source" in out and "candidate" in out

    main(["discover", "sources", "list", "--json"])
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["tenant_slug"] == "acme"
    assert listed[0]["origin"] == "manual"
    source_id = listed[0]["id"]

    main(["discover", "sources", "enable", source_id])
    assert "probe passed" in capsys.readouterr().out
    main(["discover", "sources", "list", "--status", "enabled"])
    assert "enabled" in capsys.readouterr().out

    main(["discover", "sources", "set-meta", source_id, "industry", "fintech"])
    capsys.readouterr()
    main(["discover", "sources", "list", "--json"])
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["metadata"]["industry"] == "fintech"
    assert listed[0]["metadata"]["industry_origin"] == "cli_edit"

    main(["discover", "sources", "add", "lever", "acme-new"])
    capsys.readouterr()
    main(["discover", "sources", "list", "--json"])
    new_id_ = [s["id"] for s in json.loads(capsys.readouterr().out)
               if s["ats_type"] == "lever"][0]
    main(["discover", "sources", "supersede", source_id, new_id_,
          "--notes", "migrated to Lever"])
    capsys.readouterr()
    main(["discover", "sources", "supersessions", "--json"])
    records = json.loads(capsys.readouterr().out)
    assert records[0]["old_source_id"] == source_id
    assert records[0]["new_source_id"] == new_id_

    main(["discover", "sources", "disable", source_id])
    capsys.readouterr()
    main(["discover", "sources", "retry", source_id])
    assert "re-probe" in capsys.readouterr().out


def test_duplicate_source_add_fails_cleanly(instance, capsys):
    main(["discover", "sources", "add", "greenhouse", "acme"])
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["discover", "sources", "add", "greenhouse", "acme"])
    assert "already registered" in capsys.readouterr().err


def test_run_then_opportunities_show_and_queue(instance, capsys, monkeypatch):
    patch_run_dependencies(monkeypatch)
    main(["discover", "sources", "add", "greenhouse", "acme"])
    capsys.readouterr()

    main(["discover", "run", "--json"])
    run_view = json.loads(capsys.readouterr().out)
    assert run_view["status"] == "completed"
    assert run_view["budget"]["max_fetches"] == 2000
    assert run_view["spend"]["model_calls_total"] == 2

    main(["discover", "opportunities", "--json"])
    opportunities = json.loads(capsys.readouterr().out)
    assert len(opportunities) == 1
    assert opportunities[0]["gate"] == "pass"
    opp_id = opportunities[0]["id"]

    main(["discover", "opportunities", "--status", "closed"])
    assert "no opportunities match" in capsys.readouterr().out

    main(["discover", "show", opp_id])
    out = capsys.readouterr().out
    assert "Senior Backend Engineer" in out
    assert "Gate: pass" in out
    assert "skip" in out  # skips are visible in gate output (§5)
    assert "work_authorization" in out
    assert "Staleness signals" in out
    assert "posted 0 days" in out and "salary absent" in out
    # OC-13: signals are disclosed observations; no verdict language anywhere.
    assert "ghost" not in out.lower()
    assert "score" not in out.lower()

    main(["discover", "show", opp_id, "--json"])
    view = json.loads(capsys.readouterr().out)
    assert view["staleness_signals"]["repost_count"] == 0
    assert view["judged_fit"]["fit"] == "medium"
    assert [d["dimension"] for d in view["gate"]["dimensions"]]

    main(["discover", "queue", "list", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["state"] == "judged"

    with pytest.raises(SystemExit):
        main(["discover", "queue", "retry", rows[0]["id"]])  # not failed
    assert "not in the failed state" in capsys.readouterr().err


def _seed_opportunity(conn, source_id: str, external_id: str, title: str,
                      location: str, apply_support: str = "extension") -> str:
    from adapters.storage.sqlite_opportunities import SqliteOpportunityRepository
    from domain.discovery import (
        MATERIAL_FIELDS,
        Opportunity,
        OpportunityVersion,
        material_fingerprint,
    )
    from adapters.storage.sqlite_discovery import SqliteSnapshotRepository
    repo = SqliteOpportunityRepository(conn)
    opp_id = f"opp_{source_id}_{external_id}"
    repo.add(Opportunity(id=opp_id, source_id=source_id,
                         external_job_id=external_id,
                         first_seen="2026-08-01T00:00:00Z",
                         last_seen="2026-08-01T00:00:00Z",
                         apply_support=apply_support))
    snapshot = SqliteSnapshotRepository(conn).commit(
        source_id, f"discovery/snapshots/{source_id}/x.json", "hash",
        json.dumps({"pages": 1, "complete": True}), 1)
    fields = {k: None for k in MATERIAL_FIELDS}
    fields["title"] = title
    fields["location_json"] = json.dumps({"locations": [location]})
    repo.add_version(OpportunityVersion(
        id=f"ver_{opp_id}", opportunity_id=opp_id, version=1,
        snapshot_id=snapshot.id, fingerprint=material_fingerprint(fields),
        title=title, location_json=fields["location_json"]))
    return opp_id


def test_duplicates_report_only_view(instance, capsys):
    """§3: cross-source suspected duplicates by exact normalized company +
    title + location country; computed on read, never merged; a near-miss
    (different country) stays out."""
    conn = connect(instance)
    try:
        from adapters.storage.sqlite_discovery import SqliteSourceRegistryRepository
        from domain.discovery import Source
        registry = SqliteSourceRegistryRepository(conn)
        registry.add(Source(id="src_gh", ats_type="greenhouse",
                            tenant_slug="acme", origin="manual",
                            company_name="Acme"))
        registry.add(Source(id="src_lv", ats_type="lever", tenant_slug="acme",
                            origin="manual", company_name="acme"))  # case-insensitive
        registry.add(Source(id="src_wk", ats_type="workable", tenant_slug="acme",
                            origin="manual", company_name="Acme"))
        # Positive group: same normalized company, title, and country (IT),
        # even with different city spellings.
        a = _seed_opportunity(conn, "src_gh", "1", "Backend Engineer",
                              "Milan, Italy")
        b = _seed_opportunity(conn, "src_lv", "x1", "backend engineer",
                              "Rome, IT", apply_support="extension")
        # Near-miss: identical company and title, different country.
        _seed_opportunity(conn, "src_wk", "w1", "Backend Engineer",
                          "Madrid, Spain", apply_support="none")
    finally:
        conn.close()

    main(["discover", "duplicates", "--json"])
    view = json.loads(capsys.readouterr().out)
    assert "never merged" in view["note"]
    assert len(view["groups"]) == 1
    group = view["groups"][0]
    assert group["country"] == "IT"
    assert {m["id"] for m in group["opportunities"]} == {a, b}

    main(["discover", "duplicates"])
    out = capsys.readouterr().out
    assert "Suspected duplicates by exact field match" in out
    assert "nothing is merged" in out
    assert "src_wk" not in out  # the near-miss must not match

    # Persisting nothing: the view left no new rows behind.
    conn = connect(instance)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert not any("duplicate" in t for t in tables)
    finally:
        conn.close()


def test_duplicates_empty_view(instance, capsys):
    main(["discover", "duplicates"])
    assert "no suspected cross-source duplicates" in capsys.readouterr().out


def test_run_builds_fetcher_from_configured_min_interval(instance, capsys,
                                                         monkeypatch):
    """Codex r1 finding 5: the shared HttpFetcher honors the locked config's
    per_host_min_interval_s, not the constructor default."""
    (instance / "discovery.json").write_text(
        json.dumps({"per_host_min_interval_s": 7}))
    captured = {}

    def recording_build_adapters(fetcher=None, max_pages_per_poll=None):
        captured["fetcher"] = fetcher
        canned = HttpFetcher(
            transport=lambda url, headers, timeout: (200, json.dumps(BOARD).encode()),
            sleep=lambda _s: None, clock=lambda: 0.0, min_interval_s=0)
        return {"greenhouse": GreenhouseAdapter(canned)}

    monkeypatch.setattr(sources_pkg, "build_adapters", recording_build_adapters)
    monkeypatch.setattr("apps.cli.main.ClaudeCodeAdapter", lambda: FakeModel())
    main(["discover", "sources", "add", "greenhouse", "acme"])
    capsys.readouterr()
    main(["discover", "run", "--json"])
    run_view = json.loads(capsys.readouterr().out)
    assert run_view["budget"]["per_host_min_interval_s"] == 7  # recorded (§4)
    assert captured["fetcher"]._min_interval_s == 7  # and actually applied


def test_enable_requires_a_passing_probe(instance, capsys, monkeypatch):
    """Codex r7 finding 2: enable runs the healthcheck; a failing probe leaves
    the status unchanged with the reason printed; a passing one enables."""
    failing = HttpFetcher(
        transport=lambda url, headers, timeout: (404, b"{}", {}),
        sleep=lambda _s: None, clock=lambda: 0.0, min_interval_s=0)
    monkeypatch.setattr(
        sources_pkg, "build_adapters",
        lambda fetcher=None, max_pages_per_poll=None:
        {"greenhouse": GreenhouseAdapter(failing)})
    main(["discover", "sources", "add", "greenhouse", "nosuch"])
    capsys.readouterr()
    main(["discover", "sources", "list", "--json"])
    source_id = json.loads(capsys.readouterr().out)[0]["id"]

    main(["discover", "sources", "enable", source_id])
    out = capsys.readouterr().out
    assert "NOT enabled" in out and "probe failed" in out
    main(["discover", "sources", "list", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["status"] == "candidate"

    patch_run_dependencies(monkeypatch)  # healthy canned board
    main(["discover", "sources", "enable", source_id])
    assert "probe passed" in capsys.readouterr().out
    main(["discover", "sources", "list", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["status"] == "enabled"


def test_stale_proposed_action_is_marked_in_both_cli_surfaces(
        instance, capsys, monkeypatch):
    """Codex r7 finding 1: a proposed action pinned to an older epoch or a
    superseded version never presents as current in list or show."""
    patch_run_dependencies(monkeypatch)
    main(["discover", "sources", "add", "greenhouse", "acme"])
    capsys.readouterr()
    main(["discover", "run"])
    capsys.readouterr()
    main(["discover", "opportunities", "--json"])
    row = json.loads(capsys.readouterr().out)[0]
    assert row["proposed_action"] == "monitor"
    assert row["proposed_action_stale"] is False
    opp_id = row["id"]

    # An epoch bump (audited policy write) makes the proposal stale at once.
    conn = connect(instance)
    try:
        from adapters.storage.sqlite_policies import SqliteUserPolicyRepository
        SqliteUserPolicyRepository(conn).set_policy(
            "compensation_floor",
            {"amount": 1, "currency": "EUR", "period": "annual"},
            source="user_edit")
    finally:
        conn.close()
    main(["discover", "opportunities", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["proposed_action_stale"] is True
    main(["discover", "opportunities"])
    assert "stale, awaiting re-evaluation" in capsys.readouterr().out
    main(["discover", "show", opp_id])
    assert "stale, awaiting re-evaluation" in capsys.readouterr().out

    # The next run re-evaluates; the proposal is current again...
    main(["discover", "run"])
    capsys.readouterr()
    main(["discover", "opportunities", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["proposed_action_stale"] is False

    # ...and a material version change (no run yet) makes it stale again.
    conn = connect(instance)
    try:
        from adapters.storage.sqlite_opportunities import SqliteOpportunityRepository
        from domain.discovery import (
            MATERIAL_FIELDS,
            OpportunityVersion,
            material_fingerprint,
        )
        repo = SqliteOpportunityRepository(conn)
        current = repo.list_versions(opp_id)[-1]
        fields = {k: getattr(current, k) for k in MATERIAL_FIELDS}
        fields["title"] = "Renamed Role"
        repo.add_version(OpportunityVersion(
            id="ver_new_material", opportunity_id=opp_id,
            version=current.version + 1, snapshot_id=current.snapshot_id,
            fingerprint=material_fingerprint(fields), **fields))
    finally:
        conn.close()
    main(["discover", "opportunities", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["proposed_action_stale"] is True
    main(["discover", "show", opp_id])
    assert "stale, awaiting re-evaluation" in capsys.readouterr().out


@pytest.mark.parametrize("status,body,expect_enabled", [
    (200, None, True),  # None body filled with the valid board below
    (200, b"this is not json at all", False),
    (503, b'{"error": "upstream exploded"}', False),
])
def test_cli_enable_probe_bodies_are_captured_durably(
        instance, capsys, monkeypatch, status, body, expect_enabled):
    """Codex r14: `discover sources enable` runs the shared capture-installing
    probe service; successful, malformed-JSON, and 503 probes each leave
    durable bodies, referenced (printed) by the CLI."""
    if body is None:
        body = json.dumps(BOARD).encode()

    def transport(url, headers, timeout):
        return status, body, {}

    fetcher = HttpFetcher(transport=transport, sleep=lambda _s: None,
                          clock=lambda: 0.0, min_interval_s=0)
    monkeypatch.setattr(
        sources_pkg, "build_adapters",
        lambda fetcher=None, max_pages_per_poll=None, _canned=fetcher:
        {"greenhouse": GreenhouseAdapter(_canned)})
    main(["discover", "sources", "add", "greenhouse", "acme"])
    capsys.readouterr()
    main(["discover", "sources", "list", "--json"])
    source_id = json.loads(capsys.readouterr().out)[0]["id"]

    main(["discover", "sources", "enable", source_id])
    out = capsys.readouterr().out
    assert ("probe passed" in out) is expect_enabled
    locators = [line.split("probe body preserved: ", 1)[1]
                for line in out.splitlines()
                if "probe body preserved: " in line]
    assert locators  # every received body referenced, any outcome
    for locator in locators:
        stored = instance / locator
        assert stored.exists()
    assert (instance / locators[0]).read_bytes() == body  # byte-true


def test_show_unknown_opportunity_fails_cleanly(instance, capsys):
    with pytest.raises(SystemExit):
        main(["discover", "show", "opp_nope"])
    assert "unknown opportunity" in capsys.readouterr().err


def test_cli_source_never_speaks_ghost_verdicts():
    """OC-13 as a regression: no ghost label, score, or threshold wording in
    the discover CLI surface."""
    from pathlib import Path
    import apps.cli.discover as discover_module
    text = Path(discover_module.__file__).read_text().lower()
    assert "ghost" not in text
