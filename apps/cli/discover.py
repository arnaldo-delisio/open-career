"""`open-career discover`: the OC-37 CLI surface (§7). One budgeted run per
invocation; registry management (list/enable/disable/add, supersession review,
reviewed metadata edits); opportunity listing with filters; and show with
versions, gate reasons including skips, and staleness signals as disclosed
observations only. No score, no threshold, no staleness verdict of any kind
anywhere in this surface (OC-13).
"""

import json
import sqlite3

from adapters.storage.sqlite_discovery import (
    SqliteDiscoveryLease,
    SqliteDiscoveryRunRepository,
    SqliteSourceRegistryRepository,
)
from adapters.storage.sqlite_opportunities import (
    SqliteOpportunityRepository,
    SqlitePromotionQueueRepository,
)
from domain.discovery import Source, SourceSupersession
from domain.ids import new_id


class DiscoverCliError(ValueError):
    pass


# ------------------------------------------------------------------- run

def run_run(conn, storage, model, say, as_json: bool = False) -> None:
    """One budgeted discovery run (§4): exhaustion is a safe stop, exit 0.
    A lease-blocked run names the holder and exits non-zero."""
    from adapters.sources import build_adapters
    from adapters.sources.http import HttpFetcher
    from workers.discovery.run import load_config, run_discovery
    try:
        config = load_config(storage)
    except ValueError as e:
        raise DiscoverCliError(str(e))
    if not as_json:
        # The locked budget prints BEFORE anything is spent, so a bare run's
        # real model-call spend (subscription calls) is visible upfront.
        say(f"locked budget: {config.to_json()}")
    # The shared fetcher honors the locked config's per-host interval (§4).
    fetcher = HttpFetcher(min_interval_s=config.budget.per_host_min_interval_s)
    adapters = build_adapters(fetcher=fetcher,
                              max_pages_per_poll=config.max_pages_per_poll)
    # In JSON mode the worker's progress reporting is silenced so stdout
    # stays a single valid JSON document, success or error.
    run = run_discovery(conn, storage, model, adapters, config=config,
                        say=say if not as_json else (lambda *_a, **_k: None))
    if run is None:
        owner, expires_at = SqliteDiscoveryLease(conn).holder()
        message = (
            f"another discovery run holds the lease (holder {owner}, expires"
            f" {expires_at}); if that run is dead, `discover recover` clears"
            " an expired lease")
        if as_json:
            say(json.dumps({"error": "lease_held", "holder": owner,
                            "expires_at": expires_at, "message": message},
                           indent=2))
        raise DiscoverCliError(message)
    if as_json:
        say(json.dumps(_run_view(run), indent=2))
        return
    say(f"run {run.id} {run.status}{_exhaustion_note(run)}")
    say(f"spend: {run.spend_json}")
    failure = _failure_note(run)
    if failure:
        say(failure)


def _exhausted_stages(run) -> list[str]:
    """Every stage that exhausted, not only the one the run row pins. A
    multi-stage run persists the full set as a note; reading it back is what
    keeps a second capped stage from being invisible to the operator."""
    from workers.discovery.run import EXHAUSTED_STAGES_NOTE
    stages = [run.exhausted_stage] if run.exhausted_stage else []
    outcomes = json.loads(run.source_outcomes_json) \
        if run.source_outcomes_json else {}
    for note in (outcomes.get("notes") or []):
        if not note.startswith(EXHAUSTED_STAGES_NOTE):
            continue
        for stage in note[len(EXHAUSTED_STAGES_NOTE):].split(","):
            stage = stage.strip()
            if stage and stage not in stages:
                stages.append(stage)
    return stages


def _exhaustion_note(run) -> str:
    """Honest wording: a zero cap means nothing was attempted, not that a
    budget was spent to exhaustion. Every exhausted stage is reported, so a
    capped stage with backlog remaining is never hidden behind the first."""
    stages = _exhausted_stages(run)
    if not stages:
        return ""
    budget = json.loads(run.budget_json)
    return " (" + "; ".join(_stage_exhaustion_phrase(s, budget)
                            for s in stages) + ")"


