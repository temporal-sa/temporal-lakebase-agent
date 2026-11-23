from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_TOOL = "waiting_for_tool"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolName(StrEnum):
    CREDIT_CHECK = "credit_check"
    INCOME_VERIFICATION = "income_verification"
    DEBT_TO_INCOME_CALC = "debt_to_income_calc"
    POLICY_LOOKUP = "policy_lookup"
    SAVE_NOTE = "save_note"


class UnderwritingDecision(StrEnum):
    APPROVE = "approve"
    DECLINE = "decline"
    REFER = "refer"


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    DENY = "deny"
    REQUEST_MORE_INFO = "request_more_info"


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class AgentRequest(BaseModel):
    run_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    user_request: str = Field(..., min_length=1)
    applicant_id: str = Field(..., min_length=1)
    loan_amount: float = Field(..., gt=0)
    loan_purpose: str = Field(..., min_length=1)
    model: str = "gpt-6-luna"
    max_turns: int = Field(default=8, ge=5, le=16)
    demo_model_failure_attempts: int = Field(default=0, ge=0, le=3)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentSignalPayload(BaseModel):
    message_id: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LoanRecommendation(BaseModel):
    decision: UnderwritingDecision
    rationale: str = Field(..., min_length=1)
    dti: float = Field(..., ge=0)
    credit_score: int = Field(..., ge=300, le=850)


