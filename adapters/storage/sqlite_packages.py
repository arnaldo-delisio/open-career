"""SQLite PackageRepository: the package lifecycle seam (migration 0003; spec:
decisions/package-generation-design.md, "Storage, states, CLI").

Everything status-shaped is enforced here, transactionally: the explicit
state-transition table, status-dependent required fields, write-once finalized
bundle fields, and the generation lease. Lease time comparisons run at the
database clock, never the process clock."""

import sqlite3
from typing import Callable

from domain.gauntlet import GauntletRun, ReservationLostError
from domain.gauntlet import SUITE_VERSION as CURRENT_SUITE_VERSION
from domain.ids import new_id
from domain.packages import (
    APPROVED,
    FAILED,
    GENERATING,
    VERIFIED,
    VERIFIED_REQUIRED_FIELDS,
    LeaseLostError,
    Package,
    PackageStateError,
    PackageVersion,
)
from domain.ports import PackageRepository

# The db clock, millisecond precision, lexically comparable ISO-8601 UTC.
_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"

_PKG_SELECT = ("SELECT id, role_family_id, opportunity_id, approved_version_id,"
               " created_at, updated_at FROM packages")
_VER_COLUMNS = ("id, package_id, version, status, content_model_json,"
                " context_snapshot_locator, input_context_hash, verifier_report_json,"
                " ats_report_json, artifact_locator, artifact_hash,"
                " gauntlet_report_json, failure_report_json, lease_owner,"
                " lease_generation, lease_expires_at, approved_at, created_at")
_VER_SELECT = f"SELECT {_VER_COLUMNS} FROM package_versions"

# Fields record_progress may stage on a GENERATING row. failure_report_json is
# deliberately absent: it lands only through fail/claim paths.
_PROGRESS_FIELDS = frozenset(VERIFIED_REQUIRED_FIELDS)


def _expiry(lease_seconds: int) -> str:
    return f"strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '{int(lease_seconds)} seconds')"


_GRUN_COLUMNS = ("seq, id, package_version_id, suite_version, attempt, complete,"
                 " report_json, prompt_inputs_locator, prompt_inputs_hash,"
                 " raw_completions_locator, raw_completions_hash,"
                 " resolved_models_json, policy_snapshot_locator,"
                 " policy_snapshot_hash, created_at")
_GRUN_SELECT = f"SELECT {_GRUN_COLUMNS} FROM gauntlet_runs"

# The fields insert_gauntlet_run takes besides identity; seq, attempt, and
# created_at are allocated inside the fenced transaction.
_GRUN_INSERT_FIELDS = (
    "run_id", "complete", "report_json", "prompt_inputs_locator",
    "prompt_inputs_hash", "raw_completions_locator", "raw_completions_hash",
    "resolved_models_json", "policy_snapshot_locator", "policy_snapshot_hash")


