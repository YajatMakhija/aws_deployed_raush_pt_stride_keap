from __future__ import annotations

import atexit
from contextlib import contextmanager
from functools import cache

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings


@cache
def get_pool() -> ConnectionPool:
    settings = get_settings()
    url = settings.supabase_db_url
    if not url:
        raise RuntimeError("SUPABASE_DB_URL is required for database operations")
    return ConnectionPool(
        conninfo=url,
        min_size=0,
        max_size=settings.db_pool_max_size,
        max_idle=settings.db_pool_max_idle_seconds,
        open=True,
        timeout=settings.db_pool_timeout_seconds,
        kwargs={"row_factory": dict_row, "connect_timeout": int(settings.db_pool_timeout_seconds)},
    )


@contextmanager
def transaction():
    with get_pool().connection() as conn, conn.transaction():
        yield conn


def record_integration_event(
    request_id: str,
    direction: str,
    provider: str,
    operation: str,
    status: str,
    *,
    http_status: int | None = None,
    error_category: str | None = None,
) -> None:
    """Best-effort PHI-free audit; an audit outage must not break a patient interaction."""
    if not get_settings().supabase_db_url:
        return
    try:
        with transaction() as conn:
            conn.execute(
                "insert into integration_events(request_id,direction,provider,operation,status,"
                "http_status,error_category) values(%s,%s,%s,%s,%s,%s,%s)",
                (
                    request_id,
                    direction,
                    provider,
                    operation,
                    status,
                    http_status,
                    error_category,
                ),
            )
    except Exception:  # noqa: BLE001 - the primary workflow remains authoritative
        return


def _close_pool() -> None:
    if get_pool.cache_info().currsize:
        get_pool().close()


atexit.register(_close_pool)
