# Runbook: Temporal + Lakebase underwriting agent

This runbook is for presenting the demo. The [README](README.md) explains what it is, how it's deployed, and how Lakebase is set up.

## What it shows

A loan-underwriting agent runs as one Temporal Workflow: a model picks the next check, Activities run the credit, income, debt-to-income and policy lookups, and every recommendation stops for an underwriter's decision. Lakebase holds the live review state that the screen shows and the governed policy the agent applies.

The demo makes three problems visible:

- **A model or tool failure mid-run loses the agent's progress.** Beat 3 fails the first model call on purpose and the run carries on after one retry. Beat 4 kills the Workers while a tool is running, and the run picks up at that tool when they come back.
- **An agent that waits for a person can't hold a process open for days.** Beats 2 and 5 pause every run for sign-off, keep the wait through a Worker outage, and resume on the decision.
- **The review state and the policy need to live where the business can govern and query them.** The screen, the review audit and the **Live Ops** dashboard all read Lakebase tables, and the policy check reads limits synced from Unity Catalog (beats 1 and 6).

## Before you start

The full demo needs Databricks Lakebase, so present it on tmprl-demo.cloud. Locally you can rehearse the Temporal side only; see [Rehearse locally](#rehearse-locally).

### On tmprl-demo.cloud

- The demo runs at https://lakebase-agent.tmprl-demo.cloud behind the registry's sign-in. Its Temporal Cloud Namespace name starts with `tmprl-dem-cld-lakebase-agent`.
- The hosted Worker uses the deterministic `scripted` model (no model API key), adds a 3-second delay to each underwriting tool so you have time to crash it mid-tool, and has the **Resilience lab** controls turned on.
- Beats 4 and 5 kill or stop Workers. Do them in a **crashable environment**, not on the shared deployment: on the demo's card in the tmprl-demo.cloud catalog, press **Create crashable environment**. It gives you a private copy of the Workers on their own Task Queue (`lakebase-agent-task-queue-<id>`), with **Open environment**, **Crash workers**, **Scale down workers**, **Extend +1h** and **Delete** buttons. It deletes itself after an hour.
- The **Crash active worker** button in the app's Resilience lab exits whichever Worker picks it up. On the shared deployment that's the Worker every other presenter is using, so only press it inside your crashable environment.
- The run list comes from Lakebase and isn't filtered per environment or per user, so you'll see other presenters' runs too. Start a fresh run for each beat and work from the selected one.
- The **Temporal** icon on each run card opens the run in the Temporal Cloud UI. You need access to the Namespace to see it.

## Start it

1. Open https://lakebase-agent.tmprl-demo.cloud, or press **Open environment** on your crashable environment.
2. A healthy start shows the **Underwriting** and **Live Ops** tabs, a green **Lakebase API** badge in the top bar, **Start an underwriting run** with an **Applicant scenario** list, the **Borderline referral**, **Clean approval** and **Clear decline** chips, and an **Underwrite $25,000** button. With demo controls on, a **Resilience lab** panel sits below with **Start with model failure** and **Crash active worker**.
3. If the badge says **demo data** instead, the page is showing built-in sample runs, not Lakebase. See [When something goes wrong](#when-something-goes-wrong).

## The walkthrough

Every run follows the same path. The run card and the detail header show its status: **Pending**, then **Running** while the model and the tools work, then **Awaiting review**. The **Workflow transcript** fills in as the model asks for each tool and each result comes back. The scripted model always calls the tools in the same order: credit check, income verification, debt-to-income, policy lookup. With the hosted 3-second delay, the tools alone take at least 12 seconds.

### 1. Start a borderline loan file

1. Pick **Borderline referral** (applicant `applicant-borderline`, $25,000, debt consolidation) and press **Underwrite $25,000**.
2. The run appears at the top of **Loan files** as **Pending**, then **Running** as each check runs. The transcript shows each tool result, starting with `Credit score 665 with 5 open trade lines; delinquency flags: one_30_day_late.`
3. When it stops, the status reads **Awaiting review** and the header shows **Model: refer**.
4. Say: "The agent loop is a Temporal Workflow. Each model call and each tool is an Activity with its own timeout and retry policy, and each step writes its result to Lakebase, which is what this screen reads."
5. For proof, press the **Temporal** icon on the run card. The history shows `call_model`, `execute_tool` and the `record_*` Lakebase writes in order, ending in a wait.

### 2. The underwriter decides

1. In **Underwriter review**, read **Model says Refer**, the credit score, projected DTI (50.9%) and **Model rationale**. **Policy source** says **Unity Catalog → Lakebase synced table** when the policy sync is set up, or **Fixture policy fallback** if not.
2. Press **Request more info**. The run goes back to **Running**, the model answers the request in the transcript, and the run returns to **Awaiting review** for a second round.
3. Press **Approve** (or **Deny**). The run shows **Completed** and **Recommendation approved** under **Human review recorded** straight away; the screen assumes the decision will land. A few seconds later the Workflow writes its result to Lakebase and the **Final reviewed outcome** appears.
4. Say: "Every recommendation stops here, approve or decline. The Workflow is waiting on a Signal. Nothing runs while it waits, and it would wait the same way for a week."
5. For proof, the history shows `WorkflowExecutionSignaled` for each decision, then `record_review_decision` and the completion.

### 3. The model API goes down

1. Pick **Borderline referral** again and press **Start with model failure** in the Resilience lab.
2. The notice reads `Model outage injected. Temporal will retry the first model activity automatically.` The run looks like any other and reaches **Awaiting review**.
3. Say: "The first model call failed as if the provider was down. Temporal retried it two seconds later, and nothing before it ran again."
4. For proof, in the Temporal UI the first `call_model` Activity shows attempt 2, with the last failure `Simulated model API outage on activity attempt 1`. In **Live Ops**, **Activity retries** counts it under `call_model`.

### 4. Crash the Workers mid-tool (crashable environment)

1. In your crashable environment, start a **Borderline referral** run.
2. While the run is still **Running** and the transcript has only one or two tool results, go back to the catalog card and press **Crash workers**. Wait for **Running** to come back on the card.
3. Refresh the app. The run carries on from the tool that was running and reaches **Awaiting review**. The earlier tool results are still in the transcript.
4. Say: "The Worker pods were killed mid-tool. When new pods came up, Temporal handed them the same run at the same step, and the finished steps didn't run again."
5. For proof, in the Temporal UI the `execute_tool` that was running shows attempt 2 with the last failure `activity Heartbeat timeout`. Temporal noticed the dead Worker within the 30-second heartbeat timeout.

Keep the outage short. The tool step has 2 minutes and 3 attempts in total; if the Workers aren't back in time, the run fails and the screen shows **Failed**.

The in-app **Crash active worker** button shows the same thing from inside the app: a small control Workflow whose Activity exits the Worker process on attempt 1. The notice reads `Crash requested. The worker exits on activity attempt 1 and Temporal confirms recovery on attempt 2.` Kubernetes restarts the container, and attempt 2 completes on the new process.

### 5. The review waits through a Worker outage (crashable environment)

1. In your crashable environment, start a run and wait for **Awaiting review**.
2. On the catalog card, press **Scale down workers**. The card shows **Workers scaled to zero**.
3. Back in the app, press **Approve**. The card shows **Completed** at once, but no **Final reviewed outcome** appears: the screen is showing the decision it sent, and the Workflow hasn't run yet. In the Temporal UI the run is still **Running**, with `WorkflowExecutionSignaled` as its last event. If you refresh the app, the run goes back to **Awaiting review**, because Lakebase hasn't recorded the decision.
4. Press **Scale up workers** on the card. Once the Workers start, the Workflow applies the decision, writes it to Lakebase, and the **Final reviewed outcome** appears.
5. Say: "There were no Workers at all when the underwriter clicked Approve. Temporal kept the decision with the run and delivered it when Workers came back."

Scale down only while the run is waiting for review. With no Workers during a tool step, the tool's 2-minute limit can run out.

### 6. Live operations from Lakebase

1. Open **Live Ops**.
2. **Awaiting human review**, **Success rate**, **Median latency** and **Tail latency** sit across the top, with **Run outcomes**, **Activity retries** and **Token usage trend** below. The chart cards name the Lakebase table they read, such as `Lakebase · agent_activity_retry_counts`.
3. Say: "Temporal keeps the execution history. The application also writes its own operational record to Lakebase, so this dashboard is a Postgres query, and the same tables are there for analytics."

## Show it in code

All links point at commit [`69c8f53`](https://github.com/temporal-sa/temporal-lakebase-agent/commit/69c8f53b77ff622b54cbb7ea4754d7956638bda2). Open a file locally with `code -g <path>:<line>`.

### Give the model, the tools and the database their own retry policies

**When to use it:** after beat 3, or when someone asks how many retries the agent framework needed.

**Where it is:** [`src/lakebase_agentworkflow/workflows.py` lines 61–80](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/workflows.py#L61-L80) and [lines 238–255](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/workflows.py#L238-L255) (`code -g src/lakebase_agentworkflow/workflows.py:61`)

```python
MODEL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=4,
)
TOOL_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=3,
)
LAKEBASE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=20),
    maximum_attempts=5,
)
# ...
```

```python
decision = await workflow.execute_activity(
    call_model,
    ModelRequest(
        run_id=request.run_id,
        turn=turn,
        # ...
        simulated_failure_attempts=pending_model_failure_attempts,
    ),
    start_to_close_timeout=timedelta(seconds=30),
    schedule_to_close_timeout=timedelta(minutes=3),
    retry_policy=MODEL_RETRY,
)
```

**What you need to understand:**

- Each kind of step gets the policy that fits it. The model call waits 2 seconds before its first retry and tries up to 4 times within 3 minutes (`schedule_to_close_timeout` covers all attempts). Tools get 3 attempts within 2 minutes. Lakebase writes get 5 attempts, each limited to 15 seconds.
- The backoff coefficient is left at the default of 2.0, so waits double up to each policy's `maximum_interval`.
- The OpenAI path raises `ApplicationError(..., non_retryable=True)` for authentication failures and 4xx responses, so those fail at once instead of using up the attempts. Rate limits and connection errors stay retryable.
- When a step runs out of attempts, the Workflow's `run` method catches the error, records the run as failed in Lakebase, and re-raises, so the Workflow fails with the real cause ([lines 107–134](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/workflows.py#L107-L134)).
- The customer doesn't write retry loops, backoff timers, or a checkpoint table for the agent's progress.

**Docs:** [Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies), [Activity timeouts](https://docs.temporal.io/encyclopedia/detecting-activity-failures)

### Inject a model outage without touching the agent loop

**When to use it:** during beat 3, or when someone asks whether the failure is real.

**Where it is:** [`src/lakebase_agentworkflow/activities.py` lines 167–184](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/activities.py#L167-L184) (`code -g src/lakebase_agentworkflow/activities.py:167`)

```python
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
```

**What you need to understand:**

- The failure is a real Activity failure: attempt 1 raises a retryable `ApplicationError`, and Temporal schedules attempt 2 under `MODEL_RETRY`. The attempt number comes from `activity.info().attempt`.
- **Start with model failure** sets `demo_model_failure_attempts` to 1 in the run's input. The Workflow passes it to the first model call only, then resets it to 0, so later turns don't fail.
- This is demo-only. A customer's model Activity would just call the provider and let its errors raise.

### Notice a dead Worker in 30 seconds

**When to use it:** after beat 4, or when someone asks how Temporal knew the pod was gone.

**Where it is:** [`src/lakebase_agentworkflow/workflows.py` lines 408–423](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/workflows.py#L408-L423) (`code -g src/lakebase_agentworkflow/workflows.py:408`), with the heartbeat in [`activities.py` lines 1029–1040](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/activities.py#L1029-L1040)

```python
result = await workflow.execute_activity(
    execute_tool,
    ToolExecutionRequest(
        run_id=request.run_id,
        turn=turn,
        user_id=request.user_id,
        tool_call=tool_call,
        applicant_id=request.applicant_id,
        loan_amount=request.loan_amount,
        loan_purpose=request.loan_purpose,
    ),
    start_to_close_timeout=timedelta(seconds=60),
    schedule_to_close_timeout=timedelta(minutes=2),
    heartbeat_timeout=timedelta(seconds=30),
    retry_policy=TOOL_RETRY,
)
```

```python
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
```

**What you need to understand:**

- A tool attempt may take up to 60 seconds but must heartbeat at least every 30. When the pod dies, the heartbeats stop, and Temporal times the attempt out and puts the retry back on the Task Queue for the next Worker.
- `schedule_to_close_timeout=timedelta(minutes=2)` caps the whole tool step, retries included. That's the limit beat 4 warns about.
- `execute_tool` is a plain `def` Activity, so the SDK runs it on the Worker's thread pool (`activity_executor` in `worker.py`). Its `time.sleep` doesn't block the Worker's event loop.
- The heartbeat lives in the demo delay because the fixture tools return instantly. A real long-running tool would heartbeat as it works, and could put progress in the heartbeat details so the retry picks up where it stopped.

**Docs:** [Activity heartbeats](https://docs.temporal.io/encyclopedia/detecting-activity-failures#activity-heartbeat)

### Wait for the underwriter, and accept each decision once

**When to use it:** during beats 2 and 5, or when someone asks what stops a double click from approving twice.

**Where it is:** [`src/lakebase_agentworkflow/workflows.py` lines 498–514](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/workflows.py#L498-L514) and [lines 561–567](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/workflows.py#L561-L567) (`code -g src/lakebase_agentworkflow/workflows.py:498`)

```python
@workflow.signal
async def submit_review_decision(self, payload: ReviewDecisionPayload) -> None:
    if (
        self._snapshot is None
        or self._snapshot.status != AgentRunStatus.AWAITING_REVIEW
        or self._pending_review_decisions
    ):
        return
    expected_review_id = (
        f"{self._snapshot.run_id}-review-{self._snapshot.current_turn}"
    )
    if payload.review_id != expected_review_id:
        return
    if payload.decision_id in self._seen_review_decision_ids:
        return
    self._seen_review_decision_ids.add(payload.decision_id)
    self._pending_review_decisions.append(payload)
```

```python
await workflow.wait_condition(
    lambda: bool(self._pending_review_decisions) or self._cancel_requested
)
if self._cancel_requested:
    return None

review = self._pending_review_decisions.pop(0)
```

**What you need to understand:**

- `wait_condition` has no timeout, so a run waits for review as long as it takes. While it waits, nothing runs on a Worker; the decision arrives as a Signal and the Workflow continues.
- The Signal is recorded by the Temporal Service even when no Worker is running, which is beat 5. The Worker applies it when it next processes the run.
- The handler ignores a decision for the wrong round (`review_id` names the turn), a decision when one is already queued, and a repeated `decision_id`. A second click or a stale browser tab can't approve twice or approve a round that has moved on.
- The API checks the same round against Lakebase before it sends the Signal and returns 409 if the round has changed ([`api.py` lines 282–304](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/api.py#L282-L304)). The Workflow check is the one that holds if two requests race.
- **Request more info** adds the reviewer's note to the conversation and goes around the loop again, which is why the second review is round 6, not 5.
- Ignored Signals still appear in the history as `WorkflowExecutionSignaled`; the handler just drops them. An Update with a validator would let the caller see the rejection instead.
- There's also a `request_cancel` Signal that ends a waiting run as **Cancelled**. The UI has no button for it.

**Docs:** [Python message passing](https://docs.temporal.io/develop/python/workflows/message-passing), [Signals, Queries and Updates](https://docs.temporal.io/encyclopedia/workflow-message-passing)

### Make every Lakebase write safe to retry

**When to use it:** after any retry beat, when someone asks whether a retried write duplicates rows.

**Where it is:** [`src/lakebase_agentworkflow/helpers/lakebase.py` lines 90–101](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/helpers/lakebase.py#L90-L101) (`code -g src/lakebase_agentworkflow/helpers/lakebase.py:90`) and [`workflows.py` lines 290–295](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/workflows.py#L290-L295)

```python
def record_message(self, record: MessageRecord) -> LakebaseWriteResult:
    sql = f"""
        INSERT INTO {self.settings.schema}.agent_messages
          (message_id, run_id, turn, role, name, content, metadata, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (message_id) DO UPDATE SET
          turn = EXCLUDED.turn,
          role = EXCLUDED.role,
          name = EXCLUDED.name,
          content = EXCLUDED.content,
          metadata = EXCLUDED.metadata
    """
```

```python
await self._record_message(
    request.run_id,
    turn,
    assistant_message,
    f"{request.run_id}-message-turn-{turn}-assistant",
)
```

**What you need to understand:**

- Activities run at least once: a Worker can write the row and die before reporting success. Every Lakebase write here is an upsert keyed on an ID the Workflow builds from the run ID and the step (`...-message-turn-3-assistant`, `...-event-review-pending-5`), so a retry overwrites the same row instead of adding one.
- The ID is built in Workflow code, so it's the same on every attempt and on replay.
- Temporal holds the execution state; Lakebase holds the application's view of it. The screen never queries Temporal for run state; the Activities project it into Lakebase as the run goes.

**Docs:** [Idempotent Activities](https://docs.temporal.io/activity-definition#idempotency)

### Let the model choose the tool, not the facts

**When to use it:** when someone asks how you stop an agent inventing a credit score.

**Where it is:** [`src/lakebase_agentworkflow/workflows.py` lines 803–818](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/workflows.py#L803-L818) (`code -g src/lakebase_agentworkflow/workflows.py:803`)

```python
"""Keep model-selected tools while sourcing underwriting facts from trusted state."""
if tool_call.name == ToolName.CREDIT_CHECK:
    arguments = {"applicant_id": request.applicant_id}
elif tool_call.name == ToolName.INCOME_VERIFICATION:
    arguments = {"applicant_id": request.applicant_id}
elif tool_call.name == ToolName.DEBT_TO_INCOME_CALC:
    credit = _latest_tool_data(tool_results, ToolName.CREDIT_CHECK)
    income = _latest_tool_data(tool_results, ToolName.INCOME_VERIFICATION)
    arguments = {
        "loan_amount": request.loan_amount,
        "loan_term_months": 36,
    }
    if credit is not None:
        arguments["existing_monthly_debt"] = credit.get("existing_monthly_debt")
    if income is not None:
        arguments["verified_annual_income"] = income.get("verified_annual_income")
```

**What you need to understand:**

- The model decides which tool to call next. The Workflow throws away the model's arguments and fills them from the loan request and earlier tool results it has already recorded.
- Because those results are in the Workflow's state, they survive crashes and replays with the run; nothing has to re-fetch them.
- The tool call ID is rebuilt the same way (`<run>-tool-<turn>-<index>`), which keeps the Lakebase rows for tool calls retry-safe too.

### Crash the Worker on purpose

**When to use it:** when someone asks what **Crash active worker** actually does.

**Where it is:** [`src/lakebase_agentworkflow/workflows.py` lines 83–93](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/workflows.py#L83-L93) and [`activities.py` lines 208–236](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/activities.py#L208-L236) (`code -g src/lakebase_agentworkflow/activities.py:208`)

```python
@workflow.defn(name="WorkerCrashDemoWorkflow")
class WorkerCrashDemoWorkflow:
    @workflow.run
    async def run(self, request: WorkerCrashRequest) -> WorkerCrashResult:
        return await workflow.execute_activity(
            crash_worker_process,
            request,
            start_to_close_timeout=timedelta(seconds=10),
            schedule_to_close_timeout=timedelta(minutes=2),
            retry_policy=WORKER_CRASH_RETRY,
        )
```

```python
attempt = _activity_attempt()
if attempt > 1:
    # ...
    return WorkerCrashResult(
        control_id=request.control_id,
        recovered=True,
        completed_attempt=attempt,
    )
# ...
_terminate_worker_process(137)
```

**What you need to understand:**

- Attempt 1 calls `os._exit(137)`, which ends the Worker process at once, including any tools it was running for other runs. The attempt never reports back; its 10-second `start_to_close_timeout` expires and Temporal schedules attempt 2, which succeeds on the restarted process.
- The app needs no Kubernetes credentials; the platform restarts the container. The registry's **Crash workers** button deletes the pods instead. Either way, the Workflow code doesn't change.
- The control Workflow's ID is `lakebase-agent-worker-crash-<id>`. Open it in the Temporal UI to show the timeout and the second attempt.
- This is demo-only. Both the API route and the Activity refuse to run unless `DEMO_CONTROLS_ENABLED=true`.

### Keep each run on the code it started with

**When to use it:** when someone asks what happens to a run waiting for review when you deploy a new Worker.

**Where it is:** [`src/lakebase_agentworkflow/worker.py` lines 136–154](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/worker.py#L136-L154) (`code -g src/lakebase_agentworkflow/worker.py:136`)

```python
def deployment_config(self) -> WorkerDeploymentConfig | None:
    deployment_name = os.getenv("TEMPORAL_DEPLOYMENT_NAME") or os.getenv(
        "TEMPORAL_WORKER_DEPLOYMENT_NAME"
    )
    build_id = os.getenv("TEMPORAL_WORKER_BUILD_ID")
    if not deployment_name and not build_id:
        return None
    # ...
    return WorkerDeploymentConfig(
        version=WorkerDeploymentVersion(
            deployment_name=deployment_name,
            build_id=build_id,
        ),
        use_worker_versioning=True,
        default_versioning_behavior=self.default_versioning_behavior,
    )
```

**What you need to understand:**

- When a deployment name and a build ID are set, the Worker joins a Worker Deployment as that version. The tmprl-demo.cloud catalog entry turns Worker Versioning on for this demo, with deployment name `lakebase-agent`. Locally neither is set and the Worker is unversioned.
- The default behavior is `PINNED` (`TEMPORAL_WORKER_DEFAULT_VERSIONING_BEHAVIOR`, [lines 246–257](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/worker.py#L246-L257)). A pinned run finishes on the build it started on, so a new build can change the agent loop without breaking runs already waiting for review. New runs start on the new current version.
- The cost of pinning: the old version's Workers have to keep running until its pinned runs finish. A run that waits weeks for review keeps its version around that long. Long-lived Workflows can move to Auto-Upgrade or upgrade on Continue-As-New instead.

**Docs:** [Worker Versioning](https://docs.temporal.io/worker-versioning), [Choosing a versioning behavior](https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning#choosing-behavior)

### What's demo-only

When someone asks what they'd change for their own system, these are the parts to leave out:

- **The applicants, the bureau data and the scripted model.** [`activities.py` lines 50–121](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/activities.py#L50-L121) holds five fixture applicants, and `scripted_model_decision` ([lines 273–364](https://github.com/temporal-sa/temporal-lakebase-agent/blob/69c8f53b77ff622b54cbb7ea4754d7956638bda2/src/lakebase_agentworkflow/activities.py#L273-L364)) always asks for the four tools in order. Set `AGENT_MODEL_PROVIDER=openai` and `OPENAI_API_KEY` for a real model; the tools would call real bureau and income services.
- **The fixture policy.** If Lakebase has no synced policy row, `policy_lookup` uses built-in limits and the review panel says **Fixture policy fallback**.
- **The 3-second tool delay** (`DEMO_TOOL_DELAY_SECONDS`), there only so you can crash a Worker mid-tool.
- **The Resilience lab:** the injected model failure and the crash control Workflow, both behind `DEMO_CONTROLS_ENABLED`.

## Rehearse locally

Without Lakebase, the app can start runs but can't show them: in `LAKEBASE_CONNECTION_MODE=disabled` the API returns no runs, so a run you start stays **Pending** on screen and the review panel never appears. Use it to rehearse the Temporal side and the code, and drive the review from the CLI.

1. Install Python 3.11+, [uv](https://docs.astral.sh/uv/), Node.js 22.12+ and the [Temporal CLI](https://docs.temporal.io/cli). Run `uv sync --extra lakebase --extra dev`, `cp config/example.env .env`, and `cd web && npm install`. The example `.env` already sets disabled Lakebase, the `scripted` model and a local Temporal server.
2. In four terminals: `temporal server start-dev`; `uv run python -m lakebase_agentworkflow.worker`; `uv run uvicorn lakebase_agentworkflow.api:app --reload --host 127.0.0.1 --port 8000`; `cd web && npm run dev`.
3. Open http://localhost:5173 and press **Underwrite $25,000**, or start a run from a terminal and leave it running: `uv run python -m lakebase_agentworkflow.starter "Underwrite a loan" --applicant-id applicant-borderline --loan-amount 25000 --loan-purpose debt_consolidation`.
4. Find the run in the Temporal UI at http://localhost:8233 (Workflow ID `lakebase-agent-<run id>`). Read its state with `temporal workflow query --workflow-id <workflow id> --type status`.
5. When `status` is `awaiting_review`, send the decision. The `review_id` is `<run id>-review-<current_turn>` from the query:

   ```sh
   temporal workflow signal --workflow-id lakebase-agent-<run id> --name submit_review_decision \
     --input '{"reviewer_id":"me@example.com","decision":"approve","rationale":"Looks fine","review_id":"<run id>-review-5","decision_id":"d1"}'
   ```

6. To try the crash and outage controls locally, set `DEMO_CONTROLS_ENABLED=true` (and optionally `DEMO_TOOL_DELAY_SECONDS=3`) for the Worker and the API. **Crash active worker** exits your local Worker; nothing restarts it, so start it again yourself.

## Reset and tear down

- **Between beats:** start a new run. Runs don't interfere with each other.
- **Runs left waiting:** a run you don't decide waits for review indefinitely in the shared Namespace and shows in everyone's **Loan files**. Approve or deny your runs before you leave, or end them with `temporal workflow signal --workflow-id <id> --name request_cancel`, which marks them **Cancelled**.
- **Crashable environment:** press **Delete** on the catalog card when you're done. If its Workers are scaled down, scale them up or delete it; otherwise its runs stay waiting. It deletes itself after an hour unless you press **Extend +1h**.
- **Locally:** `Ctrl-C` the web dev server, the API, the Worker and the dev server.

## When something goes wrong

| What you see | Likely cause | Fix |
|---|---|---|
| The top bar says **demo data** and the runs are `demo-loan-...` | The frontend was built with `VITE_DEMO_MODE=true` (or `VITE_USE_API=false`), or with `VITE_ALLOW_DEMO_FALLBACK=true` and `/api/runs` failed | Rebuild without those settings; check that `/api/runs` returns 200 |
| **Lakebase query failed; check the API server logs.** | The web app can't reach Lakebase (credentials, endpoint, or schema) | Check `GET /ready`; it returns 503 naming Lakebase or Temporal |
| A started run stays **Pending** | Locally: Lakebase is disabled, so the screen never sees the run. Hosted: no Worker is polling the run's Task Queue | Locally, use the Temporal UI and CLI. Hosted, check the Worker, or scale up your crashable environment |
| **Review round changed; expected ... Refresh the run before submitting this decision.** | The run moved to a new review round, for example after **Request more info** | Refresh and decide on the current round |
| **Run is ...; only awaiting-review runs accept a decision.** | Someone already decided this run, or it isn't waiting yet | Refresh |
| A run shows **Failed** after a Worker crash | The Workers were gone longer than the tool step's 2-minute limit, or a tool used up its 3 attempts | Start a new run and keep the outage shorter; scale down only while a run is awaiting review |
| No **Resilience lab** panel | `DEMO_CONTROLS_ENABLED` isn't `true` on the web app | Set it, or skip beats 3 and the in-app crash |
| A **Start with model failure** run fails at once, or the crash control Workflow fails, with `Demo controls are disabled on this worker` | The web app has demo controls on but the Worker doesn't | Set `DEMO_CONTROLS_ENABLED=true` on the Worker too |
| The **Temporal** icon opens a page you can't see | Your account has no access to the project's Temporal Cloud Namespace | Ask for read access, or show the run from the transcript |

## Keeping this runbook current

The code links point at a fixed commit. When a linked file changes, update its link, line numbers, and snippet together. Before an event, list the linked files that have changed:

```sh
grep -oE 'blob/[0-9a-f]{40}/[^#)]+' RUNBOOK.md | sort -u | while IFS=/ read -r _ sha file; do
  git diff --quiet "$sha" HEAD -- "$file" || echo "changed since ${sha:0:7}: $file"
done
```
