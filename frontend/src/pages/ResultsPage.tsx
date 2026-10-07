import clsx from "clsx";
import {
  AlertTriangle,
  Archive,
  ArrowRight,
  CheckCircle2,
  ClipboardList,
  Database,
  Download,
  Eye,
  FileCheck2,
  FileJson,
  FileSpreadsheet,
  BetweenHorizontalEnd,
  Columns3,
  PencilLine,
  Loader2,
  PlayCircle,
  RefreshCw,
  Rows3,
  ShieldAlert,
  ShieldCheck,
  Table2,
  TriangleAlert,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, ApiError, download, exportUrls } from "../api/client";
import type { BatchStatus, ColumnProfile, ConsistencyReport, JobResults, OutputSummary } from "../api/types";
import { AppendMismatchModal, AppendOutcomeAlert, appendIncomplete } from "../components/AppendOutcome";
import { ConsistencyView } from "../components/ConsistencyView";
import { DataTable } from "../components/DataTable";
import { PageHeader } from "../components/layout/Layout";
import { Badge } from "../components/ui/Badge";
import { Button, type ButtonState } from "../components/ui/Button";
import { Alert, EmptyState, Skeleton, StatTile, useToast } from "../components/ui/Feedback";
import { Modal, Tooltip } from "../components/ui/Overlay";
import { Pagination, useFitPageSize, usePaged } from "../components/ui/Pagination";
import { Select } from "../components/ui/Select";
import { TabPanel, Tabs } from "../components/ui/Tabs";
import { formatNumber, formatTimestamp, parseFlags, plural } from "../lib/format";
import { useNavigate, useWorkflow } from "../state/workflow";

type Tab = "preview" | "columns" | "consistency" | "export";

export function ResultsPage() {
  const flow = useWorkflow();
  const navigate = useNavigate();
  const batch = flow.batch;
  const succeeded = batch?.jobs.filter((job) => job.status === "succeeded") ?? [];
  const jobId = succeeded.some((job) => job.id === flow.resultsJobId) ? flow.resultsJobId : succeeded[0]?.id ?? null;

  const header = (
    <PageHeader
      page="results"
      title="Results"
      description="Review, refine and export cleaned data."
      actions={jobId && (
        <Button variant="primary" iconRight={<ArrowRight />} onClick={() => navigate("validate")}>
          Validate
        </Button>
      )}
    />
  );

  if (!jobId) {
    let body;
    if (flow.running) {
      body = (
        <EmptyState
          icon={<Loader2 className="animate-spin" />}
          title="Cleaning in progress"
          description="Results appear when the first file finishes."
          action={<Button variant="primary" iconRight={<ArrowRight />} onClick={() => navigate("run")}>View progress</Button>}
        />
      );
    } else if (batch) {
      body = (
        <EmptyState
          icon={<ShieldAlert />}
          title="No file cleaned"
          description="See the Run page for details."
          action={<Button variant="primary" iconRight={<ArrowRight />} onClick={() => navigate("run")}>View run</Button>}
        />
      );
    } else {
      body = (
        <EmptyState
          icon={<ClipboardList />}
          title="No results yet"
          description="Run a cleaning job first."
          action={<Button variant="primary" icon={<PlayCircle />} onClick={() => navigate(flow.files.length ? "run" : "configuration")}>{flow.files.length ? "Run" : "Configure"}</Button>}
        />
      );
    }
    return (
      <>
        {header}
        <div className="card">{body}</div>
      </>
    );
  }

  const results = flow.results[jobId];
  const error = flow.resultsErrors[jobId];

  return (
    <>
      {header}
      <div className="space-y-6">
        {batch && batch.files_total > 1 && !results && <FilePicker batch={batch} jobId={jobId} />}
        {results ? (
          <ResultsContent
            key={jobId}
            results={results}
            picker={batch && batch.files_total > 1 ? <FileSelect batch={batch} jobId={jobId} /> : null}
            batchExport={batch && batch.files_total > 1 ? <BatchExport batch={batch} embedded /> : null}
          />
        ) : (
          <div className="card">
            {error ? (
              <EmptyState
                icon={<ShieldAlert />}
                title="Results failed to load"
                description={error.body.advice ?? error.body.message}
                action={<Button variant="primary" icon={<RefreshCw />} onClick={() => flow.reloadResults(jobId)}>Try again</Button>}
              />
            ) : (
              <ResultsSkeleton />
            )}
          </div>
        )}
        {!results && batch && batch.files_total > 1 && <BatchExport batch={batch} />}
      </div>
    </>
  );
}

