import clsx from "clsx";
import {
  ArrowLeft,
  ArrowRight,
  BadgeCheck,
  Ban,
  CalendarRange,
  Check,
  CheckCircle2,
  CircleDashed,
  CircleDot,
  Clock,
  Columns3,
  DatabaseZap,
  FileSpreadsheet,
  Info,
  Loader2,
  RefreshCw,
  Rows3,
  ShieldAlert,
  Table2,
  TriangleAlert,
  Upload,
  XCircle,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, watchValidation } from "../api/client";
import type {
  DateRole,
  ProgressPhase,
  RequiredColumn,
  TaskProgress,
  Validation,
  ValidationChoice,
  ValidationFile,
  ValidationOutput,
  ValidationRun,
  Verdict,
} from "../api/types";
import { PageHeader } from "../components/layout/Layout";
import { Badge, type Tone } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Alert, EmptyState, ProgressBar, StatTile, useStableCallback, useToast } from "../components/ui/Feedback";
import { Tooltip } from "../components/ui/Overlay";
import { Select } from "../components/ui/Select";
import { formatDuration, formatNumber, plural } from "../lib/format";
import { useWidth } from "../lib/useWidth";
import { useNavigate, useWorkflow } from "../state/workflow";

const VERDICT: Record<Verdict, { label: string; tone: Tone; icon: JSX.Element }> = {
  ready: { label: "Ready", tone: "success", icon: <CheckCircle2 /> },
  needs_input: { label: "Needs input", tone: "warning", icon: <CircleDashed /> },
  flagged: { label: "Flagged", tone: "warning", icon: <TriangleAlert /> },
  rejected: { label: "Rejected", tone: "danger", icon: <Ban /> },
};

const ACTION_TONE: Record<string, Tone> = { INSERT: "brand", APPEND: "success", REJECTED: "danger", DECIDE: "warning" };

const METHOD_LABEL: Record<string, string> = {
  saved: "Saved", exact: "Exact", fuzzy: "Fuzzy", semantic: "Semantic", ai: "AI", reviewer: "Reviewer",
};

const ROLE_LABEL: Record<DateRole, string> = {
  AED: "Accounting effective date",
  PED: "Policy effective date",
  TED: "Transaction effective date",
};

const KEY = "exavalu.validation";
const toError = (error: unknown) => (error instanceof ApiError ? error : new ApiError(0, { code: "unknown", message: String(error) }));

const month = (iso: string | null) => (iso ? iso.slice(0, 7) : "");
const monthLabel = (iso: string | null) => {
  if (!iso) return "—";
  const [year, m] = iso.split("-").map(Number);
  return new Date(year, m - 1, 1).toLocaleDateString(undefined, { month: "short", year: "numeric" });
};
/** "Jan - Jun 2026", or "Dec 2025 - Feb 2026" across years, or one month. */
const monthSpan = (first: string, last: string | null) => {
  if (!last || first.slice(0, 7) === last.slice(0, 7)) return monthLabel(first);
  if (first.slice(0, 4) === last.slice(0, 4)) {
    const short = new Date(Number(first.slice(0, 4)), Number(first.slice(5, 7)) - 1, 1).toLocaleDateString(undefined, { month: "short" });
    return `${short} - ${monthLabel(last)}`;
  }
  return `${monthLabel(first)} - ${monthLabel(last)}`;
};
/** Lets a long identifier wrap where its words meet (CommissionPct, gross_commission_amount), not mid-word. */
const softBreaks = (text: string) =>
  text.split(/(?<=[a-z0-9])(?=[A-Z])|(?<=_)/).flatMap((part, index) => (index ? [<wbr key={index} />, part] : [part]));
const dayLabel = (iso: string | null) =>
  iso ? new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" }) : "—";

function readSaved(batchId: string): string | null {
  try {
    const saved = JSON.parse(sessionStorage.getItem(KEY) ?? "null");
    return saved?.batchId === batchId ? saved.id : null;
  } catch {
    return null;
  }
}

function save(batchId: string, id: string) {
  try {
    sessionStorage.setItem(KEY, JSON.stringify({ batchId, id }));
  } catch {
    /* storage unavailable */
  }
}