def _stage_exhaustion_phrase(stage: str, budget: dict) -> str:
    from domain.budget import STAGE_LIMITS
    limit_key = STAGE_LIMITS.get(stage)
    if limit_key is not None and budget.get(limit_key) == 0:
        return f"{stage} stage cap is 0; nothing attempted at that stage"
    if stage in ("extraction", "judgment") \
            and budget.get("max_total_model_calls") == 0:
        return (f"max_total_model_calls is 0; nothing attempted at the"
                f" {stage} stage")
    return f"exhausted at {stage}"


def _failure_note(run) -> str | None:
    """What an aborted run died of, in the operator's own view: the persisted
    exception type and message, plus where it happened."""
    if not run.failure_json:
        return None
    failure = json.loads(run.failure_json)
    where = [part for part in (
        f"stage {failure['stage']}" if failure.get("stage") else None,
        f"source {failure['source_id']}" if failure.get("source_id") else None,
    ) if part]
    location = f" ({', '.join(where)})" if where else ""
    return (f"failure: {failure.get('error_type')}:"
            f" {failure.get('error_message')}{location}")


def run_runs_list(conn, say, as_json: bool = False, limit: int = 20) -> None:
    """Recent run history, newest first: how each run ended, and for an
    aborted one what it died of (the diagnostic the run row persists)."""
    runs = SqliteDiscoveryRunRepository(conn).list_all()[-limit:][::-1]
    if as_json:
        say(json.dumps([_run_view(r) for r in runs], indent=2))
        return
    if not runs:
        say("no discovery runs yet")
        return
    for run in runs:
        say(f"{run.run_seq}  {run.id}  {run.status}{_exhaustion_note(run)}"
            f"  started {run.started_at}  finished {run.finished_at or '-'}")
        failure = _failure_note(run)
        if failure:
            say(f"    {failure}")


def run_recover(conn, say) -> None:
    """Clear an expired run lease and reconcile the run rows a dead process
    left behind (the package pipeline's recovery precedent: only an expired
    lease is ever claimed; a live one is refused, never stolen from its
    owner). Reconciliation uses the lease's own ownership and expiry test, so
    a live run is never marked interrupted."""
    lease = SqliteDiscoveryLease(conn)
    runs = SqliteDiscoveryRunRepository(conn)
    owner, expires_at = lease.holder()
    if owner is None:
        _say_reconciled(runs.reconcile_abandoned(), say,
                        empty="no lease held; nothing to recover")
        return
    if lease.claim_expired():
        say(f"cleared expired lease (was held by {owner}, expired {expires_at})")
        _say_reconciled(runs.reconcile_abandoned(), say, empty=None)
        return
    raise DiscoverCliError(
        f"lease is live (holder {owner}, expires {expires_at}); a live lease"
        " is never stolen. Wait for expiry or for the run to finish")


def _say_reconciled(run_ids: list[str], say, empty: str | None) -> None:
    if run_ids:
        say(f"reconciled {len(run_ids)} abandoned run(s) to interrupted:"
            f" {', '.join(run_ids)}")
    elif empty:
        say(empty)


def _run_view(run) -> dict:
    return {
        "id": run.id, "run_seq": run.run_seq, "status": run.status,
        "exhausted_stage": run.exhausted_stage,
        # The full set, not only the pinned first refusal: a consumer reading
        # exhausted_stage alone cannot see a second capped stage.
        "exhausted_stages": _exhausted_stages(run),
        "budget": json.loads(run.budget_json),
        "spend": json.loads(run.spend_json) if run.spend_json else None,
        "source_outcomes": json.loads(run.source_outcomes_json)
        if run.source_outcomes_json else None,
        "epoch": run.epoch,
        "failure": json.loads(run.failure_json) if run.failure_json else None,
        "started_at": run.started_at, "finished_at": run.finished_at,
    }


# --------------------------------------------------------------- sources