/** Which file's results are shown, as a card: used while that file's results load. */
function FilePicker({ batch, jobId }: { batch: BatchStatus; jobId: string }) {
  return (
    <section className="card flex flex-col gap-4 p-5 md:flex-row md:items-end md:justify-between md:p-6" aria-labelledby="file-picker-title">
      <div>
        <h2 id="file-picker-title" className="text-card text-ink-900">Files</h2>
        <p className="num text-caption text-ink-500">{batch.files_succeeded} of {plural(batch.files_total, "file")} cleaned</p>
      </div>
      <FileSelect batch={batch} jobId={jobId} />
    </section>
  );
}

/** Which file's results are shown. Files that didn't finish are listed, but can't be opened. */
function FileSelect({ batch, jobId }: { batch: BatchStatus; jobId: string }) {
  const flow = useWorkflow();
  const options = batch.jobs.map((job) => {
    const summary = flow.results[job.id]?.summary;
    return {
      value: job.id,
      label: job.source_name,
      icon: <FileSpreadsheet />,
      disabled: job.status !== "succeeded",
      description:
        job.status === "succeeded"
          ? summary
            ? `${plural(summary.outputs, "table")} · ${formatNumber(summary.rows)} rows${summary.consistency_issues ? ` · ${plural(summary.consistency_issues, "issue")}` : ""}`
            : "Loading…"
          : job.status === "failed"
            ? `Failed: ${job.error?.message ?? "see Run"}`
            : job.status === "cancelled"
              ? "Cancelled"
              : "Cleaning…",
      meta:
        job.status === "failed" ? (
          <Badge tone="danger">Failed</Badge>
        ) : job.status === "cancelled" ? (
          <Badge tone="neutral">Cancelled</Badge>
        ) : summary?.consistency_issues ? (
          <Badge tone="danger">Issues</Badge>
        ) : undefined,
    };
  });
  const position = batch.jobs.findIndex((job) => job.id === jobId) + 1;

  return (
    <Select
      value={jobId}
      options={options}
      onChange={flow.setResultsJobId}
      label={`File ${position} of ${batch.files_total} · ${batch.files_succeeded} cleaned`}
      icon={<FileSpreadsheet />}
      className="w-full sm:w-[320px]"
      width={460}
    />
  );
}

