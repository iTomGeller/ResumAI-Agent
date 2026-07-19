from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class MemoryClient:
    """Layered-memory access via the Java control plane (the durable owner).

    The runtime only ever sees memory the Java side scopes to this
    run/conversation/user; failures degrade to empty results, never to
    fabricated context.
    """

    def __init__(self, run_id: str, conversation_id: str, user_id: str) -> None:
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.user_id = user_id
        self._base = settings.java_backend_url.rstrip("/")

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Internal-Token": settings.workflow_internal_token,
        }

    async def search(self, query: str, *, types: Optional[List[str]] = None,
                     top_k: int = 5, min_confidence: float = 0.35) -> List[Dict[str, Any]]:
        body = {
            "query": query,
            "types": types,
            "userId": self.user_id,
            "conversationId": self.conversation_id,
            "runId": self.run_id,
            "topK": top_k,
            "minConfidence": min_confidence,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self._base}/api/internal/agent-runs/memory/search",
                    json=body, headers=self._headers())
            if response.status_code >= 400:
                logger.info("memory search failed status=%s", response.status_code)
                return []
            return list(response.json().get("hits") or [])
        except Exception as exc:  # noqa: BLE001
            logger.info("memory search unavailable: %s", exc)
            return []

    async def write(self, *, type_: str, owner_scope: str, content: str,
                    structured: Optional[Dict[str, Any]] = None, source: str = "model_generated",
                    source_id: Optional[str] = None, confidence: float = 0.5,
                    ttl_days: Optional[int] = None) -> Optional[str]:
        body = {
            "type": type_,
            "ownerScope": owner_scope,
            "userId": self.user_id,
            "conversationId": self.conversation_id,
            "runId": self.run_id,
            "content": content,
            "structuredContent": structured or {},
            "source": source,
            "sourceId": source_id,
            "confidence": confidence,
            "ttlDays": ttl_days,
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self._base}/api/internal/agent-runs/memory/write",
                    json=body, headers=self._headers())
            if response.status_code >= 400:
                logger.info("memory write failed status=%s body=%s",
                            response.status_code, response.text[:150])
                return None
            return response.json().get("memoryId")
        except Exception as exc:  # noqa: BLE001
            logger.info("memory write unavailable: %s", exc)
            return None


class NullMemoryClient(MemoryClient):
    """Offline stand-in for tests/benchmarks with injectable canned memory."""

    def __init__(self, canned: Optional[List[Dict[str, Any]]] = None) -> None:
        super().__init__("test-run", "test-conv", "test-user")
        self.canned = canned or []
        self.writes: List[Dict[str, Any]] = []

    async def search(self, query: str, *, types: Optional[List[str]] = None,
                     top_k: int = 5, min_confidence: float = 0.35) -> List[Dict[str, Any]]:
        hits = self.canned
        if types:
            hits = [h for h in hits if h.get("type") in types]
        return hits[:top_k]

    async def write(self, *, type_: str, owner_scope: str, content: str,
                    structured: Optional[Dict[str, Any]] = None, source: str = "model_generated",
                    source_id: Optional[str] = None, confidence: float = 0.5,
                    ttl_days: Optional[int] = None) -> Optional[str]:
        self.writes.append({
            "type": type_, "ownerScope": owner_scope, "content": content,
            "structured": structured or {}, "source": source,
            "sourceId": source_id, "confidence": confidence,
        })
        return f"mem-test-{len(self.writes)}"
