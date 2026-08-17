from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.runtime import gateway


@dataclass(frozen=True)
class RetrievalResult:
    """One deterministic business-RAG result prepared before generation."""

    retrieval_id: str
    source: str
    query: str
    result: Optional[Dict[str, Any]]
    error: Optional[str]
    duration_ms: int

    @property
    def succeeded(self) -> bool:
        return self.result is not None and self.error is None


class BusinessRagRetriever:
    """Pre-generation retrieval for the three business RAG sources.

    This is intentionally separate from ToolExecutor: the model neither
    chooses these calls nor receives them in the provider tool catalog.
    """

    async def retrieve(
            self, source: str, *, query: str = "", top_k: int = 5,
            resume_text: str = "", job_description: str = "",
    ) -> RetrievalResult:
        retrieval_id = f"rag-{uuid.uuid4().hex[:16]}"
        started = time.perf_counter()
        try:
            if source == "jd":
                raw = await gateway.java_jd_search(
                    resume_text=resume_text, top_k=top_k)
            elif source == "resume":
                raw = await gateway.java_resume_search(
                    query=query, top_k=top_k, resume_text=resume_text,
                    jd_requirements=job_description[:2000],
                    strategy="hybrid")
            elif source == "knowledge":
                raw = await gateway.java_knowledge_search(
                    query=query, top_k=top_k, rerank=False)
            else:
                raise ValueError(f"unknown business RAG source: {source}")
            parsed = self._as_object(raw)
            error = None
        except Exception as exc:  # noqa: BLE001 - retrieval degrades to empty
            parsed = None
            error = f"{type(exc).__name__}: {exc}"[:300]
        duration_ms = int((time.perf_counter() - started) * 1000)
        if parsed is not None:
            latency = parsed.get("_latency")
            latency = dict(latency) if isinstance(latency, dict) else {}
            latency.setdefault("retrieval_ms", duration_ms)
            latency.setdefault("total_ms", duration_ms)
            parsed["_latency"] = latency
        return RetrievalResult(
            retrieval_id=retrieval_id,
            source=source,
            query=query,
            result=parsed,
            error=error,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _as_object(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, str):
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        raise ValueError("RAG backend did not return a JSON object")
