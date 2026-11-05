from __future__ import annotations

import asyncio
import json
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from temporalio.common import VersioningBehavior

from lakebase_agentworkflow.activities import (
    call_model,
    crash_worker_process,
    execute_tool,
    scripted_model_decision,
)
from lakebase_agentworkflow.helpers.lakebase import LakebaseRepository
from lakebase_agentworkflow.helpers.models import (
    ActivityAttemptMetricRecord,
    AgentMessage,
    AgentRequest,
    AgentRunStatus,
    AgentSnapshot,
    LakebaseEvent,
    LakebaseWriteResult,
    LoanRecommendation,
    MessageRecord,
    ModelRequest,
    ReviewDecision,
    ReviewDecisionPayload,
    ReviewDecisionRecord,
    ReviewPendingRecord,
    StartWorkflowRequest,
    TokenUsage,
    ToolCall,
    ToolCatalog,
    ToolExecutionRequest,
    ToolName,
    ToolResult,
    WorkerCrashRequest,
    TurnMetricRecord,
    UnderwritingDecision,
    WorkflowMetricCompletedRecord,
    WorkflowRunView,
)
from lakebase_agentworkflow.worker import WorkerRuntimeSettings
from lakebase_agentworkflow.workflows import (
    LakebaseAgentWorkflow,
    _canonicalize_tool_call,
)


def _run_scripted_case(
    applicant_id: str,
    loan_amount: float,
    loan_purpose: str,
) -> tuple[list[ToolName], dict[str, Any]]:
    messages = [AgentMessage(role="user", content="Underwrite this loan.")]
    tool_names: list[ToolName] = []
    for turn in range(1, 5):
        request = ModelRequest(
            run_id="run-1",
            turn=turn,
            model="scripted",
            user_id="user@example.com",
            messages=messages,
            applicant_id=applicant_id,
            loan_amount=loan_amount,
            loan_purpose=loan_purpose,
            tools=ToolCatalog.default(),
        )
        decision = scripted_model_decision(request)
        assert decision.final is False
        assert len(decision.tool_calls) == 1
        call = decision.tool_calls[0]
        tool_names.append(call.name)
        result = execute_tool(
            ToolExecutionRequest(
                run_id="run-1",
                turn=turn,
                user_id="user@example.com",
                applicant_id=applicant_id,
                loan_amount=loan_amount,
                loan_purpose=loan_purpose,
                tool_call=call,
            )
        )
        assert result.success is True
        messages.append(
            AgentMessage(
                role="tool",
                name=result.name,
                content=result.content,
                metadata={"success": result.success, "data": result.data},
            )
        )

    final = scripted_model_decision(
        ModelRequest(
            run_id="run-1",
            turn=5,
            model="scripted",
            user_id="user@example.com",
            messages=messages,
            applicant_id=applicant_id,
            loan_amount=loan_amount,
            loan_purpose=loan_purpose,
            tools=ToolCatalog.default(),
        )
    )
    assert final.final is True
    assert final.tool_calls == []
    return tool_names, final.metadata["recommendation"]


def test_scripted_model_sequences_all_underwriting_tools() -> None:
    tool_names, recommendation = _run_scripted_case(
        "applicant-borderline", 25000, "debt_consolidation"
    )

    assert tool_names == [
        ToolName.CREDIT_CHECK,
        ToolName.INCOME_VERIFICATION,
        ToolName.DEBT_TO_INCOME_CALC,
        ToolName.POLICY_LOOKUP,
    ]
    assert recommendation["decision"] == UnderwritingDecision.REFER.value
    assert 0.45 < recommendation["dti"] < 0.55
    assert recommendation["credit_score"] == 665


def test_scripted_model_fixture_outcomes_span_approve_and_decline() -> None:
    _, clean = _run_scripted_case(
        "applicant-clean", 25000, "debt_consolidation"
    )
    _, decline = _run_scripted_case(
        "applicant-decline", 40000, "home_improvement"
    )

    assert clean["decision"] == UnderwritingDecision.APPROVE.value
    assert decline["decision"] == UnderwritingDecision.DECLINE.value


