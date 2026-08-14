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
from adapters.sources.smartrecruiters import SmartRecruitersAdapter
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
    view = json.loads(capsys.readouterr().out)
    assert view["total"] == 1 and view["shown"] == 1
    rows = view["rows"]
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


def test_duplicates_handles_a_none_country_group(instance, capsys):
    """Drive defect 3: a duplicate group whose location resolves to no country
    sorts and renders (None-safe key), alongside a resolvable group."""
    conn = connect(instance)
    try:
        from adapters.storage.sqlite_discovery import SqliteSourceRegistryRepository
        from domain.discovery import Source
        registry = SqliteSourceRegistryRepository(conn)
        for source_id, ats in (("src_gh", "greenhouse"), ("src_lv", "lever")):
            registry.add(Source(id=source_id, ats_type=ats, tenant_slug="acme",
                                origin="manual", company_name="Acme"))
        # No-country group (unresolvable location strings).
        _seed_opportunity(conn, "src_gh", "1", "Backend Engineer", "The Moon")
        _seed_opportunity(conn, "src_lv", "x1", "backend engineer", "Nowhere")
        # Resolvable group.
        _seed_opportunity(conn, "src_gh", "2", "Designer", "Rome, IT")
        _seed_opportunity(conn, "src_lv", "x2", "designer", "Milan, Italy")
    finally:
        conn.close()
    main(["discover", "duplicates", "--json"])
    view = json.loads(capsys.readouterr().out)
    assert len(view["groups"]) == 2
    assert view["groups"][0]["country"] is None  # None sorts first, no crash
    assert view["groups"][1]["country"] == "IT"
    main(["discover", "duplicates"])
    assert "no country" in capsys.readouterr().out


def test_recover_clears_expired_lease_and_refuses_a_live_one(instance, capsys):
    """Drive defect 2: recovery mirrors the package pipeline: an expired lease
    clears; a live one is refused; a lease-blocked run names the holder and
    exits non-zero."""
    conn = connect(instance)
    try:
        from adapters.storage.sqlite_discovery import SqliteDiscoveryLease
        assert SqliteDiscoveryLease(conn).acquire("stuck-run", 3600)
    finally:
        conn.close()

    with pytest.raises(SystemExit) as excinfo:
        main(["discover", "recover"])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "live" in err and "stuck-run" in err and "never stolen" in err

    with pytest.raises(SystemExit) as excinfo:  # blocked run: non-zero + holder
        main(["discover", "run"])
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "stuck-run" in err and "expires" in err

    conn = connect(instance)
    try:
        with conn:
            conn.execute("UPDATE discovery_lease SET expires_at ="
                         " '2000-01-01T00:00:00Z'")
    finally:
        conn.close()
    main(["discover", "recover"])
    out = capsys.readouterr().out
    assert "cleared expired lease" in out and "stuck-run" in out
    main(["discover", "recover"])
    assert "nothing to recover" in capsys.readouterr().out