export function ValidatePage() {
  const flow = useWorkflow();
  const navigate = useNavigate();
  const toast = useToast();
  const batch = flow.batch;
  const jobIds = useMemo(() => (batch?.jobs ?? []).filter((job) => job.status === "succeeded").map((job) => job.id), [batch]);
  const [data, setData] = useState<Validation | null>(null);
  // While the server builds the review: its latest progress, and when it arrived.
  const [run, setRun] = useState<{ id: string; progress: TaskProgress; receivedAt: number } | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  // How the page follows a validation being built: its event stream, or polling when no
  // stream gets through. Stopped on leaving the page; the work carries on on the server.
  const following = useRef<{ id: string; stop: () => void } | null>(null);
  const losses = useRef(0);
  // Answers that arrive after the page is left are dropped. A start already on its way is
  // shared, never sent twice (React may run the page's effect twice).
  const mounted = useRef(false);
  const starting = useRef<Promise<ValidationRun> | null>(null);
  const stop = useStableCallback(() => {
    following.current?.stop();
    following.current = null;
  });
  const showProgress = (id: string, progress: TaskProgress) => setRun({ id, progress, receivedAt: Date.now() });

  const settle = useStableCallback((current: Validation | ValidationRun) => {
    setReconnecting(false);
    if (current.status === "running") {
      follow(current.id, current.progress);
      return;
    }
    stop();
    if (current.status === "ready") {
      setRun(null);
      setData(current);
    } else {
      showProgress(current.id, current.progress);
      setError(new ApiError(0, current.error ?? { code: "validation_failed", message: "The validation did not finish." }));
    }
  });

  /** Ask the server every second: for when an event stream can't get through. */
  const poll = useStableCallback((id: string) => {
    stop();
    let timer = 0;
    let stopped = false;
    const tick = async () => {
      try {
        const current = await api.getValidation(id);
        if (stopped || !mounted.current) return;
        if (current.status === "running") {
          showProgress(id, current.progress);
          timer = window.setTimeout(tick, 1000);
        } else settle(current);
      } catch (err) {
        if (!stopped && mounted.current) setError(toError(err));
      }
    };
    following.current = {
      id,
      stop: () => {
        stopped = true;
        window.clearTimeout(timer);
      },
    };
    void tick();
  });

  const follow = useStableCallback((id: string, initial?: TaskProgress) => {
    if (!mounted.current) return;
    if (initial) showProgress(id, initial);
    if (following.current?.id === id) return; // already listening to this one
    stop();
    following.current = {
      id,
      stop: watchValidation(id, {
        progress: (progress) => {
          losses.current = 0;
          setReconnecting(false);
          showProgress(id, progress);
        },
        done: (validation) => {
          following.current = null;
          settle(validation);
        },
        failed: (failure, progress) => {
          following.current = null;
          setReconnecting(false);
          showProgress(id, progress);
          setError(failure);
        },
        reconnecting: () => setReconnecting(true),
        lost: () => {
          following.current = null;
          losses.current += 1;
          // Twice refused: something on the way (a proxy) doesn't pass streams. Poll instead.
          if (losses.current >= 2) poll(id);
          else void resume(id);
        },
      }),
    };
  });

  /** Pick up a validation started earlier: its review, its progress, or afresh if the server forgot it. */
  const resume = useStableCallback(async (id: string) => {
    try {
      const current = await api.getValidation(id);
      if (mounted.current) settle(current);
    } catch (err) {
      if (!mounted.current) return;
      const failure = toError(err);
      if (failure.status === 404) void start();
      else setError(failure);
    }
  });

  const start = useStableCallback(async () => {
    if (!batch || !jobIds.length) return;
    if (!starting.current) {
      stop();
      losses.current = 0;
      setError(null);
      setData(null);
      setRun(null);
      setReconnecting(false);
      starting.current = api.startValidation(jobIds, batch.id);
      starting.current.then((started) => save(batch.id, started.id), () => undefined)
        .finally(() => (starting.current = null));
    }
    try {
      const started = await starting.current;
      follow(started.id, started.progress);
    } catch (err) {
      if (mounted.current) setError(toError(err));
    }
  });

  // Reuse this batch's validation while the server has it -- finished, or still running --
  // otherwise check the files afresh.
  useEffect(() => {
    if (!batch || !jobIds.length || flow.running) return;
    mounted.current = true;
    const saved = readSaved(batch.id);
    if (saved) void resume(saved);
    else void start();
    return () => {
      mounted.current = false;
      stop();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batch?.id, jobIds.join(","), flow.running]);

  // The stepper's tick: every table staged.
  useEffect(() => {
    if (data && batch) flow.setValidation({ batchId: batch.id, id: data.id, staged: data.staged, total: data.outputs.length });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data?.id, data?.staged, data?.outputs.length, batch?.id]);

  const mutate = async (call: () => Promise<Validation>, success?: string) => {
    setBusy(true);
    try {
      setData(await call());
      if (success) toast({ severity: "success", title: success });
    } catch (err) {
      toast({ severity: "error", title: toError(err).body.message, description: toError(err).body.advice ?? undefined });
    } finally {
      setBusy(false);
    }
  };

  const header = (
    <PageHeader
      page="validate"
      title="Validate"
      description="Check each cleaned file is fit for Bronze, then stage it."
      actions={
        data && data.staged > 0 && (
          <Button variant="primary" iconRight={<ArrowRight />} onClick={() => navigate("ingest")}>
            Continue to Ingest
          </Button>
        )
      }
    />
  );

  let body;
  if (flow.running) {
    body = <div className="card"><EmptyState icon={<Loader2 className="animate-spin" />} title="Cleaning in progress" description="Validate when cleaning finishes." /></div>;
  } else if (!batch || !jobIds.length) {
    body = (
      <div className="card">
        <EmptyState icon={<Upload />} title="Nothing to validate" description="Clean a workbook first."
          action={<Button variant="primary" icon={<ArrowLeft />} onClick={() => navigate(flow.files.length ? "run" : "configuration")}>{flow.files.length ? "Run" : "Configure"}</Button>} />
      </div>
    );
  } else if (error && !run) {
    body = (
      <Alert tone="error" title={error.body.message} action={<Button size="sm" icon={<RefreshCw />} onClick={start}>Retry</Button>}>
        {error.body.advice}
      </Alert>
    );
  } else if (!data) {
    body = (
      <ValidationProgress
        progress={run?.progress ?? null}
        receivedAt={run?.receivedAt ?? null}
        reconnecting={reconnecting}
        error={error}
        files={jobIds.length}
        // Lost touch with a validation still running on the server: pick it up again.
        onRetry={run && error?.body.code === "network" ? () => void resume(run.id) : start}
      />
    );
  } else {
    const output = data.outputs.find((item) => item.key === selected) ?? data.outputs[0];
    body = (
      <ValidationView
        data={data}
        output={output}
        busy={busy}
        onSelect={setSelected}
        onFile={(jobId, change) => mutate(() => api.updateValidationFile(data.id, jobId, change))}
        onOutput={(key, change) => mutate(() => api.updateValidationOutput(data.id, key, change))}
        onStage={(keys) => mutate(() => api.stageValidation(data.id, keys), keys && keys.length === 1 ? "Staged" : "Files staged")}
        onRestart={start}
      />
    );
  }
  return (
    <>
      {header}
      {body}
    </>
  );
}

/* ---- progress while the review is built ---------------------------------- */

const COUNTERS = [
  { key: "tables_matched", label: "Tables matched", icon: <Table2 /> },
  { key: "columns_matched", label: "Columns matched", icon: <Columns3 /> },
  { key: "rows_checked", label: "Rows checked", icon: <Rows3 /> },
] as const;

/** Seconds since the server said ``seconds`` (at ``receivedAt``), ticking while live; never backwards. */
function useTicking(seconds: number | null, receivedAt: number | null, live: boolean): number | null {
  const [now, setNow] = useState(Date.now());
  const shown = useRef(0);
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [live]);
  if (seconds === null || receivedAt === null) return null;
  const value = seconds + (live ? Math.max(0, now - receivedAt) / 1000 : 0);
  shown.current = live ? Math.max(shown.current, value) : value;
  return shown.current;
}

