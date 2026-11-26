import clsx from "clsx";
import {
  ArrowLeft,
  ArrowRight,
  Ban,
  BetweenHorizontalEnd,
  Check,
  ChevronDown,
  CircleDot,
  Clock,
  FileSearch,
  FileSpreadsheet,
  FileText,
  Files,
  Info,
  Layers,
  Play,
  RefreshCw,
  Rows3,
  Save,
  ShieldCheck,
  SkipForward,
  SlidersHorizontal,
  Sparkles,
  Table2,
  Trash2,
  TriangleAlert,
  XCircle,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../api/client";
import type { BatchStatus, JobStatus, Stage } from "../api/types";
import { AppendMismatchModal, AppendOutcomeAlert, appendIncomplete } from "../components/AppendOutcome";
import { PageHeader } from "../components/layout/Layout";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Alert, EmptyState, ProgressBar, StatTile, useToast } from "../components/ui/Feedback";
import { Modal } from "../components/ui/Overlay";
import { Pagination, usePaged } from "../components/ui/Pagination";
import { formatBytes, formatDuration, formatNumber, formatTimestamp, plural } from "../lib/format";
import { appendUnavailableReason, effectiveAppend, useNavigate, useWorkflow } from "../state/workflow";

const STAGES: { id: Stage; label: string; description: string; icon: ReactNode }[] = [
  { id: "read", label: "Open", description: "Load workbook and sheets.", icon: <FileSearch /> },
  {
    id: "clean",
    label: "Clean",
    description: "Detect headers, drop noise rows, type columns.",
    icon: <Sparkles />,
  },
  { id: "append", label: "Append", description: "Merge sheets with matching columns.", icon: <BetweenHorizontalEnd /> },
  { id: "validate", label: "Validate", description: "Reconcile rows and totals.", icon: <ShieldCheck /> },
  { id: "write", label: "Export", description: "Write CSV, metadata, audit.", icon: <Save /> },
];

const isActive = (job: JobStatus) => job.status === "queued" || job.status === "running";

const FILES_PER_PAGE = 6;

export function RunPage() {
  const flow = useWorkflow();
  const navigate = useNavigate();
  const batch = flow.batch;

  let body: ReactNode;
  if (flow.running && batch) body = <RunningView batch={batch} />;
  else if (!flow.files.length) {
    body = (
      <div className="card">
        <EmptyState
          icon={<FileSpreadsheet />}
          title="No workbook yet"
          description="Upload and configure files first."
          action={<Button variant="primary" icon={<ArrowLeft />} onClick={() => navigate("configuration")}>Configure</Button>}
        />
      </div>
    );
  } else if (batch && !flow.stale && (batch.status !== "cancelled" || batch.files_succeeded + batch.files_failed > 0))
    // A cancelled batch where some files had already finished still has outcomes to show.
    body = <DoneView batch={batch} />;
  else body = <PreRunView cancelled={batch?.status === "cancelled" && !flow.stale ? batch : null} />;

  return (
    <>
      <PageHeader page="run" title="Run" description="Review the plan and start cleaning." />
      {body}
    </>
  );
}

function useStart() {
  const flow = useWorkflow();
  const toast = useToast();
  const [state, setState] = useState<"idle" | "loading">("idle");
  const [error, setError] = useState<ApiError | null>(null);
  const start = async () => {
    setState("loading");
    setError(null);
    try {
      await flow.startBatch();
    } catch (err) {
      const apiError = err instanceof ApiError ? err : new ApiError(0, { code: "unknown", message: String(err) });
      setError(apiError);
      toast({ severity: "error", title: "Couldn't start", description: apiError.body.message });
    } finally {
      setState("idle");
    }
  };
  return { state, error, start };
}

/* -------------------------------------------------------------------------- */