def test_model_api_failure_injection_is_retryable(monkeypatch) -> None:
    from lakebase_agentworkflow import activities as activities_module

    monkeypatch.setenv("DEMO_CONTROLS_ENABLED", "true")
    monkeypatch.setattr(activities_module, "_activity_info_or_none", lambda: None)
    request = ModelRequest(
        run_id="run-model-failure",
        turn=1,
        model="scripted",
        user_id="user@example.com",
        messages=[AgentMessage(role="user", content="Underwrite this loan.")],
        applicant_id="applicant-clean",
        loan_amount=25000,
        loan_purpose="debt_consolidation",
        tools=ToolCatalog.default(),
        simulated_failure_attempts=1,
    )

    try:
        asyncio.run(call_model(request))
    except Exception as exc:
        assert type(exc).__name__ == "ApplicationError"
        assert getattr(exc, "type", None) == "ModelAPIUnavailable"
        assert getattr(exc, "non_retryable", None) is False
    else:
        raise AssertionError("The first model activity attempt should fail")


def test_worker_crash_activity_exits_once_then_confirms_recovery(monkeypatch) -> None:
    from lakebase_agentworkflow import activities as activities_module

    class WorkerExitIntercepted(Exception):
        pass

    request = WorkerCrashRequest(
        control_id="control-1",
        requested_by="operator@example.com",
    )
    monkeypatch.setenv("DEMO_CONTROLS_ENABLED", "true")
    monkeypatch.setattr(
        activities_module,
        "_activity_info_or_none",
        lambda: SimpleNamespace(attempt=1),
    )
    monkeypatch.setattr(
        activities_module,
        "_terminate_worker_process",
        lambda _: (_ for _ in ()).throw(WorkerExitIntercepted()),
    )

    try:
        crash_worker_process(request)
    except WorkerExitIntercepted:
        pass
    else:
        raise AssertionError("The first crash activity attempt should exit the worker")

    monkeypatch.setattr(
        activities_module,
        "_activity_info_or_none",
        lambda: SimpleNamespace(attempt=2),
    )
    result = crash_worker_process(request)

    assert result.recovered is True
    assert result.completed_attempt == 2


def test_credit_tool_uses_applicant_fixture() -> None:
    result = execute_tool(
        ToolExecutionRequest(
            run_id="run-1",
            turn=1,
            user_id="user@example.com",
            applicant_id="applicant-clean",
            loan_amount=25000,
            loan_purpose="debt_consolidation",
            tool_call=ToolCall(
                id="tool-1",
                name=ToolName.CREDIT_CHECK,
                arguments={"applicant_id": "applicant-clean"},
            ),
        )
    )

    assert result.success is True
    assert result.data["credit_score"] == 785
    assert result.data["delinquency_flags"] == []


def test_workflow_canonicalizes_model_controlled_underwriting_arguments() -> None:
    request = AgentRequest(
        run_id="run-trusted",
        user_id="user@example.com",
        user_request="Underwrite this loan.",
        applicant_id="applicant-borderline",
        loan_amount=25000,
        loan_purpose="debt_consolidation",
    )
    trusted_results = [
        ToolResult(
            tool_call_id="credit",
            name=ToolName.CREDIT_CHECK,
            success=True,
            content="credit",
            data={
                "credit_score": 665,
                "open_trade_lines": 5,
                "delinquency_flags": ["one_30_day_late"],
            },
        ),
        ToolResult(
            tool_call_id="income",
            name=ToolName.INCOME_VERIFICATION,
            success=True,
            content="income",
            data={
                "employment_status": "full_time",
                "verification_status": "verified",
            },
        ),
        ToolResult(
            tool_call_id="dti",
            name=ToolName.DEBT_TO_INCOME_CALC,
            success=True,
            content="dti",
            data={"dti": 0.5091},
        ),
    ]

    canonical = _canonicalize_tool_call(
        ToolCall(
            id="model-controlled-id",
            name=ToolName.POLICY_LOOKUP,
            arguments={
                "loan_purpose": "major_purchase",
                "credit_score": 850,
                "dti": 0.01,
            },
        ),
        request,
        trusted_results,
        turn=4,
        tool_index=1,
    )

    assert canonical.id == "run-trusted-tool-4-1"
    assert canonical.arguments["loan_purpose"] == "debt_consolidation"
    assert canonical.arguments["credit_score"] == 665
    assert canonical.arguments["dti"] == 0.5091