class ReviewDecisionPayload(BaseModel):
    reviewer_id: str = Field(..., min_length=1)
    decision: ReviewDecision
    rationale: str = Field(..., min_length=1)
    review_id: str = Field(..., min_length=1)
    decision_id: str = Field(..., min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolSchema(BaseModel):
    name: ToolName
    description: str
    parameters: dict[str, Any]


class ToolCall(BaseModel):
    id: str = Field(..., min_length=1)
    name: ToolName
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelRequest(BaseModel):
    run_id: str
    turn: int
    model: str
    user_id: str
    messages: list[AgentMessage]
    applicant_id: str
    loan_amount: float = Field(..., gt=0)
    loan_purpose: str
    tools: list[ToolSchema]
    simulated_failure_attempts: int = Field(default=0, ge=0, le=3)


class ModelDecision(BaseModel):
    assistant_message: str
    final: bool = False
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionRequest(BaseModel):
    run_id: str
    turn: int
    user_id: str
    tool_call: ToolCall
    applicant_id: str
    loan_amount: float = Field(..., gt=0)
    loan_purpose: str


class ToolResult(BaseModel):
    tool_call_id: str
    name: ToolName
    success: bool
    content: str
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class AgentSnapshot(BaseModel):
    run_id: str
    status: AgentRunStatus
    current_turn: int = 0
    last_message: str | None = None
    pending_signal_count: int = 0
    tool_call_count: int = 0
    recommendation: LoanRecommendation | None = None
    review_decision: ReviewDecisionPayload | None = None


class AgentResult(BaseModel):
    run_id: str
    status: AgentRunStatus
    final_response: str
    turns: int
    tool_results: list[ToolResult] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    recommendation: LoanRecommendation | None = None
    review_decision: ReviewDecisionPayload | None = None


class WorkflowRunView(BaseModel):
    run_id: str
    workflow_id: str
    user_id: str
    user_request: str
    status: AgentRunStatus
    final_response: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    recommendation: LoanRecommendation | None = None
    review_decision: ReviewDecisionPayload | None = None
    turns: int = 0
    tool_results: list[ToolResult] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    created_at: datetime
    updated_at: datetime
    messages: list[AgentMessage] = Field(default_factory=list)


class WorkflowRunListResponse(BaseModel):
    runs: list[WorkflowRunView]


class StartWorkflowRequest(BaseModel):
    user_request: str = Field(..., min_length=1)
    user_id: str = Field(default="local-user@example.com", min_length=1)
    applicant_id: str = Field(..., min_length=1)
    loan_amount: float = Field(..., gt=0)
    loan_purpose: str = Field(..., min_length=1)
    model: str = "gpt-6-luna"
    max_turns: int = Field(default=8, ge=5, le=16)
    demo_model_failure_attempts: int = Field(default=0, ge=0, le=3)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StartWorkflowResponse(BaseModel):
    run: WorkflowRunView


class WorkerCrashRequest(BaseModel):
    control_id: str = Field(..., min_length=1)
    requested_by: str = Field(..., min_length=1)


class WorkerCrashResult(BaseModel):
    control_id: str
    recovered: bool
    completed_attempt: int = Field(..., ge=1)


class LakebaseWriteResult(BaseModel):
    written: bool
    skipped: bool = False
    row_count: int = 0
    message: str = ""


class LakebaseEvent(BaseModel):
    event_id: str = Field(..., min_length=1)
    run_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class RunStartedRecord(BaseModel):
    run_id: str
    workflow_id: str
    user_id: str
    user_request: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunCompletedRecord(BaseModel):
    run_id: str
    status: AgentRunStatus
    final_response: str
    turns: int
    usage: TokenUsage


class MessageRecord(BaseModel):
    message_id: str = Field(..., min_length=1)
    run_id: str
    turn: int
    message: AgentMessage


class ToolCallRecord(BaseModel):
    run_id: str
    turn: int
    tool_call: ToolCall


class ToolResultRecord(BaseModel):
    run_id: str
    turn: int
    result: ToolResult


class UnderwritingPolicy(BaseModel):
    loan_purpose: str
    min_credit_score: int = Field(..., ge=300, le=850)
    max_dti: float = Field(..., gt=0)
    decline_below_credit_score: int = Field(..., ge=300, le=850)
    decline_above_dti: float = Field(..., gt=0)


class ReviewPendingRecord(BaseModel):
    run_id: str
    recommendation: LoanRecommendation
    review_round: int = Field(..., ge=1)
    usage: TokenUsage = Field(default_factory=TokenUsage)


class ReviewDecisionRecord(BaseModel):
    review_id: str = Field(..., min_length=1)
    decision_id: str = Field(..., min_length=1)
    review_round: int = Field(..., ge=1)
    run_id: str
    reviewer_id: str
    decision: ReviewDecision
    rationale: str
    model_recommendation: LoanRecommendation
    decided_at: datetime


class WorkflowMetricStartedRecord(BaseModel):
    run_id: str
    workflow_id: str
    workflow_run_id: str
    workflow_type: str
    task_queue: str
    started_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowMetricCompletedRecord(BaseModel):
    run_id: str
    workflow_id: str
    workflow_run_id: str
    workflow_type: str
    status: AgentRunStatus
    started_at: datetime
    completed_at: datetime
    latency_ms: int = Field(..., ge=0)
    turn_count: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TurnMetricRecord(BaseModel):
    run_id: str
    workflow_id: str
    workflow_run_id: str
    workflow_type: str
    turn: int = Field(..., ge=0)
    status: str
    started_at: datetime
    completed_at: datetime
    latency_ms: int = Field(..., ge=0)
    model_latency_ms: int = Field(default=0, ge=0)
    tool_latency_ms: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActivityAttemptMetricRecord(BaseModel):
    run_id: str
    workflow_id: str
    workflow_run_id: str
    workflow_type: str
    activity_id: str
    activity_type: str
    attempt: int = Field(..., ge=1)
    turn: int | None = None
    status: str
    started_at: datetime
    completed_at: datetime
    latency_ms: int = Field(..., ge=0)
    error_type: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecentContextRequest(BaseModel):
    run_id: str | None = None
    user_id: str
    limit: int = Field(default=5, ge=1, le=20)


class ToolCatalog:
    @staticmethod
    def default() -> list[ToolSchema]:
        return [
            ToolSchema(
                name=ToolName.CREDIT_CHECK,
                description="Load the applicant's credit score, trade lines, debt, and delinquency signals.",
                parameters={
                    "type": "object",
                    "properties": {
                        "applicant_id": {
                            "type": "string",
                            "description": "Stable applicant identifier",
                        }
                    },
                    "required": ["applicant_id"],
                },
            ),
            ToolSchema(
                name=ToolName.INCOME_VERIFICATION,
                description="Verify stated annual income and employment for an applicant.",
                parameters={
                    "type": "object",
                    "properties": {
                        "applicant_id": {
                            "type": "string",
                            "description": "Stable applicant identifier",
                        }
                    },
                    "required": ["applicant_id"],
                },
            ),
            ToolSchema(
                name=ToolName.DEBT_TO_INCOME_CALC,
                description="Calculate debt-to-income including the estimated new loan payment.",
                parameters={
                    "type": "object",
                    "properties": {
                        "verified_annual_income": {"type": "number"},
                        "existing_monthly_debt": {"type": "number"},
                        "loan_amount": {"type": "number"},
                        "loan_term_months": {"type": "integer", "default": 36},
                    },
                    "required": [
                        "verified_annual_income",
                        "existing_monthly_debt",
                        "loan_amount",
                    ],
                },
            ),
            ToolSchema(
                name=ToolName.POLICY_LOOKUP,
                description="Evaluate credit and DTI signals against governed underwriting policy.",
                parameters={
                    "type": "object",
                    "properties": {
                        "credit_score": {"type": "integer"},
                        "dti": {"type": "number"},
                        "delinquency_flags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "open_trade_lines": {"type": "integer"},
                        "employment_status": {"type": "string"},
                        "verification_status": {"type": "string"},
                        "loan_purpose": {"type": "string"},
                    },
                    "required": [
                        "credit_score",
                        "dti",
                        "delinquency_flags",
                        "open_trade_lines",
                        "employment_status",
                        "verification_status",
                        "loan_purpose",
                    ],
                },
            ),
            ToolSchema(
                name=ToolName.SAVE_NOTE,
                description="Persist a short operational note for future runs.",
                parameters={
                    "type": "object",
                    "properties": {
                        "note": {"type": "string", "description": "Note to save"}
                    },
                    "required": ["note"],
                },
            ),
        ]