def test_recover_reconciles_an_abandoned_run_row_and_spares_a_live_one(
        instance, capsys):
    """Drive defect: run rows left 'running' by a killed process are phantom
    live runs forever. `discover recover` reconciles a row whose lease is no
    longer live to 'interrupted' with its spend retained, and a concurrent
    recover attempt never reconciles a genuinely live run."""
    from adapters.storage.sqlite_discovery import (
        SqliteDiscoveryLease,
        SqliteDiscoveryRunRepository,
    )
    from domain.budget import Budget

    conn = connect(instance)
    try:
        lease = SqliteDiscoveryLease(conn)
        runs = SqliteDiscoveryRunRepository(conn)
        dead_fence = lease.acquire("killed-run", 3600)
        abandoned = runs.start(Budget().to_json(), epoch=0,
                               lease_owner="killed-run", lease_fence=dead_fence)
        with conn:
            conn.execute("UPDATE discovery_runs SET spend_json = ? WHERE id = ?",
                         (json.dumps({"probe": 2000}), abandoned.id))
            conn.execute("UPDATE discovery_lease SET expires_at ="
                         " '2000-01-01T00:00:00Z'")
    finally:
        conn.close()

    main(["discover", "recover"])
    out = capsys.readouterr().out
    assert "cleared expired lease" in out
    assert "reconciled 1 abandoned run(s) to interrupted" in out

    conn = connect(instance)
    try:
        runs = SqliteDiscoveryRunRepository(conn)
        stored = runs.get(abandoned.id)
        assert stored.status == "interrupted" and stored.finished_at
        assert json.loads(stored.spend_json) == {"probe": 2000}
        # A live run: lease held and unexpired, so recover refuses and the
        # row stays 'running'.
        live_fence = SqliteDiscoveryLease(conn).acquire("live-run", 3600)
        live = runs.start(Budget().to_json(), epoch=0,
                          lease_owner="live-run", lease_fence=live_fence)
    finally:
        conn.close()

    with pytest.raises(SystemExit) as excinfo:
        main(["discover", "recover"])
    assert excinfo.value.code == 1
    conn = connect(instance)
    try:
        assert SqliteDiscoveryRunRepository(conn).get(live.id).status == "running"
    finally:
        conn.close()


def test_config_errors_are_clean_one_liners(instance, capsys):
    """Drive defect 4: unknown discovery.json keys name the allowed keys;
    malformed JSON reports the file and error without a traceback."""
    (instance / "discovery.json").write_text('{"max_fetchez": 5}')
    with pytest.raises(SystemExit):
        main(["discover", "run"])
    err = capsys.readouterr().err
    assert "unknown discovery.json keys" in err and "max_fetchez" in err
    assert "allowed keys" in err and "max_fetches" in err

    (instance / "discovery.json").write_text("{not json")
    with pytest.raises(SystemExit):
        main(["discover", "run"])
    err = capsys.readouterr().err
    assert "discovery.json" in err and "not valid JSON" in err
    assert "Traceback" not in err


def test_config_values_are_validated_before_the_run(instance, capsys):
    """Codex r16 finding 1: config values must be nonnegative integers (bool
    excluded, percentage bounded); failures are the clean one-line error."""
    (instance / "discovery.json").write_text('{"max_fetches": "0"}')
    with pytest.raises(SystemExit):
        main(["discover", "run"])
    err = capsys.readouterr().err
    assert "'max_fetches' must be an integer, got str" in err
    assert "Traceback" not in err

    (instance / "discovery.json").write_text('{"max_extraction_calls": -1}')
    with pytest.raises(SystemExit):
        main(["discover", "run"])
    assert "'max_extraction_calls' must be nonnegative" in capsys.readouterr().err

    (instance / "discovery.json").write_text(
        '{"mass_closure_guard_percent": 150}')
    with pytest.raises(SystemExit):
        main(["discover", "run"])
    assert "between 0 and 100" in capsys.readouterr().err

    (instance / "discovery.json").write_text('{"max_fetches": true}')
    with pytest.raises(SystemExit):
        main(["discover", "run"])
    assert "'max_fetches' must be an integer, got bool" in capsys.readouterr().err


def test_undecodable_config_is_a_clean_one_line_error(instance, capsys):
    """Codex r18 finding 1: invalid UTF-8 in discovery.json is the same clean
    one-line config error, never a traceback."""
    (instance / "discovery.json").write_bytes(b'\xff\xfe{"max_fetches": 1}')
    with pytest.raises(SystemExit):
        main(["discover", "run"])
    err = capsys.readouterr().err
    assert "discovery.json" in err and "could not be read" in err
    assert "Traceback" not in err


