from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, NoReturn, TypeVar

from temporalio import activity
from temporalio.exceptions import ApplicationError

from lakebase_agentworkflow.helpers.env import env_truthy
from lakebase_agentworkflow.helpers.lakebase import get_repository
from lakebase_agentworkflow.helpers.models import (
    ActivityAttemptMetricRecord,
    AgentMessage,
    LakebaseEvent,
    LakebaseWriteResult,
    LoanRecommendation,
    MessageRecord,
    ModelDecision,
    ModelRequest,
    RecentContextRequest,
    ReviewDecisionRecord,
    ReviewPendingRecord,
    RunCompletedRecord,
    RunStartedRecord,
    TokenUsage,
    ToolCall,
    ToolCallRecord,
    ToolExecutionRequest,
    ToolName,
    ToolResult,
    ToolResultRecord,
    TurnMetricRecord,
    UnderwritingDecision,
    UnderwritingPolicy,
    WorkerCrashRequest,
    WorkerCrashResult,
    WorkflowMetricCompletedRecord,
    WorkflowMetricStartedRecord,
)


logger = logging.getLogger(__name__)


APPLICANT_FIXTURES: dict[str, dict[str, dict[str, Any]]] = {
    "applicant-clean": {
        "credit": {
            "credit_score": 785,
            "open_trade_lines": 9,
            "delinquency_flags": [],
            "existing_monthly_debt": 1100.0,
        },
        "income": {
            "stated_annual_income": 132000.0,
            "verified_annual_income": 132000.0,
            "employment_status": "full_time",
            "verification_status": "verified",
        },
    },
    "applicant-borderline": {
        "credit": {
            "credit_score": 665,
            "open_trade_lines": 5,
            "delinquency_flags": ["one_30_day_late"],
            "existing_monthly_debt": 2400.0,
        },
        "income": {
            "stated_annual_income": 82000.0,
            "verified_annual_income": 76000.0,
            "employment_status": "full_time",
            "verification_status": "verified",
        },
    },
    "applicant-decline": {
        "credit": {
            "credit_score": 585,
            "open_trade_lines": 7,
            "delinquency_flags": ["90_day_delinquency", "charge_off"],
            "existing_monthly_debt": 2700.0,
        },
        "income": {
            "stated_annual_income": 56000.0,
            "verified_annual_income": 48000.0,
            "employment_status": "full_time",
            "verification_status": "verified",
        },
    },
    "applicant-new-credit": {
        "credit": {
            "credit_score": 715,
            "open_trade_lines": 1,
            "delinquency_flags": [],
            "existing_monthly_debt": 800.0,
        },
        "income": {
            "stated_annual_income": 70000.0,
            "verified_annual_income": 68000.0,
            "employment_status": "full_time",
            "verification_status": "verified",
        },
    },
    "applicant-self-employed": {
        "credit": {
            "credit_score": 735,
            "open_trade_lines": 7,
            "delinquency_flags": [],
            "existing_monthly_debt": 3100.0,
        },
        "income": {
            "stated_annual_income": 140000.0,
            "verified_annual_income": 96000.0,
            "employment_status": "self_employed",
            "verification_status": "partial",
        },
    },
}


UNDERWRITING_POLICY_FIXTURES: dict[str, UnderwritingPolicy] = {
    "default": UnderwritingPolicy(
        loan_purpose="default",
        min_credit_score=700,
        max_dti=0.43,
        decline_below_credit_score=620,
        decline_above_dti=0.55,
    ),
    "debt_consolidation": UnderwritingPolicy(
        loan_purpose="debt_consolidation",
        min_credit_score=690,
        max_dti=0.45,
        decline_below_credit_score=620,
        decline_above_dti=0.55,
    ),
    "home_improvement": UnderwritingPolicy(
        loan_purpose="home_improvement",
        min_credit_score=700,
        max_dti=0.43,
        decline_below_credit_score=620,
        decline_above_dti=0.55,
    ),
    "major_purchase": UnderwritingPolicy(
        loan_purpose="major_purchase",
        min_credit_score=710,
        max_dti=0.40,
        decline_below_credit_score=630,
        decline_above_dti=0.52,
    ),
}


