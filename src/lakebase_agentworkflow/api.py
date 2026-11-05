from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from lakebase_agentworkflow.helpers.env import env_truthy, load_env_file
from lakebase_agentworkflow.helpers.lakebase import get_repository
from lakebase_agentworkflow.helpers.models import (
    AgentMessage,
    AgentRequest,
    AgentRunStatus,
    ReviewDecision,
    ReviewDecisionPayload,
    StartWorkflowRequest,
    StartWorkflowResponse,
    TokenUsage,
    WorkerCrashRequest,
    WorkflowRunListResponse,
    WorkflowRunView,
)
from lakebase_agentworkflow.helpers.temporal_client import connect_temporal
from lakebase_agentworkflow.workflows import LakebaseAgentWorkflow, WorkerCrashDemoWorkflow


load_env_file()

logger = logging.getLogger(__name__)
app = FastAPI(title="Lakebase AgentWorkflow API")


class ReviewSubmissionResponse(BaseModel):
    accepted: bool
    run_id: str
    workflow_id: str
    decision: ReviewDecision


class RuntimeConfigResponse(BaseModel):
    temporal_ui_url: str
    temporal_namespace: str
    demo_controls_enabled: bool


class DemoWorkerCrashRequest(BaseModel):
    requested_by: str = Field(..., min_length=1)


class DemoControlResponse(BaseModel):
    accepted: bool
    control_id: str
    workflow_id: str
    message: str


class ReadinessResponse(BaseModel):
    status: str
    lakebase: str
    temporal: str


class RunMetricsResponse(BaseModel):
    total_runs: int = Field(ge=0)
    completed_runs: int = Field(ge=0)
    success_rate: float = Field(ge=0, le=1)
    p50_latency_ms: int | None = Field(default=None, ge=0)
    p95_latency_ms: int | None = Field(default=None, ge=0)
    counts_by_status: dict[str, int]


class RetryMetricResponse(BaseModel):
    activity_type: str
    attempt_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    last_attempt_completed_at: datetime | None = None


class CostMetricResponse(BaseModel):
    bucket_start: datetime
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    turn_count: int = Field(ge=0)


cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "API_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready", response_model=ReadinessResponse)
async def readiness() -> ReadinessResponse:
    try:
        get_repository().list_runs(limit=1)
    except Exception as exc:
        logger.exception("Lakebase readiness check failed")
        raise HTTPException(status_code=503, detail="Lakebase is not ready") from exc

    try:
        client = await connect_temporal()
        temporal_healthy = await client.service_client.check_health()
        if not temporal_healthy:
            raise RuntimeError("Temporal health check returned unhealthy")
    except Exception as exc:
        logger.exception("Temporal readiness check failed")
        raise HTTPException(status_code=503, detail="Temporal is not ready") from exc

    return ReadinessResponse(
        status="ready",
        lakebase="ready",
        temporal="ready",
    )


@app.get("/api/runtime-config", response_model=RuntimeConfigResponse)
def runtime_config() -> RuntimeConfigResponse:
    return RuntimeConfigResponse(
        temporal_ui_url=(
            os.getenv("TEMPORAL_UI_URL", "http://localhost:8233").rstrip("/")
        ),
        # The demo platform names the UI's namespace; it can differ from the connection's.
        temporal_namespace=(
            os.getenv("TEMPORAL_UI_NAMESPACE") or os.getenv("TEMPORAL_NAMESPACE", "default")
        ),
        demo_controls_enabled=env_truthy("DEMO_CONTROLS_ENABLED"),
    )


@app.get("/api/runs", response_model=WorkflowRunListResponse)
def list_runs(limit: int = Query(default=25, ge=1, le=100)) -> WorkflowRunListResponse:
    try:
        return WorkflowRunListResponse(runs=get_repository().list_runs(limit=limit))
    except Exception as exc:
        logger.exception("Lakebase run list query failed")
        raise HTTPException(
            status_code=503,
            detail="Lakebase query failed; check the API server logs.",
        ) from exc