def run_sources_list(conn, say, as_json: bool = False,
                     status: str | None = None) -> None:
    registry = SqliteSourceRegistryRepository(conn)
    sources = [s for s in registry.list_all()
               if status is None or s.status == status]
    if as_json:
        say(json.dumps([_source_view(s) for s in sources], indent=2))
        return
    if not sources:
        say("no sources registered")
        return
    for s in sources:
        say(f"{s.id}  [{s.ats_type}] {s.tenant_slug}  {s.status}"
            f"  origin={s.origin}  failures={s.consecutive_failures}"
            + (f"  last_poll={s.last_poll_outcome}" if s.last_poll_outcome else "")
            + (f"  company={s.company_name}" if s.company_name else ""))


def _source_view(s: Source) -> dict:
    return {
        "id": s.id, "ats_type": s.ats_type, "tenant_slug": s.tenant_slug,
        "company_name": s.company_name, "origin": s.origin, "status": s.status,
        "consecutive_failures": s.consecutive_failures,
        "last_polled_at": s.last_polled_at, "next_poll_at": s.next_poll_at,
        "next_probe_at": s.next_probe_at, "probe_attempts": s.probe_attempts,
        "last_poll_outcome": s.last_poll_outcome,
        "last_success": s.last_success,
        "metadata": {
            "industry": s.industry, "industry_origin": s.industry_origin,
            "company_stage": s.company_stage,
            "company_stage_origin": s.company_stage_origin,
            "company_size_band": s.company_size_band,
            "company_size_band_origin": s.company_size_band_origin,
        },
    }


def run_sources_add(conn, ats_type: str, tenant_slug: str,
                    company: str | None, say) -> None:
    """Manual registry entry (origin 'manual'), status candidate until one
    successful probe (§2)."""
    registry = SqliteSourceRegistryRepository(conn)
    source = Source(id=new_id("src"), ats_type=ats_type, tenant_slug=tenant_slug,
                    origin="manual", company_name=company)
    try:
        registry.add(source)
    except sqlite3.IntegrityError as e:
        raise DiscoverCliError(
            f"source [{ats_type}] {tenant_slug} already registered ({e})")
    say(f"added source {source.id} [{ats_type}] {tenant_slug} (candidate;"
        " enabled after one successful probe)")


def run_sources_enable(conn, source_id: str, say, storage, adapters=None) -> None:
    """Verification before enablement (§2), CLI included: enable runs the
    shared probe service (the worker's capture-installing probe), persisting
    every received body before parsing, and transitions only on success; a
    failed probe leaves the status unchanged with the reason printed. No
    override exists."""
    from workers.discovery.probe import probe_source
    registry = SqliteSourceRegistryRepository(conn)
    source = registry.get(source_id)
    if source is None:
        raise DiscoverCliError(f"unknown source '{source_id}'")
    if adapters is None:
        from adapters.sources import build_adapters
        adapters = build_adapters()
    ok, captured = probe_source(storage, adapters[source.ats_type],
                                source_id, source.tenant_slug)
    if ok:
        # The probe outcome path enables and resets attempt/health state.
        registry.record_probe_outcome(source_id, True, next_probe_at=None)
        say(f"source {source_id} -> enabled (probe passed)")
    else:
        registry.record_probe_outcome(
            source_id, False,
            next_probe_at=conn.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+1 day')"
            ).fetchone()[0])
        say(f"source {source_id} NOT enabled: probe failed"
            f" ([{source.ats_type}] {source.tenant_slug} did not answer with a"
            " valid feed); status unchanged, re-probe scheduled")
    for locator in captured:
        say(f"  probe body preserved: {locator}")


def run_sources_disable(conn, source_id: str, say) -> None:
    registry = SqliteSourceRegistryRepository(conn)
    if registry.get(source_id) is None:
        raise DiscoverCliError(f"unknown source '{source_id}'")
    registry.set_status(source_id, "disabled")
    say(f"source {source_id} -> disabled")


def run_sources_retry(conn, source_id: str, say) -> None:
    """Explicit retry of a rot-disabled source: schedules an immediate
    re-probe; nothing is deleted (§2)."""
    registry = SqliteSourceRegistryRepository(conn)
    source = registry.get(source_id)
    if source is None:
        raise DiscoverCliError(f"unknown source '{source_id}'")
    with conn:
        conn.execute(
            "UPDATE sources SET next_probe_at = strftime('%Y-%m-%dT%H:%M:%SZ',"
            " 'now'), probe_attempts = 0,"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (source_id,))
    say(f"source {source_id} scheduled for immediate re-probe")


