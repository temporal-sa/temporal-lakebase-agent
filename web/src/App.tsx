import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { crashWorker, fetchRuns, fetchRuntimeConfig, startWorkflow, submitReview } from "./api";
import { OpsDashboard } from "./components/OpsDashboard";
import { ReviewAudit, ReviewPanel, recommendationForRun } from "./components/ReviewPanel";
import { RunCard } from "./components/RunCard";
import { TemporalLogo, TemporalMark } from "./components/TemporalLogo";
import {
  reconcilePendingReview,
  type PendingReviewSubmission,
} from "./reviewState";
import type {
  ReviewDecisionPayload,
  RuntimeConfig,
  StartWorkflowRequest,
  WorkflowRun,
} from "./types";

type View = "underwriting" | "ops";

interface ApplicantPreset {
  id: string;
  label: string;
  shortLabel: string;
  amount: number;
  purpose: string;
}

const APPLICANTS: ApplicantPreset[] = [
  {
    id: "applicant-borderline",
    label: "Borderline · human judgment",
    shortLabel: "Borderline referral",
    amount: 25000,
    purpose: "debt_consolidation",
  },
  {
    id: "applicant-clean",
    label: "Clean approval",
    shortLabel: "Clean approval",
    amount: 25000,
    purpose: "debt_consolidation",
  },
  {
    id: "applicant-decline",
    label: "Clear decline",
    shortLabel: "Clear decline",
    amount: 40000,
    purpose: "home_improvement",
  },
  {
    id: "applicant-new-credit",
    label: "Thin credit file",
    shortLabel: "Thin credit",
    amount: 10000,
    purpose: "major_purchase",
  },
  {
    id: "applicant-self-employed",
    label: "Self-employed referral",
    shortLabel: "Self-employed",
    amount: 30000,
    purpose: "home_improvement",
  },
];

const PURPOSES = [
  ["debt_consolidation", "Debt consolidation"],
  ["home_improvement", "Home improvement"],
  ["major_purchase", "Major purchase"],
  ["medical", "Medical expense"],
];