LOAN_TOOLS = {
    ToolName.CREDIT_CHECK,
    ToolName.INCOME_VERIFICATION,
    ToolName.DEBT_TO_INCOME_CALC,
    ToolName.POLICY_LOOKUP,
}


T = TypeVar("T")


@activity.defn
async def call_model(request: ModelRequest) -> ModelDecision:
    """Call the configured model provider and return a structured decision."""
    async def operation() -> ModelDecision:
        attempt = _activity_attempt()
        if request.simulated_failure_attempts and not env_truthy(
            "DEMO_CONTROLS_ENABLED"
        ):
            raise ApplicationError(
                "Demo controls are disabled on this worker",
                type="DemoControlsDisabled",
                non_retryable=True,
            )
        if attempt <= request.simulated_failure_attempts:
            raise ApplicationError(
                f"Simulated model API outage on activity attempt {attempt}",
                type="ModelAPIUnavailable",
            )
        provider = os.getenv("AGENT_MODEL_PROVIDER", "scripted").strip().lower()
        if provider == "scripted":
            return scripted_model_decision(request)
        if provider == "openai":
            return await _call_openai(request)
        raise ApplicationError(
            f"Unsupported AGENT_MODEL_PROVIDER={provider}",
            type="ConfigurationError",
            non_retryable=True,
        )

    return await _record_async_activity_attempt(
        request.run_id,
        request.turn,
        {
            "model": request.model,
            "message_count": len(request.messages),
            "simulated_failure_attempts": request.simulated_failure_attempts,
        },
        operation,
    )


@activity.defn
def crash_worker_process(request: WorkerCrashRequest) -> WorkerCrashResult:
    """Exit the worker once, then acknowledge recovery on the retry attempt."""
    if not env_truthy("DEMO_CONTROLS_ENABLED"):
        raise ApplicationError(
            "Demo controls are disabled on this worker",
            type="DemoControlsDisabled",
            non_retryable=True,
        )

    attempt = _activity_attempt()
    if attempt > 1:
        logger.warning(
            "Worker crash demo %s recovered on attempt %s",
            request.control_id,
            attempt,
        )
        return WorkerCrashResult(
            control_id=request.control_id,
            recovered=True,
            completed_attempt=attempt,
        )

    logger.error(
        "Worker crash demo %s requested by %s; exiting process on attempt 1",
        request.control_id,
        request.requested_by,
    )
    _terminate_worker_process(137)


@activity.defn
def execute_tool(request: ToolExecutionRequest) -> ToolResult:
    """Execute one agent tool outside the deterministic workflow replay path."""
    def operation() -> ToolResult:
        tool = request.tool_call
        if tool.name in LOAN_TOOLS:
            _apply_demo_tool_delay(tool.name)
        if tool.name == ToolName.CREDIT_CHECK:
            return _credit_check(request)
        if tool.name == ToolName.INCOME_VERIFICATION:
            return _income_verification(request)
        if tool.name == ToolName.DEBT_TO_INCOME_CALC:
            return _debt_to_income_calc(request)
        if tool.name == ToolName.POLICY_LOOKUP:
            return _policy_lookup(request)
        if tool.name == ToolName.SAVE_NOTE:
            return _save_note(request)

        return ToolResult(
            tool_call_id=tool.id,
            name=tool.name,
            success=False,
            content=f"Unsupported tool: {tool.name}",
            error="unsupported_tool",
        )

    return _record_activity_attempt(
        request.run_id,
        request.turn,
        {"tool_name": request.tool_call.name.value},
        operation,
    )


