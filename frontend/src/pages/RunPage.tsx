import clsx from "clsx";
import {
  ArrowLeft,
  ArrowRight,
  Ban,
  Check,
  ChevronDown,
  CircleDot,
  Clock,
  FileSearch,
  FileSpreadsheet,
  BetweenHorizontalEnd,
  FileText,
  Table2,
  Layers,
  Play,
  RefreshCw,
  Rows3,
  ShieldCheck,
  Sparkles,
  Trash2,
  XCircle,
  Save,
  SlidersHorizontal,
  Info,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../api/client";
import type { JobStatus, Stage } from "../api/types";
import { AppendMismatchModal, AppendOutcomeAlert, appendIncomplete } from "../components/AppendOutcome";
import { PageHeader } from "../components/layout/Layout";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Alert, EmptyState, ProgressBar, StatTile, useToast } from "../components/ui/Feedback";
import { Modal } from "../components/ui/Overlay";
import { formatBytes, formatDuration, formatNumber, formatTimestamp, plural } from "../lib/format";
import { useNavigate, useWorkflow } from "../state/workflow";

const STAGES: { id: Stage; label: string; description: string; icon: ReactNode }[] = [
  { id: "read", label: "Read workbook", description: "Open the file and load the selected sheets.", icon: <FileSearch /> },
  { id: "clean", label: "Clean sheets", description: "Find each table and its header, remove titles, subtotals and blank rows, and type every column.", icon: <Sparkles /> },
  { id: "append", label: "Match & append", description: "Compare the cleaned headers; with auto-detect on, sheets with identical columns are combined.", icon: <BetweenHorizontalEnd /> },
  { id: "validate", label: "Validate", description: "Check that no rows were lost and removed totals reconcile.", icon: <ShieldCheck /> },
  { id: "write", label: "Prepare output", description: "Write cleaned tables, column metadata and the audit report.", icon: <Save /> },
];

export function RunPage() {
  const flow = useWorkflow();
  const navigate = useNavigate();
  const job = flow.job;

  let body: ReactNode;
  if (!flow.workbook) {
    body = (
      <div className="card">
        <EmptyState
          icon={<FileSpreadsheet />}
          title="No workbook uploaded yet"
          description="Upload an Excel workbook and choose its sheets before running a cleaning job."
          action={<Button variant="primary" icon={<ArrowLeft />} onClick={() => navigate("configuration")}>Go to Configuration</Button>}
        />
      </div>
    );
  } else if (job && (job.status === "queued" || job.status === "running")) body = <RunningView job={job} />;
  else if (job && job.status === "succeeded" && !flow.stale) body = <SuccessView job={job} />;
  else if (job && job.status === "failed" && !flow.stale) body = <ErrorView job={job} />;
  else body = <PreRunView cancelled={job?.status === "cancelled" && !flow.stale} />;

  return (
    <>
      <PageHeader page="run" title="Run cleaning job" description="Review what will happen, then start the job with one controlled action." />
      {body}
    </>
  );
}

/* -------------------------------------------------------------------------- */

