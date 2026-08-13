"""Discovery storage, opportunity side (migration 0006): opportunities with
their four separated state fields, append-only versions, gate verdicts,
suspect cohorts, the observed-ungated backlog, and the version-pinned
promotion queue. Closure plans and queue claims apply transactionally."""

import json
import sqlite3

from domain.closure import ClosurePlan, PendingCohort
from domain.discovery import (
    Opportunity,
    OpportunityVersion,
    QueueRow,
    StoredGateVerdict,
    SuspectCohort,
)
from domain.ids import new_id
from domain.ports import OpportunityRepository, PromotionQueueRepository
from domain.promotion import retry_outcome

_OPP_COLUMNS = (
    "id, source_id, external_job_id, first_seen, last_seen, apply_support,"
    " availability, absence_streak, last_absence_snapshot_id, closed_observed_at,"
    " closing_snapshot_ids_json, reopen_count, current_version_id,"
    " latest_gate_verdict_id, proposed_action, proposed_action_version_id,"
    " proposed_action_epoch, human_action, backlog_state,"
    " backlog_discard_reason, requirement_proposals_json, judged_fit_json,"
    " created_at, updated_at"
)

_VERSION_COLUMNS = (
    "id, opportunity_id, version, snapshot_id, fingerprint, title, seniority,"
    " work_authorization_json, language_requirements_json, description_hash,"
    " location_json, remote_policy_json, salary_json, apply_url, created_at"
)

_TOUCH = "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"


