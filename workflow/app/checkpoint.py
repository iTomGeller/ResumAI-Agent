from __future__ import annotations

import logging
from typing import Optional

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.config import settings

logger = logging.getLogger(__name__)

_checkpointer: Optional[AsyncPostgresSaver] = None


async def get_checkpointer() -> Optional[AsyncPostgresSaver]:
    global _checkpointer
    if _checkpointer is not None:
        return _checkpointer
    uri = settings.langgraph_checkpoint_db_uri
    if not uri or "postgres" not in uri:
        logger.warning("LangGraph checkpoint URI not configured, running without checkpointer")
        return None
    try:
        saver = AsyncPostgresSaver.from_conn_string(uri)
        await saver.setup()
        _checkpointer = saver
        return _checkpointer
    except Exception as exc:
        logger.warning("Failed to init AsyncPostgresSaver: %s", exc)
        return None