function PreRunView({ cancelled }: { cancelled: boolean }) {
  const flow = useWorkflow();
  const navigate = useNavigate();
  const toast = useToast();
  const [state, setState] = useState<"idle" | "loading">("idle");
  const [error, setError] = useState<ApiError | null>(null);
  const workbook = flow.workbook!;

  const start = async () => {
    setState("loading");
    setError(null);
    try {
      await flow.startJob();
    } catch (err) {
      const apiError = err instanceof ApiError ? err : new ApiError(0, { code: "unknown", message: String(err) });
      setError(apiError);
      toast({ severity: "error", title: "The job could not be started", description: apiError.body.message });
    } finally {
      setState("idle");
    }
  };

  const appendTitle = !flow.appendAvailable ? "Not applicable" : flow.append ? "Auto-detect & append" : "Keep sheets separate";
  const appendText = !flow.appendAvailable
    ? workbook.sheets.length < 2
      ? "This workbook has only one sheet."
      : "Only one sheet is selected."
    : flow.append
      ? "Sheets with identical cleaned columns are appended; the result is reported after the run."
      : "Each sheet becomes its own table.";

  return (
    <div className="space-y-6">
      {cancelled && (
        <Alert tone="info" title="The previous job was cancelled">
          No output was produced. Start the job again when you're ready.
        </Alert>
      )}
      {flow.stale && flow.job && (
        <Alert tone="warning" title="Configuration changed since the last run">
          The current results were produced from a different sheet selection or append setting. Run again to update them.
        </Alert>
      )}
      {error && (
        <Alert tone="error" title={error.body.message}>
          {error.body.advice}
        </Alert>
      )}

      <div className="grid items-stretch gap-6 xl:grid-cols-[minmax(0,1.55fr)_minmax(320px,1fr)]">
        <section className="card flex h-full flex-col p-5 md:p-6" aria-labelledby="plan-title">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start">
            <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-lg bg-emerald-50 text-emerald-600 ring-1 ring-emerald-200" aria-hidden>
              <FileSpreadsheet className="h-6 w-6" />
            </div>
            <div className="min-w-0 flex-1">
              <p className="text-caption text-ink-500">Input workbook</p>
              <h2 id="plan-title" className="truncate text-section text-ink-900" title={workbook.filename}>{workbook.filename}</h2>
              <p className="num text-body text-ink-600">
                {formatBytes(workbook.size)} · {plural(workbook.sheets.length, "sheet")} in file
              </p>
            </div>
            <Button size="sm" icon={<SlidersHorizontal />} onClick={() => navigate("configuration")}>
              Edit configuration
            </Button>
          </div>

          <dl className="mt-5 grid flex-1 content-center gap-5 border-t border-ink-200 pt-5 sm:grid-cols-3 sm:gap-0 sm:divide-x sm:divide-ink-200">
            <PlanItem icon={<Layers />} tone="sky" label="Sheets to clean" className="sm:pr-4">
              <dd className="num text-body font-semibold text-ink-900">{flow.selected.length} of {workbook.sheets.length}</dd>
              <dd className="mt-1.5 flex flex-wrap gap-1">
                {flow.selected.slice(0, 6).map((name) => (
                  <span key={name} className="max-w-full truncate rounded bg-ink-100 px-1.5 py-0.5 text-caption text-ink-700">{name}</span>
                ))}
                {flow.selected.length > 6 && <span className="text-caption text-ink-500">+{flow.selected.length - 6} more</span>}
              </dd>
            </PlanItem>
            <PlanItem icon={<BetweenHorizontalEnd />} tone="emerald" label="Append mode" className="sm:px-4">
              <dd className="text-body font-semibold text-ink-900">{appendTitle}</dd>
              <dd className="text-caption text-ink-600">{appendText}</dd>
            </PlanItem>
            <PlanItem icon={<FileText />} tone="violet" label="Output" className="sm:pl-4">
              <dd className="text-body font-semibold text-ink-900">Cleaned CSV + metadata</dd>
              <dd className="text-caption text-ink-600">Plus an audit report of every removed row.</dd>
            </PlanItem>
          </dl>
        </section>

        <aside className="flex">
          <section className="card flex w-full flex-col overflow-hidden" aria-labelledby="ready-title">
            <div className="border-b border-brand-100 bg-brand-50/60 p-5">
              <div className="flex items-start gap-3">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-brand-600 text-white" aria-hidden>
                  <Play className="h-5 w-5" fill="currentColor" />
                </div>
                <div>
                  <h2 id="ready-title" className="text-card text-brand-800">Ready for cleaning</h2>
                  <p className="text-caption text-ink-600">
                    Your source file is never modified. The cleaning job will run based on the current configuration.
                  </p>
                </div>
              </div>
            </div>
            <div className="flex flex-1 flex-col justify-center gap-3 p-5">
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center xl:flex-col xl:items-stretch">
                <Button
                  variant="primary"
                  size="lg"
                  className="w-full sm:w-auto sm:flex-1 xl:w-full xl:flex-none"
                  icon={<Play fill="currentColor" />}
                  state={state}
                  loadingText="Starting job…"
                  disabled={!flow.canRun}
                  onClick={start}
                >
                  {flow.job ? "Run cleaning job again" : "Run cleaning job"}
                </Button>
                <Button variant="ghost" className="w-full sm:w-auto xl:w-full" icon={<ArrowLeft />} onClick={() => navigate("configuration")}>
                  Back to Configuration
                </Button>
              </div>
              {flow.runBlockedReason && <p className="text-center text-caption text-ink-500">{flow.runBlockedReason}</p>}
              <p className="flex gap-2 rounded-md border border-ink-200 bg-ink-50 px-3 py-2.5 text-caption text-ink-600">
                <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                You can move between pages while the job runs; progress stays visible in the top bar.
              </p>
            </div>
          </section>
        </aside>
      </div>

      <section className="card p-5 md:p-6" aria-labelledby="steps-title">
        <h2 id="steps-title" className="text-section text-ink-900">What will happen</h2>
        <p className="mt-0.5 text-body text-ink-600">
          Each selected sheet is read and cleaned once. Sizes, and which sheets match for appending, are known once cleaning finishes.
        </p>
        <ol className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
          {STAGES.map((stage, index) => (
            <li key={stage.id} className="flex gap-3 rounded-lg border border-ink-200 p-3 sm:flex-col sm:gap-2">
              <span className="flex items-center gap-2">
                <span className="num flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-ink-200 bg-white text-caption font-semibold text-ink-600">
                  {index + 1}
                </span>
                <span className="hidden text-ink-400 sm:inline [&>svg]:h-4 [&>svg]:w-4" aria-hidden>{stage.icon}</span>
              </span>
              <div>
                <p className="text-body font-medium text-ink-900">{stage.label}</p>
                <p className="text-caption text-ink-600">{stage.description}</p>
              </div>
            </li>
          ))}
        </ol>
      </section>
    </div>
  );
}