def test_review_signal_rejects_duplicates_and_stale_rounds() -> None:
    async def scenario() -> None:
        agent = LakebaseAgentWorkflow()
        agent._snapshot = AgentSnapshot(
            run_id="run-1",
            status=AgentRunStatus.AWAITING_REVIEW,
            current_turn=5,
        )
        first = ReviewDecisionPayload(
            reviewer_id="reviewer@example.com",
            decision=ReviewDecision.REQUEST_MORE_INFO,
            rationale="Verify bonus income.",
            review_id="run-1-review-5",
            decision_id="run-1-review-5:decision-1",
        )
        await agent.submit_review_decision(first)
        await agent.submit_review_decision(first)
        assert agent._pending_review_decisions == [first]

        agent._pending_review_decisions.clear()
        agent._snapshot.current_turn = 6
        await agent.submit_review_decision(first)
        assert agent._pending_review_decisions == []

    asyncio.run(scenario())


def test_review_api_rejects_a_stale_review_round(monkeypatch) -> None:
    monkeypatch.setenv("LAKEBASE_CONNECTION_MODE", "disabled")
    from lakebase_agentworkflow import api as api_module

    now = datetime.now(timezone.utc)
    run = WorkflowRunView(
        run_id="run-1",
        workflow_id="lakebase-agent-run-1",
        user_id="user@example.com",
        user_request="Underwrite this loan.",
        status=AgentRunStatus.AWAITING_REVIEW,
        metadata={"review": {"status": "pending", "round": 6}},
        turns=6,
        created_at=now,
        updated_at=now,
    )
    monkeypatch.setattr(
        api_module,
        "get_repository",
        lambda: SimpleNamespace(get_run=lambda _: run),
    )
    stale = ReviewDecisionPayload(
        reviewer_id="reviewer@example.com",
        decision=ReviewDecision.APPROVE,
        rationale="This belongs to the old round.",
        review_id="run-1-review-5",
        decision_id="run-1-review-5:decision-1",
    )

    try:
        asyncio.run(api_module.submit_review("run-1", stale))
    except HTTPException as exc:
        assert exc.status_code == 409
        assert "expected run-1-review-6" in str(exc.detail)
    else:
        raise AssertionError("A stale review round should be rejected")


def test_spec_json_artifacts_are_valid() -> None:
    spec_dir = Path("spec")
    for path in spec_dir.glob("*.json"):
        with path.open() as handle:
            parsed = json.load(handle)
        assert parsed


