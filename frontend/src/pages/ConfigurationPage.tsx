import clsx from "clsx";
import {
  ArrowRight,
  CheckCircle2,
  EyeOff,
  FileSpreadsheet,
  FolderOpen,
  BetweenHorizontalEnd,
  Info,
  Layers,
  ListChecks,
  RefreshCw,
  Sheet,
  Table2,
  TriangleAlert,
  Upload,
  X,
} from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { ApiError, uploadWorkbook } from "../api/client";
import type { Workbook } from "../api/types";
import { PageHeader, SectionCard } from "../components/layout/Layout";
import { SectionNav } from "../components/SectionNav";
import { AppendSection } from "./AppendSection";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Checkbox, SearchInput } from "../components/ui/Controls";
import { Alert, EmptyState, ProgressBar, StatTile, useToast } from "../components/ui/Feedback";
import { Tooltip } from "../components/ui/Overlay";
import { formatBytes, formatNumber, plural } from "../lib/format";
import { useNavigate, useWorkflow } from "../state/workflow";

const ACCEPT = [".xlsx", ".xlsm", ".csv", ".tsv", ".txt"];
const MAX_MB = 500;

type UploadPhase =
  | { kind: "idle" }
  | { kind: "uploading"; name: string; size: number; fraction: number }
  | { kind: "processing"; name: string; size: number }
  | { kind: "error"; name?: string; message: string; advice?: string | null };