function ResultsContent({ results, picker, batchExport }: { results: JobResults; picker?: ReactNode; batchExport?: ReactNode }) {
  const flow = useWorkflow();
  const navigate = useNavigate();
  const [tab, setTab] = useState<Tab>("preview");
  const [appendDetails, setAppendDetails] = useState(false);
  const { summary, outputs, job } = results;
  const appendCheck = results.append_check;
  const output = outputs.find((item) => item.id === flow.outputIdFor(job.id)) ?? outputs[0];

  const options = outputs.map((item) => ({
    value: item.id,
    label: item.name,
    icon: item.kind === "stacked" ? <BetweenHorizontalEnd /> : <Table2 />,
    description: (
      <>
        {item.kind === "stacked" ? item.tables.join(" + ") : item.sheet_names[0]} · {formatNumber(item.rows)} × {item.columns}
      </>
    ),
    meta: item.kind === "stacked" ? <Badge tone="brand">Appended</Badge> : undefined,
  }));

  if (!output) {
    return (
      <div className="card">
        <EmptyState icon={<Table2 />} title="No tables found" description="Selected sheets held no tabular data." />
      </div>
    );
  }

  const consistency = results.consistency?.[output.id];

  return (
    <div className="space-y-6">
      {flow.stale && (
        <Alert
          tone="warning"
          title="Results are out of date"
          action={<Button size="sm" onClick={() => navigate("run")}>Run again</Button>}
        />
      )}

      {appendIncomplete(appendCheck) && appendCheck && (
        <>
          <AppendOutcomeAlert check={appendCheck} onDetails={() => setAppendDetails(true)} />
          <AppendMismatchModal
            open={appendDetails}
            check={appendCheck}
            onClose={() => setAppendDetails(false)}
            onChangeConfiguration={() => navigate("configuration")}
          />
        </>
      )}

      {/* One card: which file (and table) is shown, then the table itself. */}
      <section className="card" aria-labelledby="done-title">
        <div className="flex flex-col gap-4 px-5 pt-5 md:px-6 xl:flex-row xl:items-end xl:justify-between">
          <div className="flex min-w-0 items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-emerald-50 text-emerald-700" aria-hidden>
              <FileCheck2 className="h-5 w-5" />
            </div>
            <div className="min-w-0">
              <p className="label-caps !text-emerald-700">Cleaned</p>
              <h2 id="done-title" className="truncate text-section text-ink-900" title={summary.source_name}>{summary.source_name}</h2>
              <p className="num text-caption text-ink-500">
                {plural(summary.sheets_selected, "sheet")} · {plural(summary.outputs, "table")} · {formatTimestamp(job.finished_at)}
                {summary.append && summary.appended_outputs > 0 && " · matching sheets combined"}
              </p>
            </div>
          </div>
          <div className="flex w-full flex-col gap-3 sm:flex-row sm:items-end xl:w-auto">
            {picker}
            <Select
              value={output.id}
              options={options}
              onChange={(id) => flow.setOutputId(job.id, id)}
              label={`Table (${outputs.length})`}
              icon={<Table2 />}
              className="w-full sm:w-[320px]"
              width={420}
            />
          </div>
        </div>

        <OutputStats
          output={output}
          report={consistency}
          onReview={() => setTab("columns")}
          onConsistency={() => setTab("consistency")}
        />

        <div className="px-5 md:px-6">
          <Tabs
            idPrefix="results"
            label="Result views"
            value={tab}
            onChange={setTab}
            items={[
              { id: "preview", label: "Preview", icon: <Eye /> },
              { id: "columns", label: "Columns", icon: <Database />, count: output.flagged_columns || undefined },
              {
                id: "consistency",
                label: "Checks",
                icon: consistency?.issues ? <ShieldAlert /> : <ShieldCheck />,
                count: consistency?.issues || undefined,
              },
              { id: "export", label: "Export", icon: <Download /> },
            ]}
          />
        </div>
        <div className="p-5 md:p-6">
          <TabPanel idPrefix="results" id="preview" active={tab === "preview"}>
            <DataTable jobId={job.id} output={output} onOutputUpdated={(updated) => flow.applyOutputUpdate(job.id, updated)} />
          </TabPanel>
          <TabPanel idPrefix="results" id="columns" active={tab === "columns"}>
            <ColumnProfileView jobId={job.id} output={output} />
          </TabPanel>
          <TabPanel idPrefix="results" id="consistency" active={tab === "consistency"}>
            <ConsistencyView report={consistency} />
          </TabPanel>
          <TabPanel idPrefix="results" id="export" active={tab === "export"}>
            <div className="space-y-4">
              <ExportSection
                jobId={job.id}
                sourceName={summary.source_name}
                output={output}
                outputs={outputs}
                consistency={results.consistency}
                onReview={() => setTab("consistency")}
                embedded
              />
              {batchExport}
            </div>
          </TabPanel>
        </div>
      </section>
    </div>
  );
}