def test_lakebase_endpoint_mode_builds_conninfo(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakePostgres:
        def get_endpoint(self, name: str) -> object:
            calls["get_endpoint"] = name
            return SimpleNamespace(
                status=SimpleNamespace(hosts=SimpleNamespace(host="lakebase.example.com"))
            )

        def generate_database_credential(self, endpoint: str) -> object:
            calls["credential_endpoint"] = endpoint
            return SimpleNamespace(token="generated-token")

    class FakeCurrentUser:
        def me(self) -> object:
            return SimpleNamespace(user_name="user@example.com")

    class FakeWorkspaceClient:
        def __init__(self) -> None:
            self.postgres = FakePostgres()
            self.current_user = FakeCurrentUser()

    def make_conninfo(_: str, **kwargs: object) -> str:
        calls["conninfo"] = kwargs
        return "conninfo"

    databricks_module = types.ModuleType("databricks")
    databricks_sdk_module = types.ModuleType("databricks.sdk")
    databricks_sdk_module.WorkspaceClient = FakeWorkspaceClient
    psycopg_module = types.ModuleType("psycopg")
    psycopg_conninfo_module = types.ModuleType("psycopg.conninfo")
    psycopg_conninfo_module.make_conninfo = make_conninfo
    monkeypatch.setitem(sys.modules, "databricks", databricks_module)
    monkeypatch.setitem(sys.modules, "databricks.sdk", databricks_sdk_module)
    monkeypatch.setitem(sys.modules, "psycopg", psycopg_module)
    monkeypatch.setitem(sys.modules, "psycopg.conninfo", psycopg_conninfo_module)

    endpoint = "projects/project/branches/production/endpoints/primary"
    monkeypatch.setenv("LAKEBASE_CONNECTION_MODE", "endpoint")
    monkeypatch.setenv("LAKEBASE_ENDPOINT", endpoint)
    monkeypatch.setenv("LAKEBASE_DATABASE", "databricks_postgres")

    assert LakebaseRepository()._conninfo() == "conninfo"
    assert calls["get_endpoint"] == endpoint
    assert calls["credential_endpoint"] == endpoint
    assert calls["conninfo"] == {
        "host": "lakebase.example.com",
        "port": "5432",
        "dbname": "databricks_postgres",
        "user": "user@example.com",
        "password": "generated-token",
        "sslmode": "require",
    }


def test_lakebase_run_view_includes_messages_and_tool_results() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.last_query = ""

        def execute(self, query: str, params: tuple[Any, ...]) -> None:
            self.last_query = query

        def fetchall(self) -> list[dict[str, Any]]:
            if "agent_tool_calls" in self.last_query:
                return [
                    {
                        "tool_call_id": "tool-1",
                        "tool_name": "credit_check",
                        "status": "succeeded",
                        "result": {
                            "tool_call_id": "tool-1",
                            "name": "credit_check",
                            "success": True,
                            "content": "Credit context",
                            "data": {"credit_score": 785},
                            "error": None,
                        },
                        "error": None,
                    }
                ]
            if "agent_messages" in self.last_query:
                return [
                    {
                        "role": "user",
                        "name": None,
                        "content": "Underwrite loan",
                        "metadata": {"source": "initial_request"},
                    },
                    {
                        "role": "tool",
                        "name": "credit_check",
                        "content": "Credit context",
                        "metadata": {"success": True},
                    },
                ]
            return []

    now = datetime.now(timezone.utc)
    view = LakebaseRepository()._run_view(
        FakeCursor(),
        {
            "run_id": "run-1",
            "workflow_id": "lakebase-agent-run-1",
            "user_id": "user@example.com",
            "user_request": "Underwrite loan",
            "status": AgentRunStatus.COMPLETED.value,
            "final_response": "Done",
            "turns": 2,
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "created_at": now,
            "updated_at": now,
        },
    )

    assert view.messages[0].content == "Underwrite loan"
    assert view.messages[1].name == "credit_check"
    assert view.tool_results[0].content == "Credit context"
    assert view.usage.total_tokens == 15


def test_lakebase_run_view_skips_legacy_unknown_tool_results() -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.last_query = ""

        def execute(self, query: str, params: tuple[Any, ...]) -> None:
            self.last_query = query

        def fetchall(self) -> list[dict[str, Any]]:
            if "agent_tool_calls" in self.last_query:
                return [
                    {
                        "tool_call_id": "legacy-tool-1",
                        "tool_name": "ip_lookup",
                        "status": "succeeded",
                        "result": {},
                        "error": None,
                    }
                ]
            return []

    now = datetime.now(timezone.utc)
    view = LakebaseRepository()._run_view(
        FakeCursor(),
        {
            "run_id": "legacy-run",
            "workflow_id": "legacy-workflow",
            "user_id": "legacy@example.com",
            "user_request": "Legacy request",
            "status": AgentRunStatus.COMPLETED.value,
            "final_response": "Done",
            "turns": 1,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "created_at": now,
            "updated_at": now,
        },
    )

    assert view.tool_results == []


def test_lakebase_operational_metric_writes_are_separate_from_run_view() -> None:
    class CapturingRepository(LakebaseRepository):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        def _execute(self, sql: str, params: tuple[Any, ...]) -> LakebaseWriteResult:
            self.calls.append((sql, params))
            return LakebaseWriteResult(written=True, row_count=1)

    now = datetime.now(timezone.utc)
    repo = CapturingRepository()

    repo.record_activity_attempt_metric(
        ActivityAttemptMetricRecord(
            run_id="run-1",
            workflow_id="workflow-1",
            workflow_run_id="workflow-run-1",
            workflow_type="LakebaseAgentWorkflow",
            activity_id="activity-1",
            activity_type="execute_tool",
            attempt=2,
            turn=1,
            status="succeeded",
            started_at=now,
            completed_at=now,
            latency_ms=12,
            metadata={"retry_count": 1},
        )
    )
    repo.record_turn_metric(
        TurnMetricRecord(
            run_id="run-1",
            workflow_id="workflow-1",
            workflow_run_id="workflow-run-1",
            workflow_type="LakebaseAgentWorkflow",
            turn=1,
            status="completed",
            started_at=now,
            completed_at=now,
            latency_ms=20,
            model_latency_ms=8,
            tool_latency_ms=12,
            tool_call_count=1,
            usage=TokenUsage(input_tokens=3, output_tokens=4, total_tokens=7),
        )
    )
    repo.record_workflow_metric_completed(
        WorkflowMetricCompletedRecord(
            run_id="run-1",
            workflow_id="workflow-1",
            workflow_run_id="workflow-run-1",
            workflow_type="LakebaseAgentWorkflow",
            status=AgentRunStatus.COMPLETED,
            started_at=now,
            completed_at=now,
            latency_ms=32,
            turn_count=1,
        )
    )

    assert "agent_activity_attempt_metrics" in repo.calls[0][0]
    assert "agent_turn_metrics" in repo.calls[1][0]
    assert "agent_workflow_metrics" in repo.calls[2][0]
    assert "agent_runs" not in repo.calls[0][0]
    assert "execute_tool" in repo.calls[0][1]
    assert "LakebaseAgentWorkflow" in repo.calls[2][1]


def test_review_persistence_is_retry_safe_and_updates_live_status() -> None:
    class CapturingRepository(LakebaseRepository):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        def _execute(self, sql: str, params: tuple[Any, ...]) -> LakebaseWriteResult:
            self.calls.append((sql, params))
            return LakebaseWriteResult(written=True, row_count=1)

    recommendation = LoanRecommendation(
        decision=UnderwritingDecision.REFER,
        rationale="Borderline policy result.",
        dti=0.509,
        credit_score=665,
    )
    repo = CapturingRepository()
    repo.record_review_pending(
        ReviewPendingRecord(
            run_id="run-1",
            recommendation=recommendation,
            review_round=5,
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )
    )

    assert "agent_review_decisions" in repo.calls[0][0]
    assert "ON CONFLICT (review_id)" in repo.calls[0][0]
    assert repo.calls[0][1][0] == "run-1-review-5"
    assert "agent_runs" in repo.calls[1][0]
    assert AgentRunStatus.AWAITING_REVIEW.value in repo.calls[1][1]
    assert "more_info_requested" in repo.calls[1][0]
    assert "more_info_requested" in repo.calls[2][0]
    assert "agent_workflow_metrics" in repo.calls[2][0]
    assert all(sql.count("%s") == len(params) for sql, params in repo.calls)

    repo.calls.clear()
    repo.record_review_decision(
        ReviewDecisionRecord(
            review_id="run-1-review-5",
            decision_id="run-1-review-5:decision-1",
            review_round=5,
            run_id="run-1",
            reviewer_id="reviewer@example.com",
            decision=ReviewDecision.REQUEST_MORE_INFO,
            rationale="Verify the recurring bonus.",
            model_recommendation=recommendation,
            decided_at=datetime.now(timezone.utc),
        )
    )

    assert "ON CONFLICT (review_id)" in repo.calls[0][0]
    assert "AS current_review" in repo.calls[0][0]
    assert "current_review.decision_id" in repo.calls[0][0]
    assert repo.calls[1][1][0] == AgentRunStatus.RUNNING.value
    assert repo.calls[2][1][0] == AgentRunStatus.RUNNING.value
    assert all(sql.count("%s") == len(params) for sql, params in repo.calls)

    repo.calls.clear()
    repo.record_review_decision(
        ReviewDecisionRecord(
            review_id="run-2-review-4",
            decision_id="run-2-review-4:decision-1",
            review_round=4,
            run_id="run-2",
            reviewer_id="reviewer@example.com",
            decision=ReviewDecision.APPROVE,
            rationale="Approve the recommendation.",
            model_recommendation=recommendation,
            decided_at=datetime.now(timezone.utc),
        )
    )

    assert repo.calls[1][1][0] == AgentRunStatus.RUNNING.value
    assert repo.calls[2][1][0] == AgentRunStatus.RUNNING.value
    assert all(sql.count("%s") == len(params) for sql, params in repo.calls)


def test_lakebase_message_and_event_writes_use_idempotency_keys() -> None:
    class CapturingRepository(LakebaseRepository):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[str, tuple[Any, ...]]] = []

        def _execute(self, sql: str, params: tuple[Any, ...]) -> LakebaseWriteResult:
            self.calls.append((sql, params))
            return LakebaseWriteResult(written=True, row_count=1)

    repo = CapturingRepository()
    repo.record_message(
        MessageRecord(
            message_id="run-1-message-1",
            run_id="run-1",
            turn=1,
            message=AgentMessage(role="assistant", content="hello"),
        )
    )
    repo.record_event(
        LakebaseEvent(
            event_id="run-1-event-1",
            run_id="run-1",
            event_type="agent.test",
        )
    )

    assert "ON CONFLICT (message_id)" in repo.calls[0][0]
    assert repo.calls[0][1][0] == "run-1-message-1"
    assert "ON CONFLICT (event_id)" in repo.calls[1][0]
    assert repo.calls[1][1][0] == "run-1-event-1"