def scripted_model_decision(request: ModelRequest) -> ModelDecision:
    credit = _tool_data(request.messages, ToolName.CREDIT_CHECK)
    if credit is None:
        tool_call = ToolCall(
            id=f"{request.run_id}-turn-{request.turn}-credit",
            name=ToolName.CREDIT_CHECK,
            arguments={"applicant_id": request.applicant_id},
        )
    else:
        income = _tool_data(request.messages, ToolName.INCOME_VERIFICATION)
        if income is None:
            tool_call = ToolCall(
                id=f"{request.run_id}-turn-{request.turn}-income",
                name=ToolName.INCOME_VERIFICATION,
                arguments={"applicant_id": request.applicant_id},
            )
        else:
            dti = _tool_data(request.messages, ToolName.DEBT_TO_INCOME_CALC)
            if dti is None:
                tool_call = ToolCall(
                    id=f"{request.run_id}-turn-{request.turn}-dti",
                    name=ToolName.DEBT_TO_INCOME_CALC,
                    arguments={
                        "verified_annual_income": income["verified_annual_income"],
                        "existing_monthly_debt": credit["existing_monthly_debt"],
                        "loan_amount": request.loan_amount,
                        "loan_term_months": 36,
                    },
                )
            else:
                policy = _tool_data(request.messages, ToolName.POLICY_LOOKUP)
                if policy is None:
                    tool_call = ToolCall(
                        id=f"{request.run_id}-turn-{request.turn}-policy",
                        name=ToolName.POLICY_LOOKUP,
                        arguments={
                            "credit_score": credit["credit_score"],
                            "dti": dti["dti"],
                            "delinquency_flags": credit["delinquency_flags"],
                            "open_trade_lines": credit["open_trade_lines"],
                            "employment_status": income["employment_status"],
                            "verification_status": income["verification_status"],
                            "loan_purpose": request.loan_purpose,
                        },
                    )
                else:
                    tool_call = None

    if tool_call is not None:
        return ModelDecision(
            assistant_message=(
                "I need one more underwriting signal before recommending a decision: "
                f"{tool_call.name.value}."
            ),
            final=False,
            tool_calls=[tool_call],
            usage=TokenUsage(input_tokens=120, output_tokens=24, total_tokens=144),
            metadata={"provider": "scripted"},
        )

    recommendation = _recommendation_from_messages(request.messages)
    if recommendation is None:
        raise ApplicationError(
            "The tool chain completed without a structured policy recommendation",
            type="MissingRecommendationError",
            non_retryable=True,
        )
    more_info_requests = sum(
        1
        for message in request.messages
        if message.role == "user"
        and message.metadata.get("source") == "review_request_more_info"
    )
    return ModelDecision(
        assistant_message=(
            f"Underwriting recommendation: {recommendation.decision.value.upper()}. "
            f"{recommendation.rationale} DTI is {recommendation.dti:.1%} and the "
            f"credit score is {recommendation.credit_score}."
            + (
                f" This response addresses {more_info_requests} reviewer request(s) "
                "for additional information."
                if more_info_requests
                else ""
            )
        ),
        final=True,
        usage=TokenUsage(input_tokens=180, output_tokens=54, total_tokens=234),
        metadata={
            "provider": "scripted",
            "recommendation": recommendation.model_dump(mode="json"),
        },
    )


@activity.defn
def record_run_started(record: RunStartedRecord) -> LakebaseWriteResult:
    return _record_activity_attempt(
        record.run_id,
        None,
        {"category": "ui_state"},
        lambda: get_repository().record_run_started(record),
    )


@activity.defn
def record_message(record: MessageRecord) -> LakebaseWriteResult:
    return _record_activity_attempt(
        record.run_id,
        record.turn,
        {"category": "ui_state", "message_role": record.message.role},
        lambda: get_repository().record_message(record),
    )


@activity.defn
def record_tool_call(record: ToolCallRecord) -> LakebaseWriteResult:
    return _record_activity_attempt(
        record.run_id,
        record.turn,
        {"category": "ui_state", "tool_name": record.tool_call.name.value},
        lambda: get_repository().record_tool_call(record),
    )


@activity.defn
def record_tool_result(record: ToolResultRecord) -> LakebaseWriteResult:
    return _record_activity_attempt(
        record.run_id,
        record.turn,
        {"category": "ui_state", "tool_name": record.result.name.value},
        lambda: get_repository().record_tool_result(record),
    )