def run_sources_set_meta(conn, source_id: str, field: str, value: str, say) -> None:
    """Reviewed company metadata via explicit CLI edit (origin 'cli_edit');
    the §5 hard exclusions consume these, never a silent classifier."""
    registry = SqliteSourceRegistryRepository(conn)
    if registry.get(source_id) is None:
        raise DiscoverCliError(f"unknown source '{source_id}'")
    try:
        registry.set_reviewed_metadata(source_id, field,
                                       value if value else None, "cli_edit")
    except ValueError as e:
        raise DiscoverCliError(str(e))
    say(f"source {source_id} {field} = {value or '(cleared)'} (origin: cli_edit)")


def run_sources_supersede(conn, old_id: str, new_id_: str,
                          notes: str | None, say) -> None:
    """Reviewed supersession record (§2): a company migrating ATS or renaming
    its tenant; a locator change is never closure plus unrelated discovery."""
    registry = SqliteSourceRegistryRepository(conn)
    for source_id in (old_id, new_id_):
        if registry.get(source_id) is None:
            raise DiscoverCliError(f"unknown source '{source_id}'")
    now = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now')").fetchone()[0]
    supersession = SourceSupersession(
        id=new_id("sup"), old_source_id=old_id, new_source_id=new_id_,
        origin="migration", reviewed_at=now, notes=notes)
    registry.record_supersession(supersession)
    say(f"recorded supersession {supersession.id}: {old_id} -> {new_id_}")


def run_sources_supersessions(conn, say, as_json: bool = False) -> None:
    registry = SqliteSourceRegistryRepository(conn)
    records = registry.list_supersessions()
    if as_json:
        say(json.dumps([{
            "id": r.id, "old_source_id": r.old_source_id,
            "new_source_id": r.new_source_id, "origin": r.origin,
            "reviewed_at": r.reviewed_at, "notes": r.notes,
        } for r in records], indent=2))
        return
    if not records:
        say("no supersession records")
        return
    for r in records:
        say(f"{r.id}  {r.old_source_id} -> {r.new_source_id}"
            f"  reviewed={r.reviewed_at}" + (f"  {r.notes}" if r.notes else ""))


# ----------------------------------------------------------------- queue

def run_queue_list(conn, say, as_json: bool = False,
                   state: str | None = None, limit: int = 50) -> None:
    queue = SqlitePromotionQueueRepository(conn)
    # The counts and the rows are two queries; a concurrent `discover run` may
    # write between them, so they are read inside one deferred read
    # transaction and therefore describe one snapshot (Codex r1 finding 1).
    conn.execute("BEGIN DEFERRED")
    try:
        counts = queue.counts_by_state(state)
        total = sum(counts.values())
        shown = queue.list_rows(state, limit=limit)
    finally:
        conn.rollback()
    if as_json:
        say(json.dumps({
            "total": total,
            "shown": len(shown),
            "counts_by_state": counts,
            "rows": [{
                "id": r.id, "opportunity_id": r.opportunity_id,
                "version_id": r.version_id, "state": r.state,
                "coverage_bp": r.coverage_bp,
                "relevance_score": r.relevance_score, "attempts": r.attempts,
                "failure_reason": r.failure_reason,
                "superseded_reason": r.superseded_reason,
            } for r in shown],
        }, indent=2))
        return
    if not total:
        say("promotion queue is empty")
        return
    for r in shown:
        say(f"{r.id}  {r.state}  opp={r.opportunity_id}"
            f"  attempts={r.attempts}"
            f"  relevance={r.relevance_score}"
            + (f"  coverage_bp={r.coverage_bp}" if r.coverage_bp is not None else "")
            + (f"  failure={r.failure_reason}" if r.failure_reason else ""))
    # The per-state breakdown travels with the count line so truncation can
    # never hide live work behind a page of terminal rows.
    breakdown = ", ".join(f"{s}={n}" for s, n in counts.items())
    say(f"{total} row(s) total, showing {len(shown)}"
        f" (by state: {breakdown})"
        + (f" (raise --limit past {limit} for more)" if total > limit else ""))


