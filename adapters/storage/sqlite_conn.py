"""One connection boundary for the instance database.

Every process that opens the instance database opens it the same way: foreign
keys enforced, WAL journalling, and a busy timeout, so a second session
holding a write lock makes a writer wait briefly instead of failing
immediately. Write transactions that still lose the race retry a bounded
number of times through `retry_on_locked`; nothing here weakens a transaction
or a lease fence, it only decides how long a writer waits before giving up.
"""

import sqlite3
import time

# A few seconds: long enough to ride out another session's write transaction,
# short enough that a genuinely stuck writer still surfaces as an error.
BUSY_TIMEOUT_MS = 5000

# Bounded retry for the transient locked error: five attempts with doubling
# backoff from 100ms is under a second of extra waiting on top of the busy
# timeout, and a lock held longer than that is not transient.
RETRY_ATTEMPTS = 5
RETRY_BASE_DELAY_S = 0.1


class JournalModeError(sqlite3.OperationalError):
    """WAL was requested and did not take effect. Silently proceeding would
    promise concurrency the database does not have (SQLite keeps the previous
    mode when it cannot create WAL shared memory, e.g. on some network
    filesystems), so the boundary says so instead of assuming. It is an
    OperationalError so that every surface already handling database operation
    failures (the CLI's one-line error, the import path, the API dependency)
    reports it as one, with its own actionable message."""


def apply_pragmas(conn: sqlite3.Connection) -> str:
    """The pragmas every instance connection runs under, returning the journal
    mode actually in effect. journal_mode is a persistent property of the
    database file; the others are per connection."""
    # The busy timeout comes FIRST: switching the journal mode takes a
    # database lock itself, so a connection opened while another session is
    # writing would otherwise fail during setup instead of waiting.
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower()


def connect(path, **kwargs) -> sqlite3.Connection:
    conn = sqlite3.connect(path, **kwargs)
    try:
        effective = apply_pragmas(conn)
    except Exception:
        conn.close()
        raise
    # An in-memory database reports 'memory' and has no concurrency question
    # to answer; a file database that did not switch does.
    if effective not in ("wal", "memory"):
        conn.close()
        raise JournalModeError(
            f"WAL journal mode did not take effect for {path} (mode is"
            f" '{effective}'); this database cannot give the write concurrency"
            " the application expects, so it is not opened. A filesystem"
            " without shared memory support (some network mounts) is the usual"
            " cause")
    return conn


def journal_mode(conn: sqlite3.Connection) -> str:
    """The journal mode actually in effect, for verification rather than
    assumption (a database on a filesystem without shared memory silently
    stays in its previous mode)."""
    return conn.execute("PRAGMA journal_mode").fetchone()[0].lower()


def is_locked_error(error: BaseException) -> bool:
    """True only for the transient contention errors, never for a schema,
    constraint, or logic error that happens to be an OperationalError."""
    if not isinstance(error, sqlite3.OperationalError):
        return False
    message = str(error).lower()
    return "database is locked" in message or "database table is locked" in message


def retry_on_locked(operation, attempts: int = RETRY_ATTEMPTS,
                    base_delay_s: float = RETRY_BASE_DELAY_S, sleep=time.sleep):
    """Run a write transaction, retrying it whole while SQLite reports the
    database locked. The operation must be a complete transaction that rolls
    back on failure, so a retry re-runs it from a clean state; the last
    attempt's error propagates unchanged."""
    delay = base_delay_s
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except sqlite3.OperationalError as e:
            if attempt == attempts or not is_locked_error(e):
                raise
            sleep(delay)
            delay *= 2