def test_lakebase_ops_metric_queries_return_dashboard_contract() -> None:
    now = datetime.now(timezone.utc)

    class FakeCursor:
        def __init__(self) -> None:
            self.last_query = ""
            self.last_params: tuple[Any, ...] = ()

        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def execute(
            self,
            query: str,
            params: tuple[Any, ...] = (),
        ) -> None:
            self.last_query = query
            self.last_params = params

        def fetchone(self) -> dict[str, Any]:
            return {
                "total_runs": 5,
                "completed_runs": 3,
                "terminal_runs": 4,
                "p50_latency_ms": 1250.4,
                "p95_latency_ms": 3899.6,
            }

        def fetchall(self) -> list[dict[str, Any]]:
            if "GROUP BY status" in self.last_query:
                return [
                    {"status": "completed", "run_count": 3},
                    {"status": "failed", "run_count": 1},
                    {"status": "awaiting_review", "run_count": 1},
                ]
            if "agent_activity_retry_counts" in self.last_query:
                return [
                    {
                        "activity_type": "execute_tool",
                        "attempt_count": 7,
                        "retry_count": 2,
                        "last_attempt_completed_at": now,
                    }
                ]
            if "agent_turn_metrics" in self.last_query:
                assert self.last_params == (24,)
                return [
                    {
                        "bucket_start": now,
                        "input_tokens": 120,
                        "output_tokens": 30,
                        "total_tokens": 150,
                        "turn_count": 2,
                    }
                ]
            return []

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_instance = FakeCursor()

        def cursor(self, **_: object) -> FakeCursor:
            return self.cursor_instance

    connection = FakeConnection()

    @contextmanager
    def fake_connection() -> Any:
        yield connection

    repo = LakebaseRepository()
    repo.settings.mode = "url"
    repo._connection = fake_connection  # type: ignore[method-assign]

    runs = repo.get_run_metrics()
    retries = repo.get_retry_metrics()
    cost = repo.get_cost_metrics(hours=24)

    assert runs == {
        "total_runs": 5,
        "completed_runs": 3,
        "success_rate": 0.75,
        "p50_latency_ms": 1250,
        "p95_latency_ms": 3900,
        "counts_by_status": {
            "pending": 0,
            "running": 0,
            "waiting_for_tool": 0,
            "awaiting_review": 1,
            "completed": 3,
            "failed": 1,
            "cancelled": 0,
        },
    }
    assert retries[0]["retry_count"] == 2
    assert retries[0]["last_attempt_completed_at"] == now
    assert cost[0]["total_tokens"] == 150
    assert cost[0]["turn_count"] == 2