@app.get("/api/runs/{run_id}", response_model=StartWorkflowResponse)
def get_run(run_id: str) -> StartWorkflowResponse:
    try:
        run = get_repository().get_run(run_id)
    except Exception as exc:
        logger.exception("Lakebase run query failed")
        raise HTTPException(
            status_code=503,
            detail="Lakebase query failed; check the API server logs.",
        ) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return StartWorkflowResponse(run=run)


@app.post("/api/workflows", response_model=StartWorkflowResponse)
async def start_workflow(payload: StartWorkflowRequest) -> StartWorkflowResponse:
    if payload.demo_model_failure_attempts and not env_truthy(
        "DEMO_CONTROLS_ENABLED"
    ):
        raise HTTPException(status_code=403, detail="Demo controls are disabled")

    run_id = str(uuid.uuid4())
    workflow_id = f"lakebase-agent-{run_id}"
    agent_request = AgentRequest(
        run_id=run_id,
        user_id=payload.user_id,
        user_request=payload.user_request,
        applicant_id=payload.applicant_id,
        loan_amount=payload.loan_amount,
        loan_purpose=payload.loan_purpose,
        model=payload.model,
        max_turns=payload.max_turns,
        demo_model_failure_attempts=payload.demo_model_failure_attempts,
        metadata=payload.metadata,
    )

    try:
        client = await connect_temporal()
        await client.start_workflow(
            LakebaseAgentWorkflow.run,
            agent_request,
            id=workflow_id,
            task_queue=os.getenv("TEMPORAL_TASK_QUEUE", "lakebase-agent-task-queue"),
        )
    except Exception as exc:
        logger.exception("Temporal workflow start failed")
        raise HTTPException(
            status_code=503,
            detail="Temporal workflow start failed; check the API server logs.",
        ) from exc

    try:
        run = get_repository().get_run(run_id)
    except Exception:
        logger.info("Started workflow before Lakebase returned a run row", exc_info=True)
        run = None
    run = run or _pending_run_view(run_id=run_id, workflow_id=workflow_id, payload=payload)
    return StartWorkflowResponse(run=run)


@app.post(
    "/api/demo-controls/crash-worker",
    response_model=DemoControlResponse,
    status_code=202,
)
async def crash_worker(payload: DemoWorkerCrashRequest) -> DemoControlResponse:
    if not env_truthy("DEMO_CONTROLS_ENABLED"):
        raise HTTPException(status_code=404, detail="Demo controls are disabled")

    control_id = str(uuid.uuid4())
    workflow_id = f"lakebase-agent-worker-crash-{control_id}"
    request = WorkerCrashRequest(
        control_id=control_id,
        requested_by=payload.requested_by,
    )
    try:
        client = await connect_temporal()
        await client.start_workflow(
            WorkerCrashDemoWorkflow.run,
            request,
            id=workflow_id,
            task_queue=os.getenv("TEMPORAL_TASK_QUEUE", "lakebase-agent-task-queue"),
        )
    except Exception as exc:
        logger.exception("Worker crash demo workflow start failed")
        raise HTTPException(
            status_code=503,
            detail="Worker crash demo could not be started; check the API server logs.",
        ) from exc

    return DemoControlResponse(
        accepted=True,
        control_id=control_id,
        workflow_id=workflow_id,
        message=(
            "Crash requested. The worker exits on activity attempt 1 and Temporal "
            "confirms recovery on attempt 2."
        ),
    )


