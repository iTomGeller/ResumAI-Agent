from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_checkpointer: Optional[Any] = None
_checkpointer_context: Optional[Any] = None
_checkpointer_lock = asyncio.Lock()


async def get_checkpointer() -> Optional[Any]:
    """Return a shared LangGraph checkpointer, or ``None`` when unavailable.

    ``AsyncPostgresSaver.from_conn_string`` is an async context manager in
    current LangGraph releases.  Keeping that context open for the application
    lifetime avoids returning a closed connection pool.  ``memory://`` is
    supported for dependency-light tests, but production pause/resume should
    use Postgres.
    """

    global _checkpointer, _checkpointer_context
    if _checkpointer is not None:
        return _checkpointer
    async with _checkpointer_lock:
        if _checkpointer is not None:
            return _checkpointer
        try:
            from app.config import settings

            uri = (settings.langgraph_checkpoint_db_uri or "").strip()
        except Exception as exc:
            logger.warning("LangGraph checkpoint settings unavailable: %s", exc)
            return None
        if not uri:
            logger.warning("LangGraph checkpoint URI not configured; pause/resume is disabled")
            return None

        if uri.lower().startswith("memory://"):
            try:
                from langgraph.checkpoint.memory import MemorySaver

                _checkpointer = MemorySaver()
                return _checkpointer
            except Exception as exc:
                logger.warning("Failed to init MemorySaver: %s", exc)
                return None

        if "postgres" not in uri.lower():
            logger.warning("Unsupported LangGraph checkpoint URI; pause/resume is disabled")
            return None
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            candidate = AsyncPostgresSaver.from_conn_string(uri)
            if hasattr(candidate, "__aenter__"):
                _checkpointer_context = candidate
                saver = await candidate.__aenter__()
            else:  # compatibility with older checkpoint-postgres releases
                saver = candidate
            setup = getattr(saver, "setup", None)
            if setup is not None:
                result = setup()
                if hasattr(result, "__await__"):
                    await result
            _checkpointer = saver
            return _checkpointer
        except Exception as exc:
            logger.warning("Failed to init AsyncPostgresSaver: %s", exc)
            context = _checkpointer_context
            _checkpointer_context = None
            if context is not None and hasattr(context, "__aexit__"):
                try:
                    await context.__aexit__(type(exc), exc, exc.__traceback__)
                except Exception:
                    pass
            return None


async def checkpointer_available() -> bool:
    return await get_checkpointer() is not None


async def close_checkpointer() -> None:
    global _checkpointer, _checkpointer_context
    async with _checkpointer_lock:
        context = _checkpointer_context
        saver = _checkpointer
        _checkpointer = None
        _checkpointer_context = None
        if context is not None and hasattr(context, "__aexit__"):
            try:
                await context.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning("Failed to close LangGraph checkpointer context: %s", exc)
            return
        close = getattr(saver, "aclose", None) or getattr(saver, "close", None)
        if close is not None:
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                logger.warning("Failed to close LangGraph checkpointer: %s", exc)


def set_checkpointer_for_tests(checkpointer: Optional[Any]) -> None:
    """Inject a saver in unit tests without starting Postgres."""

    global _checkpointer, _checkpointer_context
    _checkpointer = checkpointer
    _checkpointer_context = None