const STATUS_LABEL: Record<WorkflowRun["status"], string> = {
  pending: "Pending",
  running: "Running",
  waiting_for_tool: "Waiting for tool",
  awaiting_review: "Awaiting review",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

function formatCount(value: number): string {
  return new Intl.NumberFormat().format(value);
}

function formatMoney(value: number): string {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(value);
}

function relativeTime(value: string): string {
  const diff = Math.max(0, Date.now() - new Date(value).getTime());
  const seconds = Math.round(diff / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  return `${hours}h ago`;
}

function initialView(): View {
  return window.location.hash === "#ops" ? "ops" : "underwriting";
}

export default function App() {
  const [view, setViewState] = useState<View>(initialView);
  const [runs, setRuns] = useState<WorkflowRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [applicantId, setApplicantId] = useState(APPLICANTS[0].id);
  const [loanAmount, setLoanAmount] = useState(APPLICANTS[0].amount);
  const [loanPurpose, setLoanPurpose] = useState(APPLICANTS[0].purpose);
  const [userId, setUserId] = useState("loan-officer@example.com");
  const [model, setModel] = useState("gpt-6-luna");
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [startingWithModelFailure, setStartingWithModelFailure] = useState(false);
  const [crashingWorker, setCrashingWorker] = useState(false);
  const [controlStatus, setControlStatus] = useState<string | null>(null);
  const [controlError, setControlError] = useState<string | null>(null);
  const [demoMode, setDemoMode] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runtimeConfig, setRuntimeConfig] = useState<RuntimeConfig>({
    temporal_ui_url: "http://localhost:8233",
    temporal_namespace: "default",
    demo_controls_enabled: false,
  });
  const transcriptRef = useRef<HTMLDivElement>(null);
  const pendingReviewsRef = useRef(new Map<string, PendingReviewSubmission>());

  const selectedRun = useMemo(
    () => runs.find((run) => run.run_id === selectedRunId) ?? runs[0],
    [runs, selectedRunId]
  );

  const setView = useCallback((nextView: View) => {
    setViewState(nextView);
    window.history.replaceState(null, "", nextView === "ops" ? "#ops" : "#underwriting");
  }, []);

  const updateDataMode = useCallback((demo: boolean) => setDemoMode(demo), []);

  useEffect(() => {
    const onHashChange = () => setViewState(initialView());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchRuntimeConfig()
      .then((config) => {
        if (!cancelled) setRuntimeConfig(config);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    async function loadRuns(initial = false) {
      try {
        const { runs: loadedRuns, demo } = await fetchRuns();
        if (cancelled) return;
        setRuns((previousRuns) => {
          const reconciledRuns = loadedRuns.map((loadedRun) => {
            const result = reconcilePendingReview(
              loadedRun,
              pendingReviewsRef.current.get(loadedRun.run_id)
            );
            if (!result.keepPending) pendingReviewsRef.current.delete(loadedRun.run_id);
            return result.run;
          });
          const optimisticRuns = previousRuns.filter(
            (run) =>
              run.status === "pending" &&
              !reconciledRuns.some((loadedRun) => loadedRun.run_id === run.run_id)
          );
          return [...optimisticRuns, ...reconciledRuns];
        });
        setSelectedRunId((currentRunId) => currentRunId ?? loadedRuns[0]?.run_id ?? null);
        setDemoMode(demo);
      } catch (err: unknown) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Could not load runs");
        }
      } finally {
        if (initial && !cancelled) setLoading(false);
      }
    }

    loadRuns(true);
    const interval = window.setInterval(() => loadRuns(), 4000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    transcriptRef.current?.scrollTo({
      top: transcriptRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [selectedRunId, selectedRun?.messages.length]);

  function applyPreset(preset: ApplicantPreset) {
    setApplicantId(preset.id);
    setLoanAmount(preset.amount);
    setLoanPurpose(preset.purpose);
  }

  async function submitWorkflow(demoModelFailureAttempts = 0) {
    if (!applicantId || !Number.isFinite(loanAmount) || loanAmount <= 0 || starting) return;
    const purposeLabel = loanPurpose.replace(/_/g, " ");
    const payload: StartWorkflowRequest = {
      user_request: `Underwrite a ${formatMoney(loanAmount)} personal loan for ${applicantId} (${purposeLabel}).`,
      user_id: userId.trim() || "loan-officer@example.com",
      applicant_id: applicantId,
      loan_amount: loanAmount,
      loan_purpose: loanPurpose,
      model: model.trim() || "gpt-6-luna",
      max_turns: 8,
      demo_model_failure_attempts: demoModelFailureAttempts,
      metadata: { channel: "loan_review_ui" },
    };

    setStarting(true);
    setStartingWithModelFailure(demoModelFailureAttempts > 0);
    setError(null);
    setControlError(null);
    try {
      const { run, demo } = await startWorkflow(payload, demoMode);
      setRuns((previous) => [run, ...previous.filter((item) => item.run_id !== run.run_id)]);
      setSelectedRunId(run.run_id);
      setDemoMode(demo);
      if (demoModelFailureAttempts > 0) {
        setControlStatus(
          "Model outage injected. Temporal will retry the first model activity automatically."
        );
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Could not start workflow");
    } finally {
      setStarting(false);
      setStartingWithModelFailure(false);
    }
  }

  async function requestWorkerCrash() {
    if (crashingWorker) return;
    const confirmed = window.confirm(
      "Crash the active worker process? Kubernetes will restart it and Temporal will retry the control activity."
    );
    if (!confirmed) return;

    setCrashingWorker(true);
    setControlError(null);
    setControlStatus(null);
    try {
      const response = await crashWorker(userId.trim() || "loan-officer@example.com");
      setControlStatus(response.message);
    } catch (err: unknown) {
      setControlError(err instanceof Error ? err.message : "Could not start worker crash demo");
    } finally {
      setCrashingWorker(false);
    }
  }

  async function reviewRun(runId: string, payload: ReviewDecisionPayload) {
    const response = await submitReview(runId, payload, demoMode);
    const acceptedAt = new Date().toISOString();
    pendingReviewsRef.current.set(runId, { payload, acceptedAt });
    setDemoMode(response.demo);
    setRuns((previous) =>
      previous.map((run) =>
        run.run_id === runId
          ? {
              ...run,
              status: payload.decision === "request_more_info" ? "running" : "completed",
              review_decision: payload,
              updated_at: acceptedAt,
            }
          : run
      )
    );
  }

  const completedRuns = runs.filter((run) => run.status === "completed").length;
  const awaitingRuns = runs.filter(
    (run) => run.status === "awaiting_review" && !run.review_decision
  ).length;
  const totalTokens = runs.reduce((sum, run) => sum + run.usage.total_tokens, 0);
  const selectedRecommendation = selectedRun ? recommendationForRun(selectedRun) : null;

  return (
    <div className="app">
      <header className="topbar">
        <TemporalLogo tone="light" />
        <span className="topbar__divider" aria-hidden="true" />
        <span className="topbar__product">loan underwriting agent</span>
        <nav className="topbar__nav" aria-label="Application views">
          <button
            className={view === "underwriting" ? "topbar__tab topbar__tab--active" : "topbar__tab"}
            onClick={() => setView("underwriting")}
          >
            Underwriting
            {awaitingRuns > 0 && <span className="topbar__count">{awaitingRuns}</span>}
          </button>
          <button
            className={view === "ops" ? "topbar__tab topbar__tab--active" : "topbar__tab"}
            onClick={() => setView("ops")}
          >
            Live Ops
          </button>
        </nav>
        <span className="topbar__spacer" />
        <span className={`status ${demoMode ? "status--warn" : "status--on"}`}>
          <span className="status__dot" />
          {demoMode ? "demo data" : "Lakebase API"}
        </span>
      </header>

      {view === "ops" ? (
        <OpsDashboard onDataModeChange={updateDataMode} />
      ) : (
        <div className="layout">
          <aside className="rail">
            <div className="rail__stats">
              <div>
                <span className="rail__stat">{formatCount(runs.length)}</span>
                <span className="rail__caption">runs</span>
              </div>
              <div className={awaitingRuns ? "rail__stat-card rail__stat-card--review" : "rail__stat-card"}>
                <span className="rail__stat">{formatCount(awaitingRuns)}</span>
                <span className="rail__caption">to review</span>
              </div>
            </div>

            <h2 className="rail__heading">Loan files</h2>
            {loading ? (
              <p className="rail__muted">Loading loan files…</p>
            ) : runs.length === 0 ? (
              <p className="rail__muted">No loan files yet.</p>
            ) : (
              <div className="run-list">
                {runs.map((run) => (
                  <RunCard
                    key={run.run_id}
                    run={run}
                    selected={selectedRun?.run_id === run.run_id}
                    temporalUiUrl={runtimeConfig.temporal_ui_url}
                    temporalNamespace={runtimeConfig.temporal_namespace}
                    onSelect={(nextRun) => setSelectedRunId(nextRun.run_id)}
                  />
                ))}
              </div>
            )}

            <h2 className="rail__heading rail__heading--activity">Operational view</h2>
            <dl className="metric-list">
              <div>
                <dt>Completed</dt>
                <dd>{formatCount(completedRuns)}</dd>
              </div>
              <div>
                <dt>Total tokens</dt>
                <dd>{formatCount(totalTokens)}</dd>
              </div>
              <div>
                <dt>Execution history</dt>
                <dd>Temporal</dd>
              </div>
              <div>
                <dt>Operational + audit</dt>
                <dd>Lakebase</dd>
              </div>
            </dl>
          </aside>

          <main className="workspace">
            <section className="starter">
              <div className="starter__title">
                <span className="starter__mark">
                  <TemporalMark size={26} />
                </span>
                <div>
                  <span className="eyebrow">New personal loan file</span>
                  <h1>Start an underwriting run</h1>
                  <p>Temporal drives the tool loop; every recommendation pauses for sign-off.</p>
                </div>
              </div>

              <div className="stack-story" aria-label="Why Temporal and Lakebase">
                <article className="stack-story__system stack-story__system--temporal">
                  <span>Why Temporal</span>
                  <strong>Protects progress</strong>
                  <p>Resumes after failure and waits durably for people.</p>
                </article>
                <span className="stack-story__plus" aria-hidden="true">+</span>
                <article className="stack-story__system stack-story__system--lakebase">
                  <span>Why Lakebase</span>
                  <strong>Closes the governed data loop</strong>
                  <p>
                    Neon or another Postgres can hold app state. Lakebase is the advantage here:
                    governed policy arrives from Unity Catalog, and—with CDF enabled—review
                    changes return as Delta history without a separately operated data pipeline.
                  </p>
                </article>
              </div>

              <div className="starter__form starter__form--loan">
                <label className="field field--applicant">
                  <span>Applicant scenario</span>
                  <select
                    value={applicantId}
                    onChange={(event) => {
                      const preset = APPLICANTS.find((item) => item.id === event.target.value);
                      if (preset) applyPreset(preset);
                    }}
                  >
                    {APPLICANTS.map((applicant) => (
                      <option key={applicant.id} value={applicant.id}>
                        {applicant.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>Loan amount</span>
                  <div className="money-input">
                    <span>$</span>
                    <input
                      type="number"
                      min={1000}
                      step={1000}
                      value={loanAmount}
                      onChange={(event) => setLoanAmount(Number(event.target.value))}
                    />
                  </div>
                </label>
                <label className="field">
                  <span>Purpose</span>
                  <select value={loanPurpose} onChange={(event) => setLoanPurpose(event.target.value)}>
                    {PURPOSES.map(([value, label]) => (
                      <option value={value} key={value}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span>Requested by</span>
                  <input value={userId} onChange={(event) => setUserId(event.target.value)} />
                </label>
                <label className="field field--model">
                  <span>Model</span>
                  <input value={model} onChange={(event) => setModel(event.target.value)} />
                </label>
              </div>

              <div className="starter__actions">
                <div className="suggestions" aria-label="Applicant presets">
                  {APPLICANTS.slice(0, 3).map((applicant) => (
                    <button
                      key={applicant.id}
                      className={applicantId === applicant.id ? "chip chip--active" : "chip"}
                      onClick={() => applyPreset(applicant)}
                    >
                      {applicant.shortLabel}
                    </button>
                  ))}
                </div>
                <button
                  className="btn btn--primary starter__submit"
                  disabled={starting || !applicantId || !Number.isFinite(loanAmount) || loanAmount <= 0}
                  onClick={() => submitWorkflow()}
                >
                  {starting ? "Starting workflow…" : `Underwrite ${formatMoney(loanAmount)}`}
                </button>
              </div>

              {runtimeConfig.demo_controls_enabled && (
                <section className="demo-controls" aria-labelledby="demo-controls-heading">
                  <div className="demo-controls__intro">
                    <span className="eyebrow">Resilience lab</span>
                    <h2 id="demo-controls-heading">Demo controls</h2>
                    <p>Trigger real failures, then watch Temporal recover without losing progress.</p>
                  </div>
                  <div className="demo-controls__actions">
                    <article>
                      <div>
                        <strong>Model API outage</strong>
                        <span>Fail attempt 1 of the next run’s first model activity.</span>
                      </div>
                      <button
                        className="btn btn--secondary"
                        disabled={starting || !applicantId || loanAmount <= 0}
                        onClick={() => submitWorkflow(1)}
                      >
                        {startingWithModelFailure ? "Injecting failure…" : "Start with model failure"}
                      </button>
                    </article>
                    <article>
                      <div>
                        <strong>Worker crash</strong>
                        <span>Exit the active worker once; its retry confirms recovery.</span>
                      </div>
                      <button
                        className="btn btn--danger"
                        disabled={crashingWorker}
                        onClick={requestWorkerCrash}
                      >
                        {crashingWorker ? "Requesting crash…" : "Crash active worker"}
                      </button>
                    </article>
                  </div>
                  {(controlStatus || controlError) && (
                    <p
                      className={controlError ? "demo-controls__notice demo-controls__notice--error" : "demo-controls__notice"}
                      aria-live="polite"
                    >
                      {controlError ?? controlStatus}
                    </p>
                  )}
                </section>
              )}

              {error && <div className="error">{error}</div>}
            </section>

            <section className="run-detail">
              {selectedRun ? (
                <>
                  <div className="run-detail__head">
                    <div>
                      <span className={`detail-status detail-status--${selectedRun.status}`}>
                        {STATUS_LABEL[selectedRun.status]}
                      </span>
                      <h2>
                        {typeof selectedRun.metadata?.applicant_id === "string"
                          ? selectedRun.metadata.applicant_id
                          : "Loan application"}
                      </h2>
                      <p>
                        <span className="mono">{selectedRun.workflow_id}</span>
                        <span>Updated {relativeTime(selectedRun.updated_at)}</span>
                        {selectedRecommendation && (
                          <span className="run-detail__decision">
                            Model: {selectedRecommendation.decision}
                          </span>
                        )}
                      </p>
                    </div>
                    <dl className="detail-metrics">
                      <div>
                        <dt>Turns</dt>
                        <dd>{selectedRun.turns}</dd>
                      </div>
                      <div>
                        <dt>Tools</dt>
                        <dd>{selectedRun.tool_results.length}</dd>
                      </div>
                      <div>
                        <dt>Tokens</dt>
                        <dd>{formatCount(selectedRun.usage.total_tokens)}</dd>
                      </div>
                    </dl>
                  </div>

                  <div className="run-detail__scroll" ref={transcriptRef}>
                    {selectedRun.status === "awaiting_review" && !selectedRun.review_decision && (
                      <ReviewPanel
                        run={selectedRun}
                        liveData={!demoMode}
                        onSubmit={(payload) => reviewRun(selectedRun.run_id, payload)}
                      />
                    )}
                    {(selectedRun.status !== "awaiting_review" || selectedRun.review_decision) && (
                      <ReviewAudit run={selectedRun} liveData={!demoMode} />
                    )}

                    <div className="transcript-heading">
                      <div>
                        <span className="eyebrow">Durable execution trail</span>
                        <h3>Workflow transcript</h3>
                      </div>
                      <span>{selectedRun.messages.length} events</span>
                    </div>
                    <div className="transcript">
                      {selectedRun.messages.map((message, index) => (
                        <div key={`${message.role}-${index}`} className={`row row--${message.role}`}>
                          {message.role !== "user" && (
                            <span className="avatar">
                              <TemporalMark size={16} />
                            </span>
                          )}
                          <div className={`bubble bubble--${message.role}`}>
                            {message.name && (
                              <span className="bubble__name">
                                {message.name.replace(/_/g, " ")}
                              </span>
                            )}
                            {message.content}
                          </div>
                        </div>
                      ))}
                      {selectedRun.final_response && (
                        <div className="final-response">
                          <span>Final reviewed outcome</span>
                          <p>{selectedRun.final_response}</p>
                        </div>
                      )}
                    </div>
                  </div>
                </>
              ) : (
                <div className="empty">
                  <TemporalMark size={42} />
                  <h2>No loan file selected</h2>
                </div>
              )}
            </section>
          </main>
        </div>
      )}
    </div>
  );
}