def test_disabled_lakebase_returns_empty_ops_metrics() -> None:
    repo = LakebaseRepository()
    repo.settings.mode = "disabled"

    assert repo.get_run_metrics()["counts_by_status"]["awaiting_review"] == 0
    assert repo.get_retry_metrics() == []
    assert repo.get_cost_metrics() == []


def test_start_workflow_generates_uuid_and_submits_to_temporal(monkeypatch) -> None:
    monkeypatch.setenv("LAKEBASE_CONNECTION_MODE", "disabled")
    monkeypatch.setenv("DEMO_CONTROLS_ENABLED", "true")
    from lakebase_agentworkflow import api as api_module

    started: dict[str, Any] = {}

    class FakeTemporalClient:
        async def start_workflow(
            self,
            workflow: Any,
            request: AgentRequest,
            *,
            id: str,
            task_queue: str,
        ) -> None:
            started.update(
                workflow=workflow,
                request=request,
                workflow_id=id,
                task_queue=task_queue,
            )

    async def fake_connect_temporal() -> FakeTemporalClient:
        return FakeTemporalClient()

    monkeypatch.setattr(api_module, "connect_temporal", fake_connect_temporal)
    monkeypatch.setattr(
        api_module,
        "get_repository",
        lambda: SimpleNamespace(get_run=lambda _: None),
    )

    response = asyncio.run(
        api_module.start_workflow(
            StartWorkflowRequest(
                user_request="Underwrite this loan.",
                applicant_id="applicant-clean",
                loan_amount=25000,
                loan_purpose="debt_consolidation",
                demo_model_failure_attempts=1,
            )
        )
    )

    assert str(UUID(response.run.run_id)) == response.run.run_id
    assert started["workflow_id"] == f"lakebase-agent-{response.run.run_id}"
    assert started["request"].run_id == response.run.run_id
    assert started["request"].demo_model_failure_attempts == 1