def run_queue_retry(conn, row_id: str, say) -> None:
    """Explicit retry of a terminal failed queue row after remediation (§4)."""
    queue = SqlitePromotionQueueRepository(conn)
    run_seq = conn.execute(
        "SELECT COALESCE(MAX(run_seq), 0) FROM discovery_runs").fetchone()[0]
    try:
        queue.retry_failed(row_id, run_seq)
    except ValueError as e:
        raise DiscoverCliError(str(e))
    say(f"queue row {row_id} released for retry")


# ---------------------------------------------------------- opportunities

def _current_epoch(conn) -> int:
    return conn.execute(
        "SELECT epoch FROM dependency_epoch WHERE id = 1").fetchone()[0]


def _proposed_action_stale(opportunity, current_epoch: int) -> bool:
    """A proposal pinned to a superseded version or an older dependency epoch
    never presents as current; it stays visible only as stale until the next
    run re-evaluates it (staleness is read-time, bumps stay O(1))."""
    if opportunity.proposed_action is None:
        return False
    return (opportunity.proposed_action_version_id
            != opportunity.current_version_id
            or (opportunity.proposed_action_epoch is not None
                and opportunity.proposed_action_epoch < current_epoch))


def _proposed_label(opportunity, current_epoch: int) -> str:
    if opportunity.proposed_action is None:
        return "-"
    if _proposed_action_stale(opportunity, current_epoch):
        return f"{opportunity.proposed_action} (stale, awaiting re-evaluation)"
    return opportunity.proposed_action


def _gate_state(conn, opportunity, current_epoch: int) -> str:
    """The verdict as a read-time state: 'none', 'stale' (pinned to a
    superseded version or an older dependency epoch, never counted as a
    current pass/fail), or the verdict itself."""
    if opportunity.latest_gate_verdict_id is None:
        return "none"
    verdict = SqliteOpportunityRepository(conn).get_gate_verdict(
        opportunity.latest_gate_verdict_id)
    if verdict.version_id != opportunity.current_version_id \
            or verdict.epoch < current_epoch:
        return "stale"
    return verdict.verdict


def run_opportunities(conn, say, as_json: bool = False,
                      status: str | None = None, gate: str | None = None,
                      source: str | None = None) -> None:
    repo = SqliteOpportunityRepository(conn)
    opportunities = repo.list_filtered(availability=status, source_id=source)
    epoch = _current_epoch(conn)
    if gate is not None:
        # The filter runs on the read-time state: stale is its own state and
        # never matches pass or fail.
        opportunities = [o for o in opportunities
                         if _gate_state(conn, o, epoch) == gate]
    if as_json:
        say(json.dumps([_opportunity_row(conn, o) for o in opportunities], indent=2))
        return
    if not opportunities:
        say("no opportunities match")
        return
    for o in opportunities:
        say(f"{o.id}  {o.availability}  {_quoted_title(conn, o)}"
            f"  gate={_gate_state(conn, o, epoch)}"
            f"  proposed={_proposed_label(o, epoch)}"
            f"  apply={o.apply_support}"
            # A gate-passing row that never reached the model stages looks
            # identical to a gated-out one without this.
            + (f"  skipped={o.promotion_skip_reason}"
               if o.promotion_skip_reason else ""))


def _quoted_title(conn, opportunity) -> str:
    """Human-surface titles render as attributed posting quotes: the words
    are the posting's, never this tool's assertion (OC-13); JSON surfaces are
    attributed structurally by their field name."""
    title = _current_title(conn, opportunity)
    return f'"{title}"' if title else "(untitled)"


def _current_title(conn, opportunity) -> str | None:
    if opportunity.current_version_id is None:
        return None
    row = conn.execute("SELECT title FROM opportunity_versions WHERE id = ?",
                       (opportunity.current_version_id,)).fetchone()
    return row[0] if row else None


def _opportunity_row(conn, o) -> dict:
    return {
        "id": o.id, "source_id": o.source_id,
        "external_job_id": o.external_job_id,
        "title": _current_title(conn, o),
        "availability": o.availability,
        "gate": _gate_state(conn, o, _current_epoch(conn)),
        "proposed_action": o.proposed_action,
        "proposed_action_stale": _proposed_action_stale(o, _current_epoch(conn)),
        "promotion_skip_reason": o.promotion_skip_reason,
        "human_action": o.human_action,
        "apply_support": o.apply_support,
        "first_seen": o.first_seen, "last_seen": o.last_seen,
    }


