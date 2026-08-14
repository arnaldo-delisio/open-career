"""One reentrant transaction boundary for repository writes.

sqlite3's connection context manager commits when its block exits, so a
repository write nested inside another one commits the outer work halfway
through: a multi-row write assembled from repository calls is not atomic by
construction, and an interruption leaves a row without the rows that make it
mean anything (a capability with no evidence chain). `transaction(conn)` is the
boundary that fixes that. The outermost block owns the commit and the rollback;
an inner block is a no-op, so a repository method stays atomic when called
alone and joins a larger write unchanged.
"""

from contextlib import contextmanager

# Nesting depth per open connection, keyed by identity because a sqlite3
# connection accepts neither attributes nor weak references. Entries are
# pushed and popped in pairs, so the map is empty whenever no write is in
# flight, and a connection's key is never left behind for a later object to
# inherit by address reuse.
_DEPTH: dict[int, int] = {}


@contextmanager
def transaction(conn):
    key = id(conn)
    depth = _DEPTH.get(key, 0)
    _DEPTH[key] = depth + 1
    try:
        yield conn
    except BaseException:
        if depth == 0:
            conn.rollback()
        raise
    else:
        if depth == 0:
            conn.commit()
    finally:
        if depth == 0:
            _DEPTH.pop(key, None)
        else:
            _DEPTH[key] = depth
