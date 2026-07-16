from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import FastAPI, HTTPException

from app.graph import run_workflow
from app.models import WorkflowRunAccepted, WorkflowRunRequest, WorkflowState
from app.events import emit_result

logger = logging.getLogger(__name__)

app = FastAPI(title="ResumAI LangGraph Workflow", version="1.0.0")

_running: set[str] = set()


@app.get("/health")
async def health() -> dict:
    return {"status": "UP", "service": "ai-resume-workflow"}


@app.post("/workflow/runs", response_model=WorkflowRunAccepted)
async def start_workflow(request: WorkflowRunRequest) -> WorkflowRunAccepted:
    if not request.traceId or not request.resumeText:
        raise HTTPException(status_code=400, detail="traceId and resumeText required")
    if request.traceId in _running:
        raise HTTPException(status_code=409, detail="workflow already running for trace")

    workflow_run_id = f"wr-{uuid.uuid4()}"
    initial = WorkflowState(
        traceId=request.traceId,
        workflowRunId=workflow_run_id,
        resumeText=request.resumeText,
        jobCategory=request.jobCategory,
        jobDescription=request.jobDescription,
        executionMode=request.executionMode,
    )

    _running.add(request.traceId)
    asyncio.create_task(_execute_background(initial))
    return WorkflowRunAccepted(
        workflowRunId=workflow_run_id,
        traceId=request.traceId,
        status="ACCEPTED",
    )


async def _execute_background(initial: WorkflowState) -> None:
    try:
        result = await run_workflow(initial)
        await emit_result(result)
    except Exception as exc:
        logger.exception("background workflow error: %s", exc)
        from app.models import WorkflowResultPayload

        await emit_result(
            WorkflowResultPayload(
                traceId=initial.traceId,
                workflowRunId=initial.workflowRunId,
                status="FAILED",
                summary=str(exc),
                errorMessage=str(exc),
            )
        )
    finally:
        _running.discard(initial.traceId)