@activity.defn
def record_review_pending(record: ReviewPendingRecord) -> LakebaseWriteResult:
    return _record_activity_attempt(
        record.run_id,
        record.review_round,
        {"category": "ui_state", "status": "awaiting_review"},
        lambda: get_repository().record_review_pending(record),
    )


@activity.defn
def record_review_decision(record: ReviewDecisionRecord) -> LakebaseWriteResult:
    return _record_activity_attempt(
        record.run_id,
        None,
        {
            "category": "ui_state",
            "review_decision": record.decision.value,
            "reviewer_id": record.reviewer_id,
        },
        lambda: get_repository().record_review_decision(record),
    )


@activity.defn
def record_event(event: LakebaseEvent) -> LakebaseWriteResult:
    return _record_activity_attempt(
        event.run_id,
        None,
        {"category": "audit_event", "event_type": event.event_type},
        lambda: get_repository().record_event(event),
    )


@activity.defn
def record_run_completed(record: RunCompletedRecord) -> LakebaseWriteResult:
    return _record_activity_attempt(
        record.run_id,
        None,
        {"category": "ui_state", "status": record.status.value},
        lambda: get_repository().record_run_completed(record),
    )


@activity.defn
def record_workflow_metric_started(record: WorkflowMetricStartedRecord) -> LakebaseWriteResult:
    return _record_activity_attempt(
        record.run_id,
        None,
        {"category": "operational_metric", "metric": "workflow_started"},
        lambda: get_repository().record_workflow_metric_started(record),
    )


@activity.defn
def record_workflow_metric_completed(
    record: WorkflowMetricCompletedRecord,
) -> LakebaseWriteResult:
    return _record_activity_attempt(
        record.run_id,
        None,
        {"category": "operational_metric", "metric": "workflow_completed"},
        lambda: get_repository().record_workflow_metric_completed(record),
    )


@activity.defn
def record_turn_metric(record: TurnMetricRecord) -> LakebaseWriteResult:
    return _record_activity_attempt(
        record.run_id,
        record.turn,
        {"category": "operational_metric", "metric": "turn"},
        lambda: get_repository().record_turn_metric(record),
    )


@activity.defn
def load_recent_context(request: RecentContextRequest) -> list[str]:
    return _record_activity_attempt(
        request.run_id or "unknown",
        None,
        {"category": "context_load", "user_id": request.user_id, "limit": request.limit},
        lambda: get_repository().load_recent_context(request),
    )


async def _record_async_activity_attempt(
    run_id: str,
    turn: int | None,
    metadata: dict[str, Any],
    operation: Callable[[], Awaitable[T]],
) -> T:
    started_at = _activity_started_at()
    try:
        result = await operation()
    except Exception as exc:
        _safe_record_activity_attempt(run_id, turn, "failed", started_at, metadata, exc)
        raise
    _safe_record_activity_attempt(run_id, turn, "succeeded", started_at, metadata, None)
    return result


def _record_activity_attempt(
    run_id: str,
    turn: int | None,
    metadata: dict[str, Any],
    operation: Callable[[], T],
) -> T:
    started_at = _activity_started_at()
    try:
        result = operation()
    except Exception as exc:
        _safe_record_activity_attempt(run_id, turn, "failed", started_at, metadata, exc)
        raise
    _safe_record_activity_attempt(run_id, turn, "succeeded", started_at, metadata, None)
    return result


def _safe_record_activity_attempt(
    run_id: str,
    turn: int | None,
    status: str,
    started_at: datetime,
    metadata: dict[str, Any],
    exc: Exception | None,
) -> None:
    info = _activity_info_or_none()
    if info is None:
        return
    completed_at = datetime.now(timezone.utc)
    enriched_metadata = {
        **metadata,
        "task_queue": info.task_queue,
        "is_local": info.is_local,
        "activity_run_id": info.activity_run_id,
        "retry_count": max(info.attempt - 1, 0),
    }
    try:
        get_repository().record_activity_attempt_metric(
            ActivityAttemptMetricRecord(
                run_id=run_id,
                workflow_id=info.workflow_id or "unknown",
                workflow_run_id=info.workflow_run_id or "unknown",
                workflow_type=info.workflow_type or "unknown",
                activity_id=info.activity_id,
                activity_type=info.activity_type,
                attempt=info.attempt,
                turn=turn,
                status=status,
                started_at=started_at,
                completed_at=completed_at,
                latency_ms=max(
                    0,
                    int((completed_at - started_at).total_seconds() * 1000),
                ),
                error_type=type(exc).__name__ if exc else None,
                error_message=str(exc)[:1000] if exc else None,
                metadata=enriched_metadata,
            )
        )
    except Exception as metric_exc:
        activity.logger.warning("Could not record activity attempt metric: %s", metric_exc)


