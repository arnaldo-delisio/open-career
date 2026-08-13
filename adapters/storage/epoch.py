"""Dependency-epoch bump (OC-37 §5): one integer, advanced inside the same
transaction as any audited write to user policies, the profile, active role
families/strategy, or the graph's eligible edge set. Gate and rank results
record the epoch they ran under; a bump makes them stale and the next
discovery run re-gates them."""

import sqlite3


def bump_dependency_epoch(conn: sqlite3.Connection) -> None:
    """Advance the epoch inside the caller's open transaction (never opens its
    own): the write and the bump land or roll back together."""
    conn.execute(
        "UPDATE dependency_epoch SET epoch = epoch + 1,"
        " updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = 1")