def test_lease_blocked_run_json_stdout_is_a_single_json_document(instance,
                                                                 capsys):
    """Codex r18 finding 2: a lease-blocked `discover run --json` writes one
    valid JSON error document to stdout, no plain-text prelude."""
    conn = connect(instance)
    try:
        from adapters.storage.sqlite_discovery import SqliteDiscoveryLease
        assert SqliteDiscoveryLease(conn).acquire("stuck-run", 3600)
    finally:
        conn.close()
    with pytest.raises(SystemExit) as excinfo:
        main(["discover", "run", "--json"])
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    view = json.loads(captured.out)  # the whole stdout parses as JSON
    assert view["error"] == "lease_held"
    assert view["holder"] == "stuck-run"
    assert view["expires_at"]
    assert "lease" in captured.err  # the human message still goes to stderr


def test_zero_total_model_calls_reads_as_nothing_attempted(instance, capsys,
                                                           monkeypatch):
    """Codex r16 finding 2: max_total_model_calls == 0 blocking a model stage
    renders the nothing-attempted wording, never 'exhausted at extraction'."""
    patch_run_dependencies(monkeypatch)
    (instance / "discovery.json").write_text(
        json.dumps({"max_total_model_calls": 0}))
    main(["discover", "sources", "add", "greenhouse", "acme"])
    capsys.readouterr()
    main(["discover", "run"])
    out = capsys.readouterr().out
    assert "max_total_model_calls is 0; nothing attempted" in out
    assert "exhausted at" not in out


def test_queue_list_limit_rejects_negatives_and_accepts_zero(instance, capsys):
    """Codex r16 finding 3: --limit is a nonnegative integer."""
    with pytest.raises(SystemExit) as excinfo:
        main(["discover", "queue", "list", "--limit", "-1"])
    assert excinfo.value.code == 2  # argparse usage error
    assert "nonnegative" in capsys.readouterr().err
    main(["discover", "queue", "list", "--limit", "0"])
    assert "promotion queue is empty" in capsys.readouterr().out


