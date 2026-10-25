from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_pool: ConnectionPool | None = None


def open_pool(database_url: str) -> None:
    global _pool
    _pool = ConnectionPool(
        database_url, min_size=1, max_size=10, kwargs={"row_factory": dict_row}, open=True
    )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """One transaction: committed when the block exits normally, rolled back if it raises."""
    if _pool is None:
        raise RuntimeError("Database pool is not open")
    with _pool.connection() as conn:
        yield conn


def init_schema(conn: psycopg.Connection) -> None:
    # API and worker may start together; serialize additive schema application.
    conn.execute("select pg_advisory_xact_lock(7201000)")
    conn.execute(SCHEMA_PATH.read_text())
    conn.execute(SCHEMA_PATH.with_name("trust_schema.sql").read_text())
    conn.execute(SCHEMA_PATH.with_name("requester_schema.sql").read_text())
