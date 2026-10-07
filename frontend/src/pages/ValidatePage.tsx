import clsx from "clsx";
import {
  ArrowLeft,
  ArrowRight,
  BadgeCheck,
  Ban,
  CalendarRange,
  CheckCircle2,
  CircleDashed,
  Columns3,
  DatabaseZap,
  FileSpreadsheet,
  Info,
  Loader2,
  RefreshCw,
  ShieldAlert,
  Table2,
  TriangleAlert,
  Upload,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api/client";
import type {
  DateRole,
  RequiredColumn,
  Validation,
  ValidationChoice,
  ValidationFile,
  ValidationOutput,
  Verdict,
} from "../api/types";
import { PageHeader } from "../components/layout/Layout";
import { Badge, type Tone } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Alert, EmptyState, Skeleton, StatTile, useToast } from "../components/ui/Feedback";
import { Tooltip } from "../components/ui/Overlay";
import { Select } from "../components/ui/Select";
import { formatNumber, plural } from "../lib/format";
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
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);

  const start = useCallback(async () => {
    if (!batch || !jobIds.length) return;
    setError(null);
    setData(null);
    try {
      const next = await api.createValidation(jobIds, batch.id);
      save(batch.id, next.id);
      setData(next);
    } catch (err) {
      setError(toError(err));
    }
  }, [batch, jobIds]);

  // Reuse this batch's validation while the server has it; otherwise check the files afresh.
  useEffect(() => {
    if (!batch || !jobIds.length || flow.running) return;
    const saved = readSaved(batch.id);
    if (!saved) {
      void start();
      return;
    }
    api.getValidation(saved).then(setData).catch(() => void start());
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
            Ingest
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
  } else if (error) {
    body = (
      <Alert tone="error" title={error.body.message} action={<Button size="sm" icon={<RefreshCw />} onClick={start}>Retry</Button>}>
        {error.body.advice}
      </Alert>
    );
  } else if (!data) {
    body = (
      <div className="card space-y-3 p-6" role="status" aria-label="Validating">
        <p className="flex items-center gap-2 text-body text-ink-600"><Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Matching columns and checking dates…</p>
        <Skeleton className="h-16" />
        <Skeleton className="h-64" />
      </div>
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
        onIngest={() => navigate("ingest")}
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

/* -------------------------------------------------------------------------- */

type OutputChange = Parameters<typeof api.updateValidationOutput>[2];
type FileChange = Parameters<typeof api.updateValidationFile>[2];

function ValidationView({
  data,
  output,
  busy,
  onSelect,
  onFile,
  onOutput,
  onStage,
  onRestart,
  onIngest,
}: {
  data: Validation;
  output: ValidationOutput;
  busy: boolean;
  onSelect: (key: string) => void;
  onFile: (jobId: string, change: FileChange) => void;
  onOutput: (key: string, change: OutputChange) => void;
  onStage: (keys: string[] | null) => void;
  onRestart: () => void;
  onIngest: () => void;
}) {
  const file = data.files.find((item) => item.job_id === output.job_id)!;
  const open = data.outputs.filter((item) => !item.staged);
  const stageable = open.filter((item) => item.verdict !== "needs_input");
  const options = data.outputs.map((item) => {
    const owner = data.files.find((candidate) => candidate.job_id === item.job_id);
    return {
      value: item.key,
      label: data.files.length > 1 ? `${owner?.file_name ?? ""} · ${item.name}` : item.name,
      icon: <Table2 />,
      group: data.files.length > 1 ? owner?.file_name : undefined,
      description: `${formatNumber(item.rows)} rows · fitness ${item.fitness}%${item.staged ? ` · staged as control ${item.staged.control_id}` : ""}`,
      meta: <Badge tone={item.staged ? "neutral" : VERDICT[item.verdict].tone}>{item.staged ? "Staged" : VERDICT[item.verdict].label}</Badge>,
    };
  });

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
      <div className="min-w-0 space-y-6">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <StatTile icon={<CheckCircle2 />} label="Ready" value={data.counts.ready} tone="success" />
          <StatTile icon={<CircleDashed />} label="Needs input" value={data.counts.needs_input} tone={data.counts.needs_input ? "warning" : "neutral"} />
          <StatTile icon={<TriangleAlert />} label="Flagged" value={data.counts.flagged} tone={data.counts.flagged ? "warning" : "neutral"} />
          <StatTile icon={<Ban />} label="Rejected" value={data.counts.rejected} tone={data.counts.rejected ? "danger" : "neutral"} />
        </div>

        <section className="card" aria-labelledby="validate-title">
          <div className="flex flex-col gap-4 border-b border-ink-100 px-5 py-4 md:px-6 lg:flex-row lg:items-end lg:justify-between">
            <div className="flex min-w-0 items-start gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-ink-200 bg-ink-50 text-ink-600" aria-hidden>
                <FileSpreadsheet className="h-5 w-5" />
              </div>
              <div className="min-w-0">
                <p className="label-caps">{output.name} · {plural(output.sheet_names.length, "sheet")} · {formatNumber(output.rows)} rows</p>
                <h2 id="validate-title" className="truncate text-section text-ink-900" title={file.file_name}>{file.file_name}</h2>
              </div>
            </div>
            {data.outputs.length > 1 && (
              <Select value={output.key} options={options} onChange={onSelect} label={`Table (${data.outputs.length})`} icon={<Table2 />}
                className="w-full lg:w-[360px]" width={460} />
            )}
          </div>

          <Fitness output={output} />

          <div className="space-y-6 p-5 md:p-6">
            {output.staged && (
              <Alert tone={output.staged.processing_action === "REJECTED" ? "warning" : "success"}
                title={`Staged as control row ${output.staged.control_id}: ${output.staged.processing_action}`}>
                {output.staged.seeded ? "The control table already listed this file; its row was filled in. " : ""}
                {output.staged.processing_action === "REJECTED" ? "It is recorded, and never loaded into Bronze." : `Its rows are in staging.${output.staged.staging_table}, ready for Ingest.`}
              </Alert>
            )}
            <FileDetails file={file} busy={busy || Boolean(output.staged)} onChange={(change) => onFile(file.job_id, change)} />
            <RequiredColumns output={output} busy={busy || Boolean(output.staged)} onChange={(mapping) => onOutput(output.key, { mapping })} />
            <ReportingDates output={output} busy={busy || Boolean(output.staged)} onChange={(change) => onOutput(output.key, change)} />
            <AgainstBronze output={output} busy={busy || Boolean(output.staged)} onChoose={(choice) => onOutput(output.key, { choice })} />
          </div>

          {!output.staged && (
            <div className="flex flex-col-reverse gap-2 border-t border-ink-100 px-5 py-4 sm:flex-row sm:items-center sm:justify-between md:px-6">
              <p className="text-caption text-ink-500">
                {output.verdict === "needs_input" ? output.needs[0] : output.verdict === "ready"
                  ? `Staging records control action ${output.action} and copies ${formatNumber(output.rows)} rows to staging.`
                  : "Staging records the file as REJECTED in the control table; nothing reaches Bronze."}
              </p>
              <Button
                variant={output.verdict === "ready" ? "primary" : "secondary"}
                icon={output.verdict === "ready" ? <BadgeCheck /> : <Ban />}
                disabled={busy || output.verdict === "needs_input"}
                onClick={() => onStage([output.key])}
              >
                {output.verdict === "ready" ? "Stage" : "Record as rejected"}
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

      <aside className="scroll-thin xl:sticky xl:top-[80px] xl:max-h-[calc(100dvh-96px)] xl:self-start xl:overflow-y-auto">
        <section className="card p-6" aria-labelledby="stage-title">
          <h2 id="stage-title" className="label-caps">Staging</h2>
          <ul className="mt-4 space-y-2">
            {data.outputs.map((item) => {
              const owner = data.files.find((candidate) => candidate.job_id === item.job_id);
              const verdict = VERDICT[item.verdict];
              return (
                <li key={item.key}>
                  <button type="button" onClick={() => onSelect(item.key)}
                    className={clsx("flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left hover:bg-ink-50", item.key === output.key && "bg-ink-50")}>
                    <span className={clsx("flex shrink-0 [&>svg]:h-4 [&>svg]:w-4", item.staged ? "text-ink-400" : {
                      success: "text-emerald-600", warning: "text-amber-600", danger: "text-danger-600",
                    }[verdict.tone as "success" | "warning" | "danger"])} aria-hidden>
                      {item.staged ? <BadgeCheck /> : verdict.icon}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-body text-ink-900">{owner?.file_name}</span>
                      <span className="block truncate text-caption text-ink-500">
                        {item.staged ? `Staged · control ${item.staged.control_id} · ${item.staged.processing_action}` : `${verdict.label} · ${item.fitness}%`}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          <Button variant="primary" size="lg" className="mt-5 w-full" icon={<BadgeCheck />} disabled={busy || !stageable.length}
            onClick={() => onStage(stageable.map((item) => item.key))}>
            {stageable.length ? `Stage ${plural(stageable.length, "table")}` : open.length ? "Fix the tables first" : "All staged"}
          </Button>
          <p className="mt-2 text-center text-caption text-ink-500">
            Ready tables go to staging; flagged and rejected ones are recorded as REJECTED.
          </p>
          {data.staged > 0 && (
            <Button className="mt-4 w-full" iconRight={<ArrowRight />} onClick={onIngest}>Continue to Ingest</Button>
          )}
          <Button variant="ghost" size="sm" className="mt-2 w-full" icon={<RefreshCw />} onClick={onRestart} disabled={busy}>
            Validate again
          </Button>
        </section>
      </aside>
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

function RequiredColumns({ output, busy, onChange }: { output: ValidationOutput; busy: boolean; onChange: (mapping: Record<string, string | null>) => void }) {
  const columns = useMemo(() => new Map(output.columns.map((column) => [column.original, column])), [output.columns]);
  const found = output.required.filter((item) => item.column && !columns.get(item.column)?.excluded).length;
  const MISSING = "__missing__";
  return (
    <section aria-labelledby={`required-${output.key}`}>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h3 id={`required-${output.key}`} className="flex items-center gap-2 text-card text-ink-900"><Columns3 className="h-4 w-4 text-ink-500" aria-hidden />Required columns</h3>
        <span className="num text-caption text-ink-500">{found} of {output.required.length} found · every one is needed for Bronze</span>
      </div>
      <div className="overflow-hidden rounded-lg border border-ink-200">
        <div className="relative overflow-x-auto scroll-thin">
          <table className="w-full min-w-[720px] border-collapse text-table">
            <caption className="sr-only">Required columns and the file columns found for them</caption>
            <thead className="bg-ink-50">
              <tr className="border-b border-ink-200 text-left text-caption font-semibold text-ink-600">
                <th scope="col" className="px-4 py-2.5">Required column</th>
                <th scope="col" className="min-w-[260px] px-3 py-2.5">File column</th>
                <th scope="col" className="px-3 py-2.5">Found by</th>
                <th scope="col" className="px-4 py-2.5">Status</th>
              </tr>
            </thead>
            <tbody>
              {output.required.map((item) => (
                <RequiredRow key={item.name} item={item} output={output} columns={columns} busy={busy} missingValue={MISSING}
                  onChange={(column) => onChange({ [item.name]: column === MISSING ? null : column })} />
              ))}
            </tbody>
          </table>
        </div>
      </div>
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
  onChange,
}: {
  item: RequiredColumn;
  output: ValidationOutput;
  columns: Map<string, ValidationOutput["columns"][number]>;
  busy: boolean;
  missingValue: string;
  onChange: (column: string) => void;
}) {
  const column = item.column ? columns.get(item.column) : null;
  const leftOut = Boolean(column?.excluded);
  const proposed = new Map(item.options.map((option) => [option.column, option]));
  const choices = [
    { value: missingValue, label: "— missing", description: "No column of this file holds it" },
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
  return (
    <tr className="border-b border-ink-100 align-middle last:border-0">
      <td className="px-4 py-2">
        <span className="flex items-center gap-2">
          <span className="font-medium text-ink-900">{item.label}</span>
          {item.date_role && <Badge tone="info" className="!px-1.5 !py-0 !text-[10px]">{item.date_role}</Badge>}
        </span>
        <span className="block font-mono text-[11px] text-ink-400">{item.name}</span>
      </td>
      <td className="px-3 py-2">
        <Select value={item.column ?? missingValue} options={choices} onChange={onChange} label={`File column for ${item.label}`} hideLabel
          disabled={busy} width={420} />
      </td>
      <td className="px-3 py-2">
        {vote?.method ? (
          <Tooltip content={vote.reason || undefined}>
            <span tabIndex={0} className="inline-flex items-center gap-1.5">
              <Badge tone={vote.method === "reviewer" ? "brand" : vote.method === "ai" ? "warning" : "neutral"}>{METHOD_LABEL[vote.method] ?? vote.method}</Badge>
              {vote.score !== null && <span className="num text-caption text-ink-500">{Math.round(vote.score * 100)}%</span>}
            </span>
          </Tooltip>
        ) : (
          <span className="text-ink-300">—</span>
        )}
      </td>
      <td className="px-4 py-2">
        {!item.column ? <Badge tone="danger" icon={<Ban />}>Missing</Badge>
          : leftOut ? <Badge tone="warning" icon={<TriangleAlert />}>Left out on Results</Badge>
            : <Badge tone="success" icon={<CheckCircle2 />}>Found</Badge>}
      </td>
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
          <table className="w-full min-w-[640px] border-collapse text-table">
            <caption className="sr-only">How populated each date column is</caption>
            <thead className="bg-ink-50">
              <tr className="border-b border-ink-200 text-left text-caption font-semibold text-ink-600">
                <th scope="col" className="px-4 py-2.5">Date</th>
                <th scope="col" className="px-3 py-2.5">File column</th>
                <th scope="col" className="px-3 py-2.5 text-right">Populated</th>
                <th scope="col" className="px-3 py-2.5 text-right">Unreadable</th>
                <th scope="col" className="px-4 py-2.5">Months</th>
              </tr>
            </thead>
            <tbody>
              {roles.map((role) => {
                const stat = output.dates[role];
                const chosen = output.date_detail === role;
                const header = stat.column ? output.columns.find((column) => column.name === stat.column || column.original === stat.column)?.header ?? stat.column : null;
                return (
                  <tr key={role} className={clsx("border-b border-ink-100 last:border-0", chosen && "bg-emerald-50/60")}>
                    <td className="px-4 py-2">
                      <span className="flex items-center gap-2">
                        <Badge tone={chosen ? "success" : "neutral"} className="!px-1.5 !py-0 !text-[10px]">{role}</Badge>
                        <span className="text-ink-800">{ROLE_LABEL[role]}</span>
                        {chosen && <span className="text-caption font-medium text-emerald-700">decides</span>}
                      </span>
                    </td>
                    <td className="max-w-[220px] truncate px-3 py-2 text-ink-700" title={header ?? undefined}>{header ?? <span className="text-ink-300">not found</span>}</td>
                    <td className={clsx("num px-3 py-2 text-right", stat.complete ? "text-emerald-700" : stat.column ? "text-amber-700" : "text-ink-300")}>
                      {stat.column ? <>{formatNumber(stat.populated)} / {formatNumber(stat.rows)} · {stat.percent}%</> : "—"}
                    </td>
                    <td className="num px-3 py-2 text-right text-ink-700">{stat.column ? formatNumber(stat.invalid) : "—"}</td>
                    <td className="num px-4 py-2 text-ink-700">{stat.first ? `${monthLabel(stat.first)} – ${monthLabel(stat.last)}` : "—"}</td>
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
