"""The discovery run orchestrator (OC-37 §2-§5): one budgeted run per
invocation, under a singleton lease, budget locked from config and recorded on
the run row, exhaustion a safe stop with everything persisted (exit 0).

Stage order is the §4 funnel: probe -> poll (snapshot commit, versioning,
closure plan) -> eligibility gate (deterministic, with dependency-epoch
re-gating) -> requirement extraction -> judged fit, the two model stages
through ModelAdapter inside the untrusted-content isolation boundary. The
proposed action defaults to IGNORE/MONITOR (OC-23); nothing here pursues.
"""

import json
from dataclasses import dataclass, field, replace

from adapters.models.claude_code import ModelCallError
from adapters.sources.base import AdapterDegradedError, OversizedFeedError
from adapters.sources.http import FetchError, RefusedHostError
from adapters.storage.sqlite_discovery import (
    SqliteDependencyEpochRepository,
    SqliteDiscoveryLease,
    SqliteDiscoveryRunRepository,
    SqliteSnapshotRepository,
    SqliteSourceRegistryRepository,
)
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteRoleFamilyRepository,
)
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_opportunities import (
    SqliteOpportunityRepository,
    SqlitePromotionQueueRepository,
)
from adapters.storage.sqlite_policies import SqliteUserPolicyRepository
from adapters.storage.sqlite_profile import SqliteUserProfileRepository
from domain.budget import Budget, BudgetLedger
from domain.closure import OpportunityState, plan_snapshot
from domain.discovery import (
    MATERIAL_FIELDS,
    Opportunity,
    OpportunityVersion,
    StoredGateVerdict,
    material_fingerprint,
)
from domain.edges import is_generation_eligible
from domain.gate import CompanyMetadata, GateContext, PostingFacts, evaluate_gate
from domain.ids import new_id
from domain.ports import ModelUnavailableError, StorageObjectExistsError
from domain.promotion import (
    PendingRow,
    coverage_priority_key,
    lane_rank,
    pre_extraction_priority_key,
    select_for_stage,
)
from domain.requirements import (
    BudgetExhausted,
    JudgedFitService,
    RequirementExtractionService,
    StageOutputError,
    build_posting_json,
    coverage_bp,
    render_judged_reason,
)
from prompts import load_prompt
from workers.discovery.probe import probe_source

CONFIG_FILENAME = "discovery.json"
LEASE_SECONDS = 3600


class LeaseLostError(RuntimeError):
    """The run lease expired or was claimed by another owner; the run stops
    before its next mutation, keeping everything already committed."""


