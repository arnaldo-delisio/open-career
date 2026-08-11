"""SQLite PackageRepository: the package lifecycle seam (migration 0003; spec:
decisions/package-generation-design.md, "Storage, states, CLI").

Everything status-shaped is enforced here, transactionally: the explicit
state-transition table, status-dependent required fields, write-once finalized
bundle fields, and the generation lease. Lease time comparisons run at the
database clock, never the process clock."""

import sqlite3

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


class SqlitePackageRepository(PackageRepository):
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

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

    def approve(self, version_id: str, approved_at: str) -> None:
        with self._conn:  # approval and approved_version_id land together
            version = self.get_version(version_id)
            if version is None:
                raise PackageStateError(f"package version '{version_id}' does not exist")
            if version.status != VERIFIED:
                raise PackageStateError(
                    f"only a VERIFIED version can be approved; '{version_id}' is {version.status}")
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