export function ConfigurationPage() {
  const flow = useWorkflow();
  const navigate = useNavigate();

  return (
    <>
      <PageHeader
        page="configuration"
        title="Configure data"
        description="Upload an Excel workbook, choose the sheets to clean, and decide how matching sheets are combined."
      />
      <SectionNav
        label="Configuration sections"
        sections={[
          { id: "section-upload", label: "Upload workbook", shortLabel: "Upload", icon: <FileSpreadsheet />, done: Boolean(flow.workbook) },
          { id: "section-sheets", label: "Sheet selection", shortLabel: "Sheets", icon: <Table2 />, done: flow.selected.length > 0 },
          {
            id: "section-append",
            label: "Append matching sheets",
            shortLabel: "Append",
            icon: <BetweenHorizontalEnd />,
            done: Boolean(flow.workbook && flow.selected.length),
          },
        ]}
      />
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0 space-y-6">
          <UploadSection />
          <SheetSection />
          <AppendSection />
        </div>
        <aside className="xl:sticky xl:top-[136px] xl:self-start">
          <SummaryPanel onContinue={() => navigate("run")} />
        </aside>
      </div>
      {!flow.hydrated && <span className="sr-only">Loading workspace</span>}
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* 1. Upload                                                                   */
/* -------------------------------------------------------------------------- */

function UploadSection() {
  const flow = useWorkflow();
  const toast = useToast();
  const input = useRef<HTMLInputElement>(null);
  const abort = useRef<AbortController | null>(null);
  const [phase, setPhase] = useState<UploadPhase>({ kind: "idle" });
  const [dragging, setDragging] = useState(false);

  const busy = phase.kind === "uploading" || phase.kind === "processing";

  const start = async (file: File) => {
    const suffix = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
    if (suffix === ".xls" || suffix === ".xlsb" || suffix === ".ods") {
      setPhase({ kind: "error", name: file.name, message: `${file.name} is in a format the cleaner can't open.`, advice: "Open it in Excel and re-save it as .xlsx, then upload again." });
      return;
    }
    if (!ACCEPT.includes(suffix)) {
      setPhase({ kind: "error", name: file.name, message: `${file.name} is not a supported file type.`, advice: "Upload an .xlsx or .xlsm workbook, or a .csv / .tsv file." });
      return;
    }
    if (file.size > MAX_MB * 1024 * 1024) {
      setPhase({ kind: "error", name: file.name, message: `${file.name} is larger than ${MAX_MB} MB.`, advice: "Remove sheets you don't need, or split the workbook, and try again." });
      return;
    }
    if (file.size === 0) {
      setPhase({ kind: "error", name: file.name, message: `${file.name} is empty.`, advice: "Check the file and upload it again." });
      return;
    }
    abort.current = new AbortController();
    setPhase({ kind: "uploading", name: file.name, size: file.size, fraction: 0 });
    try {
      const workbook = await uploadWorkbook(
        file,
        (fraction) =>
          setPhase((current) =>
            current.kind === "uploading"
              ? fraction >= 1
                ? { kind: "processing", name: file.name, size: file.size }
                : { ...current, fraction }
              : current,
          ),
        abort.current.signal,
      );
      flow.setWorkbook(workbook);
      setPhase({ kind: "idle" });
      toast({ severity: "success", title: "Workbook uploaded", description: `${workbook.filename} · ${plural(workbook.sheets.length, "sheet")} found` });
    } catch (error) {
      if (error instanceof ApiError && error.body.code === "aborted") {
        setPhase({ kind: "idle" });
        return;
      }
      const body = error instanceof ApiError ? error.body : { message: String(error), advice: null };
      setPhase({ kind: "error", name: file.name, message: body.message, advice: body.advice });
    }
  };

  const onFiles = (files: FileList | null) => {
    if (!files?.length) return;
    if (files.length > 1) toast({ severity: "info", title: "One workbook at a time", description: `Using ${files[0].name}.` });
    void start(files[0]);
  };

  const browse = () => input.current?.click();

  return (
    <SectionCard
      id="section-upload"
      step={1}
      icon={<FileSpreadsheet />}
      title="Upload workbook"
      description="Sheets are listed as soon as the file is read. Row and column counts are measured by the cleaner, after junk rows are removed."
      actions={<span className="hidden rounded-full bg-ink-100 px-3 py-1 text-caption text-ink-600 md:inline">.xlsx · .xlsm · .csv · up to {MAX_MB} MB</span>}
    >
      <input
        ref={input}
        type="file"
        accept={ACCEPT.join(",")}
        className="sr-only"
        tabIndex={-1}
        aria-hidden
        onChange={(event) => {
          onFiles(event.target.files);
          event.target.value = "";
        }}
      />

      {flow.workbook && !busy && phase.kind !== "error" ? (
        <LoadedWorkbook workbook={flow.workbook} onReplace={browse} disabled={flow.running} />
      ) : (
        <div
          role="button"
          tabIndex={busy ? -1 : 0}
          aria-label="Upload an Excel workbook. Drop a file here or press Enter to browse."
          aria-disabled={busy || undefined}
          onClick={() => !busy && browse()}
          onKeyDown={(event) => {
            if (!busy && (event.key === "Enter" || event.key === " ")) {
              event.preventDefault();
              browse();
            }
          }}
          onDragOver={(event) => {
            event.preventDefault();
            if (!busy) setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            if (!busy) onFiles(event.dataTransfer.files);
          }}
          className={clsx(
            "flex flex-col gap-4 rounded-lg border-2 border-dashed px-5 py-6 transition-colors sm:flex-row sm:items-center md:px-6",
            "focus-visible:outline-none focus-visible:shadow-focus",
            dragging ? "border-brand-500 bg-brand-50" : phase.kind === "error" ? "border-brand-300 bg-brand-50/40" : "border-ink-300 bg-ink-50/60 hover:border-brand-300 hover:bg-brand-50/30",
            busy ? "cursor-default" : "cursor-pointer",
          )}
        >
          <div
            className={clsx(
              "flex h-12 w-12 shrink-0 items-center justify-center rounded-lg",
              phase.kind === "error" ? "bg-brand-100 text-brand-600" : "bg-white text-brand-600 ring-1 ring-ink-200",
            )}
            aria-hidden
          >
            {phase.kind === "error" ? <TriangleAlert className="h-5 w-5" /> : <Upload className="h-5 w-5" />}
          </div>

          <div className="min-w-0 flex-1" aria-live="polite">
            {phase.kind === "uploading" || phase.kind === "processing" ? (
              <>
                <div className="flex items-baseline justify-between gap-4">
                  <p className="truncate text-card text-ink-900">
                    {phase.kind === "uploading" ? "Uploading" : "Reading sheets in"} {phase.name}
                  </p>
                  <span className="num shrink-0 text-body font-medium text-ink-700">
                    {phase.kind === "uploading" ? `${Math.round(phase.fraction * 100)}%` : "Almost done"}
                  </span>
                </div>
                <ProgressBar
                  className="mt-2"
                  label="Upload progress"
                  value={phase.kind === "uploading" ? phase.fraction : 1}
                  active={phase.kind === "processing"}
                />
                <p className="num mt-1.5 text-caption text-ink-500">
                  {phase.kind === "uploading"
                    ? `${formatBytes(phase.size * phase.fraction)} of ${formatBytes(phase.size)}`
                    : "Checking the file and listing its sheets"}
                </p>
              </>
            ) : phase.kind === "error" ? (
              <>
                <p className="text-card text-brand-800">{phase.message}</p>
                {phase.advice && <p className="mt-0.5 text-body text-ink-700">{phase.advice}</p>}
              </>
            ) : (
              <>
                <p className="text-card text-ink-900">{dragging ? "Drop to upload" : "Drag and drop an Excel workbook here"}</p>
                <p className="mt-0.5 text-body text-ink-600">or browse from your device. Your source file is never modified.</p>
              </>
            )}
          </div>

          {phase.kind === "uploading" ? (
            <Button icon={<X />} onClick={(event) => (event.stopPropagation(), abort.current?.abort())}>
              Cancel
            </Button>
          ) : phase.kind === "processing" ? null : (
            <div className="flex flex-wrap gap-2">
              {phase.kind === "error" && flow.workbook && (
                <Button onClick={(event) => (event.stopPropagation(), setPhase({ kind: "idle" }))}>Keep current</Button>
              )}
              <Button variant="primary" icon={phase.kind === "error" ? <RefreshCw /> : <FolderOpen />} onClick={(event) => (event.stopPropagation(), browse())}>
                {phase.kind === "error" ? "Choose another file" : "Browse files"}
              </Button>
            </div>
          )}
        </div>
      )}
    </SectionCard>
  );
}

function LoadedWorkbook({ workbook, onReplace, disabled }: { workbook: Workbook; onReplace: () => void; disabled: boolean }) {
  return (
    <div className="flex animate-fade-in flex-col gap-4 rounded-lg border border-emerald-200 bg-emerald-50/50 px-5 py-4 sm:flex-row sm:items-center">
      <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg bg-emerald-600 text-white" aria-hidden>
        <FileSpreadsheet className="h-5 w-5" />
      </div>
      <div className="min-w-0 flex-1">
        <p className="flex items-center gap-1.5 text-caption font-medium text-emerald-700">
          <CheckCircle2 className="h-3.5 w-3.5" aria-hidden /> Workbook loaded
        </p>
        <p className="truncate text-card text-ink-900" title={workbook.filename}>
          {workbook.filename}
        </p>
        <p className="num text-caption text-ink-600">
          {formatBytes(workbook.size)} · {plural(workbook.sheets.length, "sheet")} detected
        </p>
      </div>
      <Tooltip content={disabled ? "Wait for the running job to finish before replacing the workbook." : null}>
        <Button icon={<RefreshCw />} onClick={onReplace} disabled={disabled}>
          Replace file
        </Button>
      </Tooltip>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 2. Sheets                                                                   */
/* -------------------------------------------------------------------------- */

function SheetSection() {
  const flow = useWorkflow();
  const [query, setQuery] = useState("");
  const workbook = flow.workbook;
  const selected = new Set(flow.selected);

  // The cleaner's measured size, known once a run has cleaned the sheet.
  const measured = useMemo(() => {
    const map = new Map<string, { rows: number; columns: number; tables: number }>();
    if (!flow.results || flow.results.job.workbook_id !== workbook?.id) return map;
    for (const sheet of flow.results.sheets) {
      map.set(sheet.sheet, {
        rows: sheet.rows,
        columns: Math.max(0, ...sheet.tables.map((table) => table.columns)),
        tables: sheet.tables.length,
      });
    }
    return map;
  }, [flow.results, workbook?.id]);

  if (!workbook) {
    return (
      <SectionCard id="section-sheets" step={2} icon={<Table2 />} title="Sheet selection" description="Choose one or more sheets to clean.">
        <div className="rounded-lg border border-dashed border-ink-200">
          <EmptyState
            compact
            icon={<Sheet />}
            title="No workbook uploaded yet"
            description="Upload an Excel workbook to see its sheets and choose which ones to clean."
          />
        </div>
      </SectionCard>
    );
  }

  const sheets = workbook.sheets;
  const visible = sheets.filter((sheet) => sheet.name.toLowerCase().includes(query.trim().toLowerCase()));
  const attention = sheets.filter((sheet) => sheet.hidden || !sheet.has_content).length;
  const allVisibleSelected = visible.length > 0 && visible.every((sheet) => selected.has(sheet.name));
  const someVisibleSelected = visible.some((sheet) => selected.has(sheet.name));
  const locked = flow.running;

  const toggle = (name: string, on: boolean) =>
    flow.setSelected(on ? sheets.map((s) => s.name).filter((n) => n === name || selected.has(n)) : flow.selected.filter((n) => n !== name));

  const setVisible = (on: boolean) => {
    const names = new Set(visible.map((sheet) => sheet.name));
    flow.setSelected(
      on ? sheets.map((s) => s.name).filter((n) => names.has(n) || selected.has(n)) : flow.selected.filter((n) => !names.has(n)),
    );
  };

  return (
    <SectionCard
      id="section-sheets"
      step={2}
      icon={<Table2 />}
      title="Sheet selection"
      description="Choose the sheets to clean. Select all applies to every sheet that matches the search."
      actions={sheets.length > 6 ? <SearchInput value={query} onChange={setQuery} placeholder="Search sheets…" label="Search sheets" className="w-full sm:w-60" /> : undefined}
    >
      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <StatTile icon={<Layers />} label="Sheets detected" value={formatNumber(sheets.length)} tone="info" />
        <StatTile icon={<ListChecks />} label="Selected for cleaning" value={`${selected.size} of ${sheets.length}`} tone="brand" />
        <StatTile
          icon={attention ? <TriangleAlert /> : <CheckCircle2 />}
          label={attention ? "Hidden or empty sheets" : "All sheets have data"}
          value={attention ? formatNumber(attention) : "None"}
          tone={attention ? "warning" : "success"}
        />
      </div>

      {locked && (
        <Alert tone="info" className="mb-3">
          Selection is locked while a cleaning job is running.
        </Alert>
      )}

      <div className="overflow-hidden rounded-lg border border-ink-200">
        <div className="max-h-[420px] overflow-auto scroll-thin">
          <table className="w-full min-w-[560px] border-collapse text-table">
            <caption className="sr-only">Sheets in {workbook.filename}</caption>
            <thead className="sticky top-0 z-10 bg-ink-50">
              <tr className="border-b border-ink-200 text-left text-caption font-semibold text-ink-600">
                <th scope="col" className="w-12 px-4 py-2.5">
                  <Checkbox
                    label={allVisibleSelected ? "Clear all sheets" : "Select all sheets"}
                    checked={allVisibleSelected}
                    indeterminate={!allVisibleSelected && someVisibleSelected}
                    onChange={setVisible}
                    disabled={locked || !visible.length}
                  />
                </th>
                <th scope="col" className="px-2 py-2.5">Sheet name</th>
                <th scope="col" className="px-4 py-2.5 text-right">
                  <span className="inline-flex items-center gap-1">
                    Cleaned rows
                    <Tooltip content="Measured by the cleaner after titles, subtotals, blank and repeated-header rows are removed. Shown after a run.">
                      <Info className="h-3.5 w-3.5 text-ink-400" aria-label="About cleaned rows" tabIndex={0} />
                    </Tooltip>
                  </span>
                </th>
                <th scope="col" className="px-4 py-2.5 text-right">Columns</th>
                <th scope="col" className="px-4 py-2.5">Status</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((sheet) => {
                const on = selected.has(sheet.name);
                const size = measured.get(sheet.name);
                return (
                  <tr
                    key={sheet.name}
                    onClick={() => !locked && toggle(sheet.name, !on)}
                    className={clsx(
                      "border-b border-ink-100 transition-colors last:border-0",
                      locked ? "cursor-not-allowed" : "cursor-pointer",
                      on ? "bg-brand-50/50 hover:bg-brand-50" : "hover:bg-ink-50",
                    )}
                  >
                    <td className="px-4 py-2.5" onClick={(event) => event.stopPropagation()}>
                      <Checkbox label={`Select ${sheet.name}`} checked={on} onChange={(value) => toggle(sheet.name, value)} disabled={locked} />
                    </td>
                    <td className="px-2 py-2.5">
                      <span className="flex items-center gap-2.5">
                        <Sheet className={clsx("h-4 w-4 shrink-0", on ? "text-brand-600" : "text-ink-400")} aria-hidden />
                        <span className={clsx("truncate", on ? "font-semibold text-ink-900" : "font-medium text-ink-700")}>{sheet.name}</span>
                      </span>
                    </td>
                    <td className="num px-4 py-2.5 text-right text-ink-700">
                      {size ? formatNumber(size.rows) : <span className="text-ink-400">After cleaning</span>}
                    </td>
                    <td className="num px-4 py-2.5 text-right text-ink-700">
                      {size ? formatNumber(size.columns) : <span className="text-ink-400">—</span>}
                    </td>
                    <td className="px-4 py-2.5">
                      {sheet.hidden ? (
                        <Badge tone="info" icon={<EyeOff />}>Hidden sheet</Badge>
                      ) : !sheet.has_content ? (
                        <Badge tone="warning" icon={<TriangleAlert />}>No data found</Badge>
                      ) : size && size.tables === 0 ? (
                        <Badge tone="warning" icon={<TriangleAlert />}>No table found</Badge>
                      ) : size ? (
                        <Badge tone="success" icon={<CheckCircle2 />}>Cleaned</Badge>
                      ) : (
                        <Badge tone="neutral" dot>Ready to clean</Badge>
                      )}
                    </td>
                  </tr>
                );
              })}
              {!visible.length && (
                <tr>
                  <td colSpan={5}>
                    <EmptyState compact icon={<Sheet />} title="No sheets match your search" description={`Nothing matches “${query}”.`} action={<Button size="sm" onClick={() => setQuery("")}>Clear search</Button>} />
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
      {selected.size === 0 && (
        <p className="mt-3 flex items-center gap-1.5 text-caption text-amber-700" role="status">
          <TriangleAlert className="h-3.5 w-3.5" aria-hidden /> Select at least one sheet to continue.
        </p>
      )}
    </SectionCard>
  );
}

/* -------------------------------------------------------------------------- */
/* Summary                                                                     */
/* -------------------------------------------------------------------------- */

function SummaryPanel({ onContinue }: { onContinue: () => void }) {
  const flow = useWorkflow();
  const workbook = flow.workbook;
  const total = workbook?.sheets.length ?? 0;
  const reason = flow.runBlockedReason;

  const rows: { label: string; value: React.ReactNode; ok: boolean }[] = [
    { label: "Workbook", value: workbook ? <span className="block truncate" title={workbook.filename}>{workbook.filename}</span> : "Not uploaded", ok: Boolean(workbook) },
    { label: "Sheets selected", value: workbook ? `${flow.selected.length} of ${total}` : "—", ok: flow.selected.length > 0 },
    { label: "Append mode", value: appendSummary(flow), ok: true },
    { label: "Output", value: "Cleaned CSV per table, column metadata and an audit report", ok: true },
  ];

  return (
    <section className="card p-5" aria-labelledby="summary-title">
      <h2 id="summary-title" className="text-card text-ink-900">Configuration summary</h2>
      <p className="mt-0.5 text-caption text-ink-500">Check these before you run.</p>
      <dl className="mt-4 space-y-3">
        {rows.map((row) => (
          <div key={row.label} className="flex gap-3">
            <span
              className={clsx("mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full", row.ok ? "bg-emerald-100 text-emerald-600" : "bg-ink-100 text-ink-400")}
              aria-hidden
            >
              {row.ok ? <CheckCircle2 className="h-3 w-3" /> : <span className="h-1.5 w-1.5 rounded-full bg-ink-400" />}
            </span>
            <div className="min-w-0 flex-1">
              <dt className="text-caption text-ink-500">{row.label}</dt>
              <dd className={clsx("text-body", row.ok ? "font-medium text-ink-900" : "text-ink-500")}>{row.value}</dd>
            </div>
          </div>
        ))}
      </dl>
      {flow.stale && flow.job?.status === "succeeded" && (
        <Alert tone="warning" className="mt-4">
          The configuration changed since the last run. Run again to update the results.
        </Alert>
      )}
      <div className="mt-5 border-t border-ink-200 pt-4">
        <Tooltip content={reason} className="w-full">
          <Button variant="primary" size="lg" className="w-full" iconRight={<ArrowRight />} disabled={Boolean(reason)} onClick={onContinue}>
            Continue to Run
          </Button>
        </Tooltip>
        {reason && (
          <p className="mt-2 text-center text-caption text-ink-500" id="continue-reason">
            {reason}
          </p>
        )}
      </div>
    </section>
  );
}

function appendSummary(flow: ReturnType<typeof useWorkflow>): string {
  if (!flow.workbook) return "—";
  if (!flow.appendAvailable) return "Not applicable — needs 2+ sheets";
  if (!flow.append) return "Keep sheets separate — one table per sheet";
  return "Auto-detect & append — matching sheets combine during the run";
}