function ValidationProgress({
  progress,
  receivedAt,
  reconnecting,
  error,
  files,
  onRetry,
}: {
  progress: TaskProgress | null;
  receivedAt: number | null;
  reconnecting: boolean;
  error: ApiError | null;
  files: number;
  onRetry: () => void;
}) {
  const failed = progress?.status === "failed" || Boolean(error);
  const live = !failed && progress?.status !== "succeeded";
  const elapsed = useTicking(progress?.elapsed ?? null, receivedAt, live && Boolean(progress));
  const current = progress?.phases.find((phase) => phase.state === "active" || phase.state === "failed");
  const percent = progress?.percent ?? 0;
  const title = failed ? "Validation stopped" : progress?.label ?? "Starting the validation";
  const where = progress ? `Step ${progress.step} of ${progress.steps}` : "Sending the files to the server";
  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
      <section className="card p-5 md:p-6" aria-labelledby="validating-title" aria-busy={live}>
        <div className="min-w-0">
          <Badge tone={failed ? "danger" : reconnecting ? "warning" : "info"} dot className="mb-2">
            {failed ? "Stopped" : reconnecting ? "Reconnecting" : "Validating"}
          </Badge>
          <h2 id="validating-title" className="truncate text-section text-ink-900">{title}</h2>
          <p className="truncate text-body text-ink-600">
            {where} · {plural(files, "file")}
            {progress?.counters.tables_matched ? ` · ${plural(progress.counters.tables_matched.total, "table")}` : ""}
          </p>
          {/* Read out when the step changes, not on every tick of the bar. */}
          <span className="sr-only" aria-live="polite">{failed ? title : `${where}: ${progress?.label ?? title}`}</span>
        </div>

        <div className="mt-6">
          <div className="mb-2 flex items-baseline justify-between">
            <span className="label-caps">Progress</span>
            <span className="num text-[20px] font-semibold text-ink-900">{percent}%</span>
          </div>
          <ProgressBar value={progress?.fraction ?? 0} label="Validation progress" active={live} indeterminate={!progress}
            tone={failed ? "danger" : "brand"} />
          <p className="mt-1.5 truncate text-caption text-ink-500" title={current?.detail ?? undefined}>
            {reconnecting ? "Connection lost; reconnecting. The validation carries on meanwhile."
              : current?.detail ?? (progress ? " " : "Checking the files can be validated…")}
          </p>
        </div>

        <div className="mt-6 grid grid-cols-2 gap-3 2xl:grid-cols-4">
          {COUNTERS.map((counter) => {
            const value = progress?.counters[counter.key];
            return (
              <StatTile key={counter.key} icon={counter.icon} label={counter.label} tone="info" loading={!value}
                value={value ? formatNumber(value.done) : "—"} hint={value ? `of ${formatNumber(value.total)}` : undefined} />
            );
          })}
          <StatTile icon={<Clock />} label="Elapsed" value={formatDuration(elapsed)} />
        </div>

        {failed && error && (
          <Alert tone="error" className="mt-5" title={error.body.message}
            action={<Button size="sm" icon={<RefreshCw />} onClick={onRetry}>Validate again</Button>}>
            {error.body.advice}
            {error.body.detail && <span className="mt-1 block break-words font-mono text-caption">{error.body.detail}</span>}
          </Alert>
        )}
        {!failed && (
          <p className="mt-5 flex items-center gap-2 text-caption text-ink-500">
            <Info className="h-3.5 w-3.5" aria-hidden /> Safe to leave this page: the validation carries on, and picks up here when you return.
          </p>
        )}
      </section>

      <aside className="card p-5 md:p-6 xl:self-start" aria-labelledby="validating-steps">
        <h2 id="validating-steps" className="label-caps">Steps</h2>
        <ProgressSteps phases={progress?.phases ?? []} receivedAt={receivedAt} live={live} />
      </aside>
    </div>
  );
}

function ProgressSteps({ phases, receivedAt, live }: { phases: ProgressPhase[]; receivedAt: number | null; live: boolean }) {
  if (!phases.length) {
    return <p className="mt-4 flex items-center gap-2 text-body text-ink-600"><Loader2 className="h-4 w-4 animate-spin" aria-hidden />Starting…</p>;
  }
  return (
    <ol className="mt-4" aria-label="Validation steps">
      {phases.map((phase, index) => (
        <ProgressStep key={phase.id} phase={phase} number={index + 1} last={index === phases.length - 1}
          receivedAt={receivedAt} live={live} />
      ))}
    </ol>
  );
}

function ProgressStep({ phase, number, last, receivedAt, live }: {
  phase: ProgressPhase;
  number: number;
  last: boolean;
  receivedAt: number | null;
  live: boolean;
}) {
  const active = phase.state === "active";
  const seconds = useTicking(phase.seconds, receivedAt, live && active);
  const stateText = { done: "complete", active: "in progress", failed: "failed", pending: "pending" }[phase.state];
  return (
    <li className="relative flex gap-3 pb-5 last:pb-0">
      {!last && (
        <span className={clsx("absolute left-[13px] top-7 h-[calc(100%-24px)] w-px", phase.state === "done" ? "bg-emerald-400" : "bg-ink-200")} aria-hidden />
      )}
      <span
        className={clsx(
          "relative z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full transition-colors duration-300",
          phase.state === "done" && "bg-emerald-600 text-white",
          active && "bg-brand-600 text-white ring-4 ring-brand-100",
          phase.state === "failed" && "bg-danger-700 text-white",
          phase.state === "pending" && "border border-ink-300 bg-white text-ink-400",
        )}
        aria-hidden
      >
        {phase.state === "done" ? <Check className="h-3.5 w-3.5" strokeWidth={3} />
          : phase.state === "failed" ? <XCircle className="h-4 w-4" />
            : active ? <CircleDot className="h-3.5 w-3.5 animate-pulse" />
              : <span className="num text-caption">{number}</span>}
      </span>
      <div className="min-w-0 flex-1 pt-0.5">
        <p className={clsx("flex items-baseline justify-between gap-2 text-body", phase.state === "pending" ? "text-ink-500" : "font-medium text-ink-900")}>
          <span className="min-w-0">{phase.label}<span className="sr-only">{`: ${stateText}`}</span></span>
          {seconds !== null && phase.state !== "pending" && (
            <span className="num shrink-0 text-caption font-normal text-ink-500">{formatDuration(seconds)}</span>
          )}
        </p>
        {active && (
          <>
            {phase.detail && <p className="mt-0.5 break-words text-caption text-brand-700">{phase.detail}</p>}
            {phase.total > 1 && (
              <ProgressBar className="mt-1.5 !h-1" value={phase.fraction} label={`${phase.label}: progress`} />
            )}
          </>
        )}
        {phase.state === "failed" && <p className="mt-0.5 break-words text-caption text-danger-700">Stopped here{phase.detail ? `: ${phase.detail}` : ""}</p>}
      </div>
    </li>
  );
}

/* -------------------------------------------------------------------------- */

type OutputChange = Parameters<typeof api.updateValidationOutput>[2];
type FileChange = Parameters<typeof api.updateValidationFile>[2];

const VERDICT_ORDER: Verdict[] = ["ready", "needs_input", "flagged", "rejected"];
const TONE_TEXT: Partial<Record<Tone, string>> = { success: "text-emerald-600", warning: "text-amber-600", danger: "text-danger-600" };