class SqliteOpportunityRepository(OpportunityRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(self, opportunity: Opportunity) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO opportunities (id, source_id, external_job_id,"
                " first_seen, last_seen, apply_support, availability,"
                " backlog_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (opportunity.id, opportunity.source_id, opportunity.external_job_id,
                 opportunity.first_seen, opportunity.last_seen,
                 opportunity.apply_support, opportunity.availability,
                 opportunity.backlog_state))

    def get(self, opportunity_id: str) -> Opportunity | None:
        row = self._conn.execute(
            f"SELECT {_OPP_COLUMNS} FROM opportunities WHERE id = ?",
            (opportunity_id,)).fetchone()
        return Opportunity(*row) if row else None

    def get_by_key(self, source_id: str, external_job_id: str) -> Opportunity | None:
        row = self._conn.execute(
            f"SELECT {_OPP_COLUMNS} FROM opportunities WHERE source_id = ?"
            " AND external_job_id = ?", (source_id, external_job_id)).fetchone()
        return Opportunity(*row) if row else None

    def list_for_source(self, source_id: str) -> list[Opportunity]:
        rows = self._conn.execute(
            f"SELECT {_OPP_COLUMNS} FROM opportunities WHERE source_id = ?"
            " ORDER BY id", (source_id,)).fetchall()
        return [Opportunity(*r) for r in rows]

    def add_version(self, version: OpportunityVersion) -> None:
        with self._conn:  # append and current pointer land together
            self._conn.execute(
                "INSERT INTO opportunity_versions (id, opportunity_id, version,"
                " snapshot_id, fingerprint, title, seniority,"
                " work_authorization_json, language_requirements_json,"
                " description_hash, location_json, remote_policy_json,"
                " salary_json, apply_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (version.id, version.opportunity_id, version.version,
                 version.snapshot_id, version.fingerprint, version.title,
                 version.seniority, version.work_authorization_json,
                 version.language_requirements_json, version.description_hash,
                 version.location_json, version.remote_policy_json,
                 version.salary_json, version.apply_url))
            self._conn.execute(
                f"UPDATE opportunities SET current_version_id = ?, {_TOUCH}"
                " WHERE id = ?", (version.id, version.opportunity_id))

    def list_versions(self, opportunity_id: str) -> list[OpportunityVersion]:
        rows = self._conn.execute(
            f"SELECT {_VERSION_COLUMNS} FROM opportunity_versions"
            " WHERE opportunity_id = ? ORDER BY version", (opportunity_id,)).fetchall()
        return [OpportunityVersion(*r) for r in rows]

    def apply_closure_plan(self, plan: ClosurePlan, observed_at: str) -> None:
        """One transaction for one committed snapshot's effects (§3). Never
        touches proposed_action or human_action: a closure while a
        human-selected pursuit is active surfaces the conflict, it does not
        delete the selection."""
        with self._conn:
            for opp_id in plan.present:
                self._conn.execute(
                    "UPDATE opportunities SET absence_streak = 0,"
                    f" last_absence_snapshot_id = NULL, last_seen = ?, {_TOUCH}"
                    " WHERE id = ?", (observed_at, opp_id))
            for opp_id in plan.reopened:
                self._conn.execute(
                    "UPDATE opportunities SET availability = 'reopened',"
                    " absence_streak = 0, last_absence_snapshot_id = NULL,"
                    " reopen_count = reopen_count + 1,"
                    f" last_seen = ?, {_TOUCH} WHERE id = ?", (observed_at, opp_id))
            for opp_id, new_streak in plan.absences:
                self._conn.execute(
                    "UPDATE opportunities SET absence_streak = ?,"
                    " last_absence_snapshot_id = COALESCE(last_absence_snapshot_id, ?),"
                    f" {_TOUCH} WHERE id = ?", (new_streak, plan.snapshot_id, opp_id))
            for opp_id, confirming in plan.closures:
                self._close(opp_id, confirming, observed_at)
            if plan.resolved_cohort_id is not None:
                cohort = self._conn.execute(
                    "SELECT triggering_snapshot_id FROM suspect_cohorts WHERE id = ?",
                    (plan.resolved_cohort_id,)).fetchone()
                for opp_id in plan.cohort_closed:
                    self._conn.execute(
                        "UPDATE suspect_cohort_members SET outcome = 'closed'"
                        " WHERE cohort_id = ? AND opportunity_id = ?",
                        (plan.resolved_cohort_id, opp_id))
                    # Row-level confirmation: the cohort's triggering snapshot
                    # plus this one are the confirming pair.
                    self._close(opp_id, (cohort[0], plan.snapshot_id), observed_at)
                for opp_id in plan.cohort_reappeared:
                    self._conn.execute(
                        "UPDATE suspect_cohort_members SET outcome = 'reappeared'"
                        " WHERE cohort_id = ? AND opportunity_id = ?",
                        (plan.resolved_cohort_id, opp_id))
                    self._conn.execute(
                        "UPDATE opportunities SET absence_streak = 0,"
                        f" last_absence_snapshot_id = NULL, last_seen = ?, {_TOUCH}"
                        " WHERE id = ?", (observed_at, opp_id))
                self._conn.execute(
                    "UPDATE suspect_cohorts SET resolved = 1 WHERE id = ?",
                    (plan.resolved_cohort_id,))
            if plan.new_cohort_member_ids:
                cohort_id = new_id("coh")
                source_id = self._conn.execute(
                    "SELECT source_id FROM snapshots WHERE id = ?",
                    (plan.snapshot_id,)).fetchone()[0]
                self._conn.execute(
                    "INSERT INTO suspect_cohorts (id, source_id, triggering_snapshot_id)"
                    " VALUES (?, ?, ?)", (cohort_id, source_id, plan.snapshot_id))
                self._conn.executemany(
                    "INSERT INTO suspect_cohort_members (cohort_id, opportunity_id)"
                    " VALUES (?, ?)",
                    [(cohort_id, opp_id) for opp_id in plan.new_cohort_member_ids])

    def _close(self, opp_id: str, confirming: tuple[str, ...], observed_at: str) -> None:
        # Closure marks derived results stale in the same statement (§3/§4):
        # proposals and judged fit carry stale=true and never count as current
        # again until a fresh evaluation replaces them after a reopen.
        stale = ("CASE WHEN {col} IS NULL THEN NULL"
                 " ELSE json_set({col}, '$.stale', json('true')) END")
        self._conn.execute(
            "UPDATE opportunities SET availability = 'closed', absence_streak = 0,"
            " last_absence_snapshot_id = NULL, closed_observed_at = ?,"
            " closing_snapshot_ids_json = ?,"
            f" requirement_proposals_json = {stale.format(col='requirement_proposals_json')},"
            f" judged_fit_json = {stale.format(col='judged_fit_json')},"
            f" {_TOUCH} WHERE id = ?",
            (observed_at, json.dumps(list(confirming)), opp_id))

    def pending_cohort_for_source(self, source_id: str) -> PendingCohort | None:
        row = self._conn.execute(
            "SELECT id, triggering_snapshot_id FROM suspect_cohorts"
            " WHERE source_id = ? AND resolved = 0", (source_id,)).fetchone()
        if not row:
            return None
        members = self._conn.execute(
            "SELECT opportunity_id FROM suspect_cohort_members"
            " WHERE cohort_id = ? AND outcome = 'pending'", (row[0],)).fetchall()
        return PendingCohort(
            cohort_id=row[0], triggering_snapshot_id=row[1],
            pending_member_ids=frozenset(m[0] for m in members))

    def list_cohorts(self, source_id: str) -> list[SuspectCohort]:
        rows = self._conn.execute(
            "SELECT id, source_id, triggering_snapshot_id, resolved, created_at"
            " FROM suspect_cohorts WHERE source_id = ? ORDER BY created_at, id",
            (source_id,)).fetchall()
        return [SuspectCohort(*r) for r in rows]

    def record_gate_verdict(self, verdict: StoredGateVerdict) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO gate_verdicts (id, opportunity_id, version_id, epoch,"
                " verdict, dimensions_json) VALUES (?, ?, ?, ?, ?, ?)",
                (verdict.id, verdict.opportunity_id, verdict.version_id,
                 verdict.epoch, verdict.verdict, verdict.dimensions_json))
            self._conn.execute(
                f"UPDATE opportunities SET latest_gate_verdict_id = ?, {_TOUCH}"
                " WHERE id = ?", (verdict.id, verdict.opportunity_id))

    def get_gate_verdict(self, verdict_id: str) -> StoredGateVerdict | None:
        row = self._conn.execute(
            "SELECT id, opportunity_id, version_id, epoch, verdict,"
            " dimensions_json, created_at FROM gate_verdicts WHERE id = ?",
            (verdict_id,)).fetchone()
        return StoredGateVerdict(*row) if row else None

    def set_proposed_action(self, opportunity_id: str, action: str,
                            version_id: str | None = None,
                            epoch: int | None = None) -> None:
        """The proposal lands with its version and epoch pin, so readers can
        tell a current proposal from one derived under superseded inputs."""
        if action not in ("ignore", "monitor", "pursue"):
            raise ValueError(f"unknown proposed action '{action}'")
        with self._conn:
            self._conn.execute(
                "UPDATE opportunities SET proposed_action = ?,"
                " proposed_action_version_id = ?, proposed_action_epoch = ?,"
                f" {_TOUCH} WHERE id = ?",
                (action, version_id, epoch, opportunity_id))

    def set_human_action(self, opportunity_id: str, action: str | None) -> None:
        with self._conn:
            self._conn.execute(
                f"UPDATE opportunities SET human_action = ?, {_TOUCH} WHERE id = ?",
                (action, opportunity_id))

    def set_requirement_proposals(self, opportunity_id: str, proposals_json: str) -> None:
        """Extracted requirement phrases persist as proposals pinned to the
        version they came from, never verified graph edges (§5)."""
        with self._conn:
            self._conn.execute(
                f"UPDATE opportunities SET requirement_proposals_json = ?, {_TOUCH}"
                " WHERE id = ?", (proposals_json, opportunity_id))

    def set_judged_fit(self, opportunity_id: str, judged_fit_json: str) -> None:
        with self._conn:
            self._conn.execute(
                f"UPDATE opportunities SET judged_fit_json = ?, {_TOUCH}"
                " WHERE id = ?", (judged_fit_json, opportunity_id))

    def list_backlog_pending(self) -> list[Opportunity]:
        """The observed-ungated backlog (§4), stable order; the worker applies
        the lane and recency priority on top."""
        rows = self._conn.execute(
            f"SELECT {_OPP_COLUMNS} FROM opportunities WHERE backlog_state = 'pending'"
            " ORDER BY first_seen, id").fetchall()
        return [Opportunity(*r) for r in rows]

    def list_open_with_stale_gate(self, current_epoch: int) -> list[Opportunity]:
        """Open opportunities whose latest gate verdict predates the current
        dependency epoch (§5), oldest verdicts first for the re-gate sweep."""
        rows = self._conn.execute(
            f"SELECT {_OPP_COLUMNS} FROM opportunities o"
            " WHERE o.availability IN ('open', 'reopened')"
            " AND o.latest_gate_verdict_id IS NOT NULL"
            " AND (SELECT epoch FROM gate_verdicts g"
            "      WHERE g.id = o.latest_gate_verdict_id) < ?"
            " ORDER BY (SELECT g.created_at FROM gate_verdicts g"
            "           WHERE g.id = o.latest_gate_verdict_id), o.id",
            (current_epoch,)).fetchall()
        return [Opportunity(*r) for r in rows]

    def list_filtered(self, availability: str | None = None,
                      gate: str | None = None,
                      source_id: str | None = None) -> list[Opportunity]:
        """CLI listing (§7): status and gate filters. gate is 'pass', 'fail',
        or 'none' (no verdict yet)."""
        clauses, params = [], []
        if availability is not None:
            clauses.append("availability = ?")
            params.append(availability)
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        if gate == "none":
            clauses.append("latest_gate_verdict_id IS NULL")
        elif gate is not None:
            clauses.append("(SELECT verdict FROM gate_verdicts g"
                           " WHERE g.id = latest_gate_verdict_id) = ?")
            params.append(gate)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT {_OPP_COLUMNS} FROM opportunities{where}"
            " ORDER BY first_seen DESC, id", params).fetchall()
        return [Opportunity(*r) for r in rows]

    def promote_from_backlog(self, opportunity_id: str) -> OpportunityVersion | None:
        """Transactional promotion out of observed-ungated (§4): the current
        open version at gate time, or a recorded discard for a closed or
        superseded observation. The backlog needs no per-row version pinning
        because this selection is the pinning moment."""
        with self._conn:
            row = self._conn.execute(
                "SELECT availability, backlog_state, current_version_id"
                " FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
            if not row:
                raise ValueError(f"unknown opportunity '{opportunity_id}'")
            availability, backlog_state, current_version_id = row
            if backlog_state != "pending":
                raise ValueError(
                    f"opportunity '{opportunity_id}' is not in the backlog"
                    f" (state '{backlog_state}')")
            if availability == "closed" or current_version_id is None:
                reason = ("closed before gating" if availability == "closed"
                          else "no current version at gate time")
                self._conn.execute(
                    "UPDATE opportunities SET backlog_state = 'discarded',"
                    f" backlog_discard_reason = ?, {_TOUCH} WHERE id = ?",
                    (reason, opportunity_id))
                return None
            self._conn.execute(
                f"UPDATE opportunities SET backlog_state = 'gated', {_TOUCH}"
                " WHERE id = ?", (opportunity_id,))
            version_row = self._conn.execute(
                f"SELECT {_VERSION_COLUMNS} FROM opportunity_versions WHERE id = ?",
                (current_version_id,)).fetchone()
            return OpportunityVersion(*version_row)


_QUEUE_COLUMNS = (
    "id, opportunity_id, version_id, state, lane_rank, first_seen, enqueue_seq,"
    " epoch, coverage_bp, attempts, next_attempt_run_seq, claimed_by,"
    " claimed_fence, superseded_reason, failure_reason, created_at, updated_at"
)

# The state-transition table (§4): anything else is a refused write.
_QUEUE_TRANSITIONS: dict[tuple[str, str], bool] = {
    ("pending_extraction", "extracted"): True,
    ("extracted", "pending_judgment"): True,
    ("pending_judgment", "judged"): True,
}

_UNFINISHED_STATES = ("pending_extraction", "extracted", "pending_judgment")
# The same set as a SQL literal for the supersession sweeps.
_UNFINISHED_SQL = "('pending_extraction', 'extracted', 'pending_judgment')"


class SqlitePromotionQueueRepository(PromotionQueueRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def enqueue(self, opportunity_id: str, version_id: str, lane_rank: int,
                first_seen: str, epoch: int) -> QueueRow:
        with self._conn:
            existing = self._conn.execute(
                "SELECT id FROM promotion_queue WHERE opportunity_id = ?"
                " AND version_id = ? AND epoch = ?",
                (opportunity_id, version_id, epoch)).fetchone()
            if existing:  # new survivors from a later run merge by key
                return self.get(existing[0])
            row_id = new_id("pmq")
            enqueue_seq = self._conn.execute(
                "SELECT COALESCE(MAX(enqueue_seq), 0) + 1 FROM promotion_queue"
            ).fetchone()[0]
            self._conn.execute(
                "INSERT INTO promotion_queue (id, opportunity_id, version_id,"
                " lane_rank, first_seen, enqueue_seq, epoch)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row_id, opportunity_id, version_id, lane_rank, first_seen,
                 enqueue_seq, epoch))
        return self.get(row_id)

    def get(self, row_id: str) -> QueueRow | None:
        row = self._conn.execute(
            f"SELECT {_QUEUE_COLUMNS} FROM promotion_queue WHERE id = ?",
            (row_id,)).fetchone()
        return QueueRow(*row) if row else None

    def pending_for_stage(self, state: str, current_run_seq: int) -> list[QueueRow]:
        rows = self._conn.execute(
            f"SELECT {_QUEUE_COLUMNS} FROM promotion_queue WHERE state = ?"
            " AND next_attempt_run_seq <= ? ORDER BY enqueue_seq",
            (state, current_run_seq)).fetchall()
        return [QueueRow(*r) for r in rows]

    def list_rows(self, state: str | None = None) -> list[QueueRow]:
        clause = " WHERE state = ?" if state is not None else ""
        params = (state,) if state is not None else ()
        rows = self._conn.execute(
            f"SELECT {_QUEUE_COLUMNS} FROM promotion_queue{clause}"
            " ORDER BY enqueue_seq", params).fetchall()
        return [QueueRow(*r) for r in rows]

    def claim(self, row_id: str, current_epoch: int,
              owner_token: str | None = None,
              fence: int | None = None) -> QueueRow | None:
        """Claiming re-checks the pinned version is still the current open
        version and the epoch is current (§4); a stale row is released as
        superseded, never worked.

        With owner_token and fence given, the claim is exclusive: one
        conditional update marks the row claimed before any model call. A row
        claimed under an older fence is recoverable (its claimant's lease is
        no longer live); a row claimed under the current fence by another
        owner refuses the claim."""
        with self._conn:
            row = self.get(row_id)
            if row is None or row.state not in _UNFINISHED_STATES:
                return None
            opp = self._conn.execute(
                "SELECT availability, current_version_id FROM opportunities"
                " WHERE id = ?", (row.opportunity_id,)).fetchone()
            availability, current_version_id = opp
            if availability == "closed" or current_version_id != row.version_id:
                self._supersede(row_id, "pinned version is no longer the current open version")
                return None
            if row.epoch < current_epoch:
                self._supersede(row_id, "dependency epoch advanced past the row's epoch")
                return None
            if owner_token is not None:
                cursor = self._conn.execute(
                    "UPDATE promotion_queue SET claimed_by = ?,"
                    f" claimed_fence = ?, {_TOUCH} WHERE id = ?"
                    f" AND state IN {_UNFINISHED_SQL}"
                    " AND (claimed_fence IS NULL OR claimed_fence < ?"
                    "      OR (claimed_by = ? AND claimed_fence = ?))",
                    (owner_token, fence, row_id, fence, owner_token, fence))
                if cursor.rowcount != 1:
                    return None  # another live claimant holds this row
                return self.get(row_id)
            return row

    def transition(self, row_id: str, new_state: str, coverage_bp: int | None = None) -> None:
        with self._conn:
            row = self.get(row_id)
            if row is None:
                raise ValueError(f"unknown queue row '{row_id}'")
            if not _QUEUE_TRANSITIONS.get((row.state, new_state)):
                raise ValueError(
                    f"illegal queue transition {row.state} -> {new_state}")
            if new_state == "extracted" and coverage_bp is None:
                raise ValueError("the extracted transition requires coverage_bp")
            self._conn.execute(
                "UPDATE promotion_queue SET state = ?,"
                f" coverage_bp = COALESCE(?, coverage_bp), attempts = 0, {_TOUCH}"
                " WHERE id = ?", (new_state, coverage_bp, row_id))

    def release_failed(self, row_id: str, reason: str, current_run_seq: int) -> None:
        with self._conn:
            row = self.get(row_id)
            if row is None or row.state not in _UNFINISHED_STATES:
                raise ValueError(f"queue row '{row_id}' is not in a claimable state")
            outcome, next_run = retry_outcome(row.attempts, current_run_seq)
            if outcome == "failed":
                self._conn.execute(
                    "UPDATE promotion_queue SET state = 'failed', attempts = ?,"
                    f" failure_reason = ?, {_TOUCH} WHERE id = ?",
                    (row.attempts + 1, reason, row_id))
            else:
                self._conn.execute(
                    "UPDATE promotion_queue SET attempts = ?,"
                    f" next_attempt_run_seq = ?, failure_reason = ?, {_TOUCH}"
                    " WHERE id = ?", (row.attempts + 1, next_run, reason, row_id))

    def retry_failed(self, row_id: str, current_run_seq: int) -> None:
        with self._conn:
            row = self.get(row_id)
            if row is None or row.state != "failed":
                raise ValueError(f"queue row '{row_id}' is not in the failed state")
            # Explicit CLI retry after remediation: attempts reset, claimable
            # from this run; the row keeps its frozen ordering key.
            self._conn.execute(
                "UPDATE promotion_queue SET state = 'pending_extraction',"
                " attempts = 0, next_attempt_run_seq = ?, failure_reason = NULL,"
                f" {_TOUCH} WHERE id = ?", (current_run_seq, row_id))

    def supersede_for_opportunity(self, opportunity_id: str,
                                  current_version_id: str | None, reason: str) -> None:
        with self._conn:
            if current_version_id is None:  # closure: everything unfinished cancels
                self._conn.execute(
                    "UPDATE promotion_queue SET state = 'superseded',"
                    f" superseded_reason = ?, {_TOUCH} WHERE opportunity_id = ?"
                    f" AND state IN {_UNFINISHED_SQL}",
                    (reason, opportunity_id))
            else:
                self._conn.execute(
                    "UPDATE promotion_queue SET state = 'superseded',"
                    f" superseded_reason = ?, {_TOUCH} WHERE opportunity_id = ?"
                    f" AND version_id != ? AND state IN {_UNFINISHED_SQL}",
                    (reason, opportunity_id, current_version_id))

    def supersede_stale_epochs(self, current_epoch: int, reason: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE promotion_queue SET state = 'superseded',"
                f" superseded_reason = ?, {_TOUCH} WHERE epoch < ?"
                f" AND state IN {_UNFINISHED_SQL}", (reason, current_epoch))

    def _supersede(self, row_id: str, reason: str) -> None:
        self._conn.execute(
            "UPDATE promotion_queue SET state = 'superseded',"
            f" superseded_reason = ?, {_TOUCH} WHERE id = ?", (reason, row_id))