class _AtomicConnection:
    """Connection proxy whose `with` blocks defer to an enclosing explicit
    transaction: repository code keeps its own transactional style, while one
    poll's snapshot, observations, versions, and closure effects commit (or
    roll back) together (§1/§3)."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args):
        return self._conn.execute(*args)

    def executemany(self, *args):
        return self._conn.executemany(*args)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False  # neither commits nor swallows: the outer BEGIN owns it


class _FencedTransaction:
    """BEGIN -> in-transaction owner/fence/expiry verification -> caller's
    mutations -> COMMIT; any failure (fence lost included) rolls the whole
    transition back."""

    def __init__(self, conn, lease, owner: str, fence: int):
        self._conn = conn
        self._lease = lease
        self._owner = owner
        self._fence = fence

    def __enter__(self):
        self._conn.commit()  # nothing pending crosses into this transaction
        self._conn.execute("BEGIN")
        if not self._lease.held_by(self._owner, self._fence):
            self._conn.rollback()
            raise LeaseLostError(
                "discovery lease fence lost inside a transition transaction;"
                " transition rolled back, nothing mutated")
        return _AtomicConnection(self._conn)

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        return False


@dataclass(frozen=True)
class DiscoveryConfig:
    """Locked before the run; the budget and these scheduler numbers are all
    recorded on the run row (§4)."""

    budget: Budget = field(default_factory=Budget)
    max_pages_per_poll: int = 200
    default_page_cost: int = 2
    poll_interval_days: int = 1
    probe_backoff_base_days: int = 1
    probe_backoff_cap_days: int = 30
    disabled_reprobe_days: int = 30

    def to_json(self) -> str:
        record = json.loads(self.budget.to_json())
        record.update({
            "max_pages_per_poll": self.max_pages_per_poll,
            "default_page_cost": self.default_page_cost,
            "poll_interval_days": self.poll_interval_days,
            "probe_backoff_base_days": self.probe_backoff_base_days,
            "probe_backoff_cap_days": self.probe_backoff_cap_days,
            "disabled_reprobe_days": self.disabled_reprobe_days,
        })
        return json.dumps(record, sort_keys=True)


def load_config(storage) -> DiscoveryConfig:
    """Config overrides from the instance's discovery.json (optional); unknown
    keys are refused so a typo never silently runs defaults."""
    if not storage.exists(CONFIG_FILENAME):
        return DiscoveryConfig()
    overrides = json.loads(storage.read_text(CONFIG_FILENAME))
    if not isinstance(overrides, dict):
        raise ValueError("discovery.json must be a JSON object")
    budget_fields = set(Budget.__dataclass_fields__)
    config_fields = set(DiscoveryConfig.__dataclass_fields__) - {"budget"}
    unknown = set(overrides) - budget_fields - config_fields
    if unknown:
        raise ValueError(f"unknown discovery config keys: {sorted(unknown)}")
    budget = Budget(**{k: v for k, v in overrides.items() if k in budget_fields})
    return DiscoveryConfig(budget=budget, **{
        k: v for k, v in overrides.items() if k in config_fields})


def _db_now(conn, days_ahead: int = 0) -> str:
    return conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+' || ? || ' days')",
        (days_ahead,)).fetchone()[0]


class DiscoveryRunner:
    def __init__(self, conn, storage, model, adapters, config: DiscoveryConfig,
                 say=print):
        self._conn = conn
        self._storage = storage
        self._model = model
        self._adapters = adapters
        self._config = config
        self._say = say
        self._registry = SqliteSourceRegistryRepository(conn)
        self._snapshots = SqliteSnapshotRepository(conn)
        self._opps = SqliteOpportunityRepository(conn)
        self._queue = SqlitePromotionQueueRepository(conn)
        self._runs = SqliteDiscoveryRunRepository(conn)
        self._epoch_repo = SqliteDependencyEpochRepository(conn)
        self._outcomes: dict = {}
        self._notes: list[str] = []
        self._model_available = True
        # Opportunities whose material version changed this run: their gate
        # verdict re-evaluates like an epoch bump does (§3/§5).
        self._version_changed: list[str] = []

    # ------------------------------------------------------------------ run

    def run(self):
        """One budgeted run. Returns the finished DiscoveryRun, or None when
        the singleton lease is held by another run."""
        self._lease = SqliteDiscoveryLease(self._conn)
        self._owner = new_id("dlease")
        self._fence = self._lease.acquire(self._owner, LEASE_SECONDS)
        if self._fence is None:
            self._say("another discovery run holds the lease; nothing ran")
            return None
        epoch = self._epoch_repo.current()
        ledger = BudgetLedger(self._config.budget)
        run = self._runs.start(self._config.to_json(), epoch)
        try:
            # Exhaustion at any stage stops the run there (§4): completed work
            # is persisted, and no later stage (gate or model calls included)
            # runs after an earlier stage exhausted its budget.
            for stage in (lambda: self._probe(ledger),
                          lambda: self._poll(ledger, run),
                          lambda: self._gate(ledger, epoch, run),
                          lambda: self._promote(ledger, epoch, run)):
                stage()
                if ledger.exhaustion:
                    break
            status = "budget_exhausted" if ledger.exhaustion else "completed"
            stage = ledger.exhaustion.stage if ledger.exhaustion else None
            self._runs.finish(run.id, status, ledger.spend_json(),
                              self._outcomes_json(), exhausted_stage=stage)
        except LeaseLostError as e:
            # A clean stop: everything already committed stays; nothing more
            # mutates under a lease another owner may hold.
            self._notes.append(str(e))
            self._runs.finish(run.id, "failed", ledger.spend_json(),
                              self._outcomes_json())
        except Exception:
            self._notes.append("run aborted by an unexpected error")
            self._runs.finish(run.id, "failed", ledger.spend_json(),
                              self._outcomes_json())
            raise
        finally:
            self._lease.release(self._owner)
        return self._runs.get(run.id)

    def _checkpoint(self) -> None:
        """Renew the lease and re-check owner and fence before each persistent
        transition (the package pipeline's discipline); a failed renewal
        terminates the run cleanly before the next mutation."""
        if not self._lease.renew(self._owner, self._fence, LEASE_SECONDS):
            raise LeaseLostError(
                "discovery lease lost (expired or claimed by another owner);"
                " run stopped before its next mutation")

    def _fenced(self):
        """An explicit transaction whose FIRST read re-verifies owner, fence,
        and expiry inside the transaction itself: a paused run whose lease was
        re-acquired rolls back whole and mutates nothing. Yields the proxy
        connection repositories run over."""
        return _FencedTransaction(self._conn, self._lease, self._owner,
                                  self._fence)

    def _outcomes_json(self) -> str:
        return json.dumps({"sources": self._outcomes, "notes": self._notes},
                          sort_keys=True)

    # ---------------------------------------------------------------- probe

    def _probe(self, ledger: BudgetLedger) -> None:
        """§2: verification before enablement. Deterministic priority (curated,
        manual, harvest, stable order); the cursor is the persisted per-source
        next_probe_at/attempt state, so probing resumes where it stopped."""
        now = _db_now(self._conn)
        due = [s for s in self._registry.list_all()
               if s.status in ("candidate", "disabled")
               and (s.next_probe_at is None or s.next_probe_at <= now)]
        due.sort(key=lambda s: (lane_rank(s.origin), s.next_probe_at or "", s.id))
        for source in due:
            if not ledger.try_spend("probe"):
                return
            self._checkpoint()  # lease re-checked before each probe lands
            adapter = self._adapters[source.ats_type]
            # The shared probe service (workers/discovery/probe.py): every
            # received body persists before parsing, evidence for enabled and
            # failed probes alike; the CLI enable path uses the same service.
            ok, captured = probe_source(self._storage, adapter, source.id,
                                        source.tenant_slug)
            backoff_days = min(
                self._config.probe_backoff_cap_days,
                self._config.probe_backoff_base_days * (2 ** source.probe_attempts))
            with self._fenced() as proxy:
                SqliteSourceRegistryRepository(proxy).record_probe_outcome(
                    source.id, ok,
                    next_probe_at=None if ok else _db_now(self._conn, backoff_days))
            entry = self._outcomes.setdefault(source.id, {})
            entry["probe"] = "enabled" if ok else "failed"
            if captured:
                entry["probe_raw_pages"] = list(captured)

    # ----------------------------------------------------------------- poll

    def _poll(self, ledger: BudgetLedger, run) -> None:
        """§1/§2: due enabled sources, oldest last_polled_at first within
        priority lanes; the fetch budget splits between lanes so harvest
        cannot starve curated nor vice versa, with backfill; admission by last
        known page cost; a deferred poll is recorded and touches no health."""
        now = _db_now(self._conn)
        due = [s for s in self._registry.list_all()
               if s.status == "enabled"
               and (s.next_poll_at is None or s.next_poll_at <= now)]
        lanes: dict[int, list] = {0: [], 1: [], 2: []}
        for source in due:
            lanes[lane_rank(source.origin)].append(source)
        for lane in lanes.values():
            lane.sort(key=lambda s: (s.last_polled_at or "", s.id))
        max_fetches = self._config.budget.max_fetches
        share = max_fetches // 3
        quotas = {0: max_fetches - 2 * share, 1: share, 2: share}
        spent = {0: 0, 1: 0, 2: 0}
        deferred: list = []
        for lane_index in (0, 1, 2):  # first pass: within lane quotas
            for source in lanes[lane_index]:
                cost = self._estimated_cost(source)
                if spent[lane_index] + cost > quotas[lane_index] \
                        or ledger.spent("fetch") + cost > max_fetches:
                    deferred.append(source)
                    continue
                spent[lane_index] += self._poll_source(source, ledger, run)
        still_deferred = []
        for source in deferred:  # backfill: leftover budget, lane-free
            cost = self._estimated_cost(source)
            if ledger.spent("fetch") + cost > max_fetches:
                still_deferred.append(source)
                continue
            self._poll_source(source, ledger, run)
        for source in still_deferred:
            with self._fenced() as proxy:
                SqliteSourceRegistryRepository(proxy).record_poll_outcome(
                    source.id, "deferred")
            self._outcomes.setdefault(source.id, {})["poll"] = "deferred"
        if still_deferred:
            # A due source refused admission for budget IS fetch exhaustion:
            # the run records the stage and counts and finishes
            # budget_exhausted, with the deferrals kept (§4).
            ledger.note_exhausted("fetch")

    def _estimated_cost(self, source) -> int:
        latest = self._snapshots.latest_for_source(source.id)
        if latest is None:
            return self._config.default_page_cost
        return max(1, json.loads(latest.completion_json).get("pages", 1))

    def _poll_source(self, source, ledger: BudgetLedger, run) -> int:
        """One admitted poll: fetch all pages (an in-flight poll may finish
        past the run cap, §1), commit the snapshot atomically, mint and
        version opportunities, apply the closure plan. Returns pages spent."""
        self._checkpoint()  # lease re-checked before this poll's mutations
        adapter = self._adapters[source.ats_type]
        outcome = self._outcomes.setdefault(source.id, {})
        fetcher = getattr(adapter, "_fetcher", None)
        requests_before = fetcher.request_count if fetcher is not None else 0

        charged = 0

        def spend_requests() -> int:
            """Charge spend at the request boundary for every outcome: a
            failed or oversized poll consumed real requests; an admitted
            in-flight poll may still finish past the run cap (§1). Idempotent
            per poll: only requests not yet charged are charged, so a failure
            after a successful fetch never double-charges."""
            nonlocal charged
            total = (fetcher.request_count - requests_before) \
                if fetcher is not None else 0
            for _ in range(total - charged):
                ledger.try_spend("fetch")
            charged = total
            return total

        # Raw capture precedes parsing (§1): each page's raw bytes stream to
        # durable storage as fetched, BEFORE JSON decoding or adapter
        # validation, so a later failure still leaves replayable evidence.
        attempt_id = new_id("att")
        captured_locators: list[str] = []  # every received body, any status
        page_locators: list[str] = []  # the 2xx feed pages only

        def capture(body: bytes, status) -> None:
            locator = (f"discovery/raw/{source.id}/{attempt_id}"
                       f"/response-{len(captured_locators) + 1:04d}.json")
            self._storage.write_bytes_new(locator, body)
            captured_locators.append(locator)
            if status is not None and 200 <= status < 300:
                # Only real pages enter the snapshot manifest; an error body
                # is preserved evidence, never a replayable feed page.
                page_locators.append(locator)

        def degrade(error) -> int:
            spent = spend_requests()
            with self._fenced() as proxy:
                SqliteSourceRegistryRepository(proxy).record_poll_outcome(
                    source.id, "degraded",
                    next_poll_at=_db_now(self._conn, self._config.poll_interval_days),
                    rot_threshold=self._config.budget.rot_threshold,
                    next_probe_at=_db_now(self._conn,
                                          self._config.disabled_reprobe_days))
            outcome["poll"] = "degraded"
            outcome["poll_error"] = str(error)
            outcome["requests"] = spent
            if captured_locators:
                # Auditable and replayable: the degraded record references
                # every preserved raw body while committing no snapshot.
                outcome["raw_pages"] = list(captured_locators)
            return spent

        if fetcher is not None:
            fetcher.capture = capture
        try:
            payload = adapter.poll(source.tenant_slug)
        except OversizedFeedError as e:
            spent = spend_requests()
            with self._fenced() as proxy:
                SqliteSourceRegistryRepository(proxy).record_poll_outcome(
                    source.id, "oversized")
            outcome["poll"] = "oversized"
            outcome["requests"] = spent
            if captured_locators:
                outcome["raw_pages"] = list(captured_locators)
            self._notes.append(str(e))
            return spent
        except (AdapterDegradedError, FetchError, RefusedHostError,
                ValueError) as e:
            # A refused host (e.g. a redirect to an unlisted target) or a
            # malformed adapter URL degrades THIS source with the refusal
            # reason preserved; the run continues with other sources and
            # queued work, never failing run-level.
            return degrade(e)
        finally:
            if fetcher is not None:
                fetcher.capture = None
        requests_spent = spend_requests()

        # Full validation and normalization BEFORE anything commits (§1): a
        # single shape-invalid job degrades the source and commits nothing.
        try:
            prepared = []
            for raw in payload.jobs:
                fields = adapter.material_fields(raw)
                _validate_material_fields(fields)
                prepared.append((adapter.external_id(raw), fields,
                                 material_fingerprint(fields)))
        except (AdapterDegradedError, TypeError, ValueError) as e:
            return degrade(AdapterDegradedError(
                f"posting failed material-field validation: {e}"))

        # The committed snapshot references the SAME stored raw objects the
        # fetch layer captured (no double write): the manifest lists them.
        if page_locators:
            raw_locator = f"discovery/raw/{source.id}/{attempt_id}/manifest.json"
            self._storage.write_text_new(raw_locator, json.dumps(
                {"page_locators": page_locators}))
        else:
            # Adapters over transports without the capture seam (tests) fall
            # back to storing the combined parsed payload content-addressed.
            raw_locator = (f"discovery/snapshots/{source.id}"
                           f"/{payload.content_hash}.json")
            try:
                self._storage.write_text_new(raw_locator, payload.raw_text)
            except StorageObjectExistsError:
                pass  # content-addressed: identical payload already stored

        # One fenced transaction for the snapshot, observations, versions, and
        # closure effects: repositories run over a proxy whose `with` blocks
        # defer to this explicit BEGIN, the lease fence is re-verified INSIDE
        # the transaction, and it all lands or rolls back whole.
        self._checkpoint()  # the poll may have outlived the lease; re-check
        with self._fenced() as proxy:
            counts = self._apply_poll(
                source, payload, prepared, raw_locator, run,
                SqliteSnapshotRepository(proxy),
                SqliteOpportunityRepository(proxy),
                SqlitePromotionQueueRepository(proxy),
                SqliteSourceRegistryRepository(proxy))
        outcome["poll"] = "success"
        outcome.update(counts)
        outcome["requests"] = requests_spent
        return requests_spent

    def _apply_poll(self, source, payload, prepared, raw_locator, run,
                    snapshots, opps, queue, registry) -> dict:
        """All DB effects of one validated poll; runs inside the caller's
        explicit transaction."""
        snapshot = snapshots.commit(
            source.id, raw_locator, payload.content_hash,
            payload.completion_json(), len(prepared),
            remote_version_token=payload.remote_version_token, run_id=run.id)
        now = _db_now(self._conn)
        present_ids: set[str] = set()
        fields_by_opp: dict[str, tuple[dict, str]] = {}
        new_count = versioned_count = 0
        for external_id, fields, fingerprint in prepared:
            existing = opps.get_by_key(source.id, external_id)
            if existing is None:
                opportunity = Opportunity(
                    id=new_id("opp"), source_id=source.id,
                    external_job_id=external_id, first_seen=now, last_seen=now,
                    apply_support=self._adapters[source.ats_type].apply_support())
                opps.add(opportunity)
                self._add_version(opps, opportunity.id, 1, snapshot.id,
                                  fingerprint, fields)
                present_ids.add(opportunity.id)
                new_count += 1
                continue
            present_ids.add(existing.id)
            fields_by_opp[existing.id] = (fields, fingerprint)
            if existing.availability == "closed":
                continue  # a reopen re-versions after the closure plan below
            current = self._current_version(existing, opps)
            if current is None or current.fingerprint != fingerprint:
                next_number = (current.version + 1) if current else 1
                version = self._add_version(opps, existing.id, next_number,
                                            snapshot.id, fingerprint, fields)
                # §4: unfinished rows for older versions cancel atomically;
                # derived results stay pinned to their version (stale by pin).
                queue.supersede_for_opportunity(
                    existing.id, version.id, "material version change")
                if existing.backlog_state == "gated":
                    self._version_changed.append(existing.id)
                versioned_count += 1

        known = opps.list_for_source(source.id)
        states = [OpportunityState(o.id, o.availability, o.absence_streak,
                                   o.last_absence_snapshot_id) for o in known]
        plan = plan_snapshot(
            snapshot.id, present_ids, states,
            pending_cohort=opps.pending_cohort_for_source(source.id),
            authoritative=payload.remote_version_token is not None,
            guard_percent=self._config.budget.mass_closure_guard_percent,
            guard_min=self._config.budget.mass_closure_guard_min)
        opps.apply_closure_plan(plan, now)
        closed_ids = [opp_id for opp_id, _ in plan.closures] + list(plan.cohort_closed)
        for opp_id in closed_ids:
            queue.supersede_for_opportunity(opp_id, None, "opportunity closed")
        # §3: a reopen appends a version even when material fields are
        # unchanged, so prior derived results (pinned to the old version)
        # never count as current, and the opportunity re-gates and re-enqueues.
        for opp_id in plan.reopened:
            fields, fingerprint = fields_by_opp[opp_id]
            current = self._current_version(opps.get(opp_id), opps)
            version = self._add_version(opps, opp_id, current.version + 1,
                                        snapshot.id, fingerprint, fields)
            queue.supersede_for_opportunity(opp_id, version.id, "reopened")
            self._version_changed.append(opp_id)
        self._notes.extend(plan.notes)

        registry.record_poll_outcome(
            source.id, "success",
            next_poll_at=_db_now(self._conn, self._config.poll_interval_days))
        return {"pages": payload.page_count, "postings": len(prepared),
                "new": new_count, "changed": versioned_count,
                "closed": len(closed_ids), "reopened": len(plan.reopened)}

    def _add_version(self, repo, opportunity_id: str, number: int,
                     snapshot_id: str, fingerprint: str,
                     fields: dict) -> OpportunityVersion:
        version = OpportunityVersion(
            id=new_id("oppv"), opportunity_id=opportunity_id, version=number,
            snapshot_id=snapshot_id, fingerprint=fingerprint,
            **{k: fields.get(k) for k in MATERIAL_FIELDS})
        repo.add_version(version)
        return version

    def _current_version(self, opportunity, repo=None) -> OpportunityVersion | None:
        versions = (repo or self._opps).list_versions(opportunity.id)
        return versions[-1] if versions else None

    # ----------------------------------------------------------------- gate

    def _gate(self, ledger: BudgetLedger, epoch: int, run) -> None:
        """§5: deterministic gate over the promotion cap, half reserved for
        dependency-epoch re-gates (oldest verdicts first), half for the new
        backlog in priority order, backfilled both ways."""
        with self._fenced() as proxy:
            SqlitePromotionQueueRepository(proxy).supersede_stale_epochs(
                epoch, "dependency epoch advanced")
        context = self._gate_context()
        sources = {s.id: s for s in self._registry.list_all()}

        regates = self._opps.list_open_with_stale_gate(epoch)
        seen = {o.id for o in regates}
        for opp_id in self._version_changed:  # §3: material change re-gates
            if opp_id not in seen:
                regates.append(self._opps.get(opp_id))
                seen.add(opp_id)
        backlog = self._opps.list_backlog_pending()
        backlog.sort(key=lambda o: (
            lane_rank(sources[o.source_id].origin),
            _descending_text(o.first_seen), o.id))
        # Order: the aging half (re-gates, oldest verdicts first), then the
        # new backlog in priority order, then remaining re-gates. The ledger
        # is the cap: the first refusal past it records the run's exhaustion.
        regate_quota = self._config.budget.max_new_opportunities_gated // 2
        chosen: list[tuple[str, object]] = \
            [("regate", o) for o in regates[:regate_quota]]
        chosen += [("new", o) for o in backlog]
        chosen += [("regate", o) for o in regates[regate_quota:]]

        for kind, opportunity in chosen:
            if not ledger.try_spend("gate"):
                return
            self._checkpoint()  # lease re-checked before each verdict lands
            source = sources[opportunity.source_id]
            # Backlog promotion, gate verdict, proposed action, and enqueue
            # land as ONE fenced transaction: a crash cannot strand a gated
            # opportunity between verdict and queue row.
            with self._fenced() as proxy:
                opps = SqliteOpportunityRepository(proxy)
                queue = SqlitePromotionQueueRepository(proxy)
                if kind == "new":
                    version = opps.promote_from_backlog(opportunity.id)
                    # None: discarded with its recorded reason (§4), which
                    # itself belongs in this transaction.
                else:
                    current = opps.get(opportunity.id)
                    version = None \
                        if (current.availability == "closed"
                            or current.current_version_id is None) \
                        else self._current_version(current, opps)
                if version is not None:
                    self._gate_one(opps, queue, opportunity.id, version,
                                   source, context, epoch)

    def _gate_one(self, opps, queue, opportunity_id: str,
                  version: OpportunityVersion, source, context: GateContext,
                  epoch: int) -> None:
        """One gate evaluation's writes, on the caller's transactional repos."""
        facts = _posting_facts(version)
        result = evaluate_gate(facts, replace(context, company=CompanyMetadata(
            industry=source.industry, company_stage=source.company_stage,
            company_size_band=source.company_size_band)))
        opps.record_gate_verdict(StoredGateVerdict(
            id=new_id("gate"), opportunity_id=opportunity_id,
            version_id=version.id, epoch=epoch, verdict=result.verdict,
            dimensions_json=json.dumps([{
                "dimension": d.dimension, "verdict": d.verdict,
                "reason": d.reason, "note": d.note,
            } for d in result.dimensions])))
        if result.verdict == "pass":
            # OC-23: the proposal defaults to MONITOR; nothing here pursues.
            opps.set_proposed_action(opportunity_id, "monitor",
                                     version_id=version.id, epoch=epoch)
            queue.enqueue(
                opportunity_id, version.id, lane_rank(source.origin),
                opps.get(opportunity_id).first_seen, epoch)
        else:
            opps.set_proposed_action(opportunity_id, "ignore",
                                     version_id=version.id, epoch=epoch)

    def _gate_context(self) -> GateContext:
        policies = SqliteUserPolicyRepository(self._conn).get_policies()
        fields = SqliteUserProfileRepository(self._conn).get_fields()
        families = SqliteRoleFamilyRepository(self._conn).list_all()
        return GateContext(
            policies=policies,
            residence_country=fields.get("country") or fields.get("location"),
            active_family_target_seniorities=tuple(
                f.target_seniority for f in families if f.status == "active"))

    # ------------------------------------------------- extraction + judgment

    def _promote(self, ledger: BudgetLedger, epoch: int, run) -> None:
        run_seq = run.run_seq
        capability_names = self._eligible_capability_names()
        self._extract(ledger, epoch, run_seq, capability_names)
        if ledger.exhaustion:
            # §4: exhaustion at any substage stops the funnel there; judgment
            # never runs after an extraction-stage exhaustion.
            return
        self._judge(ledger, epoch, run_seq, capability_names)

    def _eligible_capability_names(self) -> list[str]:
        """Capability names with at least one generation-eligible SUPPORTS
        edge (OC-31 discipline): the deterministic coverage vocabulary."""
        edges = SqliteCareerEdgeRepository(self._conn)
        names = []
        for capability in SqliteCapabilityRepository(self._conn).list_all():
            eligible = [e for e in edges.active_edges_to(
                "capability", capability.id, "SUPPORTS")
                if is_generation_eligible(e)]
            if eligible:
                names.append(capability.name)
        return names

    def _extract(self, ledger: BudgetLedger, epoch: int, run_seq: int,
                 capability_names: list[str]) -> None:
        if not self._model_available:
            return
        service = RequirementExtractionService(
            self._model, load_prompt("requirement_extraction.md"))
        pending = self._queue.pending_for_stage("pending_extraction", run_seq)
        rows = [PendingRow(r.id, r.enqueue_seq, pre_extraction_priority_key(
            r.lane_rank, r.first_seen, r.opportunity_id)) for r in pending]
        for row_id in _staged_order(self._config.budget.max_extraction_calls, rows):
            self._checkpoint()  # lease re-checked before each claim/transition
            claimed = self._claim_row(row_id, epoch)
            if claimed is None:
                continue  # released as superseded, never worked (§4)
            try:
                posting_json, _ = self._posting_payload(claimed)
                # Every model call, the schema retry included, is charged
                # against the budget before it is made.
                requirements = service.extract(
                    posting_json,
                    charge=lambda: ledger.try_spend("extraction"))
            except BudgetExhausted:
                return  # exhaustion recorded; the row stays claimable next run
            except ModelUnavailableError as e:
                self._model_available = False
                self._notes.append(f"model unavailable; model stages stopped: {e}")
                self._release_failed(row_id, "model unavailable", run_seq)
                return
            except (ModelCallError, StageOutputError, ValueError) as e:
                self._release_failed(row_id, _neutral_reason(e), run_seq)
                continue
            self._checkpoint()  # the model call may have outlived the lease
            coverage = coverage_bp(requirements, capability_names)
            # The proposal write and the extracted transition land as one
            # fenced transaction: a crash cannot persist a result the queue
            # does not know about (which would cost a second charged call).
            with self._fenced() as proxy:
                SqliteOpportunityRepository(proxy).set_requirement_proposals(
                    claimed.opportunity_id, json.dumps({
                        "version_id": claimed.version_id,
                        "epoch": claimed.epoch,
                        "requirements": [
                            {"id": f"r{i + 1}", "phrase": phrase}
                            for i, phrase in enumerate(requirements)],
                        "coverage_bp": coverage,
                    }, ensure_ascii=False))
                SqlitePromotionQueueRepository(proxy).transition(
                    row_id, "extracted", coverage_bp=coverage)

    def _judge(self, ledger: BudgetLedger, epoch: int, run_seq: int,
               capability_names: list[str]) -> None:
        if not self._model_available:
            return
        service = JudgedFitService(self._model, load_prompt("judged_fit.md"))
        # Crash recovery: a pending_judgment row with no committed result can
        # only exist from an interrupted older run (the result write is
        # atomic with the transitions), so it is claimable like an extracted
        # row rather than invisible.
        extracted = (self._queue.pending_for_stage("extracted", run_seq)
                     + self._queue.pending_for_stage("pending_judgment", run_seq))
        if not extracted:
            return
        cold_start = all((r.coverage_bp or 0) == 0 for r in extracted)
        if cold_start:
            # §5 cold start: coverage is uniformly zero, so the judged-fit
            # order falls back to the stated deterministic order (curated
            # lane first, then first_seen recency); recorded on the run.
            self._notes.append("cold_start_fallback: judged-fit order used the"
                               " deterministic lane/recency fallback")
            rows = [PendingRow(r.id, r.enqueue_seq, pre_extraction_priority_key(
                r.lane_rank, r.first_seen, r.opportunity_id)) for r in extracted]
        else:
            rows = [PendingRow(r.id, r.enqueue_seq, coverage_priority_key(
                r.coverage_bp or 0, r.enqueue_seq)) for r in extracted]
        families = [f for f in SqliteRoleFamilyRepository(self._conn).list_all()
                    if f.status == "active"]
        candidate = {
            "capabilities": capability_names,
            "role_families": [{"name": f.name, "target_seniority": f.target_seniority}
                              for f in families],
        }
        for row_id in _staged_order(self._config.budget.judged_fit_k, rows):
            self._checkpoint()  # lease re-checked before each claim/transition
            claimed = self._claim_row(row_id, epoch)
            if claimed is None:
                continue
            try:
                posting_json, proposals = self._posting_payload(claimed)
                requirements = tuple(proposals.get("requirements", []))
                # Charged per call, retry included; the row transitions only
                # after a judged result exists, so a stop leaves it claimable.
                judged = service.judge(
                    posting_json, requirements,
                    dict(candidate, coverage_bp=claimed.coverage_bp or 0),
                    charge=lambda: ledger.try_spend("judgment"))
            except BudgetExhausted:
                return  # exhaustion recorded; the row stays extracted
            except ModelUnavailableError as e:
                self._model_available = False
                self._notes.append(f"model unavailable; model stages stopped: {e}")
                self._release_failed(row_id, "model unavailable", run_seq)
                return
            except (ModelCallError, StageOutputError, ValueError) as e:
                self._release_failed(row_id, _neutral_reason(e), run_seq)
                continue
            self._checkpoint()  # the model call may have outlived the lease
            # The displayed reason is rendered in code from the stored
            # phrases behind the validated ids: no model prose persists.
            phrases_by_id = {r["id"]: r["phrase"] for r in requirements}
            reason = render_judged_reason(judged, phrases_by_id)
            # The result write and the state transitions land as one fenced
            # transaction: no externally visible intermediate, no commit under
            # a re-acquired lease, and a crash cannot strand a result-less
            # pending_judgment row.
            with self._fenced() as proxy:
                atomic_queue = SqlitePromotionQueueRepository(proxy)
                if claimed.state == "extracted":
                    atomic_queue.transition(row_id, "pending_judgment")
                SqliteOpportunityRepository(proxy).set_judged_fit(
                    claimed.opportunity_id, json.dumps({
                        "version_id": claimed.version_id,
                        "epoch": claimed.epoch,
                        "fit": judged.fit,
                        "matched_requirement_ids":
                            list(judged.matched_requirement_ids),
                        "gap_requirement_ids": list(judged.gap_requirement_ids),
                        "reason": reason,
                        "coverage_bp": claimed.coverage_bp or 0,
                    }, ensure_ascii=False))
                atomic_queue.transition(row_id, "judged")

    def _claim_row(self, row_id: str, epoch: int):
        """Exclusive fenced claim (§4): the durable claimed marker lands by
        one conditional update BEFORE any model call, so a second runner's
        claim on the same row fails and never spends a model call; a claim
        left by a no-longer-live fence is recoverable."""
        with self._fenced() as proxy:
            return SqlitePromotionQueueRepository(proxy).claim(
                row_id, epoch, owner_token=self._owner, fence=self._fence)

    def _release_failed(self, row_id: str, reason: str, run_seq: int) -> None:
        """Fenced release: the retry bookkeeping is a persistent transition
        too. reason must already be neutral (never model text)."""
        with self._fenced() as proxy:
            SqlitePromotionQueueRepository(proxy).release_failed(
                row_id, reason, run_seq)

    def _posting_payload(self, queue_row) -> tuple[str, dict]:
        """Replay the pinned version's posting from its committed snapshot's
        raw payload (§1: raw responses are the replayable input) and build the
        isolation payload. Also returns the stored requirement proposals."""
        versions = self._opps.list_versions(queue_row.opportunity_id)
        version = next(v for v in versions if v.id == queue_row.version_id)
        opportunity = self._opps.get(queue_row.opportunity_id)
        source = self._registry.get(opportunity.source_id)
        adapter = self._adapters[source.ats_type]
        snapshot = self._snapshots.get(version.snapshot_id)
        stored = json.loads(self._storage.read_text(snapshot.raw_locator))
        if "page_locators" in stored:
            # Manifest form: the snapshot references the raw page objects the
            # fetch layer captured (§1); each page decodes at replay time.
            pages = [json.loads(self._storage.read_text(locator))
                     for locator in stored["page_locators"]]
        else:
            pages = stored["pages"]
        raw_job = next(
            (r for r in adapter.jobs_from_pages(pages)
             if adapter.external_id(r) == opportunity.external_job_id), None)
        if raw_job is None:
            # The external id is fetched data: attributed quoted value.
            raise ValueError(
                f'posting-supplied external id "{opportunity.external_job_id}"'
                f" not found in its own snapshot {snapshot.id}; raw payload"
                " inconsistent")
        normalized = adapter.normalize(raw_job)
        salary_view = json.loads(version.salary_json)["salary"] \
            if version.salary_json else None
        posting_json = build_posting_json(
            title=version.title, description=normalized.get("description"),
            locations=json.loads(version.location_json).get("locations", [])
            if version.location_json else [],
            salary=salary_view)
        proposals = json.loads(opportunity.requirement_proposals_json or "{}")
        if proposals.get("version_id") != version.id or proposals.get("stale") \
                or proposals.get("epoch") != queue_row.epoch:
            # Stale by pin, closure, or epoch: never silently reused (§4/§5).
            proposals = {}
        return posting_json, proposals


def _posting_facts(version: OpportunityVersion) -> PostingFacts:
    location = json.loads(version.location_json) if version.location_json else {}
    remote = json.loads(version.remote_policy_json) if version.remote_policy_json else {}
    salary = json.loads(version.salary_json)["salary"] if version.salary_json else None
    restriction = remote.get("restriction_countries")
    return PostingFacts(
        locations=tuple(location.get("locations") or ()),
        remote_mode=remote.get("mode"),
        remote_restriction_countries=tuple(restriction) if restriction else None,
        timezone_requirement=None,  # no vendor states one structurally (§5)
        salary=salary,
        seniority=version.seniority,
    )


def _descending_text(text: str) -> tuple:
    return tuple(-byte for byte in text.encode())


def _neutral_reason(error) -> str:
    """Stored failure reasons never reproduce rejected model output or model
    text: StageOutputError messages are neutral by construction (they name
    the violated rule, never the text); anything else is categorized."""
    if isinstance(error, StageOutputError):
        return f"output failed validation: {error}"
    if isinstance(error, ModelCallError):
        return "model call failed operationally"
    return f"stage failed: {error}"


def _validate_material_fields(fields: dict) -> None:
    """Every persisted material field is type-checked before anything commits
    (§1): each is a string or None (the JSON fields arrive pre-serialized)."""
    for key in MATERIAL_FIELDS:
        value = fields.get(key)
        if value is not None and not isinstance(value, str):
            raise AdapterDegradedError(
                f"material field '{key}' is {type(value).__name__}, not a string")


def _staged_order(cap: int, rows: list[PendingRow]) -> list[str]:
    """The §4 half-cap aging order for the whole candidate set: the capped
    selection first, overflow after it in priority order. The BudgetLedger is
    the cap; iterating past it is what records the run's exhaustion honestly
    instead of silently truncating."""
    selected = select_for_stage(cap, rows)
    chosen = set(selected)
    overflow = sorted((r for r in rows if r.row_id not in chosen),
                      key=lambda r: (r.priority_key, r.enqueue_seq))
    return selected + [r.row_id for r in overflow]


def run_discovery(conn, storage, model, adapters, config: DiscoveryConfig | None = None,
                  say=print):
    config = config or load_config(storage)
    return DiscoveryRunner(conn, storage, model, adapters, config, say=say).run()