function ValidationView({
  data,
  output,
  busy,
  onSelect,
  onFile,
  onOutput,
  onStage,
  onRestart,
}: {
  data: Validation;
  output: ValidationOutput;
  busy: boolean;
  onSelect: (key: string) => void;
  onFile: (jobId: string, change: FileChange) => void;
  onOutput: (key: string, change: OutputChange) => void;
  onStage: (keys: string[] | null) => void;
  onRestart: () => void;
}) {
  const file = data.files.find((item) => item.job_id === output.job_id)!;
  const open = data.outputs.filter((item) => !item.staged);
  const stageable = open.filter((item) => item.verdict !== "needs_input");
  // A file is staged whole: staging one of its sheets stages them all.
  const sheets = data.outputs.filter((item) => item.job_id === output.job_id).length;
  const [bodyRef, bodyWidth] = useWidth();

  return (
    <div className="space-y-5">
      {/* The batch at a glance, and what can be done with all of it at once. */}
      <section className="card flex flex-col gap-3 px-4 py-3 md:px-5 xl:flex-row xl:items-center xl:justify-between" aria-label="All tables">
        <div className="min-w-0">
          <dl className="flex flex-wrap items-center gap-x-5 gap-y-1.5">
            {VERDICT_ORDER.map((verdict) => {
              const meta = VERDICT[verdict];
              const value = data.counts[verdict];
              return (
                <div key={verdict} className="flex items-center gap-1.5">
                  <span className={clsx("flex [&>svg]:h-4 [&>svg]:w-4", value ? TONE_TEXT[meta.tone] : "text-ink-300")} aria-hidden>{meta.icon}</span>
                  <dt className="text-caption text-ink-500">{meta.label}</dt>
                  <dd className={clsx("num text-body font-semibold", value ? "text-ink-900" : "text-ink-400")}>{value}</dd>
                </div>
              );
            })}
            {data.staged > 0 && (
              <div className="flex items-center gap-1.5">
                <BadgeCheck className="h-4 w-4 text-ink-500" aria-hidden />
                <dt className="text-caption text-ink-500">Staged</dt>
                <dd className="num text-body font-semibold text-ink-900">{data.staged} of {data.outputs.length}</dd>
              </div>
            )}
          </dl>
          <p className="mt-1 text-caption text-ink-500">
            Ready tables go to staging; flagged and rejected ones are recorded as REJECTED. One sheet rejected rejects its whole file.
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <Button variant="ghost" size="sm" icon={<RefreshCw />} onClick={onRestart} disabled={busy}>Validate again</Button>
          <Button variant="primary" icon={<BadgeCheck />} disabled={busy || !stageable.length} onClick={() => onStage(stageable.map((item) => item.key))}>
            {stageable.length ? `Stage ${plural(stageable.length, "table")}` : open.length ? "Fix the tables first" : "All staged"}
          </Button>
        </div>
      </section>

      {/* Which table is open below: one switcher, every table's state on it. */}
      {data.outputs.length > 1 && (
        <nav aria-label="Tables" className="grid gap-2 [grid-template-columns:repeat(auto-fill,minmax(250px,1fr))]">
          {data.outputs.map((item) => {
            const owner = data.files.find((candidate) => candidate.job_id === item.job_id);
            const verdict = VERDICT[item.verdict];
            const current = item.key === output.key;
            return (
              <button
                key={item.key}
                type="button"
                onClick={() => onSelect(item.key)}
                aria-current={current ? "true" : undefined}
                className={clsx(
                  "flex min-w-0 items-center gap-2.5 rounded-lg border bg-white px-3 py-2 text-left transition-[border-color,box-shadow] duration-200 hover:border-ink-300 focus-visible:outline-none focus-visible:shadow-focus active:translate-y-px",
                  current ? "border-brand-500 ring-2 ring-brand-100 hover:border-brand-500" : "border-ink-200",
                )}
              >
                <span className={clsx("flex shrink-0 [&>svg]:h-4 [&>svg]:w-4", item.staged ? "text-ink-400" : TONE_TEXT[verdict.tone])} aria-hidden>
                  {item.staged ? <BadgeCheck /> : verdict.icon}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="line-clamp-2 text-body font-medium leading-5 text-ink-900 [overflow-wrap:anywhere]" title={owner?.file_name}>{owner?.file_name}</span>
                  <span className="block truncate text-caption text-ink-500">
                    {item.sheet_name ?? item.name} · {item.staged ? `Staged, control ${item.staged.control_id} (${item.staged.processing_action})` : verdict.label}
                  </span>
                </span>
                <span className={clsx("num shrink-0 text-caption font-semibold", item.fitness === 100 ? "text-emerald-700" : "text-ink-600")}>{item.fitness}%</span>
              </button>
            );
          })}
        </nav>
      )}

      <section className="card" aria-labelledby="validate-title">
        <div className="flex min-w-0 items-start gap-3 border-b border-ink-100 px-5 py-4 md:px-6">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-ink-200 bg-ink-50 text-ink-600" aria-hidden>
            <FileSpreadsheet className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <p className="label-caps">
              {output.sheet_name ? `${output.sheet_names.length > 1 ? "Sheets" : "Sheet"} ${output.sheet_name}` : output.name} · {formatNumber(output.rows)} rows
            </p>
            <h2 id="validate-title" className="truncate text-section text-ink-900" title={file.file_name}>{file.file_name}</h2>
          </div>
        </div>

        <Fitness output={output} />

        <div ref={bodyRef} className="space-y-6 p-5 md:p-6">
          {output.staged && (
            <Alert tone={output.staged.processing_action === "REJECTED" ? "warning" : "success"}
              title={`Staged as control row ${output.staged.control_id}: ${output.staged.processing_action}`}>
              {output.staged.seeded ? "The control table already listed this file; its row was filled in. " : ""}
              {output.staged.processing_action === "REJECTED" ? "It is recorded, and never loaded into Bronze." : `Its rows are in staging.${output.staged.staging_table}, ready for Ingest.`}
            </Alert>
          )}
          <FileDetails file={file} busy={busy || Boolean(output.staged)} onChange={(change) => onFile(file.job_id, change)} />
          <RequiredColumns output={output} busy={busy || Boolean(output.staged)} onChange={(mapping) => onOutput(output.key, { mapping })} />
          <div className={clsx("grid gap-6", bodyWidth >= SPLIT_DATES_AT ? "grid-cols-[minmax(0,2fr)_minmax(0,1fr)]" : "grid-cols-[minmax(0,1fr)]")}>
            <ReportingDates output={output} busy={busy || Boolean(output.staged)} onChange={(change) => onOutput(output.key, change)} />
            <AgainstBronze output={output} busy={busy || Boolean(output.staged)} onChoose={(choice) => onOutput(output.key, { choice })} />
          </div>
        </div>

        {!output.staged && (
          <div className="flex flex-col-reverse gap-2 border-t border-ink-100 px-5 py-4 sm:flex-row sm:items-center sm:justify-between md:px-6">
            <p className="text-caption text-ink-500">
              {output.verdict === "needs_input" ? output.needs[0] : output.verdict === "ready"
                ? `Staging records control action ${output.action} and copies ${formatNumber(output.rows)} rows to staging.`
                : "Staging records the file as REJECTED in the control table; nothing reaches Bronze."}
              {sheets > 1 && output.verdict !== "needs_input" && ` The file's other ${plural(sheets - 1, "sheet")} ${sheets > 2 ? "are" : "is"} staged with it.`}
            </p>
            <Button
              variant={output.verdict === "ready" ? "primary" : "secondary"}
              icon={output.verdict === "ready" ? <BadgeCheck /> : <Ban />}
              disabled={busy || output.verdict === "needs_input"}
              onClick={() => onStage([output.key])}
              className="shrink-0"
            >
              {output.verdict === "ready" ? (sheets > 1 ? `Stage the file (${sheets} sheets)` : "Stage")
                : sheets > 1 ? "Record the file as rejected" : "Record as rejected"}
            </Button>
          </div>
        )}
      </section>

      {data.notes.length > 0 && (
        <p className="flex items-start gap-1.5 text-caption text-ink-500">
          <Info className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>{data.notes.join(" ")}</span>
        </p>
      )}
    </div>
  );
}