function OutputStats({
  output,
  report,
  onReview,
  onConsistency,
}: {
  output: OutputSummary;
  report: ConsistencyReport | undefined;
  onReview: () => void;
  onConsistency: () => void;
}) {
  const review = output.flagged_columns;
  const issues = report?.issues ?? 0;
  const tile = "h-full";
  const clickable = "h-full rounded-lg text-left transition-shadow hover:shadow-card focus-visible:outline-none focus-visible:shadow-focus";

  return (
    <div className="mx-5 my-4 grid auto-rows-fr grid-cols-2 gap-3 md:mx-6 md:grid-cols-3 xl:grid-cols-5" aria-live="polite">
      <StatTile className={tile} icon={<Rows3 />} tone="success" label="Rows" value={formatNumber(output.rows)} />
      <StatTile className={tile} icon={<Columns3 />} tone="info" label="Columns" value={formatNumber(output.columns)} />
      <StatTile className={tile} icon={<PencilLine />} tone={output.renamed_columns ? "info" : "neutral"} label="Renamed" value={output.renamed_columns} />
      <button type="button" onClick={onReview} className={clickable} aria-label={`${review} columns to review. Open the column profile.`}>
        <StatTile className={tile} icon={review ? <AlertTriangle /> : <CheckCircle2 />} tone={review ? "warning" : "success"} label="To review" value={review} />
      </button>
      <button type="button" onClick={onConsistency} className={clickable} aria-label={`${issues} consistency issues. Open the consistency checks.`}>
        <StatTile
          className={clsx(tile, issues > 0 && "border-danger-200")}
          icon={issues ? <ShieldAlert /> : <ShieldCheck />}
          tone={issues ? "danger" : "success"}
          label={issues ? "Issues" : "Checks"}
          value={issues || "Passed"}
        />
      </button>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function ColumnProfileView({ jobId, output }: { jobId: string; output: OutputSummary }) {
  const [rows, setRows] = useState<ColumnProfile[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [onlyFlagged, setOnlyFlagged] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setRows(null);
    setError(null);
    api
      .getColumns(jobId, output.id)
      .then((data) => !cancelled && setRows(data))
      .catch((err) => !cancelled && setError(err instanceof ApiError ? err : new ApiError(0, { code: "unknown", message: String(err) })));
    return () => {
      cancelled = true;
    };
  }, [jobId, output.id, output.headers_updated_at, attempt]);

  const visible = useMemo(() => (rows ?? []).filter((row) => !onlyFlagged || parseFlags(row.type_flag).some((flag) => flag.level === "CHECK")), [rows, onlyFlagged]);
  const perSheet = output.kind === "stacked";
  const list = useRef<HTMLDivElement>(null);
  const paged = usePaged(visible, useFitPageSize(list, { min: 6, fallbackRow: 41, reserve: 130 }), onlyFlagged);

  if (error)
    return (
      <Alert tone="error" title="Profile failed to load" action={<Button size="sm" onClick={() => setAttempt((n) => n + 1)}>Retry</Button>}>
        {error.body.message}
      </Alert>
    );
  if (!rows) return <ProfileSkeleton />;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-caption text-ink-500">{perSheet ? "Per source sheet" : "Per column"}</p>
        <label className="flex cursor-pointer items-center gap-2 text-body text-ink-700">
          <input type="checkbox" className="h-4 w-4 accent-brand-600" checked={onlyFlagged} onChange={(event) => setOnlyFlagged(event.target.checked)} />
          Flagged only
        </label>
      </div>
      <div ref={list} className="overflow-hidden rounded-lg border border-ink-200">
      <div className="relative overflow-x-auto scroll-thin">
        <table className="w-full min-w-[960px] border-separate border-spacing-0 text-table">
          <caption className="sr-only">Column profile for {output.name}</caption>
          <thead>
            <tr className="text-left text-caption font-semibold text-ink-600">
              {["Column", perSheet ? "Sheet" : null, "Type", "Filled", "Distinct", "Empty %", "Min", "Max", "Sum", "Notes"].filter(Boolean).map((label) => (
                <th key={label} scope="col" className={clsx("border-b border-ink-200 bg-ink-50 px-3 py-2", ["Filled", "Distinct", "Empty %", "Sum"].includes(label!) && "text-right")}>
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {paged.slice.map((row, index) => {
              const flags = parseFlags(row.type_flag);
              const empty = Number(row.null_percentage);
              return (
                <tr key={`${row.header_name}-${row.sheet_name}-${paged.from + index}`} data-row className="align-top hover:bg-ink-50">
                  <td className="border-b border-ink-100 px-3 py-2 font-medium text-ink-900">{row.header_name}</td>
                  {perSheet && <td className="border-b border-ink-100 px-3 py-2 text-ink-600">{row.sheet_name}</td>}
                  <td className="border-b border-ink-100 px-3 py-2">
                    <code className="rounded bg-ink-100 px-1.5 py-0.5 font-mono text-[11px] text-ink-700">{row.datatype}</code>
                    {row.inferred_datatype && row.inferred_datatype !== row.datatype && (
                      <Tooltip content={`Auto-loaders may read this as ${row.inferred_datatype}. Use the stated type.`}>
                        <span tabIndex={0} className="ml-1 inline-flex text-amber-600" aria-label="Type differs from what a loader would infer">
                          <TriangleAlert className="h-3.5 w-3.5" />
                        </span>
                      </Tooltip>
                    )}
                  </td>
                  <td className="num border-b border-ink-100 px-3 py-2 text-right">{formatNumber(row.count)}</td>
                  <td className="num border-b border-ink-100 px-3 py-2 text-right">{formatNumber(row.distinct_count)}</td>
                  <td className={clsx("num border-b border-ink-100 px-3 py-2 text-right", empty >= 50 ? "text-amber-700" : "text-ink-700")}>{row.null_percentage}</td>
                  <td className="num max-w-[140px] truncate border-b border-ink-100 px-3 py-2 text-ink-700" title={row.min}>{row.min === "NA" ? <span className="text-ink-300">—</span> : row.min}</td>
                  <td className="num max-w-[140px] truncate border-b border-ink-100 px-3 py-2 text-ink-700" title={row.max}>{row.max === "NA" ? <span className="text-ink-300">—</span> : row.max}</td>
                  <td className="num max-w-[140px] truncate border-b border-ink-100 px-3 py-2 text-right text-ink-700" title={row.sum}>{row.sum === "NA" ? <span className="text-ink-300">—</span> : row.sum}</td>
                  <td className="min-w-[280px] border-b border-ink-100 px-3 py-2">
                    {flags.length === 0 ? (
                      <span className="text-ink-300">—</span>
                    ) : (
                      <ul className="space-y-1">
                        {flags.map((flag, i) => (
                          <li key={i} className="flex gap-1.5 text-caption">
                            <Badge tone={flag.level === "CHECK" ? "warning" : "info"} className="!px-1.5 !py-0 !text-[10px]">
                              {flag.level === "CHECK" ? "Check" : "Info"}
                            </Badge>
                            <span className="text-ink-700">{flag.text}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {visible.length === 0 && (
          <EmptyState compact icon={<CheckCircle2 />} title="Nothing flagged" description="All columns look consistent." />
        )}
      </div>
      <Pagination {...paged} onPage={paged.setPage} noun="column" />
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

/* -------------------------------------------------------------------------- */

function useDownload() {
  const toast = useToast();
  const [states, setStates] = useState<Record<string, ButtonState>>({});
  const run = async (key: string, url: string, label: string) => {
    setStates((all) => ({ ...all, [key]: "loading" }));
    try {
      const filename = await download(url);
      setStates((all) => ({ ...all, [key]: "success" }));
      toast({ severity: "success", title: `${label} downloaded`, description: filename });
    } catch (err) {
      setStates((all) => ({ ...all, [key]: "error" }));
      toast({ severity: "error", title: `${label} export failed`, description: err instanceof ApiError ? err.body.message : String(err) });
    } finally {
      setTimeout(() => setStates((all) => ({ ...all, [key]: "idle" })), 2500);
    }
  };
  return { states, run };
}

function ExportSection({
  jobId,
  sourceName,
  output,
  outputs,
  consistency,
  onReview,
  embedded,
}: {
  jobId: string;
  sourceName: string;
  output: OutputSummary;
  outputs: OutputSummary[];
  consistency: JobResults["consistency"];
  onReview: () => void;
  /** Inside the results card's Export tab: no card or heading of its own. */
  embedded?: boolean;
}) {
  const { states, run } = useDownload();
  const renamed = output.renamed_columns;
  const issues = consistency?.[output.id]?.issues ?? 0;
  const failedTables = outputs.filter((item) => (consistency?.[item.id]?.issues ?? 0) > 0);
  const [pending, setPending] = useState<null | { key: string; url: string; label: string; tables: OutputSummary[] }>(null);

  // Loading data that failed a consistency check is a deliberate choice, never a slip.
  const guarded = (key: string, url: string, label: string, tables: OutputSummary[]) => {
    if (tables.length) setPending({ key, url, label, tables });
    else void run(key, url, label);
  };
  return (
    <section className={embedded ? undefined : "card p-5 md:p-6"} aria-labelledby={embedded ? undefined : "export-title"} aria-label={embedded ? "Export" : undefined}>
      <div className="flex flex-col gap-5 xl:flex-row xl:items-start">
        <div className={clsx("flex items-start gap-3 xl:w-[320px] xl:shrink-0", embedded && "hidden")}>
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-ink-200 bg-ink-50 text-ink-600" aria-hidden>
            <Download className="h-5 w-5" />
          </div>
          <div>
            <h2 id="export-title" className="text-section text-ink-900">Export</h2>
            <p className="truncate text-caption text-ink-500" title={sourceName}>{sourceName}</p>
          </div>
        </div>

        <div className="min-w-0 flex-1 space-y-3">
        {issues > 0 && (
          <Alert tone="error" title={`${plural(issues, "check")} failed`} action={<Button size="sm" onClick={onReview}>Review</Button>} />
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <ExportCard
            icon={<FileSpreadsheet />}
            title="Cleaned CSV"
            description={
              <>
                <strong className="text-ink-800">{output.name}</strong> · {formatNumber(output.rows)} × {output.columns}
                {renamed ? ` · ${renamed} renamed` : ""}
              </>
            }
            action={
              <Button variant="primary" icon={<Download />} state={states.csv} loadingText="Preparing…" successText="Downloaded" errorText="Failed — retry" onClick={() => guarded("csv", exportUrls.csv(jobId, output.id), "CSV", issues ? [output] : [])}>
                CSV
              </Button>
            }
          />
          <ExportCard
            icon={<Database />}
            title="Metadata"
            description="Types, counts and stats per column."
            action={
              <Button icon={<Download />} state={states.metadata} loadingText="Preparing…" successText="Downloaded" errorText="Failed — retry" onClick={() => run("metadata", exportUrls.metadata(jobId, output.id), "Metadata")}>
                Metadata
              </Button>
            }
          />
          <ExportCard
            icon={<FileJson />}
            title="Audit report"
            description="Every removed row and decision."
            action={
              <Button icon={<Download />} state={states.audit} loadingText="Preparing…" successText="Downloaded" errorText="Failed — retry" onClick={() => run("audit", exportUrls.audit(jobId), "Audit report")}>
                Audit
              </Button>
            }
          />
          <ExportCard
            icon={<Archive />}
            title="Full package"
            description={`${plural(outputs.length, "table")} · metadata · audit (.zip)`}
            action={
              <Button icon={<Archive />} state={states.zip} loadingText="Packaging…" successText="Downloaded" errorText="Failed — retry" onClick={() => guarded("zip", exportUrls.zip(jobId), "Package", failedTables)}>
                ZIP
              </Button>
            }
          />
        </div>
      </div>
      </div>
      <Modal
        open={pending !== null}
        onClose={() => setPending(null)}
        title="Download with failed checks?"
        description="These tables may contain missing or misplaced rows."
        footer={
          <>
            <Button onClick={() => { setPending(null); onReview(); }}>Review</Button>
            <Button
              variant="primary"
              icon={<Download />}
              onClick={() => {
                const next = pending;
                setPending(null);
                if (next) void run(next.key, next.url, next.label);
              }}
            >
              Download anyway
            </Button>
          </>
        }
      >
        <ul className="space-y-1.5">
          {pending?.tables.map((table) => (
            <li key={table.id} className="flex items-center justify-between gap-3 rounded-md bg-danger-50 px-3 py-2 text-body">
              <span className="truncate font-medium text-ink-900">{table.name}</span>
              <Badge tone="danger">{plural(consistency?.[table.id]?.issues ?? 0, "issue")}</Badge>
            </li>
          ))}
        </ul>
      </Modal>
    </section>
  );
}

function ExportCard({ icon, title, description, action }: { icon: React.ReactNode; title: string; description: React.ReactNode; action: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col justify-between gap-3 rounded-lg border border-ink-200 p-4">
      <div className="flex gap-3">
        <span className="mt-0.5 text-ink-500 [&>svg]:h-5 [&>svg]:w-5" aria-hidden>{icon}</span>
        <div className="min-w-0 [overflow-wrap:anywhere]">
          <p className="text-body font-semibold text-ink-900">{title}</p>
          <p className="text-caption text-ink-600">{description}</p>
        </div>
      </div>
      <div>{action}</div>
    </div>
  );
}

/** One zip for every cleaned file, a folder per file. */
function BatchExport({ batch, embedded }: { batch: BatchStatus; embedded?: boolean }) {
  const flow = useWorkflow();
  const { states, run } = useDownload();
  const [confirm, setConfirm] = useState(false);
  const succeeded = batch.jobs.filter((job) => job.status === "succeeded");
  const failing = succeeded.flatMap((job) => {
    const results = flow.results[job.id];
    if (!results) return [];
    return results.outputs
      .filter((output) => (results.consistency?.[output.id]?.issues ?? 0) > 0)
      .map((output) => ({ key: `${job.id}:${output.id}`, file: job.source_name, table: output.name, issues: results.consistency[output.id].issues }));
  });
  const skipped = batch.files_total - succeeded.length;
  const go = () => void run("batch", exportUrls.batchZip(batch.id), "All files");

  return (
    <section
      className={clsx("flex flex-col gap-4 md:flex-row md:items-center", embedded ? "rounded-lg border border-ink-200 p-4" : "card p-5 md:p-6")}
      aria-labelledby="batch-export-title"
    >
      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-ink-200 bg-ink-50 text-ink-600" aria-hidden>
        <Archive className="h-5 w-5" />
      </div>
      <div className="min-w-0 flex-1">
        <h2 id="batch-export-title" className="text-card text-ink-900">All files (.zip)</h2>
        <p className="num text-caption text-ink-500">
          {plural(succeeded.length, "file")}, one folder each{skipped > 0 && ` · ${skipped} skipped`}
        </p>
      </div>
      <Button
        variant="primary"
        icon={<Archive />}
        state={states.batch}
        loadingText="Packaging…"
        successText="Downloaded"
        errorText="Failed — retry"
        disabled={!succeeded.length}
        onClick={() => (failing.length ? setConfirm(true) : go())}
      >
        Download all
      </Button>
      <Modal
        open={confirm}
        onClose={() => setConfirm(false)}
        title="Download with failed checks?"
        description="These tables may contain missing or misplaced rows."
        footer={
          <>
            <Button onClick={() => setConfirm(false)}>Cancel</Button>
            <Button
              variant="primary"
              icon={<Download />}
              onClick={() => {
                setConfirm(false);
                go();
              }}
            >
              Download anyway
            </Button>
          </>
        }
      >
        <ul className="space-y-1.5">
          {failing.map((item) => (
            <li key={item.key} className="flex items-center justify-between gap-3 rounded-md bg-danger-50 px-3 py-2 text-body">
              <span className="min-w-0">
                <span className="block truncate font-medium text-ink-900">{item.table}</span>
                <span className="block truncate text-caption text-ink-600">{item.file}</span>
              </span>
              <Badge tone="danger">{plural(item.issues, "issue")}</Badge>
            </li>
          ))}
        </ul>
      </Modal>
    </section>
  );
}

/* -------------------------------------------------------------------------- */

function ResultsSkeleton() {
  return (
    <div className="space-y-4 p-6" role="status" aria-label="Loading results">
      <Skeleton className="h-6 w-64" />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-16" />
        ))}
      </div>
      <Skeleton className="h-10 w-80" />
      <Skeleton className="h-72" />
    </div>
  );
}

function ProfileSkeleton() {
  return (
    <div className="space-y-2" role="status" aria-label="Loading column profile">
      {Array.from({ length: 8 }).map((_, i) => (
        <Skeleton key={i} className="h-8" />
      ))}
    </div>
  );
}