# ------------------------------------------------------------- duplicates

def run_duplicates(conn, say, as_json: bool = False) -> None:
    """The §3 report-only cross-source duplicate view: computed on read,
    persisting nothing, never merging. A group is open opportunities from
    DIFFERENT sources agreeing on exact normalized company name, normalized
    title, and canonical location country; anything less is not reported."""
    from domain.normalization import normalize_location

    registry = SqliteSourceRegistryRepository(conn)
    sources = {s.id: s for s in registry.list_all()}
    repo = SqliteOpportunityRepository(conn)
    groups: dict[tuple, list] = {}
    for opportunity in repo.list_filtered():
        if opportunity.availability == "closed":
            continue
        source = sources[opportunity.source_id]
        company = (source.company_name or "").strip().lower()
        title = (_current_title(conn, opportunity) or "").strip().lower()
        if not company or not title:
            continue  # no exact match basis without both fields
        country = None
        row = conn.execute(
            "SELECT location_json FROM opportunity_versions WHERE id = ?",
            (opportunity.current_version_id,)).fetchone() \
            if opportunity.current_version_id else None
        if row and row[0]:
            for raw in json.loads(row[0]).get("locations", []):
                location = normalize_location(raw)
                if location is not None:
                    country = location.country
                    break
        groups.setdefault((company, title, country), []).append(opportunity)

    duplicates = []
    # None-safe sort: a group without a resolvable country sorts first.
    for (company, title, country), members in sorted(
            groups.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or "")):
        if len({o.source_id for o in members}) < 2:
            continue  # cross-source only; same-source dedup is deterministic
        duplicates.append({
            "company": company, "title": title, "country": country,
            "opportunities": [{
                "id": o.id, "source_id": o.source_id,
                "ats_type": sources[o.source_id].ats_type,
                "external_job_id": o.external_job_id,
            } for o in sorted(members, key=lambda o: o.id)],
        })

    if as_json:
        say(json.dumps({
            "note": "suspected duplicates by exact field match (normalized"
                    " company name + title + location country); report-only,"
                    " computed on read, never merged",
            "groups": duplicates,
        }, indent=2))
        return
    if not duplicates:
        say("no suspected cross-source duplicates (exact field match)")
        return
    say("Suspected duplicates by exact field match (normalized company name +"
        " title + location country). Report-only: nothing is merged.")
    for group in duplicates:
        # The title is normalized posting text: attributed quoted.
        say(f"\n{group['company']} | posting title \"{group['title']}\""
            f" | {group['country'] or 'no country'}")
        for member in group["opportunities"]:
            say(f"  {member['id']}  [{member['ats_type']}]"
                f"  external={member['external_job_id']}")


# ------------------------------------------------------------------ show