function PreRunView({ cancelled }: { cancelled: BatchStatus | null }) {
  const flow = useWorkflow();
  // Files listed a page at a time: a 20-file batch never pushes the start button off screen.
  const filesPage = usePaged(flow.files, FILES_PER_PAGE);
  const navigate = useNavigate();
  const { state, error, start } = useStart();
  const count = flow.files.length;
  const size = flow.files.reduce((sum, entry) => sum + entry.workbook.size, 0);
  const cleanedBeforeCancel = cancelled?.files_succeeded ?? 0;

  return (
    <div className="space-y-6">
      {cancelled && (
        <Alert tone="info" title="Previous run cancelled">
          {cleanedBeforeCancel ? `${plural(cleanedBeforeCancel, "file")} finished; results are kept.` : "No output produced."}
        </Alert>
      )}
      {flow.stale && flow.batch && (
        <Alert tone="warning" title="Configuration changed">
          Run again to refresh results.
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
            <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-md border border-ink-200 bg-ink-50 text-ink-600" aria-hidden>
              {count > 1 ? <Files className="h-6 w-6" /> : <FileSpreadsheet className="h-6 w-6" />}
            </div>
            <div className="min-w-0 flex-1">
              <p className="label-caps">Input</p>
              <h2 id="plan-title" className="truncate text-section text-ink-900" title={count === 1 ? flow.files[0].workbook.filename : undefined}>
                {count === 1 ? flow.files[0].workbook.filename : plural(count, "file")}
              </h2>
              <p className="num text-body text-ink-600">
                {formatBytes(size)} · {flow.totalSelected} of {plural(flow.totalSheets, "sheet")} selected
              </p>
            </div>
            <Button size="sm" icon={<SlidersHorizontal />} onClick={() => navigate("configuration")}>
              Edit
            </Button>
          </div>

          <div className="mt-5 flex-1 border-t border-ink-200 pt-5">
            <div className="overflow-hidden rounded-lg border border-ink-200">
              <div className="relative overflow-x-auto scroll-thin">
                <table className="w-full min-w-[480px] border-collapse text-table">
                  <caption className="sr-only">Files to clean</caption>
                  <thead className="bg-ink-50">
                    <tr className="border-b border-ink-200 text-left text-caption font-semibold text-ink-600">
                      <th scope="col" className="w-10 px-3 py-2 text-right">#</th>
                      <th scope="col" className="px-3 py-2">File</th>
                      <th scope="col" className="px-3 py-2">Sheets</th>
                      <th scope="col" className="px-3 py-2">Append</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filesPage.slice.map((entry, index) => {
                      const reason = appendUnavailableReason(entry);
                      return (
                        <tr key={entry.workbook.id} className="border-b border-ink-100 align-top last:border-0">
                          <td className="num px-3 py-2.5 text-right text-ink-500">{filesPage.from + index}</td>
                          <td className="max-w-[220px] px-3 py-2.5">
                            <p className="truncate font-medium text-ink-900" title={entry.workbook.filename}>{entry.workbook.filename}</p>
                            <p className="num text-caption text-ink-500">{formatBytes(entry.workbook.size)}</p>
                          </td>
                          <td className="px-3 py-2.5">
                            <p className="num font-medium text-ink-900">
                              {entry.selected.length} of {entry.workbook.sheets.length}
                            </p>
                            <div className="mt-1 flex flex-wrap gap-1">
                              {entry.selected.slice(0, 4).map((name) => (
                                <span key={name} className="max-w-[140px] truncate rounded bg-ink-100 px-1.5 py-0.5 text-caption text-ink-700">{name}</span>
                              ))}
                              {entry.selected.length > 4 && <span className="text-caption text-ink-500">+{entry.selected.length - 4} more</span>}
                            </div>
                          </td>
                          <td className="px-3 py-2.5">
                            <p className="font-medium text-ink-900">{reason ? "—" : effectiveAppend(entry) ? "Auto-detect" : "Separate"}</p>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <Pagination {...filesPage} onPage={filesPage.setPage} noun="file" />
            </div>
            <p className="mt-3 flex items-center gap-2 text-caption text-ink-500">
              <FileText className="h-3.5 w-3.5 shrink-0" aria-hidden />
              Output: CSV · metadata · audit report
            </p>
          </div>
        </section>

        <aside className="flex">
          <section className="card flex w-full flex-col overflow-hidden" aria-labelledby="ready-title">
            <div className="border-b border-ink-200 bg-ink-50 p-5">
              <div className="flex items-start gap-3">
                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-ink-900 text-white" aria-hidden>
                  <Play className="h-5 w-5" fill="currentColor" />
                </div>
                <div>
                  <h2 id="ready-title" className="text-card text-ink-900">Ready</h2>
                  <p className="text-caption text-ink-600">Source files stay unchanged.</p>
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
                  loadingText="Starting…"
                  disabled={!flow.canRun}
                  onClick={start}
                >
                  {flow.batch ? "Run again" : count > 1 ? `Clean ${count} files` : "Start cleaning"}
                </Button>
                <Button variant="ghost" className="w-full sm:w-auto xl:w-full" icon={<ArrowLeft />} onClick={() => navigate("preview")}>
                  Back
                </Button>
              </div>
              {flow.runBlockedReason && <p className="text-center text-caption text-ink-500">{flow.runBlockedReason}</p>}
              <p className="flex items-center justify-center gap-1.5 text-caption text-ink-500">
                <Info className="h-3.5 w-3.5 shrink-0" aria-hidden />
                Runs in the background
              </p>
            </div>
          </section>
        </aside>
      </div>

      <section className="card p-5 md:p-6" aria-labelledby="steps-title">
        <h2 id="steps-title" className="label-caps">Pipeline</h2>
        <ol className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
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

/* -------------------------------------------------------------------------- */

function useElapsed(start: number | null, end: number | null, live: boolean): number | null {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [live]);
  if (!start) return null;
  return ((end ? end * 1000 : now) - start * 1000) / 1000;
}

function StageList({ job }: { job: JobStatus }) {
  const current = job.stage ? STAGES.findIndex((stage) => stage.id === job.stage) : -1;
  const finished = job.status === "succeeded";
  const failed = job.status === "failed";
  return (
    <ol className="space-y-0" aria-label={`Stages for ${job.source_name}`}>
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
                state === "failed" && "bg-danger-700 text-white",
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
              {state === "active" && job.stage === "clean" && <ActiveSheets job={job} />}
              {state === "done" && stage.id === "clean" && job.parallel_workers > 1 && (
                <p className="text-caption text-ink-500">{job.parallel_workers} workers</p>
              )}
              {state === "failed" && job.error?.sheet && <p className="text-caption text-danger-700">Stopped at {job.error.sheet}</p>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** Which sheets are being cleaned right now -- one, or several side by side. */
function ActiveSheets({ job }: { job: JobStatus }) {
  const active = job.active_sheets?.length ? job.active_sheets : job.current_sheet ? [job.current_sheet] : [];
  if (!active.length) return null;
  const shown = active.slice(0, 3);
  return (
    <div className="text-caption text-brand-700">
      <p>
        {shown.join(", ")}
        {active.length > shown.length && ` +${active.length - shown.length} more`}
      </p>
      <p className="text-ink-500">
        {job.sheets_done}/{job.sheets_total} sheets
        {job.parallel_workers > 1 && ` · ${job.parallel_workers} workers`}
      </p>
    </div>
  );
}

function JobStateIcon({ job }: { job: JobStatus }) {
  const base = "flex h-7 w-7 shrink-0 items-center justify-center rounded-full";
  if (job.status === "succeeded")
    return (
      <span className={clsx(base, "bg-emerald-600 text-white")} aria-hidden>
        <Check className="h-3.5 w-3.5" strokeWidth={3} />
      </span>
    );
  if (job.status === "failed")
    return (
      <span className={clsx(base, "bg-danger-700 text-white")} aria-hidden>
        <XCircle className="h-4 w-4" />
      </span>
    );
  if (job.status === "running")
    return (
      <span className={clsx(base, "bg-brand-600 text-white ring-4 ring-brand-100")} aria-hidden>
        <CircleDot className="h-3.5 w-3.5 animate-pulse" />
      </span>
    );
  if (job.status === "cancelled")
    return (
      <span className={clsx(base, "border border-ink-300 bg-ink-100 text-ink-500")} aria-hidden>
        <Ban className="h-3.5 w-3.5" />
      </span>
    );
  return (
    <span className={clsx(base, "border border-ink-300 bg-white text-ink-400")} aria-hidden>
      <Clock className="h-3.5 w-3.5" />
    </span>
  );
}

const STATE_LABEL: Record<JobStatus["status"], string> = {
  queued: "Waiting",
  running: "Cleaning",
  succeeded: "Cleaned",
  failed: "Failed",
  cancelled: "Cancelled",
};

function RunningView({ batch }: { batch: BatchStatus }) {
  const jobsPage = usePaged(batch.jobs, FILES_PER_PAGE);
  const flow = useWorkflow();
  const [confirm, setConfirm] = useState(false);
  const elapsed = useElapsed(batch.started_at, batch.finished_at, true);
  const current = batch.jobs.find((job) => job.id === batch.current_job_id) ?? batch.jobs.find(isActive) ?? batch.jobs[batch.jobs.length - 1];
  const runningFiles = batch.jobs.filter((job) => job.status === "running");
  const [shownId, setShownId] = useState<string | null>(null);
  const shown = batch.jobs.find((job) => job.id === shownId) ?? current;
  const multi = batch.files_total > 1;
  const cancelling = batch.jobs.some((job) => job.message.startsWith("Cancelling"));
  const percent = Math.round(batch.progress * 100);
  const position = current ? batch.jobs.indexOf(current) + 1 : 1;
  const stepIndex = current?.stage ? STAGES.findIndex((stage) => stage.id === current.stage) + 1 : 1;
  const sheetsDone = batch.jobs.reduce((sum, job) => sum + job.sheets_done, 0);
  const sheetsTotal = batch.jobs.reduce((sum, job) => sum + job.sheets_total, 0);
  const rowsKept = batch.jobs.reduce((sum, job) => sum + job.rows_kept, 0);
  const rowsRemoved = batch.jobs.reduce((sum, job) => sum + job.rows_removed, 0);

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
      <div className="min-w-0 space-y-6">
        <section className="card p-5 md:p-6" aria-labelledby="running-title" aria-busy="true">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0">
              <Badge tone="info" dot className="mb-2">{batch.status === "queued" ? "Queued" : "Running"}</Badge>
              <h2 id="running-title" className="truncate text-section text-ink-900">
                {runningFiles.length > 1 ? `Cleaning ${runningFiles.length} files` : current?.message ?? "Starting"}
              </h2>
              <p className="truncate text-body text-ink-600">
                {runningFiles.length > 1
                  ? `${batch.files_done}/${batch.files_total} done · ${runningFiles.map((job) => job.source_name).join(", ")}`
                  : `${multi ? `File ${position} of ${batch.files_total} · ` : ""}${
                      current?.status === "running" ? `Step ${stepIndex} of ${STAGES.length} · ` : ""
                    }${current?.source_name ?? ""}`}
              </p>
            </div>
            <Button variant="danger" icon={<Ban />} onClick={() => setConfirm(true)} disabled={cancelling}>
              {cancelling ? "Cancelling…" : multi ? "Cancel all" : "Cancel"}
            </Button>
          </div>

          <div className="mt-6" aria-live="polite">
            <div className="mb-2 flex items-baseline justify-between">
              <span className="label-caps">Progress</span>
              <span className="num text-[20px] font-semibold text-ink-900">{percent}%</span>
            </div>
            <ProgressBar
              value={batch.progress}
              label="Cleaning progress"
              active
              indeterminate={!multi && (current?.stage === "read" || current?.status === "queued")}
            />
            {current?.stage === "read" && current.status === "running" && (
              <p className="mt-1.5 text-caption text-ink-500">Opening {current.source_name}…</p>
            )}
          </div>

          <div className={clsx("mt-6 gap-3", multi ? "flex flex-wrap [&>*]:min-w-[150px] [&>*]:flex-1" : "grid grid-cols-2 lg:grid-cols-4")}>
            {multi && <StatTile icon={<Files />} label="Files" value={`${batch.files_done} / ${batch.files_total}`} tone="brand" />}
            <StatTile icon={<Layers />} label="Sheets" value={`${sheetsDone} / ${sheetsTotal}`} tone="info" />
            <StatTile icon={<Rows3 />} label="Rows kept" value={formatNumber(rowsKept)} tone="success" />
            <StatTile icon={<Trash2 />} label="Rows removed" value={formatNumber(rowsRemoved)} />
            <StatTile icon={<Clock />} label="Elapsed" value={formatDuration(elapsed)} />
          </div>

          <p className="mt-5 flex items-center gap-2 text-caption text-ink-500">
            <Info className="h-3.5 w-3.5" aria-hidden /> Safe to leave this page.
          </p>
        </section>

        {multi && (
          <section className="card" aria-labelledby="files-title">
            <h2 id="files-title" className="px-5 pt-5 text-card text-ink-900 md:px-6">Files</h2>
            <p className="px-5 text-caption text-ink-500 md:px-6">Select a file to follow its steps.</p>
            <ul className="mt-3 divide-y divide-ink-100 border-t border-ink-200">
              {jobsPage.slice.map((job) => (
                <li
                  key={job.id}
                  className={clsx("flex items-center gap-3 px-5 py-3 md:px-6", shown?.id === job.id && "bg-ink-50")}
                >
                  <JobStateIcon job={job} />
                  <button type="button" onClick={() => setShownId(job.id)} className="min-w-0 flex-1 rounded text-left focus-visible:outline-none focus-visible:shadow-focus">
                    <p className="truncate text-body font-medium text-ink-900" title={job.source_name}>{job.source_name}</p>
                    <p className="num text-caption text-ink-600">
                      {job.status === "running"
                        ? job.message
                        : job.status === "failed"
                          ? job.error?.message ?? "Failed"
                          : `${STATE_LABEL[job.status]} · ${plural(job.sheets_total, "sheet")}${job.status === "succeeded" ? ` · ${formatNumber(job.rows_kept)} rows kept` : ""}`}
                    </p>
                    {job.status === "running" && <ProgressBar className="mt-1.5" value={job.progress} label={`Progress for ${job.source_name}`} active />}
                  </button>
                  {job.status === "queued" ? (
                    <Button size="sm" variant="ghost" icon={<SkipForward />} onClick={() => flow.cancelJob(job.id)}>
                      Skip
                    </Button>
                  ) : (
                    <Badge
                      tone={job.status === "succeeded" ? "success" : job.status === "failed" ? "danger" : job.status === "running" ? "info" : "neutral"}
                      className="shrink-0"
                    >
                      {job.status === "running" ? `${Math.round(job.progress * 100)}%` : STATE_LABEL[job.status]}
                    </Badge>
                  )}
                </li>
              ))}
            </ul>
            <Pagination {...jobsPage} onPage={jobsPage.setPage} noun="file" />
          </section>
        )}
      </div>

      <aside className="card self-start p-5 md:p-6">
        <h2 className="text-card text-ink-900">Progress</h2>
        {multi && shown && <p className="mb-4 truncate text-caption text-ink-500" title={shown.source_name}>{shown.source_name}</p>}
        <div className={multi ? "" : "mt-4"}>{shown && <StageList job={shown} />}</div>
      </aside>

      <Modal
        open={confirm}
        onClose={() => setConfirm(false)}
        title={multi ? "Cancel remaining files?" : "Cancel cleaning?"}
        description={
          multi
            ? "Queued files are skipped. Finished files keep their results."
            : "Stops after the current sheet. No output is produced."
        }
        footer={
          <>
            <Button onClick={() => setConfirm(false)}>Keep running</Button>
            <Button
              variant="primary"
              icon={<Ban />}
              onClick={async () => {
                setConfirm(false);
                await flow.cancelBatch();
              }}
            >
              {multi ? "Cancel all" : "Cancel"}
            </Button>
          </>
        }
      />
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function DoneView({ batch }: { batch: BatchStatus }) {
  const outcomes = usePaged(batch.jobs, FILES_PER_PAGE);
  const flow = useWorkflow();
  const navigate = useNavigate();
  const { state: rerun, start } = useStart();
  const multi = batch.files_total > 1;
  const succeeded = batch.jobs.filter((job) => job.status === "succeeded");
  const loaded = succeeded.map((job) => flow.results[job.id]).filter(Boolean);
  const loading = loaded.length < succeeded.length && succeeded.every((job) => !flow.resultsErrors[job.id]);
  const sum = (pick: (summary: NonNullable<(typeof loaded)[number]>["summary"]) => number) =>
    loaded.reduce((total, results) => total + pick(results.summary), 0);
  const failedResults = succeeded.filter((job) => flow.resultsErrors[job.id]);

  // A single file whose append didn't fully work: say so right away, once.
  const [detailsFor, setDetailsFor] = useState<string | null>(null);
  const shownFor = useRef<string | null>(null);
  useEffect(() => {
    if (multi) return;
    const job = succeeded[0];
    const results = job && flow.results[job.id];
    if (results && appendIncomplete(results.append_check) && shownFor.current !== job.id) {
      shownFor.current = job.id;
      setDetailsFor(job.id);
    }
  }, [flow.results, multi, succeeded]);
  const detailsCheck = detailsFor ? flow.results[detailsFor]?.append_check : null;

  const review = (jobId?: string) => {
    if (jobId) flow.setResultsJobId(jobId);
    navigate("results");
  };

  const tone = batch.status === "succeeded" ? "success" : batch.status === "partial" ? "warning" : batch.status === "cancelled" ? "neutral" : "error";
  const headline =
    batch.status === "succeeded"
      ? "Completed"
      : batch.status === "partial"
        ? `${batch.files_succeeded} of ${plural(batch.files_total, "file")} cleaned`
        : batch.status === "cancelled"
          ? `Cancelled · ${batch.files_succeeded} of ${plural(batch.files_total, "file")} cleaned`
          : "Failed";
  const single = batch.jobs[0];

  return (
    <div className={clsx("grid items-start gap-6", multi && "xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]")}>
      <section className="card p-5 md:p-6" aria-labelledby="done-title">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center">
          {tone === "success" ? (
            <div className="flex h-14 w-14 shrink-0 animate-scale-in items-center justify-center rounded-full bg-emerald-600 text-white" aria-hidden>
              <svg viewBox="0 0 24 24" className="h-7 w-7" fill="none" stroke="currentColor" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round">
                <path d="M5 12.5l4.5 4.5L19 7.5" strokeDasharray="24" className="animate-draw-check" />
              </svg>
            </div>
          ) : (
            <div
              className={clsx(
                "flex h-14 w-14 shrink-0 items-center justify-center rounded-full text-white",
                tone === "warning" ? "bg-amber-500" : tone === "neutral" ? "bg-ink-500" : "bg-danger-600",
              )}
              aria-hidden
            >
              {tone === "warning" ? <TriangleAlert className="h-7 w-7" /> : tone === "neutral" ? <Ban className="h-7 w-7" /> : <XCircle className="h-7 w-7" />}
            </div>
          )}
          <div className="min-w-0 flex-1" role={tone === "warning" || tone === "error" ? "alert" : undefined}>
            <p
              className={clsx(
                "text-caption font-medium",
                tone === "success" ? "text-emerald-700" : tone === "warning" ? "text-amber-700" : tone === "neutral" ? "text-ink-600" : "text-danger-700",
              )}
            >
              {headline}
            </p>
            <h2 id="done-title" className="truncate text-section text-ink-900">
              {multi ? plural(batch.files_total, "file") : single.source_name}
            </h2>
            <p className="text-caption text-ink-500">
              {formatTimestamp(batch.finished_at)} · {formatDuration(batch.elapsed_seconds)}
              {batch.status === "partial" &&
                ` · ${plural(batch.files_total - batch.files_succeeded, "file")} ${batch.files_total - batch.files_succeeded === 1 ? "needs" : "need"} attention`}
              {batch.status === "cancelled" && batch.files_cancelled > 0 && ` · ${plural(batch.files_cancelled, "file")} not cleaned`}
            </p>
          </div>
        </div>

        {succeeded.length > 0 && (
          <div className={clsx("mt-6 gap-3", multi ? "flex flex-wrap [&>*]:min-w-[150px] [&>*]:flex-1" : "grid grid-cols-2 lg:grid-cols-4")}>
            {multi && <StatTile icon={<Files />} label="Files" value={`${batch.files_succeeded} / ${batch.files_total}`} tone={batch.files_succeeded === batch.files_total ? "success" : "warning"} />}
            <StatTile loading={loading} icon={<Layers />} label="Sheets" value={sum((s) => s.sheets_selected)} tone="info" />
            <StatTile
              loading={loading}
              icon={<Table2 />}
              label="Tables"
              value={sum((s) => s.outputs)}
              hint={sum((s) => s.appended_outputs) ? `${sum((s) => s.appended_outputs)} appended` : undefined}
              tone="brand"
            />
            <StatTile loading={loading} icon={<Rows3 />} label="Rows kept" value={formatNumber(sum((s) => s.rows))} tone="success" />
            <StatTile loading={loading} icon={<Trash2 />} label="Rows removed" value={formatNumber(sum((s) => s.rows_removed))} />
          </div>
        )}

        {!multi && single.status === "succeeded" && flow.results[single.id]?.append_check && flow.results[single.id].append_check!.status !== "single_table" && (
          <div className="mt-5">
            <AppendOutcomeAlert check={flow.results[single.id].append_check!} onDetails={() => setDetailsFor(single.id)} />
          </div>
        )}
        {!loading && sum((s) => s.consistency_issues) > 0 && (
          <Alert tone="error" className="mt-5" title={`${plural(sum((s) => s.consistency_issues), "consistency check")} failed`}>
            Review before loading the data.
          </Alert>
        )}
        {!loading && sum((s) => s.flagged_columns) > 0 && (
          <Alert tone="warning" className="mt-5" title={`${plural(sum((s) => s.flagged_columns), "column")} to review`} />
        )}
        {failedResults.length > 0 && (
          <Alert
            tone="error"
            className="mt-5"
            title="Results failed to load"
            action={<Button size="sm" onClick={() => failedResults.forEach((job) => void flow.reloadResults(job.id))}>Retry</Button>}
          >
            {failedResults.map((job) => job.source_name).join(", ")}
          </Alert>
        )}
        {!multi && single.status === "failed" && <FailureDetails job={single} className="mt-5" />}

        <div className="mt-6 flex flex-col gap-2 border-t border-ink-200 pt-5 sm:flex-row">
          <Button variant="primary" size="lg" iconRight={<ArrowRight />} onClick={() => review()} disabled={!flow.canReview}>
            Review results
          </Button>
          <Button size="lg" icon={<RefreshCw />} state={rerun} loadingText="Starting…" onClick={start} disabled={!flow.canRun}>
            {batch.status === "succeeded" ? "Run again" : "Retry"}
          </Button>
          <Button size="lg" variant="ghost" icon={<SlidersHorizontal />} onClick={() => navigate("configuration")}>
            Configure
          </Button>
        </div>
      </section>

      {multi && (
        <section className="card" aria-labelledby="outcomes-title">
          <div className="px-5 pt-5 md:px-6">
            <h2 id="outcomes-title" className="text-section text-ink-900">Files</h2>
            <p className="mt-0.5 text-caption text-ink-500">Open a file to review its tables.</p>
          </div>
          <ul className="mt-4 divide-y divide-ink-100 border-t border-ink-200">
            {outcomes.slice.map((job) => (
              <FileOutcome key={job.id} job={job} onReview={() => review(job.id)} onAppendDetails={() => setDetailsFor(job.id)} />
            ))}
          </ul>
          <Pagination {...outcomes} onPage={outcomes.setPage} noun="file" />
        </section>
      )}

      {detailsCheck && (
        <AppendMismatchModal
          open={Boolean(detailsFor) && appendIncomplete(detailsCheck)}
          check={detailsCheck}
          onClose={() => setDetailsFor(null)}
          onChangeConfiguration={() => {
            setDetailsFor(null);
            navigate("configuration");
          }}
        />
      )}
    </div>
  );
}

function FileOutcome({ job, onReview, onAppendDetails }: { job: JobStatus; onReview: () => void; onAppendDetails: () => void }) {
  const flow = useWorkflow();
  const results = flow.results[job.id];
  const summary = results?.summary;
  const [open, setOpen] = useState(false);
  const issues = summary?.consistency_issues ?? 0;

  return (
    <li className="px-5 py-4 md:px-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="flex min-w-0 flex-1 items-start gap-3">
          <JobStateIcon job={job} />
          <div className="min-w-0 flex-1">
            <p className="truncate text-body font-semibold text-ink-900" title={job.source_name}>{job.source_name}</p>
            {job.status === "succeeded" ? (
              <p className="num text-caption text-ink-600">
                {summary
                  ? `${plural(summary.sheets_selected, "sheet")} · ${plural(summary.outputs, "table")}${summary.appended_outputs ? ` (${summary.appended_outputs} appended)` : ""} · ${formatNumber(summary.rows)} rows kept · ${formatNumber(summary.rows_removed)} removed`
                  : "Loading results…"}
              </p>
            ) : job.status === "failed" ? (
              <p className="text-caption text-danger-700">{job.error?.message ?? "The job stopped unexpectedly."}</p>
            ) : (
              <p className="text-caption text-ink-500">{job.message || "Cancelled"}</p>
            )}
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {job.status === "succeeded" && results && appendIncomplete(results.append_check) && (
                <button type="button" onClick={onAppendDetails} className="rounded focus-visible:outline-none focus-visible:shadow-focus">
                  <Badge tone="warning" icon={<BetweenHorizontalEnd />}>Append partial</Badge>
                </button>
              )}
              {job.status === "succeeded" && results?.append_check?.status === "all_match" && (
                <Badge tone="success" icon={<BetweenHorizontalEnd />}>Appended</Badge>
              )}
              {issues > 0 && <Badge tone="danger">{plural(issues, "consistency issue")}</Badge>}
              {(summary?.flagged_columns ?? 0) > 0 && <Badge tone="warning">{plural(summary!.flagged_columns, "column")} to review</Badge>}
            </div>
          </div>
        </div>
        <div className="flex shrink-0 gap-2 pl-10 sm:pl-0">
          {job.status === "succeeded" && (
            <Button size="sm" iconRight={<ArrowRight />} onClick={onReview} disabled={!results}>
              Review
            </Button>
          )}
          {job.status === "failed" && (
            <Button size="sm" iconRight={<ChevronDown className={clsx("transition-transform", open && "rotate-180")} />} aria-expanded={open} onClick={() => setOpen(!open)}>
              {open ? "Hide" : "Details"}
            </Button>
          )}
        </div>
      </div>
      {open && job.status === "failed" && <FailureDetails job={job} className="mt-3 animate-fade-in sm:ml-10" />}
    </li>
  );
}

function FailureDetails({ job, className }: { job: JobStatus; className?: string }) {
  const [open, setOpen] = useState(false);
  const error = job.error;
  const stage = STAGES.find((item) => item.id === error?.stage);
  return (
    <div className={clsx("overflow-hidden rounded-lg border border-danger-200", className)}>
      <dl className="grid gap-4 bg-danger-50/40 p-4 sm:grid-cols-2">
        <div>
          <dt className="label-caps">Error</dt>
          <dd className="text-body font-medium text-ink-900">{error?.detail || error?.message || "An unexpected error"}</dd>
        </div>
        <div>
          <dt className="label-caps">Where</dt>
          <dd className="text-body font-medium text-ink-900">
            {stage ? stage.label : "Before processing"}
            {error?.sheet ? ` · ${error.sheet}` : ""}
          </dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="label-caps">Fix</dt>
          <dd className="text-body text-ink-900">{error?.advice ?? "Retry, or deselect the sheet above."}</dd>
        </div>
      </dl>
      {(error?.technical || error?.kind) && (
        <div className="border-t border-danger-100">
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
            className="flex w-full items-center justify-between px-4 py-2.5 text-body font-medium text-ink-700 hover:bg-ink-50"
          >
            Technical
            <ChevronDown className={clsx("h-4 w-4 transition-transform", open && "rotate-180")} aria-hidden />
          </button>
          {open && (
            <div className="animate-fade-in px-4 pb-4">
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
    </div>
  );
}
