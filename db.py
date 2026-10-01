# db.py — PostgreSQL connection layer for BitLink.
#
# PostgreSQL is the only source of truth. Google Sheets was the previous store
# and is gone: there is no read or write fallback anywhere in the runtime path.
# Connection helpers still return neutral values on failure so a bridge process
# can degrade quietly, but a caller that needs data will see empty/None and
# report the outage rather than silently serving stale Sheets content.

import logging
import os
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.pool import PoolError
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s'
)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/bitlink")

_pool: Optional["_BoundedPool"] = None
_pool_lock = threading.Lock()
_unavailable_logged = False

# The bridges and the control dashboard are threaded and bursty: a dashboard
# save fires several requests at once, and each bridge holds its own thread pool.
# A small pool would exhaust under that load, and every caller would then see a
# spurious "database is down". This is sized for concurrency, not for memory,
# since connections here are cheap and short-lived.
POOL_MIN_CONN = 2
POOL_MAX_CONN = int(os.getenv("DB_POOL_MAX", "30"))
# How long a caller waits for a free connection before giving up. Long enough to
# ride out a burst, short enough that a genuinely stuck database does not hang
# request threads.
POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "10"))


class _BoundedPool:
    """Thread-safe connection pool with real waiting semantics.

    psycopg2's ThreadedConnectionPool raises PoolError the instant the pool is
    empty: it has no timeout and never blocks. Under a burst of concurrent
    requests that becomes a wave of spurious failures, which callers would
    otherwise interpret as a real outage and report to the user as one.

    This wraps it in a semaphore so callers queue for a connection for up to
    POOL_TIMEOUT rather than failing immediately.
    """

    def __init__(self, minconn: int, maxconn: int, dsn: str, timeout: float):
        self._pool = ThreadedConnectionPool(minconn, maxconn, dsn=dsn)
        self._sem = threading.BoundedSemaphore(maxconn)
        self._timeout = timeout

    def getconn(self) -> Any:
        if not self._sem.acquire(timeout=self._timeout):
            raise PoolError(
                f"timed out after {self._timeout}s waiting for a free connection"
            )
        try:
            return self._pool.getconn()
        except Exception:
            # Release the slot we just took, or it leaks and the pool wedges.
            self._sem.release()
            raise

    def putconn(self, conn: Any) -> None:
        try:
            self._pool.putconn(conn)
        finally:
            self._sem.release()

    def closeall(self) -> None:
        self._pool.closeall()


def _get_pool() -> Optional[_BoundedPool]:
    """Lazily build the connection pool. Returns None if the DB is unreachable."""
    global _pool, _unavailable_logged
    with _pool_lock:
        if _pool is not None:
            return _pool
        try:
            _pool = _BoundedPool(POOL_MIN_CONN, POOL_MAX_CONN, DATABASE_URL, POOL_TIMEOUT)
            logging.info(
                f"PostgreSQL connection pool initialised "
                f"(min={POOL_MIN_CONN}, max={POOL_MAX_CONN})."
            )
            return _pool
        except Exception as e:
            if not _unavailable_logged:
                logging.error(
                    f"PostgreSQL unavailable ({e}). There is no fallback store; "
                    f"reads will return empty and writes will fail until it recovers."
                )
                _unavailable_logged = True
            return None


def is_available() -> bool:
    """True if we can reach PostgreSQL right now."""
    pool = _get_pool()
    if pool is None:
        return False
    try:
        conn = pool.getconn()
    except PoolError:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.rollback()
        return True
    except Exception:
        return False
    finally:
        pool.putconn(conn)


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


@contextmanager
def _cursor(commit: bool = False):
    """Yield a RealDictCursor, always returning the connection to the pool."""
    pool = _get_pool()
    if pool is None:
        raise RuntimeError("PostgreSQL pool is not available")
    try:
        conn = pool.getconn()
    except PoolError as e:
        # Exhaustion is a capacity problem, not an outage. Say so plainly, so a
        # burst of traffic is never mistaken for the database being down.
        raise RuntimeError(
            f"no free PostgreSQL connection after {POOL_TIMEOUT}s "
            f"(pool max={POOL_MAX_CONN}): {e}"
        ) from e
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        if commit:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        pool.putconn(conn)


def query_all(sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    """Run a SELECT and return all rows as dicts."""
    with _cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
    """Run a SELECT and return the first row, or None."""
    with _cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """Run an INSERT/UPDATE/DELETE and return the affected row count."""
    with _cursor(commit=True) as cur:
        cur.execute(sql, params)
        return cur.rowcount


def execute_returning_id(sql: str, params: Sequence[Any] = ()) -> Optional[int]:
    """Run an INSERT and return the generated primary key."""
    with _cursor(commit=True) as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        if not row:
            return None
        # RealDictCursor: prefer an explicit id column, else fall back to the first.
        if "id" in row:
            return row["id"]
        return list(row.values())[0]


def execute_many(sql: str, rows: Iterable[Sequence[Any]]) -> None:
    """Batch insert in a single transaction."""
    with _cursor(commit=True) as cur:
        psycopg2.extras.execute_batch(cur, sql, rows)


def run_script(sql: str) -> None:
    """Execute a multi-statement SQL file (used to apply schema.sql)."""
    pool = _get_pool()
    if pool is None:
        raise RuntimeError("PostgreSQL pool is not available")
    try:
        conn = pool.getconn()
    except PoolError as e:
        raise RuntimeError(
            f"no free PostgreSQL connection after {POOL_TIMEOUT}s: {e}"
        ) from e
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)