@app.post(
    "/api/runs/{run_id}/review",
    response_model=ReviewSubmissionResponse,
    status_code=202,
)
async def submit_review(
    run_id: str,
    payload: ReviewDecisionPayload,
) -> ReviewSubmissionResponse:
    try:
        run = get_repository().get_run(run_id)
    except Exception as exc:
        logger.exception("Lakebase run lookup failed before review signal")
        raise HTTPException(
            status_code=503,
            detail="Lakebase query failed; check the API server logs.",
        ) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != AgentRunStatus.AWAITING_REVIEW:
        raise HTTPException(
            status_code=409,
            detail=f"Run is {run.status.value}; only awaiting-review runs accept a decision.",
        )

    review_metadata = run.metadata.get("review", {})
    review_round = (
        review_metadata.get("round")
        if isinstance(review_metadata, dict)
        else None
    )
    if not isinstance(review_round, int):
        review_round = run.turns
    expected_review_id = f"{run.run_id}-review-{review_round}"
    if payload.review_id != expected_review_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Review round changed; expected {expected_review_id}. "
                "Refresh the run before submitting this decision."
            ),
        )

    try:
        client = await connect_temporal()
        handle = client.get_workflow_handle(run.workflow_id)
        await handle.signal(LakebaseAgentWorkflow.submit_review_decision, payload)
    except Exception as exc:
        logger.exception("Temporal review signal failed")
        raise HTTPException(
            status_code=503,
            detail="Temporal review signal failed; check the API server logs.",
        ) from exc

    return ReviewSubmissionResponse(
        accepted=True,
        run_id=run.run_id,
        workflow_id=run.workflow_id,
        decision=payload.decision,
    )


@app.get("/api/metrics/runs", response_model=RunMetricsResponse)
def get_run_metrics() -> RunMetricsResponse:
    try:
        return RunMetricsResponse.model_validate(get_repository().get_run_metrics())
    except Exception as exc:
        logger.exception("Lakebase workflow metrics query failed")
        raise HTTPException(
            status_code=503,
            detail="Lakebase metrics query failed; check the API server logs.",
        ) from exc


@app.get("/api/metrics/retries", response_model=list[RetryMetricResponse])
def get_retry_metrics() -> list[RetryMetricResponse]:
    try:
        return [
            RetryMetricResponse.model_validate(row)
            for row in get_repository().get_retry_metrics()
        ]
    except Exception as exc:
        logger.exception("Lakebase activity retry metrics query failed")
        raise HTTPException(
            status_code=503,
            detail="Lakebase metrics query failed; check the API server logs.",
        ) from exc


@app.get("/api/metrics/cost", response_model=list[CostMetricResponse])
def get_cost_metrics(
    hours: int = Query(default=24, ge=1, le=168),
) -> list[CostMetricResponse]:
    try:
        return [
            CostMetricResponse.model_validate(row)
            for row in get_repository().get_cost_metrics(hours=hours)
        ]
    except Exception as exc:
        logger.exception("Lakebase token cost metrics query failed")
        raise HTTPException(
            status_code=503,
            detail="Lakebase metrics query failed; check the API server logs.",
        ) from exc


def _pending_run_view(
    *,
    run_id: str,
    workflow_id: str,
    payload: StartWorkflowRequest,
) -> WorkflowRunView:
    now = datetime.now(timezone.utc)
    metadata = {
        **payload.metadata,
        "applicant_id": payload.applicant_id,
        "loan_amount": payload.loan_amount,
        "loan_purpose": payload.loan_purpose,
        "demo_model_failure_attempts": payload.demo_model_failure_attempts,
    }
    return WorkflowRunView(
        run_id=run_id,
        workflow_id=workflow_id,
        user_id=payload.user_id,
        user_request=payload.user_request,
        status=AgentRunStatus.PENDING,
        final_response=None,
        metadata=metadata,
        turns=0,
        usage=TokenUsage(),
        created_at=now,
        updated_at=now,
        messages=[
            AgentMessage(
                role="user",
                content=payload.user_request,
                metadata={"source": "api_request", **metadata},
            )
        ],
    )


def mount_built_frontend(
    application: FastAPI,
    dist_dir: str | Path,
) -> None:
    """Mount the production React build after API routes have been registered."""
    directory = Path(dist_dir).resolve()
    if not (directory / "index.html").is_file():
        raise RuntimeError(
            f"Built frontend not found at {directory}; run the React production build first"
        )
    application.mount(
        "/",
        StaticFiles(directory=str(directory), html=True),
        name="frontend",
    )


if os.getenv("SERVE_BUILT_FRONTEND", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}:
    mount_built_frontend(app, os.getenv("WEB_DIST_DIR", "web/dist"))