def test_runtime_config_uses_runtime_temporal_values(monkeypatch) -> None:
    from lakebase_agentworkflow import api as api_module

    monkeypatch.setenv("TEMPORAL_UI_URL", "https://cloud.temporal.io/")
    monkeypatch.delenv("TEMPORAL_UI_NAMESPACE", raising=False)
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "tmprl-dem-cld-lakebase-agent.a2dd6")
    monkeypatch.setenv("DEMO_CONTROLS_ENABLED", "true")

    response = api_module.runtime_config()

    assert response.temporal_ui_url == "https://cloud.temporal.io"
    assert response.temporal_namespace == "tmprl-dem-cld-lakebase-agent.a2dd6"
    assert response.demo_controls_enabled is True

    monkeypatch.setenv("TEMPORAL_UI_NAMESPACE", "lakebase-agent")

    assert api_module.runtime_config().temporal_namespace == "lakebase-agent"


def test_worker_crash_control_starts_temporal_workflow(monkeypatch) -> None:
    from lakebase_agentworkflow import api as api_module

    started: dict[str, Any] = {}

    class FakeTemporalClient:
        async def start_workflow(
            self,
            workflow: Any,
            request: WorkerCrashRequest,
            *,
            id: str,
            task_queue: str,
        ) -> None:
            started.update(
                workflow=workflow,
                request=request,
                workflow_id=id,
                task_queue=task_queue,
            )

    async def fake_connect_temporal() -> FakeTemporalClient:
        return FakeTemporalClient()

    monkeypatch.setenv("DEMO_CONTROLS_ENABLED", "true")
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", "lakebase-agent-task-queue-test")
    monkeypatch.setattr(api_module, "connect_temporal", fake_connect_temporal)

    response = asyncio.run(
        api_module.crash_worker(
            api_module.DemoWorkerCrashRequest(requested_by="operator@example.com")
        )
    )

    assert response.accepted is True
    assert started["request"].control_id == response.control_id
    assert started["request"].requested_by == "operator@example.com"
    assert started["workflow_id"] == response.workflow_id
    assert started["task_queue"] == "lakebase-agent-task-queue-test"


def test_demo_failure_controls_are_disabled_by_default(monkeypatch) -> None:
    from lakebase_agentworkflow import api as api_module

    monkeypatch.delenv("DEMO_CONTROLS_ENABLED", raising=False)

    try:
        asyncio.run(
            api_module.start_workflow(
                StartWorkflowRequest(
                    user_request="Underwrite this loan with an outage.",
                    applicant_id="applicant-clean",
                    loan_amount=25000,
                    loan_purpose="debt_consolidation",
                    demo_model_failure_attempts=1,
                )
            )
        )
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("Model failure injection must be environment-gated")

    try:
        asyncio.run(
            api_module.crash_worker(
                api_module.DemoWorkerCrashRequest(requested_by="operator@example.com")
            )
        )
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("Worker crash control must be environment-gated")