/* ---- fitness ------------------------------------------------------------- */

function Fitness({ output }: { output: ValidationOutput }) {
  const verdict = VERDICT[output.verdict];
  const passed = output.checks.filter((check) => check.ok).length;
  return (
    <div className="border-b border-ink-100 bg-ink-50/60 px-5 py-4 md:px-6">
      <div className="flex flex-wrap items-center gap-3">
        <Badge tone={verdict.tone} icon={verdict.icon}>{verdict.label}</Badge>
        <span className="num text-body font-semibold text-ink-900">{output.fitness}% fit for Bronze</span>
        <span className="num text-caption text-ink-500">{passed} of {output.checks.length} checks passed</span>
      </div>
      <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-ink-100" role="meter" aria-label="Fitness for Bronze"
        aria-valuemin={0} aria-valuemax={100} aria-valuenow={output.fitness}>
        <div className={clsx("h-full rounded-full transition-[width]", output.fitness === 100 ? "bg-emerald-500" : output.verdict === "rejected" ? "bg-danger-500" : "bg-amber-500")}
          style={{ width: `${output.fitness}%` }} />
      </div>
      <ul className="mt-3 grid gap-x-4 gap-y-1.5 sm:grid-cols-2 xl:grid-cols-3">
        {output.checks.map((check) => (
          <li key={check.id} className="flex min-w-0 items-start gap-1.5 text-caption">
            {check.ok ? <CheckCircle2 className="mt-px h-3.5 w-3.5 shrink-0 text-emerald-600" aria-label="Passed" />
              : <CircleDashed className="mt-px h-3.5 w-3.5 shrink-0 text-amber-600" aria-label="Not passed" />}
            <span className="min-w-0"><span className="font-medium text-ink-800">{check.label}</span> <span className="text-ink-500">· {check.detail}</span></span>
          </li>
        ))}
      </ul>
      {output.needs.length > 0 && !output.staged && (
        <ul className="mt-3 space-y-0.5">
          {output.needs.map((need) => (
            <li key={need} className="flex items-start gap-1.5 text-caption font-medium text-amber-800"><TriangleAlert className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />{need}</li>
          ))}
        </ul>
      )}
      {output.warnings.map((warning) => (
        <p key={warning} className="mt-1.5 flex items-start gap-1.5 text-caption text-amber-800"><Info className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />{warning}</p>
      ))}
    </div>
  );
}

/* ---- file details --------------------------------------------------------- */

const input = "h-9 w-full rounded border bg-white px-2.5 text-body outline-none focus-visible:shadow-focus disabled:bg-ink-50 disabled:text-ink-500";

