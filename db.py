"""Thin PostgreSQL helper: a connection pool and a transaction context manager."""
import os
from contextlib import contextmanager

from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(
            1,
            int(os.environ.get("DB_POOL_MAX", "10")),
            dsn=os.environ["DATABASE_URL"],
        )
    return _pool


@contextmanager
def transaction():
    """Yield a dict-cursor inside a transaction.

    Commits when the block finishes normally, rolls back if it raises.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)