def test_run_prints_the_locked_budget_before_spending(instance, capsys,
                                                      monkeypatch):
    """Drive defect 5: the locked budget (all caps) is the first output line,
    before anything is spent."""
    patch_run_dependencies(monkeypatch)
    main(["discover", "sources", "add", "greenhouse", "acme"])
    capsys.readouterr()
    main(["discover", "run"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("locked budget: ")
    budget = json.loads(lines[0].split("locked budget: ", 1)[1])
    assert budget["max_total_model_calls"] == 40
    assert budget["max_extraction_calls"] == 30


def test_queue_list_limit_and_total_count(instance, capsys):
    """Drive defect 7: --limit (default 50) plus a total count line."""
    conn = connect(instance)
    try:
        from adapters.storage.sqlite_discovery import SqliteSourceRegistryRepository
        from adapters.storage.sqlite_opportunities import (
            SqlitePromotionQueueRepository,
        )
        from domain.discovery import Source
        SqliteSourceRegistryRepository(conn).add(Source(
            id="src_q", ats_type="greenhouse", tenant_slug="acme",
            origin="manual"))
        queue = SqlitePromotionQueueRepository(conn)
        for i in range(3):
            opp_id = _seed_opportunity(conn, "src_q", str(i), f"Role {i}",
                                       "Rome, IT")
            queue.enqueue(opp_id, f"ver_{opp_id}", 0,
                          "2026-08-01T00:00:00Z", 0)
    finally:
        conn.close()
    main(["discover", "queue", "list", "--limit", "2"])
    out = capsys.readouterr().out
    assert "3 row(s) total, showing 2" in out
    main(["discover", "queue", "list", "--limit", "2", "--json"])
    view = json.loads(capsys.readouterr().out)
    assert view["total"] == 3 and view["shown"] == 2 and len(view["rows"]) == 2


def test_zero_cap_exhaustion_wording(instance, capsys, monkeypatch):
    """Drive defect 8: a zero stage cap reads as 'nothing attempted', never as
    an exhausted budget."""
    patch_run_dependencies(monkeypatch)
    (instance / "discovery.json").write_text(
        json.dumps({"max_new_opportunities_gated": 0}))
    main(["discover", "sources", "add", "greenhouse", "acme"])
    capsys.readouterr()
    main(["discover", "run"])
    out = capsys.readouterr().out
    assert "gate stage cap is 0; nothing attempted" in out
    assert "exhausted at" not in out


def test_broken_pipe_exits_quietly(instance, capsys, monkeypatch):
    """Drive defect 6: a closed reader (| head) exits 0 with no traceback."""
    def raising_print(*_args, **_kwargs):
        raise BrokenPipeError

    monkeypatch.setattr("builtins.print", raising_print)
    with pytest.raises(SystemExit) as excinfo:
        main(["discover", "sources", "list"])
    assert excinfo.value.code == 0


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


def test_enable_of_a_smartrecruiters_slug_needs_tenant_evidence(
        instance, capsys, monkeypatch):
    """The CLI enable path shares the probe service, so the vendor's
    undiscriminating postings collection cannot enable an invented slug there
    either: verification reads the departments resource, which 404s."""
    tenant_exists = [False]

    def transport(url, headers, timeout):
        if "/departments" in url:
            if tenant_exists[0]:
                return 200, b'{"totalFound":1,"content":[{"id":1}]}', {}
            return 404, b'{"httpCode":404,"code":"RESOURCE_NOT_FOUND"}', {}
        # 200 with an empty page for any company id, real or invented.
        return 200, b'{"offset":0,"limit":100,"totalFound":0,"content":[]}', {}

    canned = HttpFetcher(transport=transport, sleep=lambda _s: None,
                         clock=lambda: 0.0, min_interval_s=0)
    monkeypatch.setattr(
        sources_pkg, "build_adapters",
        lambda fetcher=None, max_pages_per_poll=None:
        {"smartrecruiters": SmartRecruitersAdapter(canned)})
    main(["discover", "sources", "add", "smartrecruiters", "nosuchtenant"])
    capsys.readouterr()
    main(["discover", "sources", "list", "--json"])
    source_id = json.loads(capsys.readouterr().out)[0]["id"]

    main(["discover", "sources", "enable", source_id])
    assert "NOT enabled" in capsys.readouterr().out
    main(["discover", "sources", "list", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["status"] == "candidate"

    tenant_exists[0] = True  # the same slug, now a tenant that exists
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


def test_runs_command_shows_why_an_aborted_run_died(instance, capsys):
    """Drive defect: the operator saw `database is locked` in the terminal
    while the persisted record said only that something unexpected happened.
    Run history now names the failure, in human and JSON form alike."""
    from adapters.storage.sqlite_discovery import SqliteDiscoveryRunRepository
    from domain.budget import Budget

    conn = connect(instance)
    runs = SqliteDiscoveryRunRepository(conn)
    run = runs.start(Budget().to_json(), epoch=0)
    runs.finish(run.id, "failed", json.dumps({"fetch": 888}),
                json.dumps({"sources": {}, "notes": []}),
                failure_json=json.dumps({
                    "error_type": "OperationalError",
                    "error_message": "database is locked",
                    "stage": "poll", "source_id": "src_acme"}))
    conn.close()

    main(["discover", "runs"])
    out = capsys.readouterr().out
    assert "failed" in out
    assert "OperationalError: database is locked" in out
    assert "stage poll" in out and "source src_acme" in out

    main(["discover", "runs", "--json"])
    view = json.loads(capsys.readouterr().out)[0]
    assert view["failure"]["error_message"] == "database is locked"
    assert view["failure"]["stage"] == "poll"


def test_runs_command_with_no_runs_says_so(instance, capsys):
    main(["discover", "runs"])
    assert "no discovery runs yet" in capsys.readouterr().out


def test_cli_source_never_speaks_ghost_verdicts():
    """OC-13 as a regression: no ghost label, score, or threshold wording in
    the discover CLI surface."""
    from pathlib import Path
    import apps.cli.discover as discover_module
    text = Path(discover_module.__file__).read_text().lower()
    assert "ghost" not in text