class SqlitePackageRepository(PackageRepository):
    def __init__(self, conn: sqlite3.Connection,
                 suite_version_provider: Callable[[], str] | None = None):
        self._conn = conn
        # The repository owns the current suite_version (injected at
        # construction from the shipped suite constant, never an approve-call
        # argument), so no caller can pass a stale suite.
        self._suite_version = suite_version_provider or (lambda: CURRENT_SUITE_VERSION)

    # -- packages ----------------------------------------------------------

    def get_or_create_base_package(self, role_family_id: str) -> Package:
        existing = self.get_base_package_for_family(role_family_id)
        if existing:
            return existing
        package_id = new_id("pkg")
        with self._conn:
            self._conn.execute(
                "INSERT INTO packages (id, role_family_id) VALUES (?, ?)",
                (package_id, role_family_id),
            )
        return self.get_package(package_id)

    def get_package(self, package_id: str) -> Package | None:
        row = self._conn.execute(f"{_PKG_SELECT} WHERE id = ?", (package_id,)).fetchone()
        return Package(*row) if row else None

    def get_base_package_for_family(self, role_family_id: str) -> Package | None:
        row = self._conn.execute(
            f"{_PKG_SELECT} WHERE role_family_id = ? AND opportunity_id IS NULL",
            (role_family_id,),
        ).fetchone()
        return Package(*row) if row else None

    # -- versions ----------------------------------------------------------

    def get_version(self, version_id: str) -> PackageVersion | None:
        row = self._conn.execute(f"{_VER_SELECT} WHERE id = ?", (version_id,)).fetchone()
        return PackageVersion(*row) if row else None

    def list_versions(self, package_id: str) -> list[PackageVersion]:
        rows = self._conn.execute(
            f"{_VER_SELECT} WHERE package_id = ? ORDER BY version", (package_id,)
        ).fetchall()
        return [PackageVersion(*r) for r in rows]

    def reserve_version(self, package_id: str, owner_token: str,
                        lease_seconds: int) -> PackageVersion:
        if self.get_package(package_id) is None:
            raise PackageStateError(f"package '{package_id}' does not exist")
        for _ in range(5):  # a concurrent generate loses on UNIQUE and retries
            try:
                with self._conn:
                    (next_version,) = self._conn.execute(
                        "SELECT COALESCE(MAX(version), 0) + 1 FROM package_versions"
                        " WHERE package_id = ?", (package_id,),
                    ).fetchone()
                    version_id = new_id("pkgv")
                    self._conn.execute(
                        "INSERT INTO package_versions (id, package_id, version, status,"
                        f" lease_owner, lease_generation, lease_expires_at, created_at)"
                        f" VALUES (?, ?, ?, ?, ?, 1, {_expiry(lease_seconds)}, {_NOW})",
                        (version_id, package_id, next_version, GENERATING, owner_token),
                    )
                return self.get_version(version_id)
            except sqlite3.IntegrityError:
                continue
        raise PackageStateError(
            f"could not reserve a version for package '{package_id}' (contention)")

    # -- lease -------------------------------------------------------------

    def renew_lease(self, version_id: str, owner_token: str, lease_generation: int,
                    lease_seconds: int) -> bool:
        with self._conn:  # one atomic conditional update; zero rows = stop
            cur = self._conn.execute(
                f"UPDATE package_versions SET lease_expires_at = {_expiry(lease_seconds)}"
                f" WHERE id = ? AND status = ? AND lease_owner = ?"
                f" AND lease_generation = ? AND lease_expires_at > {_NOW}",
                (version_id, GENERATING, owner_token, lease_generation),
            )
        return cur.rowcount == 1

    def check_lease(self, version_id: str, owner_token: str, lease_generation: int) -> bool:
        row = self._conn.execute(
            f"SELECT 1 FROM package_versions WHERE id = ? AND status = ?"
            f" AND lease_owner = ? AND lease_generation = ?"
            f" AND lease_expires_at > {_NOW}",
            (version_id, GENERATING, owner_token, lease_generation),
        ).fetchone()
        return row is not None

    # -- progress and transitions -----------------------------------------

    def record_progress(self, version_id: str, owner_token: str, lease_generation: int,
                        **fields) -> None:
        unknown = set(fields) - _PROGRESS_FIELDS
        if unknown:
            raise PackageStateError(f"not stageable progress fields: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._conn:
            cur = self._conn.execute(
                f"UPDATE package_versions SET {assignments}"
                f" WHERE id = ? AND status = ? AND lease_owner = ?"
                f" AND lease_generation = ? AND lease_expires_at > {_NOW}",
                (*fields.values(), version_id, GENERATING, owner_token, lease_generation),
            )
        if cur.rowcount != 1:
            self._raise_lease_or_state(version_id, "record progress on")

    def finalize_verified(self, version_id: str, owner_token: str, lease_generation: int,
                          **bundle) -> None:
        missing = [f for f in VERIFIED_REQUIRED_FIELDS if not bundle.get(f)]
        unknown = set(bundle) - set(VERIFIED_REQUIRED_FIELDS)
        if missing or unknown:
            raise PackageStateError(
                f"VERIFIED requires the full audit bundle; missing {missing},"
                f" unknown {sorted(unknown)}")
        assignments = ", ".join(f"{name} = ?" for name in VERIFIED_REQUIRED_FIELDS)
        with self._conn:  # lease ownership, token, generation validated atomically
            cur = self._conn.execute(
                f"UPDATE package_versions SET status = ?, {assignments}"
                f" WHERE id = ? AND status = ? AND lease_owner = ?"
                f" AND lease_generation = ? AND lease_expires_at > {_NOW}",
                (VERIFIED, *(bundle[f] for f in VERIFIED_REQUIRED_FIELDS),
                 version_id, GENERATING, owner_token, lease_generation),
            )
            if cur.rowcount != 1:
                self._raise_lease_or_state(version_id, "finalize")

    def fail(self, version_id: str, owner_token: str, lease_generation: int,
             failure_report_json: str) -> None:
        self._require_report(failure_report_json)
        with self._conn:
            cur = self._conn.execute(
                f"UPDATE package_versions SET status = ?, failure_report_json = ?"
                f" WHERE id = ? AND status = ? AND lease_owner = ?"
                f" AND lease_generation = ? AND lease_expires_at > {_NOW}",
                (FAILED, failure_report_json, version_id, GENERATING,
                 owner_token, lease_generation),
            )
        if cur.rowcount != 1:
            self._raise_lease_or_state(version_id, "fail")

    def claim_expired_and_fail(self, version_id: str, failure_report_json: str) -> bool:
        self._require_report(failure_report_json)
        with self._conn:  # compare-and-set on status and lease expiry; bumps the
            # generation so the fenced old owner can never resurrect the lease.
            cur = self._conn.execute(
                f"UPDATE package_versions SET status = ?, failure_report_json = ?,"
                f" lease_generation = lease_generation + 1"
                f" WHERE id = ? AND status = ? AND lease_expires_at <= {_NOW}",
                (FAILED, failure_report_json, version_id, GENERATING),
            )
        return cur.rowcount == 1

    def approve(self, version_id: str, approved_at: str, override: bool = False,
                override_reason: str | None = None) -> None:
        with self._conn:  # decision, approval, and pointer land together
            version = self.get_version(version_id)
            if version is None:
                raise PackageStateError(f"package version '{version_id}' does not exist")
            if version.status != VERIFIED:
                raise PackageStateError(
                    f"only a VERIFIED version can be approved; '{version_id}' is {version.status}")
            # The approval gate: the repository resolves the current suite's
            # effective run itself, inside this transaction. An effective
            # terminal current-suite run is a precondition of every approval,
            # override included; an override can waive only a recorded FAIL
            # or ATTENTION verdict, never missing adjudication.
            suite = self._suite_version()
            run = self.effective_gauntlet_run(version_id, suite)
            if run is None:
                raise PackageStateError(
                    f"no effective Gauntlet run under the current suite '{suite}'"
                    f" for '{version_id}'; run `open-career package gauntlet"
                    f" {version_id}` (or `package review`, which reconciles)"
                    " before approving")
            verdict = run.verdict
            if not override:
                if verdict != "PASS":
                    raise PackageStateError(
                        f"the effective Gauntlet run's verdict is {verdict};"
                        " approve with --accept-despite-gauntlet \"<reason>\""
                        " to record an override, or regenerate")
            else:
                if not override_reason or not override_reason.strip():
                    raise PackageStateError(
                        "an override always requires a non-empty reason")
                if verdict not in ("FAIL", "ATTENTION"):
                    raise PackageStateError(
                        f"nothing to override: the effective run's verdict is"
                        f" {verdict}, not FAIL or ATTENTION")
            self._conn.execute(
                "INSERT INTO approval_decisions (id, package_version_id,"
                " gauntlet_run_id, verdict_at_decision, override, override_reason)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (new_id("apd"), version_id, run.id, verdict,
                 1 if override else 0, override_reason))
            self._conn.execute(
                "UPDATE package_versions SET status = ?, approved_at = ? WHERE id = ?",
                (APPROVED, approved_at, version_id),
            )
            # Same-package ownership is structural: the package id comes from
            # the version row itself inside this transaction.
            self._conn.execute(
                f"UPDATE packages SET approved_version_id = ?, updated_at = {_NOW}"
                " WHERE id = ?",
                (version_id, version.package_id),
            )

    # -- Gauntlet (spec: the scope's decisions/gauntlet-design.md) ---------

    def claim_gauntlet_reservation(self, version_id: str, suite_version: str,
                                   owner_token: str, ttl_seconds: int) -> bool:
        with self._conn:  # existing-complete check and claim are one atomic txn
            if self.get_version(version_id) is None:
                raise PackageStateError(f"package version '{version_id}' does not exist")
            complete = self._conn.execute(
                "SELECT 1 FROM gauntlet_runs WHERE package_version_id = ?"
                " AND suite_version = ? AND complete = 1",
                (version_id, suite_version)).fetchone()
            if complete:
                raise PackageStateError(
                    f"a complete Gauntlet attempt already exists for"
                    f" '{version_id}' under suite '{suite_version}'; a suite"
                    " bump is the only path to re-judging (no --force)")
            try:
                self._conn.execute(
                    "INSERT INTO gauntlet_reservations (package_version_id,"
                    f" suite_version, owner_token, expires_at)"
                    f" VALUES (?, ?, ?, {_expiry(ttl_seconds)})",
                    (version_id, suite_version, owner_token))
                return True
            except sqlite3.IntegrityError:
                # Take over only past expiry at the database clock, minting
                # the new owner token in the same conditional update.
                cur = self._conn.execute(
                    "UPDATE gauntlet_reservations SET owner_token = ?,"
                    f" expires_at = {_expiry(ttl_seconds)}"
                    f" WHERE package_version_id = ? AND suite_version = ?"
                    f" AND expires_at <= {_NOW}",
                    (owner_token, version_id, suite_version))
                return cur.rowcount == 1

    def renew_gauntlet_reservation(self, version_id: str, suite_version: str,
                                   owner_token: str, ttl_seconds: int) -> bool:
        with self._conn:  # one atomic conditional renewal; zero rows = stop
            cur = self._conn.execute(
                f"UPDATE gauntlet_reservations SET expires_at = {_expiry(ttl_seconds)}"
                f" WHERE package_version_id = ? AND suite_version = ?"
                f" AND owner_token = ? AND expires_at > {_NOW}",
                (version_id, suite_version, owner_token))
        return cur.rowcount == 1

    def release_gauntlet_reservation(self, version_id: str, suite_version: str,
                                     owner_token: str) -> bool:
        with self._conn:  # one atomic fenced release; zero rows = not ours
            cur = self._conn.execute(
                "DELETE FROM gauntlet_reservations WHERE package_version_id = ?"
                f" AND suite_version = ? AND owner_token = ? AND expires_at > {_NOW}",
                (version_id, suite_version, owner_token))
        return cur.rowcount == 1

    def insert_gauntlet_run(self, version_id: str, suite_version: str,
                            owner_token: str, **fields) -> GauntletRun:
        missing = [f for f in _GRUN_INSERT_FIELDS if fields.get(f) is None]
        unknown = set(fields) - set(_GRUN_INSERT_FIELDS)
        if missing or unknown:
            raise PackageStateError(
                f"a Gauntlet run row is write-once and complete on insert;"
                f" missing {missing}, unknown {sorted(unknown)}")
        with self._conn:  # fenced consume and append-only insert, one txn
            cur = self._conn.execute(
                f"DELETE FROM gauntlet_reservations WHERE package_version_id = ?"
                f" AND suite_version = ? AND owner_token = ?"
                f" AND expires_at > {_NOW}",
                (version_id, suite_version, owner_token))
            if cur.rowcount != 1:
                # A zero-row consume discards the stale worker's result
                # entirely: it inserts nothing.
                raise ReservationLostError(
                    f"gauntlet reservation on '{version_id}' (suite"
                    f" '{suite_version}') expired or is owned by a successor;"
                    " result discarded")
            (attempt,) = self._conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 FROM gauntlet_runs"
                " WHERE package_version_id = ? AND suite_version = ?",
                (version_id, suite_version)).fetchone()
            try:
                self._conn.execute(
                    "INSERT INTO gauntlet_runs (id, package_version_id,"
                    " suite_version, attempt, complete, report_json,"
                    " prompt_inputs_locator, prompt_inputs_hash,"
                    " raw_completions_locator, raw_completions_hash,"
                    " resolved_models_json, policy_snapshot_locator,"
                    " policy_snapshot_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (fields["run_id"], version_id, suite_version, attempt,
                     fields["complete"], fields["report_json"],
                     fields["prompt_inputs_locator"], fields["prompt_inputs_hash"],
                     fields["raw_completions_locator"],
                     fields["raw_completions_hash"],
                     fields["resolved_models_json"],
                     fields["policy_snapshot_locator"],
                     fields["policy_snapshot_hash"]))
            except sqlite3.IntegrityError as e:
                # The partial unique index: a second complete same-suite
                # adjudication is impossible regardless of application logic.
                raise PackageStateError(
                    f"gauntlet run insert violated append-only uniqueness: {e}") from e
        return self.get_gauntlet_run(fields["run_id"])

    def get_gauntlet_run(self, run_id: str) -> GauntletRun | None:
        row = self._conn.execute(
            f"{_GRUN_SELECT} WHERE id = ?", (run_id,)).fetchone()
        return GauntletRun(*row) if row else None

    def list_gauntlet_runs(self, version_id: str) -> list[GauntletRun]:
        rows = self._conn.execute(
            f"{_GRUN_SELECT} WHERE package_version_id = ? ORDER BY seq DESC",
            (version_id,)).fetchall()
        return [GauntletRun(*r) for r in rows]

    def effective_gauntlet_run(self, version_id: str,
                               suite_version: str) -> GauntletRun | None:
        row = self._conn.execute(
            f"{_GRUN_SELECT} WHERE package_version_id = ? AND suite_version = ?"
            " AND complete = 1 ORDER BY seq DESC LIMIT 1",
            (version_id, suite_version)).fetchone()
        return GauntletRun(*row) if row else None

    def list_approval_decisions(self, version_id: str) -> list[tuple]:
        return self._conn.execute(
            "SELECT id, package_version_id, gauntlet_run_id, verdict_at_decision,"
            " override, override_reason, created_at FROM approval_decisions"
            " WHERE package_version_id = ? ORDER BY created_at, id",
            (version_id,)).fetchall()

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _require_report(failure_report_json: str) -> None:
        if not failure_report_json or not failure_report_json.strip():
            raise PackageStateError("FAILED always requires a structured failure report")

    def _raise_lease_or_state(self, version_id: str, action: str) -> None:
        version = self.get_version(version_id)
        if version is None:
            raise PackageStateError(f"package version '{version_id}' does not exist")
        if version.status != GENERATING:
            raise PackageStateError(
                f"cannot {action} version '{version_id}': status is {version.status}"
                " and finalized bundle fields are write-once")
        raise LeaseLostError(
            f"cannot {action} version '{version_id}': lease expired or held by another owner")