def _activity_started_at() -> datetime:
    info = _activity_info_or_none()
    if info is None:
        return datetime.now(timezone.utc)
    if info.started_time.tzinfo is None:
        return info.started_time.replace(tzinfo=timezone.utc)
    return info.started_time


def _activity_info_or_none() -> Any | None:
    try:
        return activity.info()
    except Exception:
        return None


def _activity_attempt() -> int:
    info = _activity_info_or_none()
    return max(int(info.attempt), 1) if info is not None else 1


def _terminate_worker_process(exit_code: int) -> NoReturn:
    os._exit(exit_code)


async def _call_openai(request: ModelRequest) -> ModelDecision:
    try:
        from openai import (
            APIConnectionError,
            APIStatusError,
            AsyncOpenAI,
            AuthenticationError,
            RateLimitError,
        )
    except ImportError as exc:
        raise ApplicationError(
            "openai package is required for AGENT_MODEL_PROVIDER=openai",
            type="ConfigurationError",
            non_retryable=True,
        ) from exc

    client = AsyncOpenAI(max_retries=0, timeout=30.0)
    messages = [
        {
            "role": "system",
            "content": (
                "Underwrite the supplied personal loan by calling the available tools "
                "in dependency order: credit_check, income_verification, "
                "debt_to_income_calc, then policy_lookup. Applicant "
                f"{request.applicant_id} requests ${request.loan_amount:,.2f} for "
                f"{request.loan_purpose}. Base the final recommendation on the policy "
                "tool result."
            ),
        }
    ]
    for message in request.messages:
        if message.role == "tool":
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"Completed tool {message.name}: {message.content}\n"
                        f"Structured result: {json.dumps(message.metadata.get('data', {}))}"
                    ),
                }
            )
        else:
            messages.append({"role": message.role, "content": message.content})
    tools = [
        {
            "type": "function",
            "function": {
                "name": tool.name.value,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in request.tools
    ]

    try:
        response = await client.chat.completions.create(
            model=request.model,
            messages=messages,
            tools=tools,
            temperature=0.2,
            # gpt-6-luna takes Chat Completions tools only with reasoning off.
            reasoning_effort="none",
        )
    except AuthenticationError as exc:
        raise ApplicationError(
            f"OpenAI authentication failed: {exc}",
            type="AuthenticationError",
            non_retryable=True,
        ) from exc
    except RateLimitError as exc:
        raise ApplicationError(
            f"OpenAI rate limited the request: {exc}",
            type="RateLimitError",
        ) from exc
    except APIConnectionError as exc:
        raise ApplicationError(
            f"OpenAI connection error: {exc}",
            type="ConnectionError",
        ) from exc
    except APIStatusError as exc:
        raise ApplicationError(
            f"OpenAI status error {exc.status_code}: {exc}",
            type="OpenAIStatusError",
            non_retryable=exc.status_code < 500,
        ) from exc

    choice = response.choices[0].message
    usage = getattr(response, "usage", None)
    tool_calls = []
    for call in choice.tool_calls or []:
        function = call.function
        arguments = json.loads(function.arguments) if function.arguments else {}
        if not isinstance(arguments, dict):
            raise ValueError("Tool call arguments must decode to a JSON object")
        tool_calls.append(
            ToolCall(
                id=call.id,
                name=ToolName(function.name),
                arguments=arguments,
            )
        )

    metadata: dict[str, Any] = {"provider": "openai"}
    recommendation = _recommendation_from_messages(request.messages)
    if not tool_calls and recommendation is not None:
        metadata["recommendation"] = recommendation.model_dump(mode="json")
    return ModelDecision(
        assistant_message=choice.content or "",
        final=not tool_calls,
        tool_calls=tool_calls,
        usage=TokenUsage(
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            total_tokens=getattr(usage, "total_tokens", 0) if usage else 0,
        ),
        metadata=metadata,
    )


def _credit_check(request: ToolExecutionRequest) -> ToolResult:
    applicant_id = str(
        request.tool_call.arguments.get("applicant_id") or request.applicant_id
    ).strip()
    fixture = APPLICANT_FIXTURES.get(applicant_id)
    if fixture is None:
        return ToolResult(
            tool_call_id=request.tool_call.id,
            name=ToolName.CREDIT_CHECK,
            success=False,
            content=f"No credit fixture exists for applicant {applicant_id}.",
            error="unknown_applicant",
        )

    data = {"applicant_id": applicant_id, **fixture["credit"]}
    flags = data["delinquency_flags"]
    flag_summary = ", ".join(flags) if flags else "none"
    return ToolResult(
        tool_call_id=request.tool_call.id,
        name=ToolName.CREDIT_CHECK,
        success=True,
        content=(
            f"Credit score {data['credit_score']} with {data['open_trade_lines']} "
            f"open trade lines; delinquency flags: {flag_summary}."
        ),
        data=data,
    )


def _income_verification(request: ToolExecutionRequest) -> ToolResult:
    applicant_id = str(
        request.tool_call.arguments.get("applicant_id") or request.applicant_id
    ).strip()
    fixture = APPLICANT_FIXTURES.get(applicant_id)
    if fixture is None:
        return ToolResult(
            tool_call_id=request.tool_call.id,
            name=ToolName.INCOME_VERIFICATION,
            success=False,
            content=f"No income fixture exists for applicant {applicant_id}.",
            error="unknown_applicant",
        )

    data = {"applicant_id": applicant_id, **fixture["income"]}
    return ToolResult(
        tool_call_id=request.tool_call.id,
        name=ToolName.INCOME_VERIFICATION,
        success=True,
        content=(
            f"Verified annual income is ${data['verified_annual_income']:,.0f} "
            f"against ${data['stated_annual_income']:,.0f} stated; "
            f"employment is {data['employment_status']} and verification is "
            f"{data['verification_status']}."
        ),
        data=data,
    )


def _debt_to_income_calc(request: ToolExecutionRequest) -> ToolResult:
    arguments = request.tool_call.arguments
    try:
        annual_income = float(arguments["verified_annual_income"])
        existing_monthly_debt = float(arguments["existing_monthly_debt"])
        loan_amount = float(arguments.get("loan_amount") or request.loan_amount)
        loan_term_months = int(arguments.get("loan_term_months", 36))
    except (KeyError, TypeError, ValueError) as exc:
        return ToolResult(
            tool_call_id=request.tool_call.id,
            name=ToolName.DEBT_TO_INCOME_CALC,
            success=False,
            content="DTI inputs were missing or invalid.",
            error=f"invalid_dti_inputs: {exc}",
        )

    if annual_income <= 0 or existing_monthly_debt < 0 or loan_term_months <= 0:
        return ToolResult(
            tool_call_id=request.tool_call.id,
            name=ToolName.DEBT_TO_INCOME_CALC,
            success=False,
            content="Income must be positive and debt/term inputs must be non-negative.",
            error="invalid_dti_inputs",
        )

    annual_percentage_rate = 11.5
    monthly_rate = annual_percentage_rate / 100 / 12
    estimated_loan_payment = (
        loan_amount
        * monthly_rate
        * (1 + monthly_rate) ** loan_term_months
        / ((1 + monthly_rate) ** loan_term_months - 1)
    )
    gross_monthly_income = annual_income / 12
    total_monthly_debt = existing_monthly_debt + estimated_loan_payment
    dti = round(total_monthly_debt / gross_monthly_income, 4)
    data = {
        "dti": dti,
        "verified_annual_income": annual_income,
        "gross_monthly_income": round(gross_monthly_income, 2),
        "existing_monthly_debt": existing_monthly_debt,
        "estimated_loan_payment": round(estimated_loan_payment, 2),
        "total_monthly_debt": round(total_monthly_debt, 2),
        "loan_amount": loan_amount,
        "loan_term_months": loan_term_months,
        "annual_percentage_rate": annual_percentage_rate,
    }
    return ToolResult(
        tool_call_id=request.tool_call.id,
        name=ToolName.DEBT_TO_INCOME_CALC,
        success=True,
        content=(
            f"Calculated DTI is {dti:.1%} including an estimated "
            f"${estimated_loan_payment:,.0f} monthly payment."
        ),
        data=data,
    )


def _policy_lookup(request: ToolExecutionRequest) -> ToolResult:
    arguments = request.tool_call.arguments
    try:
        credit_score = int(arguments["credit_score"])
        dti = float(arguments["dti"])
        open_trade_lines = int(arguments["open_trade_lines"])
        delinquency_flags = [str(flag) for flag in arguments["delinquency_flags"]]
        employment_status = str(arguments["employment_status"])
        verification_status = str(arguments["verification_status"])
    except (KeyError, TypeError, ValueError) as exc:
        return ToolResult(
            tool_call_id=request.tool_call.id,
            name=ToolName.POLICY_LOOKUP,
            success=False,
            content="Policy inputs were missing or invalid.",
            error=f"invalid_policy_inputs: {exc}",
        )

    loan_purpose = (
        str(arguments.get("loan_purpose") or request.loan_purpose)
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    policy, policy_source, fallback_reason = _load_underwriting_policy(loan_purpose)
    hard_delinquency_flags = {"90_day_delinquency", "charge_off", "bankruptcy"}
    material_delinquency = sorted(hard_delinquency_flags.intersection(delinquency_flags))

    rule_results = {
        "minimum_credit_score": {
            "passed": credit_score >= policy.min_credit_score,
            "actual": credit_score,
            "threshold": policy.min_credit_score,
        },
        "maximum_dti": {
            "passed": dti <= policy.max_dti,
            "actual": dti,
            "threshold": policy.max_dti,
        },
        "hard_credit_floor": {
            "passed": credit_score >= policy.decline_below_credit_score,
            "actual": credit_score,
            "threshold": policy.decline_below_credit_score,
        },
        "hard_dti_ceiling": {
            "passed": dti <= policy.decline_above_dti,
            "actual": dti,
            "threshold": policy.decline_above_dti,
        },
        "material_delinquency": {
            "passed": not material_delinquency,
            "actual": material_delinquency,
            "threshold": [],
        },
        "credit_file_depth": {
            "passed": open_trade_lines >= 2,
            "actual": open_trade_lines,
            "threshold": 2,
        },
        "income_verification": {
            "passed": verification_status == "verified",
            "actual": verification_status,
            "threshold": "verified",
        },
    }

    decline_reasons: list[str] = []
    if not rule_results["hard_credit_floor"]["passed"]:
        decline_reasons.append(
            f"credit score {credit_score} is below hard floor "
            f"{policy.decline_below_credit_score}"
        )
    if not rule_results["hard_dti_ceiling"]["passed"]:
        decline_reasons.append(
            f"DTI {dti:.1%} exceeds hard ceiling {policy.decline_above_dti:.1%}"
        )
    if material_delinquency:
        decline_reasons.append(
            "material delinquency flags are present: " + ", ".join(material_delinquency)
        )

    referral_reasons: list[str] = []
    if not rule_results["minimum_credit_score"]["passed"]:
        referral_reasons.append(
            f"credit score {credit_score} is below auto-approval minimum "
            f"{policy.min_credit_score}"
        )
    if not rule_results["maximum_dti"]["passed"]:
        referral_reasons.append(
            f"DTI {dti:.1%} exceeds auto-approval maximum {policy.max_dti:.1%}"
        )
    if delinquency_flags and not material_delinquency:
        referral_reasons.append("non-material delinquency history needs review")
    if not rule_results["credit_file_depth"]["passed"]:
        referral_reasons.append("credit file has fewer than two open trade lines")
    if not rule_results["income_verification"]["passed"]:
        referral_reasons.append(
            f"income verification is {verification_status}, not fully verified"
        )
    if employment_status == "unemployed":
        referral_reasons.append("employment is currently unverified")

    if decline_reasons:
        decision = UnderwritingDecision.DECLINE
        rationale = "Decline: " + "; ".join(decline_reasons) + "."
    elif referral_reasons:
        decision = UnderwritingDecision.REFER
        rationale = "Refer for human judgment: " + "; ".join(referral_reasons) + "."
    else:
        decision = UnderwritingDecision.APPROVE
        rationale = (
            f"Approve: credit score {credit_score} meets {policy.min_credit_score}, "
            f"DTI {dti:.1%} is within {policy.max_dti:.1%}, and income/credit "
            "quality checks passed."
        )

    data = {
        "decision": decision.value,
        "rationale": rationale,
        "credit_score": credit_score,
        "dti": dti,
        "loan_purpose": loan_purpose,
        "policy": policy.model_dump(mode="json"),
        "policy_source": policy_source,
        "rule_results": rule_results,
    }
    if fallback_reason:
        data["fallback_reason"] = fallback_reason
    return ToolResult(
        tool_call_id=request.tool_call.id,
        name=ToolName.POLICY_LOOKUP,
        success=True,
        content=rationale,
        data=data,
    )


def _load_underwriting_policy(
    loan_purpose: str,
) -> tuple[UnderwritingPolicy, str, str | None]:
    try:
        policy = get_repository().load_underwriting_policy(loan_purpose)
        if policy is not None:
            return policy, "lakebase_synced_table", None
        fallback_reason = "lakebase_disabled_or_policy_missing"
    except Exception as exc:
        logger.warning(
            "Could not load underwriting policy from Lakebase; using fixture: %s",
            exc,
        )
        fallback_reason = type(exc).__name__

    policy = UNDERWRITING_POLICY_FIXTURES.get(
        loan_purpose,
        UNDERWRITING_POLICY_FIXTURES["default"],
    )
    return policy, "fixture_fallback", fallback_reason


def _tool_data(
    messages: list[AgentMessage],
    tool_name: ToolName,
) -> dict[str, Any] | None:
    for message in reversed(messages):
        if (
            message.role == "tool"
            and message.name == tool_name.value
            and message.metadata.get("success") is True
        ):
            data = message.metadata.get("data")
            return data if isinstance(data, dict) else None
    return None


def _recommendation_from_messages(
    messages: list[AgentMessage],
) -> LoanRecommendation | None:
    policy = _tool_data(messages, ToolName.POLICY_LOOKUP)
    if policy is None:
        return None
    return LoanRecommendation(
        decision=UnderwritingDecision(policy["decision"]),
        rationale=str(policy["rationale"]),
        dti=float(policy["dti"]),
        credit_score=int(policy["credit_score"]),
    )


def _apply_demo_tool_delay(tool_name: ToolName) -> None:
    raw_delay = os.getenv("DEMO_TOOL_DELAY_SECONDS", "0")
    try:
        delay_seconds = min(max(float(raw_delay), 0.0), 30.0)
    except ValueError:
        delay_seconds = 0.0
    if delay_seconds == 0:
        return

    deadline = time.monotonic() + delay_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        activity.heartbeat(
            {
                "tool_name": tool_name.value,
                "remaining_seconds": round(remaining, 1),
            }
        )
        time.sleep(min(0.5, remaining))


def _save_note(request: ToolExecutionRequest) -> ToolResult:
    note = str(request.tool_call.arguments.get("note", "")).strip()
    if not note:
        return ToolResult(
            tool_call_id=request.tool_call.id,
            name=ToolName.SAVE_NOTE,
            success=False,
            content="No note was supplied.",
            error="missing_note",
        )

    return ToolResult(
        tool_call_id=request.tool_call.id,
        name=ToolName.SAVE_NOTE,
        success=True,
        content="The note is ready to persist through the Lakebase activity.",
        data={"note": note, "user_id": request.user_id},
    )