def test_readiness_checks_lakebase_and_temporal(monkeypatch) -> None:
    from lakebase_agentworkflow import api as api_module

    calls: list[str] = []

    class FakeServiceClient:
        async def check_health(self) -> bool:
            calls.append("temporal")
            return True

    async def fake_connect_temporal() -> object:
        return SimpleNamespace(service_client=FakeServiceClient())

    monkeypatch.setattr(
        api_module,
        "get_repository",
        lambda: SimpleNamespace(
            list_runs=lambda limit: calls.append(f"lakebase:{limit}") or []
        ),
    )
    monkeypatch.setattr(api_module, "connect_temporal", fake_connect_temporal)

    response = asyncio.run(api_module.readiness())

    assert response.status == "ready"
    assert calls == ["lakebase:1", "temporal"]


def test_mount_built_frontend_serves_react_and_preserves_api_routes(tmp_path) -> None:
    from lakebase_agentworkflow.api import mount_built_frontend

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<html><body>loan app</body></html>")

    application = FastAPI()

    @application.get("/api/ping")
    def ping() -> dict[str, str]:
        return {"status": "ok"}

    mount_built_frontend(application, dist_dir)
    client = TestClient(application)

    assert client.get("/api/ping").json() == {"status": "ok"}
    assert "loan app" in client.get("/").text


def test_worker_runtime_settings_read_temporal_concurrency_env(monkeypatch) -> None:
    monkeypatch.setenv("TEMPORAL_WORKER_ACTIVITY_EXECUTOR_MAX_WORKERS", "48")
    monkeypatch.setenv("TEMPORAL_WORKER_MAX_CONCURRENT_ACTIVITIES", "40")
    monkeypatch.setenv("TEMPORAL_WORKER_MAX_CONCURRENT_WORKFLOW_TASKS", "24")
    monkeypatch.setenv("TEMPORAL_WORKER_MAX_CONCURRENT_ACTIVITY_TASK_POLLS", "8")
    monkeypatch.setenv("TEMPORAL_WORKER_MAX_ACTIVITIES_PER_SECOND", "12.5")
    monkeypatch.setenv("TEMPORAL_WORKER_GRACEFUL_SHUTDOWN_SECONDS", "27.5")
    monkeypatch.setenv("TEMPORAL_WORKER_READY_FILE", "/tmp/custom-worker-ready")
    monkeypatch.setenv("TEMPORAL_DEPLOYMENT_NAME", "loan-underwriting")
    monkeypatch.setenv("TEMPORAL_WORKER_BUILD_ID", "build-20260712")
    monkeypatch.setenv("TEMPORAL_WORKER_DEFAULT_VERSIONING_BEHAVIOR", "PINNED")

    settings = WorkerRuntimeSettings.from_env()
    kwargs = settings.worker_kwargs()

    assert settings.activity_executor_max_workers == 48
    assert kwargs["max_concurrent_activities"] == 40
    assert kwargs["max_concurrent_workflow_tasks"] == 24
    assert kwargs["max_concurrent_activity_task_polls"] == 8
    assert kwargs["max_activities_per_second"] == 12.5
    assert kwargs["graceful_shutdown_timeout"] == timedelta(seconds=27.5)
    assert settings.ready_file == Path("/tmp/custom-worker-ready")
    assert kwargs["deployment_config"].version.deployment_name == "loan-underwriting"
    assert kwargs["deployment_config"].version.build_id == "build-20260712"
    assert (
        kwargs["deployment_config"].default_versioning_behavior
        == VersioningBehavior.PINNED
    )


def test_worker_runtime_settings_reject_invalid_concurrency(monkeypatch) -> None:
    monkeypatch.setenv("TEMPORAL_WORKER_MAX_CONCURRENT_ACTIVITIES", "0")

    try:
        WorkerRuntimeSettings.from_env()
    except ValueError as exc:
        assert "TEMPORAL_WORKER_MAX_CONCURRENT_ACTIVITIES" in str(exc)
    else:
        raise AssertionError("Zero worker concurrency must be rejected")