def run_show(conn, opportunity_id: str, say, as_json: bool = False) -> None:
    repo = SqliteOpportunityRepository(conn)
    opportunity = repo.get(opportunity_id)
    if opportunity is None:
        raise DiscoverCliError(f"unknown opportunity '{opportunity_id}'")
    versions = repo.list_versions(opportunity_id)
    verdict = repo.get_gate_verdict(opportunity.latest_gate_verdict_id) \
        if opportunity.latest_gate_verdict_id else None
    signals = _staleness_signals(conn, opportunity, versions)
    current_epoch = conn.execute(
        "SELECT epoch FROM dependency_epoch WHERE id = 1").fetchone()[0]

    def result_is_stale(result: dict) -> bool:
        """Stale by closure stamp, version pin, or dependency epoch (§4/§5):
        a pre-bump result never presents as current."""
        return bool(result.get("stale")) \
            or result.get("version_id") != opportunity.current_version_id \
            or (result.get("epoch") is not None
                and result["epoch"] < current_epoch)
    if as_json:
        say(json.dumps({
            **_opportunity_row(conn, opportunity),
            "versions": [{
                "id": v.id, "version": v.version, "title": v.title,
                "seniority": v.seniority, "apply_url": v.apply_url,
                "location_json": v.location_json, "salary_json": v.salary_json,
                "created_at": v.created_at,
            } for v in versions],
            "gate": {
                "verdict": verdict.verdict, "epoch": verdict.epoch,
                "stale": _gate_state(conn, opportunity, current_epoch) == "stale",
                "dimensions": json.loads(verdict.dimensions_json),
            } if verdict else None,
            "requirement_proposals": json.loads(
                opportunity.requirement_proposals_json)
            if opportunity.requirement_proposals_json else None,
            "requirement_proposals_stale": result_is_stale(
                json.loads(opportunity.requirement_proposals_json))
            if opportunity.requirement_proposals_json else None,
            "judged_fit": json.loads(opportunity.judged_fit_json)
            if opportunity.judged_fit_json else None,
            "judged_fit_stale": result_is_stale(
                json.loads(opportunity.judged_fit_json))
            if opportunity.judged_fit_json else None,
            "staleness_signals": signals,
        }, indent=2))
        return

    say(f"{opportunity.id}  {_quoted_title(conn, opportunity)}")
    say(f"  availability: {opportunity.availability}"
        f"   proposed: {_proposed_label(opportunity, current_epoch)}"
        f"   human: {opportunity.human_action or '-'}"
        f"   apply: {opportunity.apply_support}")
    say(f"\nVersions ({len(versions)}):")
    for v in versions:
        version_title = f'"{v.title}"' if v.title else "(untitled)"
        say(f"  v{v.version}  {version_title}"
            f"  seniority={v.seniority or '?'}  {v.created_at}")
    if verdict:
        stale_note = " (stale, awaiting re-evaluation)" \
            if _gate_state(conn, opportunity, current_epoch) == "stale" else ""
        say(f"\nGate: {verdict.verdict}{stale_note} (epoch {verdict.epoch})")
        for d in json.loads(verdict.dimensions_json):
            note = f"  [{d['note']}]" if d.get("note") else ""
            say(f"  {d['dimension']}: {d['verdict']}  {d['reason']}{note}")
    else:
        say("\nGate: not evaluated yet")
    if opportunity.requirement_proposals_json:
        proposals = json.loads(opportunity.requirement_proposals_json)
        stale = " (stale: superseded by closure, a newer version, or a" \
                " policy/profile change)" if result_is_stale(proposals) else ""
        say(f"\nRequirement proposals{stale}"
            f" (coverage {proposals.get('coverage_bp', 0)}bp):")
        for requirement in proposals.get("requirements", []):
            # Attributed posting quotes (OC-13): the words are the posting's.
            say(f"  - [{requirement['id']}] posting text:"
                f" \"{requirement['phrase']}\"")
    if opportunity.judged_fit_json:
        judged = json.loads(opportunity.judged_fit_json)
        stale = " (stale: superseded by closure, a newer version, or a" \
                " policy/profile change)" if result_is_stale(judged) else ""
        say(f"\nJudged fit{stale}: {judged['fit']}  {judged['reason']}")
    say("\nStaleness signals (disclosed observations, not verdicts; the"
        " underlying heuristics are untested folk knowledge):")
    say(f"  posted {signals['days_since_first_seen']} days"
        f", reposted {signals['repost_count']}x"
        f", description changed {signals['description_change_count']}x"
        f", salary {'stated' if signals['salary_stated'] else 'absent'}")


def _staleness_signals(conn, opportunity, versions) -> dict:
    """§6: per-opportunity observables versioning already produces. No score,
    no threshold, no label."""
    days = conn.execute(
        "SELECT CAST(julianday('now') - julianday(?) AS INTEGER)",
        (opportunity.first_seen,)).fetchone()[0]
    hashes = [v.description_hash for v in versions if v.description_hash]
    distinct = len(dict.fromkeys(hashes))
    current = versions[-1] if versions else None
    return {
        "days_since_first_seen": days,
        "repost_count": opportunity.reopen_count,
        "description_change_count": max(0, distinct - 1),
        "salary_stated": bool(current and current.salary_json
                              and json.loads(current.salary_json).get("salary")),
    }
