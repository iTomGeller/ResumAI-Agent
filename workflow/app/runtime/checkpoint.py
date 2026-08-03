from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import settings

logger = logging.getLogger(__name__)

_checkpointer: Optional[Any] = None
_pool: Optional[AsyncConnectionPool] = None
_lock = asyncio.Lock()


async def initialize_checkpointer() -> Any:
    """Open the shared PostgreSQL-backed LangGraph checkpointer.

    LangGraph is an explicitly selected runtime, so initialization is
    fail-closed: an enabled graph runtime must never silently fall back to an
    in-memory saver or to the legacy executor after accepting a run.
    """

    global _checkpointer, _pool
    if _checkpointer is not None:
        return _checkpointer

    async with _lock:
        if _checkpointer is not None:
            return _checkpointer

        dsn = (settings.langgraph_checkpoint_dsn or "").strip()
        if not dsn:
            raise RuntimeError(
                "LANGGRAPH_CHECKPOINT_DSN is required when LangGraph runtime is enabled"
            )

        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            pool = AsyncConnectionPool(
                conninfo=dsn,
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
                open=False,
                name="resumai-langgraph-checkpoints",
            )
            await pool.open(wait=True)
            saver = AsyncPostgresSaver(pool)
            await saver.setup()
        except Exception:
            logger.exception("failed to initialize LangGraph PostgreSQL checkpointer")
            if "pool" in locals():
                await pool.close()
            raise

        _pool = pool
        _checkpointer = saver
        logger.info("LangGraph PostgreSQL checkpointer ready")
        return saver


async def get_checkpointer() -> Any:
    if _checkpointer is not None:
        return _checkpointer
    return await initialize_checkpointer()


def checkpointer_ready() -> bool:
    return _checkpointer is not None


async def close_checkpointer() -> None:
    global _checkpointer, _pool
    async with _lock:
        pool = _pool
        _checkpointer = None
        _pool = None
        if pool is not None:
            await pool.close()


def set_checkpointer_for_tests(checkpointer: Optional[Any]) -> None:
    """Inject a dependency-light saver without opening PostgreSQL."""

    global _checkpointer, _pool
    _checkpointer = checkpointer
    _pool = None