function PlanItem({
  icon,
  tone,
  label,
  className,
  children,
}: {
  icon: ReactNode;
  tone: "sky" | "emerald" | "violet";
  label: string;
  className?: string;
  children: ReactNode;
}) {
  const tones = {
    sky: "bg-sky-50 text-sky-600",
    emerald: "bg-emerald-50 text-emerald-600",
    violet: "bg-violet-50 text-violet-600",
  };
  return (
    <div className={clsx("flex min-w-0 gap-3", className)}>
      <span className={clsx("flex h-9 w-9 shrink-0 items-center justify-center rounded-lg [&>svg]:h-[18px] [&>svg]:w-[18px]", tones[tone])} aria-hidden>
        {icon}
      </span>
      <div className="min-w-0">
        <dt className="text-caption text-ink-500">{label}</dt>
        {children}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function useElapsed(job: JobStatus): number | null {
  const [now, setNow] = useState(Date.now());
  const running = job.status === "running" || job.status === "queued";
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  if (!job.started_at) return null;
  return ((job.finished_at ? job.finished_at * 1000 : now) - job.started_at * 1000) / 1000;
}

function StageList({ job }: { job: JobStatus }) {
  const current = job.stage ? STAGES.findIndex((stage) => stage.id === job.stage) : -1;
  const finished = job.status === "succeeded";
  const failed = job.status === "failed";
  return (
    <ol className="space-y-0" aria-label="Job stages">
      {STAGES.map((stage, index) => {
        const state = finished || index < current ? "done" : index === current ? (failed ? "failed" : "active") : "pending";
        return (
          <li key={stage.id} className="relative flex gap-3 pb-5 last:pb-0">
            {index < STAGES.length - 1 && (
              <span className={clsx("absolute left-[13px] top-7 h-[calc(100%-24px)] w-px", state === "done" ? "bg-emerald-400" : "bg-ink-200")} aria-hidden />
            )}
            <span
              className={clsx(
                "relative z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full transition-colors duration-300",
                state === "done" && "bg-emerald-600 text-white",
                state === "active" && "bg-brand-600 text-white ring-4 ring-brand-100",
                state === "failed" && "bg-brand-700 text-white",
                state === "pending" && "border border-ink-300 bg-white text-ink-400",
              )}
              aria-hidden
            >
              {state === "done" ? <Check className="h-3.5 w-3.5" strokeWidth={3} /> : state === "failed" ? <XCircle className="h-4 w-4" /> : state === "active" ? <CircleDot className="h-3.5 w-3.5 animate-pulse" /> : <span className="num text-caption">{index + 1}</span>}
            </span>
            <div className="min-w-0 pt-0.5">
              <p className={clsx("text-body", state === "pending" ? "text-ink-500" : "font-medium text-ink-900")}>
                {stage.label}
                <span className="sr-only">{` — ${state === "done" ? "complete" : state === "active" ? "in progress" : state === "failed" ? "failed" : "pending"}`}</span>
              </p>
              {state === "active" && job.stage === "clean" && job.current_sheet && (
                <p className="text-caption text-brand-700">
                  {job.current_sheet} · sheet {Math.min(job.sheets_done + 1, job.sheets_total)} of {job.sheets_total}
                </p>
              )}
              {state === "failed" && job.error?.sheet && <p className="text-caption text-brand-700">Stopped at {job.error.sheet}</p>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function RunningView({ job }: { job: JobStatus }) {
  const flow = useWorkflow();
  const [confirm, setConfirm] = useState(false);
  const elapsed = useElapsed(job);
  const stepIndex = job.stage ? STAGES.findIndex((stage) => stage.id === job.stage) + 1 : 1;
  const percent = Math.round(job.progress * 100);

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
      <section className="card p-5 md:p-6" aria-labelledby="running-title" aria-busy="true">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <Badge tone="info" dot className="mb-2">{job.status === "queued" ? "Queued" : "Running"}</Badge>
            <h2 id="running-title" className="truncate text-section text-ink-900">
              {job.message}
            </h2>
            <p className="text-body text-ink-600">Step {stepIndex} of {STAGES.length} · {job.source_name}</p>
          </div>
          <Button variant="danger" icon={<Ban />} onClick={() => setConfirm(true)} disabled={job.message.startsWith("Cancelling")}>
            {job.message.startsWith("Cancelling") ? "Cancelling…" : "Cancel job"}
          </Button>
        </div>

        <div className="mt-6" aria-live="polite">
          <div className="mb-2 flex items-baseline justify-between">
            <span className="text-caption font-medium text-ink-600">Overall progress</span>
            <span className="num text-[20px] font-semibold text-ink-900">{percent}%</span>
          </div>
          <ProgressBar value={job.progress} label="Cleaning progress" active indeterminate={job.stage === "read" || job.status === "queued"} />
          {job.stage === "read" && (
            <p className="mt-1.5 text-caption text-ink-500">Opening the workbook. Large files can take a little while before sheet-by-sheet progress starts.</p>
          )}
        </div>

        <div className="mt-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile icon={<Layers />} label="Sheets processed" value={`${job.sheets_done} / ${job.sheets_total}`} tone="info" />
          <StatTile icon={<Rows3 />} label="Rows kept" value={formatNumber(job.rows_kept)} tone="success" />
          <StatTile icon={<Trash2 />} label="Rows removed" value={formatNumber(job.rows_removed)} tone="warning" />
          <StatTile icon={<Clock />} label="Elapsed" value={formatDuration(elapsed)} />
        </div>

        <p className="mt-5 flex items-center gap-2 text-caption text-ink-500">
          <Info className="h-3.5 w-3.5" aria-hidden /> Processing continues if you leave this page. Your source file is not modified.
        </p>
      </section>

      <aside className="card p-5 md:p-6">
        <h2 className="mb-4 text-card text-ink-900">Progress</h2>
        <StageList job={job} />
      </aside>

      <Modal
        open={confirm}
        onClose={() => setConfirm(false)}
        title="Cancel this cleaning job?"
        description="The job stops after the sheet currently being cleaned. No output is produced, and you can run it again at any time."
        footer={
          <>
            <Button onClick={() => setConfirm(false)}>Keep running</Button>
            <Button
              variant="primary"
              icon={<Ban />}
              onClick={async () => {
                setConfirm(false);
                await flow.cancelJob();
              }}
            >
              Cancel job
            </Button>
          </>
        }
      />
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function SuccessView({ job }: { job: JobStatus }) {
  const flow = useWorkflow();
  const navigate = useNavigate();
  const toast = useToast();
  const [rerun, setRerun] = useState<"idle" | "loading">("idle");
  const [detailsOpen, setDetailsOpen] = useState(false);
  const shownFor = useRef<string | null>(null);

  // Append didn't fully work: say so right away, once per job.
  useEffect(() => {
    const results = flow.results;
    if (results && results.job.id === job.id && appendIncomplete(results.append_check) && shownFor.current !== job.id) {
      shownFor.current = job.id;
      setDetailsOpen(true);
    }
  }, [flow.results, job.id]);
  const summary = flow.results?.summary;
  const loading = !summary;

  const runAgain = async () => {
    setRerun("loading");
    try {
      await flow.startJob();
    } catch (err) {
      toast({ severity: "error", title: "The job could not be started", description: err instanceof ApiError ? err.body.message : String(err) });
    } finally {
      setRerun("idle");
    }
  };

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
      <section className="card p-5 md:p-6" aria-labelledby="success-title">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center">
          <div className="flex h-14 w-14 shrink-0 animate-scale-in items-center justify-center rounded-full bg-emerald-600 text-white" aria-hidden>
            <svg viewBox="0 0 24 24" className="h-7 w-7" fill="none" stroke="currentColor" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round">
              <path d="M5 12.5l4.5 4.5L19 7.5" strokeDasharray="24" className="animate-draw-check" />
            </svg>
          </div>
          <div className="min-w-0 flex-1">
            <p className="text-caption font-medium text-emerald-700">Cleaning completed successfully</p>
            <h2 id="success-title" className="truncate text-section text-ink-900">{job.source_name}</h2>
            <p className="text-caption text-ink-500">
              Finished {formatTimestamp(job.finished_at)} · took {formatDuration(job.elapsed_seconds)}
            </p>
          </div>
        </div>

        <div className="mt-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile loading={loading} icon={<Layers />} label="Sheets processed" value={summary?.sheets_selected} tone="info" />
          <StatTile loading={loading} icon={<Table2 />} label="Cleaned tables" value={summary?.outputs} hint={summary?.appended_outputs ? `${summary.appended_outputs} appended` : undefined} tone="brand" />
          <StatTile loading={loading} icon={<Rows3 />} label="Rows kept" value={summary && formatNumber(summary.rows)} tone="success" />
          <StatTile loading={loading} icon={<Trash2 />} label="Rows removed" value={summary && formatNumber(summary.rows_removed)} tone="warning" />
        </div>

        {flow.results?.append_check && flow.results.append_check.status !== "single_table" && (
          <div className="mt-5">
            <AppendOutcomeAlert check={flow.results.append_check} onDetails={() => setDetailsOpen(true)} />
          </div>
        )}
        {flow.results?.append_check && (
          <AppendMismatchModal
            open={detailsOpen && appendIncomplete(flow.results.append_check)}
            check={flow.results.append_check}
            onClose={() => setDetailsOpen(false)}
            onChangeConfiguration={() => {
              setDetailsOpen(false);
              navigate("configuration");
            }}
          />
        )}
        {summary && summary.consistency_issues > 0 && (
          <Alert tone="error" className="mt-5" title={`${plural(summary.consistency_issues, "consistency check")} failed`}>
            Some output does not fully reconcile with the source. Review the Consistency checks tab before loading the data.
          </Alert>
        )}
        {summary && summary.flagged_columns > 0 && (
          <Alert tone="warning" className="mt-5" title="Some columns need a quick review">
            {summary.flagged_columns > 0 && <>{plural(summary.flagged_columns, "column")} marked for a judgement check. </>}
            Details are in Review & Results.
          </Alert>
        )}
        {flow.resultsError && (
          <Alert tone="error" className="mt-5" title={flow.resultsError.body.message} action={<Button size="sm" onClick={() => flow.reloadResults()}>Retry</Button>}>
            {flow.resultsError.body.advice}
          </Alert>
        )}

        <div className="mt-6 flex flex-col gap-2 border-t border-ink-200 pt-5 sm:flex-row">
          <Button variant="primary" size="lg" iconRight={<ArrowRight />} onClick={() => navigate("results")} disabled={!flow.results}>
            Review results
          </Button>
          <Button size="lg" icon={<RefreshCw />} state={rerun} loadingText="Starting…" onClick={runAgain}>
            Run again
          </Button>
          <Button size="lg" variant="ghost" icon={<SlidersHorizontal />} onClick={() => navigate("configuration")}>
            Change configuration
          </Button>
        </div>
      </section>
      <aside className="card p-5 md:p-6">
        <h2 className="mb-4 text-card text-ink-900">Completed steps</h2>
        <StageList job={job} />
      </aside>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function ErrorView({ job }: { job: JobStatus }) {
  const flow = useWorkflow();
  const navigate = useNavigate();
  const toast = useToast();
  const [retrying, setRetrying] = useState<"idle" | "loading">("idle");
  const [open, setOpen] = useState(false);
  const error = job.error;
  const stage = STAGES.find((item) => item.id === error?.stage);

  const retry = async () => {
    setRetrying("loading");
    try {
      await flow.startJob();
    } catch (err) {
      toast({ severity: "error", title: "The job could not be started", description: err instanceof ApiError ? err.body.message : String(err) });
    } finally {
      setRetrying("idle");
    }
  };

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
      <section className="card overflow-hidden" aria-labelledby="error-title" role="alert">
        <div className="flex gap-4 border-b border-brand-100 bg-brand-50/60 p-5 md:p-6">
          <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-brand-600 text-white" aria-hidden>
            <XCircle className="h-6 w-6" />
          </div>
          <div className="min-w-0">
            <h2 id="error-title" className="text-section text-brand-800">Cleaning could not be completed</h2>
            <p className="mt-0.5 text-body text-ink-700">{error?.message ?? "The job stopped unexpectedly."}</p>
          </div>
        </div>
        <dl className="grid gap-4 p-5 sm:grid-cols-2 md:p-6">
          <div>
            <dt className="text-caption text-ink-500">What failed</dt>
            <dd className="text-body font-medium text-ink-900">{error?.detail || "An unexpected error"}</dd>
          </div>
          <div>
            <dt className="text-caption text-ink-500">Where</dt>
            <dd className="text-body font-medium text-ink-900">
              {stage ? stage.label : "Before processing"}
              {error?.sheet ? ` · ${error.sheet}` : ""}
            </dd>
          </div>
          <div className="sm:col-span-2">
            <dt className="text-caption text-ink-500">What you can do</dt>
            <dd className="text-body text-ink-900">{error?.advice ?? "Retry the job. If it fails again, go back and deselect the sheet named above."}</dd>
          </div>
        </dl>
        <div className="flex flex-col gap-2 border-t border-ink-200 px-5 py-4 sm:flex-row md:px-6">
          <Button variant="primary" icon={<RefreshCw />} state={retrying} loadingText="Retrying…" onClick={retry}>
            Retry
          </Button>
          <Button icon={<ArrowLeft />} onClick={() => navigate("configuration")}>
            Back to Configuration
          </Button>
        </div>
        {(error?.technical || error?.kind) && (
          <div className="border-t border-ink-200">
            <button
              type="button"
              aria-expanded={open}
              onClick={() => setOpen(!open)}
              className="flex w-full items-center justify-between px-5 py-3 text-body font-medium text-ink-700 hover:bg-ink-50 md:px-6"
            >
              Technical details
              <ChevronDown className={clsx("h-4 w-4 transition-transform", open && "rotate-180")} aria-hidden />
            </button>
            {open && (
              <div className="animate-fade-in px-5 pb-5 md:px-6">
                <p className="mb-2 text-caption text-ink-500">
                  Error type: <code className="font-mono text-ink-800">{error?.kind}</code> · Job {job.id}
                </p>
                {error?.technical && (
                  <pre className="max-h-72 overflow-auto rounded-md bg-ink-900 p-4 font-mono text-[12px] leading-5 text-ink-100 scroll-thin">
                    {error.technical}
                  </pre>
                )}
              </div>
            )}
          </div>
        )}
      </section>
      <aside className="card p-5 md:p-6">
        <h2 className="mb-4 text-card text-ink-900">Where it stopped</h2>
        <StageList job={job} />
      </aside>
    </div>
  );
}