function FileDetails({ file, busy, onChange }: { file: ValidationFile; busy: boolean; onChange: (change: FileChange) => void }) {
  const [pc, setPc] = useState(file.pc_id ?? "");
  const [received, setReceived] = useState(file.file_received_date ?? "");
  useEffect(() => setPc(file.pc_id ?? ""), [file.pc_id]);
  useEffect(() => setReceived(file.file_received_date ?? ""), [file.file_received_date]);
  const divisionNeeded = file.division_matches.length > 1 && !file.division_name;
  return (
    <section aria-labelledby={`file-${file.job_id}`}>
      <h3 id={`file-${file.job_id}`} className="mb-3 flex items-center gap-2 text-card text-ink-900"><FileSpreadsheet className="h-4 w-4 text-ink-500" aria-hidden />File</h3>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <label className="block">
          <span className="mb-1 block text-caption font-medium text-ink-600">Profit center</span>
          <input value={pc} disabled={busy} placeholder="PC0796" onChange={(event) => setPc(event.target.value)}
            onBlur={() => pc !== (file.pc_id ?? "") && onChange({ pc_id: pc || null })}
            onKeyDown={(event) => event.key === "Enter" && (event.target as HTMLInputElement).blur()}
            className={clsx(input, "font-mono", file.pc_id ? "border-ink-200" : "border-amber-400")} />
        </label>
        <div>
          <span className="mb-1 block text-caption font-medium text-ink-600">Source system</span>
          <p className="flex h-9 items-center rounded border border-ink-100 bg-ink-50 px-2.5 font-mono text-body text-ink-800">{file.source_system ?? "—"}</p>
        </div>
        <label className="block">
          <span className="mb-1 block text-caption font-medium text-ink-600">File received date</span>
          <input type="date" value={received} disabled={busy} onChange={(event) => setReceived(event.target.value)}
            onBlur={() => received !== (file.file_received_date ?? "") && onChange({ file_received_date: received || null })}
            className={clsx(input, "num", file.file_received_date ? "border-ink-200" : "border-amber-400")} />
        </label>
        <label className="block">
          <span className="mb-1 block text-caption font-medium text-ink-600">Division</span>
          {file.division_matches.length === 1 ? (
            <p className="flex h-9 items-center truncate rounded border border-ink-100 bg-ink-50 px-2.5 text-body text-ink-800" title="From division_mapping">{file.division_name}</p>
          ) : (
            <select value={file.division_name ?? ""} disabled={busy} onChange={(event) => onChange({ division_name: event.target.value || null })}
              className={clsx(input, "pr-1", divisionNeeded ? "border-amber-400" : "border-ink-200")}>
              <option value="">{file.division_matches.length > 1 ? "Choose…" : "— not listed"}</option>
              {file.division_options.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
          )}
        </label>
      </div>
      <div className="mt-2 space-y-0.5">
        {file.detected_received_date && (
          <p className="text-caption text-ink-500">From the file name: {dayLabel(file.detected_received_date)}{file.received_note ? ` · ${file.received_note}` : ""}</p>
        )}
        {!file.detected_received_date && <p className="text-caption text-ink-500">The file name gives no date: enter when the file was received.</p>}
        {file.warnings.map((warning) => (
          <p key={warning} className="flex items-start gap-1.5 text-caption text-amber-800"><TriangleAlert className="mt-px h-3 w-3 shrink-0" aria-hidden />{warning}</p>
        ))}
      </div>
    </section>
  );
}

/* ---- required columns ------------------------------------------------------ */

/** The width the required columns need before they go into two side-by-side panels. */
const SPLIT_REQUIRED_AT = 940;
/** Narrower than this, each required column is a stacked block rather than a table row. */
const STACK_REQUIRED_BELOW = 560;
/** The width the card body needs before the reporting dates and Against Bronze sit side by side. */
const SPLIT_DATES_AT = 1040;

/** Two halves of about the same length, for side-by-side panels; a group of alternatives is never split. */
function halves(groups: RequiredColumn[][]): [RequiredColumn[][], RequiredColumn[][]] {
  const total = groups.reduce((count, group) => count + group.length, 0);
  let rows = 0;
  let split = 0;
  while (split < groups.length && rows < total / 2) rows += groups[split++].length;
  return [groups.slice(0, split), groups.slice(split)];
}

function RequiredColumns({ output, busy, onChange }: { output: ValidationOutput; busy: boolean; onChange: (mapping: Record<string, string | null>) => void }) {
  const columns = useMemo(() => new Map(output.columns.map((column) => [column.original, column])), [output.columns]);
  const byName = useMemo(() => new Map(output.required.map((item) => [item.name, item])), [output.required]);
  const present = (item: RequiredColumn) => Boolean(item.column && !columns.get(item.column)?.excluded);
  // Each requirement once: a column on its own, or a group of alternatives any one of which will do.
  const groups: RequiredColumn[][] = [];
  const grouped = new Set<string>();
  for (const item of output.required) {
    if (grouped.has(item.name)) continue;
    const group = [item, ...item.one_of.map((name) => byName.get(name)).filter((other): other is RequiredColumn => Boolean(other))];
    group.forEach((member) => grouped.add(member.name));
    groups.push(group);
  }
  const met = groups.filter((group) => group.some(present)).length;
  const MISSING = "__missing__";
  // Two panels side by side when each half has room for its selects; one table otherwise.
  const [ref, width] = useWidth<HTMLElement>();
  const panels = (width >= SPLIT_REQUIRED_AT ? halves(groups) : [groups]).filter((panel) => panel.length);
  const stacked = width > 0 && width < STACK_REQUIRED_BELOW;
  const rows = (panel: RequiredColumn[][]) =>
    panel.map((group) =>
      group.map((item, position) => {
        const alternatives = item.one_of.map((name) => byName.get(name)).filter((other): other is RequiredColumn => Boolean(other));
        return (
          <RequiredRow key={item.name} item={item} output={output} columns={columns} busy={busy} missingValue={MISSING}
            alternatives={alternatives} standIn={alternatives.find(present) ?? null} stacked={stacked}
            pair={group.length > 1 ? (position === 0 ? "first" : position === group.length - 1 ? "last" : "middle") : null}
            onChange={(column) => onChange({ [item.name]: column === MISSING ? null : column })} />
        );
      }),
    );
  return (
    <section ref={ref} aria-labelledby={`required-${output.key}`}>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h3 id={`required-${output.key}`} className="flex items-center gap-2 text-card text-ink-900"><Columns3 className="h-4 w-4 text-ink-500" aria-hidden />Required columns</h3>
        <span className="num text-caption text-ink-500">
          {met} of {groups.length} found. Each is needed for Bronze{groups.length < output.required.length ? "; of an “or” pair, one is enough" : ""}.
        </span>
      </div>
      {stacked ? (
        <ul className="overflow-hidden rounded-lg border border-ink-200" aria-label="Required columns and the file columns found for them">
          {rows(groups)}
        </ul>
      ) : (
      <div className={clsx("grid gap-3", panels.length > 1 ? "grid-cols-2" : "grid-cols-[minmax(0,1fr)]")}>
        {panels.map((panel, index) => (
          <div key={index} className="overflow-hidden rounded-lg border border-ink-200">
            <table className="w-full table-fixed border-collapse text-table">
              <caption className="sr-only">
                Required columns{panels.length > 1 ? `, part ${index + 1} of ${panels.length}` : ""}, and the file columns found for them
              </caption>
              <colgroup>
                <col className="w-[37%]" />
                <col />
                <col className="w-[116px]" />
              </colgroup>
              <thead className="bg-ink-50">
                <tr className="border-b border-ink-200 text-left text-caption font-semibold text-ink-600">
                  <th scope="col" className="px-3 py-2">Required column</th>
                  <th scope="col" className="px-2 py-2">File column</th>
                  <th scope="col" className="px-3 py-2">Match</th>
                </tr>
              </thead>
              <tbody>{rows(panel)}</tbody>
            </table>
          </div>
        ))}
      </div>
      )}
      <p className="mt-2 text-caption text-ink-500">
        The bronze column mapping votes first (seeded from the Silver DRT mapping), then exact, fuzzy, semantic and AI matches.
        What you stage is added to the mapping for this profit center.
      </p>
    </section>
  );
}

function RequiredRow({
  item,
  output,
  columns,
  busy,
  missingValue,
  alternatives,
  standIn,
  pair,
  stacked,
  onChange,
}: {
  item: RequiredColumn;
  output: ValidationOutput;
  columns: Map<string, ValidationOutput["columns"][number]>;
  busy: boolean;
  missingValue: string;
  /** The required columns that can stand in for this one. */
  alternatives: RequiredColumn[];
  /** The alternative found in the file, if any: this column is then not needed. */
  standIn: RequiredColumn | null;
  /** Where the row sits in a group of alternatives, which is drawn as one block. */
  pair: "first" | "middle" | "last" | null;
  /** Too narrow for a table: label and match above, the file column below. */
  stacked: boolean;
  onChange: (column: string) => void;
}) {
  const column = item.column ? columns.get(item.column) : null;
  const leftOut = Boolean(column?.excluded);
  const proposed = new Map(item.options.map((option) => [option.column, option]));
  const choices = [
    { value: missingValue, label: "Missing", description: "No column of this file holds it" },
    ...[...output.columns]
      .sort((a, b) => (proposed.get(b.original)?.score ?? -1) - (proposed.get(a.original)?.score ?? -1))
      .map((candidate) => {
        const option = proposed.get(candidate.original);
        return {
          value: candidate.original,
          label: candidate.header,
          description: [candidate.current !== candidate.header ? candidate.current : null, candidate.dtype,
            option ? `${option.methods.map((m) => METHOD_LABEL[m]).join(" + ")} ${Math.round(option.score * 100)}%` : null,
            candidate.excluded ? "left out on Results" : null].filter(Boolean).join(" · "),
          meta: option ? <Badge tone="info">Proposed</Badge> : undefined,
        };
      }),
  ];
  const vote = item.vote;
  const status = (!item.column || leftOut) && standIn ? (
    <Tooltip content={`${standIn.label} is found: one of the pair is enough.`}>
      <span tabIndex={0} className="inline-flex"><Badge tone="neutral">Not needed</Badge></span>
    </Tooltip>
  ) : !item.column ? <Badge tone="danger" icon={<Ban />}>Missing</Badge>
    : leftOut ? (
      <Tooltip content="Left out of ingestion on Results. Include it there, or choose another column.">
        <span tabIndex={0} className="inline-flex"><Badge tone="warning" icon={<TriangleAlert />}>Left out</Badge></span>
      </Tooltip>
    ) : <Badge tone="success" icon={<CheckCircle2 />}>Found</Badge>;
  const label = (
    <>
        <span className="flex min-w-0 items-start gap-1.5">
          <span className="min-w-0 font-medium leading-5 text-ink-900">{softBreaks(item.label)}</span>
          {item.date_role && <Badge tone="info" className="mt-0.5 shrink-0 !px-1.5 !py-0 !text-[10px]">{item.date_role}</Badge>}
        </span>
        <span className="block font-mono text-[11px] text-ink-400">{softBreaks(item.name)}</span>
        {alternatives.length > 0 && (
          <span className="block text-caption text-brand-700" title={`Or ${alternatives.map((other) => other.label).join(" or ")}: one is enough`}>
            or {softBreaks(alternatives.map((other) => other.label).join(" or "))}
          </span>
        )}
    </>
  );
  const select = (
    <Select value={item.column ?? missingValue} options={choices} onChange={onChange} label={`File column for ${item.label}`} hideLabel
      disabled={busy} width={420} className="w-full" hideSelectedMeta />
  );
  const match = (
    <>
        {status}
        {vote?.method && (
          <Tooltip content={vote.reason || undefined}>
            <span tabIndex={0} className="mt-1 flex items-center gap-1 text-caption text-ink-500">
              <span className={clsx("font-medium", vote.method === "reviewer" ? "text-brand-700" : vote.method === "ai" ? "text-amber-700" : "text-ink-600")}>
                {METHOD_LABEL[vote.method] ?? vote.method}
              </span>
              {vote.score !== null && <span className="num">{Math.round(vote.score * 100)}%</span>}
            </span>
          </Tooltip>
        )}
    </>
  );
  const grouped = pair === "first" || pair === "middle";
  if (stacked) {
    return (
      <li className={clsx("px-3 py-2.5", pair && "border-l-2 border-l-brand-400 bg-brand-50/30", grouped ? "border-b border-dashed border-ink-200" : "border-b border-ink-100 last:border-0")}>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">{label}</div>
          <div className="shrink-0 text-right">{match}</div>
        </div>
        <div className="mt-2">{select}</div>
      </li>
    );
  }
  return (
    <tr className={clsx("align-middle", pair && "bg-brand-50/30", grouped ? "border-b border-dashed border-ink-200" : "border-b border-ink-100 last:border-0")}>
      <td className={clsx("py-1.5 pr-2", pair ? "border-l-2 border-l-brand-400 pl-2.5" : "pl-3")}>{label}</td>
      <td className="px-2 py-1.5">{select}</td>
      <td className="px-3 py-1.5">{match}</td>
    </tr>
  );
}


/* ---- reporting dates ----------------------------------------------------- */

function ReportingDates({ output, busy, onChange }: { output: ValidationOutput; busy: boolean; onChange: (change: OutputChange) => void }) {
  const [start, setStart] = useState(month(output.reporting_start_date));
  const [end, setEnd] = useState(month(output.reporting_end_date));
  useEffect(() => setStart(month(output.reporting_start_date)), [output.reporting_start_date]);
  useEffect(() => setEnd(month(output.reporting_end_date)), [output.reporting_end_date]);
  const commit = (nextStart: string, nextEnd: string) => {
    if (nextStart === month(output.reporting_start_date) && nextEnd === month(output.reporting_end_date)) return;
    if (!nextStart && !nextEnd) return;
    onChange({ reporting_start_date: nextStart || nextEnd, reporting_end_date: nextEnd || nextStart });
  };
  const roles: DateRole[] = ["AED", "PED", "TED"];
  const editable = !output.date_detail || output.flag || output.entered || output.use_control_dates;
  return (
    <section aria-labelledby={`dates-${output.key}`}>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h3 id={`dates-${output.key}`} className="flex items-center gap-2 text-card text-ink-900"><CalendarRange className="h-4 w-4 text-ink-500" aria-hidden />Reporting dates</h3>
        <span className="text-caption text-ink-500">AED, then PED, then TED: the first populated on every row decides</span>
      </div>
      <div className="overflow-hidden rounded-lg border border-ink-200">
        <div className="relative overflow-x-auto scroll-thin">
          <table className="w-full min-w-[500px] border-collapse text-table">
            <caption className="sr-only">How populated each date column is</caption>
            <thead className="bg-ink-50">
              <tr className="border-b border-ink-200 text-left text-caption font-semibold text-ink-600">
                <th scope="col" className="px-3 py-2">Date</th>
                <th scope="col" className="px-2.5 py-2">File column</th>
                <th scope="col" className="px-2.5 py-2 text-right">Populated</th>
                <th scope="col" className="px-2.5 py-2 text-right">Unreadable</th>
                <th scope="col" className="px-3 py-2">Months</th>
              </tr>
            </thead>
            <tbody>
              {roles.map((role) => {
                const stat = output.dates[role];
                const chosen = output.date_detail === role;
                const header = stat.column ? output.columns.find((column) => column.name === stat.column || column.original === stat.column)?.header ?? stat.column : null;
                return (
                  <tr key={role} className={clsx("border-b border-ink-100 last:border-0", chosen && "bg-emerald-50/60")}>
                    <td className="px-3 py-1.5">
                      <span className="flex items-center gap-1.5 whitespace-nowrap">
                        <Badge tone={chosen ? "success" : "neutral"} className="!px-1.5 !py-0 !text-[10px]">{role}</Badge>
                        <span className="text-ink-800">{ROLE_LABEL[role]}</span>
                      </span>
                      {chosen && <span className="block text-caption font-medium text-emerald-700">Decides the reporting dates</span>}
                    </td>
                    <td className="max-w-[140px] truncate px-2.5 py-1.5 text-ink-700" title={header ?? undefined}>{header ?? <span className="text-ink-300">not found</span>}</td>
                    <td className={clsx("num px-2.5 py-1.5 text-right", stat.complete ? "text-emerald-700" : stat.column ? "text-amber-700" : "text-ink-300")}>
                      {stat.column ? (
                        <>
                          <span className="block font-medium">{stat.percent}%</span>
                          <span className="block whitespace-nowrap text-caption text-ink-500">{formatNumber(stat.populated)} of {formatNumber(stat.rows)}</span>
                        </>
                      ) : "—"}
                    </td>
                    <td className="num px-2.5 py-1.5 text-right text-ink-700">{stat.column ? formatNumber(stat.invalid) : "—"}</td>
                    <td className="num whitespace-nowrap px-3 py-1.5 text-ink-700">{stat.first ? monthSpan(stat.first, stat.last) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div className="mt-4 grid gap-4 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
        <label className="block">
          <span className="mb-1 block text-caption font-medium text-ink-600">Reporting start date</span>
          <input type="month" value={start} disabled={busy || !editable} onChange={(event) => setStart(event.target.value)} onBlur={() => commit(start, end)}
            className={clsx(input, "num", output.reporting_start_date ? "border-ink-200" : "border-amber-400")} />
        </label>
        <label className="block">
          <span className="mb-1 block text-caption font-medium text-ink-600">Reporting end date</span>
          <input type="month" value={end} disabled={busy || !editable} onChange={(event) => setEnd(event.target.value)} onBlur={() => commit(start, end)}
            className={clsx(input, "num", output.reporting_end_date ? "border-ink-200" : "border-amber-400")} />
        </label>
        <div className="flex h-9 items-center gap-2">
          {output.period_type ? <Badge tone="success">{output.period_type === "YTD" ? "Year to date" : "Monthly"}</Badge>
            : <Badge tone="warning">{output.flag ? "Flagged" : "Not set"}</Badge>}
        </div>
      </div>
      <p className="mt-2 text-caption text-ink-500">
        {output.reporting_start_date
          ? `${dayLabel(output.reporting_start_date)} – ${dayLabel(output.reporting_end_date)} · ${
            output.origin === "entered" ? "entered by you" : output.origin === "control" ? "from the control table" : `from ${output.origin} (${ROLE_LABEL[output.origin as DateRole]})`}. Whole months.`
          : "No date column is populated on every row: enter the months this file reports."}
      </p>
      {output.flag && <Alert tone="warning" className="mt-3" title="Neither year-to-date nor monthly">{output.flag}</Alert>}
      <div className="mt-3 flex flex-wrap gap-2">
        {output.listed && !output.use_control_dates && (
          <Button size="sm" disabled={busy} onClick={() => onChange({ use_control_dates: true })}>
            Use the control table's {monthLabel(output.listed.start)} – {monthLabel(output.listed.end)}
          </Button>
        )}
        {(output.entered || output.use_control_dates) && (
          <Button size="sm" variant="ghost" icon={<RefreshCw />} disabled={busy}
            onClick={() => onChange(output.use_control_dates ? { use_control_dates: false } : { reporting_start_date: null, reporting_end_date: null })}>
            Back to the data's dates
          </Button>
        )}
      </div>
    </section>
  );
}

/* ---- against bronze ------------------------------------------------------- */

function AgainstBronze({ output, busy, onChoose }: { output: ValidationOutput; busy: boolean; onChoose: (choice: ValidationChoice | null) => void }) {
  const action = output.action;
  return (
    <section aria-labelledby={`bronze-${output.key}`}>
      <h3 id={`bronze-${output.key}`} className="mb-3 flex items-center gap-2 text-card text-ink-900"><DatabaseZap className="h-4 w-4 text-ink-500" aria-hidden />Against Bronze</h3>
      <div className="rounded-lg border border-ink-200 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-caption font-medium text-ink-600">Control table action</span>
          {action ? <Badge tone={ACTION_TONE[action] ?? "neutral"}>{action === "DECIDE" ? "Your call" : action}</Badge> : <Badge tone="neutral">Waiting for the reporting dates</Badge>}
          {output.file_replaced && <span className="text-caption text-ink-600">replaces <span className="font-medium text-ink-800">{output.file_replaced}</span></span>}
          {output.confirm && <Badge tone="warning">Confirmed again at Ingest</Badge>}
        </div>
        {output.reasons.length > 0 && (
          <ul className="mt-2 space-y-0.5 text-body text-ink-700">{output.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
        )}

        {output.compare && <Comparison compare={output.compare} />}

        {(output.options.length > 0 || output.choice) && !output.staged && (
          <div className="mt-4 flex flex-wrap gap-2" role="group" aria-label="What happens to this file">
            {output.options.includes("replace_month") && (
              <Button size="sm" variant={output.choice === "replace_month" ? "primary" : "secondary"} disabled={busy} onClick={() => onChoose("replace_month")}>
                Replace {monthLabel(output.compare ? `${output.compare.month}-01` : output.reporting_start_date)}
              </Button>
            )}
            {output.options.includes("replace") && (
              <Button size="sm" variant={output.choice === "replace" ? "primary" : "secondary"} disabled={busy} onClick={() => onChoose("replace")}>
                Replace the newer file
              </Button>
            )}
            {output.choice && output.choice !== "reject" && (
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => onChoose(null)}>Undo</Button>
            )}
          </div>
        )}
        {!output.staged && (
          <div className="mt-4 border-t border-ink-100 pt-3">
            {output.choice === "reject" ? (
              <Button size="sm" variant="ghost" icon={<RefreshCw />} disabled={busy} onClick={() => onChoose(null)}>Don't reject</Button>
            ) : (
              <Button size="sm" variant="ghost" icon={<ShieldAlert />} disabled={busy} onClick={() => onChoose("reject")}>Reject this file</Button>
            )}
          </div>
        )}
      </div>
    </section>
  );
}

function Comparison({ compare }: { compare: NonNullable<ValidationOutput["compare"]> }) {
  const percent = (value: number | null | undefined) => (value === null || value === undefined ? "—" : `${value}%`);
  const rows = [
    { label: "This file", ...compare.this, file_name: compare.this.file_name, highlight: true },
    ...compare.earlier.map((item) => ({ label: `Control ${item.control_id}`, ...item, highlight: false })),
  ];
  return (
    <div className="mt-4 overflow-hidden rounded-md border border-amber-200">
      <p className="bg-amber-50 px-3 py-2 text-caption font-medium text-amber-900">{monthLabel(`${compare.month}-01`)}: what each file holds for the month</p>
      <div className="relative overflow-x-auto scroll-thin">
        <table className="w-full min-w-[560px] border-collapse text-table">
          <thead className="bg-ink-50">
            <tr className="border-b border-ink-200 text-left text-caption font-semibold text-ink-600">
              <th scope="col" className="px-3 py-2">File</th>
              <th scope="col" className="px-3 py-2 text-right">Rows</th>
              <th scope="col" className="px-3 py-2 text-right">Columns</th>
              <th scope="col" className="px-3 py-2 text-right">AED</th>
              <th scope="col" className="px-3 py-2 text-right">PED</th>
              <th scope="col" className="px-3 py-2 text-right">TED</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.label}-${row.file_name}`} className={clsx("border-b border-ink-100 last:border-0", row.highlight && "bg-brand-50/40")}>
                <td className="max-w-[280px] px-3 py-2">
                  <span className="block text-caption font-medium text-ink-600">{row.label}</span>
                  <span className="block truncate text-ink-900" title={row.file_name}>{row.file_name}</span>
                </td>
                <td className="num px-3 py-2 text-right">{row.rows === null || row.rows === undefined ? "—" : formatNumber(row.rows)}</td>
                <td className="num px-3 py-2 text-right">{row.columns ?? "—"}</td>
                <td className="num px-3 py-2 text-right">{percent(row.AED)}</td>
                <td className="num px-3 py-2 text-right">{percent(row.PED)}</td>
                <td className="num px-3 py-2 text-right">{percent(row.TED)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
