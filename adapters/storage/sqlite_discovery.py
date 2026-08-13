"""Discovery storage, registry side (migration 0006; spec: the scope's
decisions/discovery-design.md): source registry with scheduler state and
reviewed metadata provenance, immutable per-source-sequenced snapshots, run
records, and the dependency-epoch counter."""

import sqlite3

from domain.discovery import DiscoveryRun, Snapshot, Source, SourceSupersession
from domain.ids import new_id
from domain.ports import (
    DependencyEpochRepository,
    DiscoveryRunRepository,
    SnapshotRepository,
    SourceRegistryRepository,
)

_SOURCE_COLUMNS = (
    "id, ats_type, tenant_slug, origin, status, company_name, last_checked,"
    " last_success, consecutive_failures, last_polled_at, next_poll_at,"
    " next_probe_at, probe_attempts, last_poll_outcome, policy_notes, industry,"
    " industry_origin, company_stage, company_stage_origin, company_size_band,"
    " company_size_band_origin, created_at, updated_at"
)

_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"

# The only fields set_reviewed_metadata may touch, each with its provenance
# column (§5 hard exclusions consume these; no silent classifier writes).
_REVIEWED_FIELDS = {"industry", "company_stage", "company_size_band"}


class SqliteSourceRegistryRepository(SourceRegistryRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def add(self, source: Source) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO sources (id, ats_type, tenant_slug, origin, status,"
                " company_name, policy_notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source.id, source.ats_type, source.tenant_slug, source.origin,
                 source.status, source.company_name, source.policy_notes),
            )

    def get(self, source_id: str) -> Source | None:
        row = self._conn.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        return Source(*row) if row else None

    def list_all(self) -> list[Source]:
        rows = self._conn.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM sources ORDER BY id").fetchall()
        return [Source(*r) for r in rows]

    def set_status(self, source_id: str, status: str) -> None:
        if status not in ("candidate", "enabled", "disabled"):
            raise ValueError(f"unknown source status '{status}'")
        with self._conn:
            self._conn.execute(
                "UPDATE sources SET status = ?,"
                " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (status, source_id))

    def set_reviewed_metadata(self, source_id: str, field: str, value: str | None,
                              origin: str) -> None:
        if field not in _REVIEWED_FIELDS:
            raise ValueError(f"'{field}' is not a reviewed metadata field")
        if origin not in ("curated", "cli_edit"):
            raise ValueError(f"unknown metadata origin '{origin}'")
        with self._conn:  # value and provenance land together
            self._conn.execute(
                f"UPDATE sources SET {field} = ?, {field}_origin = ?,"
                " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (value, origin if value is not None else None, source_id))

    def record_probe_outcome(self, source_id: str, success: bool,
                             next_probe_at: str | None) -> None:
        """One probe's scheduler effects (§2): success enables the source and
        resets attempt state; failure backs off on the caller-computed
        next_probe_at (exponential aging is worker policy, storage records)."""
        with self._conn:
            if success:
                self._conn.execute(
                    "UPDATE sources SET status = 'enabled', probe_attempts = 0,"
                    " consecutive_failures = 0, next_probe_at = NULL,"
                    f" last_checked = {_NOW}, last_success = {_NOW},"
                    f" updated_at = {_NOW} WHERE id = ?", (source_id,))
            else:
                self._conn.execute(
                    "UPDATE sources SET probe_attempts = probe_attempts + 1,"
                    f" next_probe_at = ?, last_checked = {_NOW},"
                    f" updated_at = {_NOW} WHERE id = ?",
                    (next_probe_at, source_id))

    def record_poll_outcome(self, source_id: str, outcome: str,
                            next_poll_at: str | None = None,
                            rot_threshold: int = 5,
                            next_probe_at: str | None = None) -> None:
        """One poll's scheduler effects (§1/§2). success resets failures;
        degraded increments them and disables at the rot threshold (nothing is
        deleted; a disabled source re-enters the slow re-probe cycle);
        oversized and deferred touch no health or rot state."""
        if outcome not in ("success", "degraded", "oversized", "deferred"):
            raise ValueError(f"unknown poll outcome '{outcome}'")
        with self._conn:
            if outcome == "success":
                self._conn.execute(
                    "UPDATE sources SET last_poll_outcome = 'success',"
                    " consecutive_failures = 0,"
                    f" last_polled_at = {_NOW}, last_checked = {_NOW},"
                    f" last_success = {_NOW}, next_poll_at = ?,"
                    f" updated_at = {_NOW} WHERE id = ?",
                    (next_poll_at, source_id))
            elif outcome == "degraded":
                self._conn.execute(
                    "UPDATE sources SET last_poll_outcome = 'degraded',"
                    " consecutive_failures = consecutive_failures + 1,"
                    f" last_polled_at = {_NOW}, last_checked = {_NOW},"
                    f" next_poll_at = ?, updated_at = {_NOW} WHERE id = ?",
                    (next_poll_at, source_id))
                self._conn.execute(
                    "UPDATE sources SET status = 'disabled', next_probe_at = ?"
                    " WHERE id = ? AND consecutive_failures >= ?",
                    (next_probe_at, source_id, rot_threshold))
            elif outcome == "oversized":  # contacted the source: auditable,
                # last_checked advances, no rot effects
                self._conn.execute(
                    "UPDATE sources SET last_poll_outcome = 'oversized',"
                    f" last_checked = {_NOW}, updated_at = {_NOW} WHERE id = ?",
                    (source_id,))
            else:  # deferred: budget only, nothing was contacted; only the
                # outcome is recorded and every health field stays untouched
                self._conn.execute(
                    "UPDATE sources SET last_poll_outcome = 'deferred',"
                    f" updated_at = {_NOW} WHERE id = ?", (source_id,))

    def record_supersession(self, supersession: SourceSupersession) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO source_supersessions (id, old_source_id, new_source_id,"
                " origin, notes, reviewed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (supersession.id, supersession.old_source_id,
                 supersession.new_source_id, supersession.origin,
                 supersession.notes, supersession.reviewed_at))

    def list_supersessions(self) -> list[SourceSupersession]:
        rows = self._conn.execute(
            "SELECT id, old_source_id, new_source_id, origin, reviewed_at, notes,"
            " created_at FROM source_supersessions ORDER BY created_at, id").fetchall()
        return [SourceSupersession(*r) for r in rows]


_SNAPSHOT_COLUMNS = (
    "id, source_id, seq, raw_locator, content_hash, completion_json,"
    " posting_count, remote_version_token, run_id, committed_at"
)


class SqliteSnapshotRepository(SnapshotRepository):
    """Immutable rows: commit assigns the next per-source sequence in one
    transaction; there is no update path, by design (§1)."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def commit(self, source_id: str, raw_locator: str, content_hash: str,
               completion_json: str, posting_count: int,
               remote_version_token: str | None = None,
               run_id: str | None = None) -> Snapshot:
        snapshot_id = new_id("snp")
        with self._conn:
            seq = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM snapshots WHERE source_id = ?",
                (source_id,)).fetchone()[0]
            self._conn.execute(
                "INSERT INTO snapshots (id, source_id, seq, raw_locator, content_hash,"
                " completion_json, posting_count, remote_version_token, run_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (snapshot_id, source_id, seq, raw_locator, content_hash,
                 completion_json, posting_count, remote_version_token, run_id))
        return self.get(snapshot_id)

    def get(self, snapshot_id: str) -> Snapshot | None:
        row = self._conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM snapshots WHERE id = ?",
            (snapshot_id,)).fetchone()
        return Snapshot(*row) if row else None

    def list_for_source(self, source_id: str) -> list[Snapshot]:
        rows = self._conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM snapshots WHERE source_id = ?"
            " ORDER BY seq", (source_id,)).fetchall()
        return [Snapshot(*r) for r in rows]

    def latest_for_source(self, source_id: str) -> Snapshot | None:
        row = self._conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM snapshots WHERE source_id = ?"
            " ORDER BY seq DESC LIMIT 1", (source_id,)).fetchone()
        return Snapshot(*row) if row else None


_RUN_COLUMNS = ("id, run_seq, status, budget_json, epoch, exhausted_stage,"
                " spend_json, source_outcomes_json, started_at, finished_at")


class SqliteDiscoveryRunRepository(DiscoveryRunRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def start(self, budget_json: str, epoch: int, lease_owner: str | None = None,
              lease_fence: int | None = None) -> DiscoveryRun:
        run_id = new_id("run")
        with self._conn:
            run_seq = self._conn.execute(
                "SELECT COALESCE(MAX(run_seq), 0) + 1 FROM discovery_runs").fetchone()[0]
            self._conn.execute(
                "INSERT INTO discovery_runs (id, run_seq, status, budget_json,"
                " epoch, lease_owner, lease_fence)"
                " VALUES (?, ?, 'running', ?, ?, ?, ?)",
                (run_id, run_seq, budget_json, epoch, lease_owner, lease_fence))
        return self.get(run_id)

    def reconcile_abandoned(self) -> list[str]:
        """Reconcile run rows left 'running' by a process that died: status
        becomes 'interrupted' (terminal, and distinguishable from a clean
        finish) with whatever spend was persisted retained. The ownership test
        is the lease's own held_by logic (matching owner AND fence, unexpired
        at the database clock), so a genuinely live run is never touched; a row
        predating the lease columns carries no owner and is a past run.
        Returns the reconciled run ids."""
        with self._conn:
            rows = self._conn.execute(
                "SELECT id FROM discovery_runs r WHERE r.status = 'running'"
                " AND NOT EXISTS (SELECT 1 FROM discovery_lease l"
                "   WHERE l.id = 1 AND l.owner_token IS NOT NULL"
                "     AND l.owner_token = r.lease_owner AND l.fence = r.lease_fence"
                f"    AND l.expires_at IS NOT NULL AND l.expires_at >= {_NOW})"
            ).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                self._conn.executemany(
                    "UPDATE discovery_runs SET status = 'interrupted',"
                    " finished_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                    " WHERE id = ? AND status = 'running'",
                    [(i,) for i in ids])
        return ids

    def finish(self, run_id: str, status: str, spend_json: str,
               source_outcomes_json: str, exhausted_stage: str | None = None) -> None:
        if status not in ("completed", "budget_exhausted", "failed"):
            raise ValueError(f"unknown terminal run status '{status}'")
        with self._conn:
            # Only a still-running row finishes: a run that lost its lease and
            # was already reconciled to 'interrupted' by its successor keeps
            # that terminal record, never overwritten by the old owner's late
            # finalization.
            self._conn.execute(
                "UPDATE discovery_runs SET status = ?, spend_json = ?,"
                " source_outcomes_json = ?, exhausted_stage = ?,"
                " finished_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
                " WHERE id = ? AND status = 'running'",
                (status, spend_json, source_outcomes_json, exhausted_stage, run_id))

    def get(self, run_id: str) -> DiscoveryRun | None:
        row = self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM discovery_runs WHERE id = ?", (run_id,)).fetchone()
        return DiscoveryRun(*row) if row else None

    def list_all(self) -> list[DiscoveryRun]:
        rows = self._conn.execute(
            f"SELECT {_RUN_COLUMNS} FROM discovery_runs ORDER BY run_seq").fetchall()
        return [DiscoveryRun(*r) for r in rows]


class SqliteDiscoveryLease:
    """The singleton run lease (§2): the package pipeline's lease discipline
    (owner token, expiry at the database clock) on one row. acquire is a
    single conditional update, so concurrent runs cannot both hold it."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def acquire(self, owner_token: str, lease_seconds: int) -> int | None:
        """Claim the lease, bumping the monotonic fence generation. Returns
        the new fence on success, None when another run holds the lease."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE discovery_lease SET owner_token = ?,"
                " fence = fence + 1,"
                " expires_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now',"
                " '+' || ? || ' seconds')"
                " WHERE id = 1 AND (owner_token IS NULL"
                f" OR expires_at IS NULL OR expires_at < {_NOW})",
                (owner_token, int(lease_seconds)))
            if cursor.rowcount != 1:
                return None
            return self._conn.execute(
                "SELECT fence FROM discovery_lease WHERE id = 1").fetchone()[0]

    def renew(self, owner_token: str, fence: int, lease_seconds: int) -> bool:
        """One atomic conditional update, the package pipeline's discipline:
        matching owner AND fence, unexpired at the db clock. Zero rows renewed
        means the run must stop before its next mutation."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE discovery_lease SET expires_at ="
                " strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+' || ? || ' seconds')"
                " WHERE id = 1 AND owner_token = ? AND fence = ?"
                f" AND expires_at IS NOT NULL AND expires_at >= {_NOW}",
                (int(lease_seconds), owner_token, fence))
            return cursor.rowcount == 1

    def held_by(self, owner_token: str, fence: int) -> bool:
        """Read-side fence check for use INSIDE a transition transaction: true
        only while this owner and fence hold an unexpired lease."""
        row = self._conn.execute(
            "SELECT 1 FROM discovery_lease WHERE id = 1 AND owner_token = ?"
            f" AND fence = ? AND expires_at IS NOT NULL AND expires_at >= {_NOW}",
            (owner_token, fence)).fetchone()
        return row is not None

    def holder(self) -> tuple[str | None, str | None]:
        """(owner_token, expires_at) of the current lease row, for operator
        surfaces (a blocked run names who holds it and until when)."""
        row = self._conn.execute(
            "SELECT owner_token, expires_at FROM discovery_lease WHERE id = 1"
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def claim_expired(self) -> bool:
        """Recovery (the package pipeline's precedent): clear the lease only
        if it has expired; a live lease is never stolen from under its owner.
        Returns True when a stale lease was cleared."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE discovery_lease SET owner_token = NULL, expires_at = NULL"
                " WHERE id = 1 AND owner_token IS NOT NULL"
                f" AND (expires_at IS NULL OR expires_at < {_NOW})")
            return cursor.rowcount == 1

    def release(self, owner_token: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE discovery_lease SET owner_token = NULL, expires_at = NULL"
                " WHERE id = 1 AND owner_token = ?", (owner_token,))


class SqliteDependencyEpochRepository(DependencyEpochRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def current(self) -> int:
        return self._conn.execute(
            "SELECT epoch FROM dependency_epoch WHERE id = 1").fetchone()[0]

    def bump(self) -> int:
        with self._conn:
            self._conn.execute(
                "UPDATE dependency_epoch SET epoch = epoch + 1,"
                " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1")
        return self.current()
