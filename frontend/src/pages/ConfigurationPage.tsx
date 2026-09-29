import clsx from "clsx";
import {
  ArrowRight,
  BetweenHorizontalEnd,
  CheckCircle2,
  EyeOff,
  FileSpreadsheet,
  FilePlus2,
  FolderOpen,
  Info,
  Layers,
  ListChecks,
  RefreshCw,
  Sheet,
  Table2,
  Trash2,
  TriangleAlert,
  Upload,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, uploadWorkbook } from "../api/client";
import { PageHeader, SectionCard } from "../components/layout/Layout";
import { SectionNav } from "../components/SectionNav";
import { AppendModeOptions } from "./AppendSection";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Checkbox, SearchInput } from "../components/ui/Controls";
import { Alert, EmptyState, ProgressBar, StatTile, useToast } from "../components/ui/Feedback";
import { Modal, Tooltip } from "../components/ui/Overlay";
import { Select } from "../components/ui/Select";
import { formatBytes, formatNumber, plural } from "../lib/format";
import { effectiveAppend, MAX_FILES, useNavigate, useWorkflow, type FileEntry } from "../state/workflow";

const ACCEPT = [".xlsx", ".csv", ".tsv", ".txt"];
const MAX_MB = 500;
/** Uploads sent at the same time; the rest wait their turn. */
const PARALLEL_UPLOADS = 3;

interface QueueItem {
  key: string;
  file: File;
  phase: "waiting" | "uploading" | "processing" | "error";
  fraction: number;
  message?: string;
  advice?: string | null;
}

export function ConfigurationPage() {
  const flow = useWorkflow();
  const navigate = useNavigate();
  const allSelected = flow.files.length > 0 && flow.files.every((entry) => entry.selected.length > 0);

  return (
    <>
      <PageHeader
        page="configuration"
        title="Configure data"
        description="Upload one or more Excel workbooks, choose the sheets to clean in each, and decide how matching sheets are combined."
      />
      <SectionNav
        label="Configuration sections"
        sections={[
          { id: "section-upload", label: "Upload workbooks", shortLabel: "Upload", icon: <FileSpreadsheet />, done: flow.files.length > 0 },
          { id: "section-sheets", label: "Sheet selection", shortLabel: "Sheets", icon: <Table2 />, done: allSelected },
          { id: "section-append", label: "Append matching sheets", shortLabel: "Append", icon: <BetweenHorizontalEnd />, done: allSelected },
        ]}
      />
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0 space-y-6">
          <UploadSection />
          <SheetSection />
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

function rejectReason(file: File): { message: string; advice: string } | null {
  const suffix = file.name.includes(".") ? file.name.slice(file.name.lastIndexOf(".")).toLowerCase() : "";
  if (suffix === ".xls" || suffix === ".xlsb" || suffix === ".ods")
    return { message: `${file.name} is in a format the cleaner can't open.`, advice: "Open it in Excel and re-save it as .xlsx, then upload again." };
  if (!ACCEPT.includes(suffix)) return { message: `${file.name} is not a supported file type.`, advice: "Upload an .xlsx workbook, or a .csv / .tsv file." };
  if (file.size > MAX_MB * 1024 * 1024)
    return { message: `${file.name} is larger than ${MAX_MB} MB.`, advice: "Remove sheets you don't need, or split the workbook, and try again." };
  if (file.size === 0) return { message: `${file.name} is empty.`, advice: "Check the file and upload it again." };
  return null;
}

function UploadSection() {
  const flow = useWorkflow();
  const toast = useToast();
  const input = useRef<HTMLInputElement>(null);
  const controllers = useRef(new Map<string, AbortController>());
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const locked = flow.running;

  const patch = (key: string, change: Partial<QueueItem>) =>
    setQueue((items) => items.map((item) => (item.key === key ? { ...item, ...change } : item)));
  const drop = (key: string) => setQueue((items) => items.filter((item) => item.key !== key));
  // A transferring upload is aborted (and dropped by its own error path); a waiting one just leaves the queue.
  const cancel = (key: string) => {
    const controller = controllers.current.get(key);
    if (controller) controller.abort();
    else drop(key);
  };

  const inFlight = queue.filter((item) => item.phase !== "error").length;
  useEffect(() => flow.setUploading(inFlight), [inFlight, flow.setUploading]);

  // Start waiting uploads while there is a free slot.
  useEffect(() => {
    const active = queue.filter((item) => item.phase === "uploading" || item.phase === "processing").length;
    const next = queue.filter((item) => item.phase === "waiting").slice(0, Math.max(0, PARALLEL_UPLOADS - active));
    for (const item of next) void send(item);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queue]);

  // Abort anything still transferring if the page goes away, so nothing blocks the run.
  useEffect(
    () => () => {
      controllers.current.forEach((controller) => controller.abort());
      flow.setUploading(0);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const send = async (item: QueueItem) => {
    const controller = new AbortController();
    controllers.current.set(item.key, controller);
    patch(item.key, { phase: "uploading", fraction: 0 });
    try {
      const workbook = await uploadWorkbook(
        item.file,
        (fraction) => patch(item.key, fraction >= 1 ? { phase: "processing", fraction: 1 } : { fraction }),
        controller.signal,
      );
      drop(item.key);
      flow.addWorkbook(workbook);
    } catch (error) {
      if (error instanceof ApiError && error.body.code === "aborted") {
        drop(item.key);
        return;
      }
      const body = error instanceof ApiError ? error.body : { message: String(error), advice: null };
      patch(item.key, { phase: "error", message: body.message, advice: body.advice });
    } finally {
      controllers.current.delete(item.key);
    }
  };

  const onFiles = (list: FileList | null) => {
    if (!list?.length || locked) return;
    const known = new Set([
      ...flow.files.map((entry) => `${entry.workbook.filename}|${entry.workbook.size}`),
      ...queue.filter((item) => item.phase !== "error").map((item) => `${item.file.name}|${item.file.size}`),
    ]);
    let room = MAX_FILES - flow.files.length - inFlight;
    const added: QueueItem[] = [];
    const duplicates: string[] = [];
    let overflow = 0;
    for (const file of Array.from(list)) {
      const id = `${file.name}|${file.size}`;
      if (known.has(id)) {
        duplicates.push(file.name);
        continue;
      }
      const key = `${id}|${Date.now()}|${Math.random().toString(36).slice(2, 8)}`;
      const rejected = rejectReason(file);
      if (rejected) {
        added.push({ key, file, phase: "error", fraction: 0, ...rejected });
        continue;
      }
      if (room <= 0) {
        overflow += 1;
        continue;
      }
      room -= 1;
      known.add(id);
      added.push({ key, file, phase: "waiting", fraction: 0 });
    }
    if (added.length) setQueue((items) => [...items, ...added]);
    if (duplicates.length)
      toast({ severity: "info", title: duplicates.length === 1 ? "File already added" : "Some files were already added", description: duplicates.join(", ") });
    if (overflow)
      toast({ severity: "warning", title: `Up to ${MAX_FILES} files per run`, description: `${plural(overflow, "file")} ${overflow === 1 ? "was" : "were"} not added.` });
  };

  const browse = () => !locked && input.current?.click();
  const hasContent = flow.files.length > 0 || queue.length > 0;

  const dropHandlers = {
    onDragOver: (event: React.DragEvent) => {
      event.preventDefault();
      if (!locked) setDragging(true);
    },
    onDragLeave: () => setDragging(false),
    onDrop: (event: React.DragEvent) => {
      event.preventDefault();
      setDragging(false);
      onFiles(event.dataTransfer.files);
    },
  };

  return (
    <SectionCard
      id="section-upload"
      step={1}
      icon={<FileSpreadsheet />}
      title="Upload workbooks"
      description="Add one or more files. Each file keeps its own sheet selection and append mode, and is cleaned on its own."
      actions={
        <span className="hidden rounded-full bg-ink-100 px-3 py-1 text-caption text-ink-600 md:inline">
          .xlsx · .csv · up to {MAX_MB} MB each · max {MAX_FILES} files
        </span>
      }
    >
      <input
        ref={input}
        type="file"
        multiple
        accept={ACCEPT.join(",")}
        className="sr-only"
        tabIndex={-1}
        aria-hidden
        onChange={(event) => {
          onFiles(event.target.files);
          event.target.value = "";
        }}
      />

      {hasContent ? (
        <div className="space-y-3">
          {flow.files.length > 0 && (
            <ul className="divide-y divide-ink-100 overflow-hidden rounded-lg border border-ink-200" aria-label="Uploaded files">
              {flow.files.map((entry) => (
                <LoadedFileRow key={entry.workbook.id} entry={entry} />
              ))}
            </ul>
          )}
          {queue.length > 0 && (
            <ul className="space-y-2" aria-label="Uploads in progress" aria-live="polite">
              {queue.map((item) => (
                <QueueRow
                  key={item.key}
                  item={item}
                  onCancel={() => cancel(item.key)}
                  onDismiss={() => drop(item.key)}
                  onRetry={() => patch(item.key, { phase: "waiting", fraction: 0, message: undefined, advice: undefined })}
                  canRetry={!rejectReason(item.file)}
                />
              ))}
            </ul>
          )}
          <div
            role="button"
            tabIndex={locked ? -1 : 0}
            aria-disabled={locked || undefined}
            aria-label="Add more files. Drop files here or press Enter to browse."
            onClick={browse}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                browse();
              }
            }}
            {...dropHandlers}
            className={clsx(
              "flex items-center justify-center gap-2 rounded-lg border-2 border-dashed px-4 py-3 text-body transition-colors",
              "focus-visible:outline-none focus-visible:shadow-focus",
              locked
                ? "cursor-not-allowed border-ink-200 text-ink-400"
                : dragging
                  ? "cursor-copy border-brand-500 bg-brand-50 text-brand-700"
                  : "cursor-pointer border-ink-300 text-ink-600 hover:border-brand-300 hover:bg-brand-50/30",
            )}
          >
            <FilePlus2 className="h-4 w-4" aria-hidden />
            {locked ? "Files can't be added while cleaning is running" : dragging ? "Drop to add" : "Drop more files here, or click to browse"}
          </div>
        </div>
      ) : (
        <div
          role="button"
          tabIndex={0}
          aria-label="Upload Excel workbooks. Drop files here or press Enter to browse."
          onClick={browse}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              browse();
            }
          }}
          {...dropHandlers}
          className={clsx(
            "flex cursor-pointer flex-col gap-4 rounded-lg border-2 border-dashed px-5 py-6 transition-colors sm:flex-row sm:items-center md:px-6",
            "focus-visible:outline-none focus-visible:shadow-focus",
            dragging ? "border-brand-500 bg-brand-50" : "border-ink-300 bg-ink-50/60 hover:border-brand-300 hover:bg-brand-50/30",
          )}
        >
          <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-lg bg-white text-brand-600 ring-1 ring-ink-200" aria-hidden>
            <Upload className="h-5 w-5" />
          </div>
          <div className="min-w-0 flex-1">
            <p className="text-card text-ink-900">{dragging ? "Drop to upload" : "Drag and drop Excel workbooks here"}</p>
            <p className="mt-0.5 text-body text-ink-600">or browse from your device. You can add several files at once; your source files are never modified.</p>
          </div>
          <Button variant="primary" icon={<FolderOpen />} onClick={(event) => (event.stopPropagation(), browse())}>
            Browse files
          </Button>
        </div>
      )}
    </SectionCard>
  );
}

function fileStatus(flow: ReturnType<typeof useWorkflow>, entry: FileEntry) {
  const job = flow.jobFor(entry.workbook.id);
  if (!job || (flow.stale && !flow.running)) return null;
  switch (job.status) {
    case "queued":
      return <Badge tone="neutral" dot>Queued</Badge>;
    case "running":
      return <Badge tone="info" dot>Cleaning · {Math.round(job.progress * 100)}%</Badge>;
    case "succeeded":
      return <Badge tone="success" icon={<CheckCircle2 />}>Cleaned</Badge>;
    case "failed":
      return <Badge tone="danger" icon={<TriangleAlert />}>Failed</Badge>;
    default:
      return <Badge tone="neutral">Cancelled</Badge>;
  }
}

function LoadedFileRow({ entry }: { entry: FileEntry }) {
  const flow = useWorkflow();
  const [confirm, setConfirm] = useState(false);
  const { workbook } = entry;
  const focused = flow.focused?.workbook.id === workbook.id;
  const none = entry.selected.length === 0;

  const focus = () => {
    flow.setFocusedId(workbook.id);
    document.getElementById("section-sheets")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <li className={clsx("flex animate-fade-in items-center gap-3 px-4 py-3", focused ? "bg-brand-50/40" : "bg-white")}>
      <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-emerald-600 text-white" aria-hidden>
        <FileSpreadsheet className="h-4 w-4" />
      </div>
      <button type="button" onClick={focus} className="group min-w-0 flex-1 rounded text-left focus-visible:outline-none focus-visible:shadow-focus">
        <span className="block truncate text-body font-semibold text-ink-900 group-hover:text-brand-700" title={workbook.filename}>
          {workbook.filename}
        </span>
        <span className="num block text-caption text-ink-600">
          {formatBytes(workbook.size)} · {plural(workbook.sheets.length, "sheet")} ·{" "}
          <span className={none ? "font-medium text-amber-700" : undefined}>
            {entry.selected.length} selected
          </span>
          {effectiveAppend(entry) ? " · auto-append" : ""}
        </span>
      </button>
      <div className="hidden shrink-0 sm:block">
        {fileStatus(flow, entry) ?? (none ? <Badge tone="warning" icon={<TriangleAlert />}>No sheets selected</Badge> : <Badge tone="neutral" dot>Ready</Badge>)}
      </div>
      <Tooltip content={flow.running ? "Wait for the running job to finish before removing files." : `Remove ${workbook.filename}`}>
        <button
          type="button"
          aria-label={`Remove ${workbook.filename}`}
          disabled={flow.running}
          onClick={() => setConfirm(true)}
          className="rounded p-1.5 text-ink-500 hover:bg-ink-100 hover:text-brand-700 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Trash2 className="h-4 w-4" />
        </button>
      </Tooltip>
      <Modal
        open={confirm}
        onClose={() => setConfirm(false)}
        title="Remove this file?"
        description={`${workbook.filename} will be removed from this workspace, along with its sheet selection. Files you already downloaded are not affected.`}
        footer={
          <>
            <Button onClick={() => setConfirm(false)}>Keep file</Button>
            <Button
              variant="primary"
              icon={<Trash2 />}
              onClick={async () => {
                setConfirm(false);
                await flow.removeWorkbook(workbook.id);
              }}
            >
              Remove
            </Button>
          </>
        }
      />
    </li>
  );
}

function QueueRow({
  item,
  onCancel,
  onDismiss,
  onRetry,
  canRetry,
}: {
  item: QueueItem;
  onCancel: () => void;
  onDismiss: () => void;
  onRetry: () => void;
  canRetry: boolean;
}) {
  const { file } = item;
  if (item.phase === "error") {
    return (
      <li className="flex animate-fade-in items-start gap-3 rounded-lg border border-brand-200 bg-brand-50/50 px-4 py-3" role="alert">
        <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-brand-600" aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="text-body font-medium text-brand-800">{item.message}</p>
          {item.advice && <p className="text-caption text-ink-700">{item.advice}</p>}
        </div>
        {canRetry && (
          <Button size="sm" icon={<RefreshCw />} onClick={onRetry}>
            Retry
          </Button>
        )}
        <button type="button" aria-label={`Dismiss error for ${file.name}`} onClick={onDismiss} className="rounded p-1 text-ink-500 hover:bg-brand-100">
          <X className="h-4 w-4" />
        </button>
      </li>
    );
  }
  const label = item.phase === "waiting" ? "Waiting" : item.phase === "uploading" ? "Uploading" : "Reading sheets in";
  return (
    <li className="flex animate-fade-in items-center gap-3 rounded-lg border border-ink-200 bg-ink-50/60 px-4 py-3">
      <Upload className="h-4 w-4 shrink-0 text-brand-600" aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <p className="truncate text-body text-ink-900">
            {label} <span className="font-medium">{file.name}</span>
          </p>
          <span className="num shrink-0 text-caption font-medium text-ink-700">
            {item.phase === "uploading" ? `${Math.round(item.fraction * 100)}%` : item.phase === "processing" ? "Almost done" : formatBytes(file.size)}
          </span>
        </div>
        <ProgressBar
          className="mt-1.5"
          label={`Upload progress for ${file.name}`}
          value={item.phase === "waiting" ? 0 : item.fraction}
          active={item.phase === "processing"}
        />
      </div>
      {item.phase !== "processing" && (
        <button type="button" aria-label={`Cancel upload of ${file.name}`} onClick={onCancel} className="rounded p-1 text-ink-500 hover:bg-ink-200">
          <X className="h-4 w-4" />
        </button>
      )}
    </li>
  );
}

/* -------------------------------------------------------------------------- */
/* 2. Sheets (per file) and append mode                                         */
/* -------------------------------------------------------------------------- */

function SheetSection() {
  const flow = useWorkflow();
  const [query, setQuery] = useState("");
  const entry = flow.focused;
  const workbook = entry?.workbook ?? null;
  const selected = new Set(entry?.selected ?? []);

  // The cleaner's measured size, known once a run has cleaned this file.
  const job = workbook ? flow.jobFor(workbook.id) : null;
  const results = job ? flow.results[job.id] : undefined;
  const measured = useMemo(() => {
    const map = new Map<string, { rows: number; columns: number; tables: number }>();
    for (const sheet of results?.sheets ?? []) {
      map.set(sheet.sheet, {
        rows: sheet.rows,
        columns: Math.max(0, ...sheet.tables.map((table) => table.columns)),
        tables: sheet.tables.length,
      });
    }
    return map;
  }, [results]);

  useEffect(() => setQuery(""), [workbook?.id]);

  if (!entry || !workbook) {
    return (
      <SectionCard id="section-sheets" step={2} icon={<Table2 />} title="Sheet selection" description="Choose the sheets to clean in each file.">
        <div id="section-append" className="rounded-lg border border-dashed border-ink-200">
          <EmptyState
            compact
            icon={<Sheet />}
            title="No workbook uploaded yet"
            description="Upload one or more Excel workbooks to see their sheets and choose which ones to clean."
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
  const setSelected = (names: string[]) => flow.setSelected(workbook.id, names);

  const toggle = (name: string, on: boolean) =>
    setSelected(on ? sheets.map((s) => s.name).filter((n) => n === name || selected.has(n)) : entry.selected.filter((n) => n !== name));

  const setVisible = (on: boolean) => {
    const names = new Set(visible.map((sheet) => sheet.name));
    setSelected(on ? sheets.map((s) => s.name).filter((n) => names.has(n) || selected.has(n)) : entry.selected.filter((n) => !names.has(n)));
  };

  const fileOptions = flow.files.map((item) => ({
    value: item.workbook.id,
    label: item.workbook.filename,
    icon: <FileSpreadsheet />,
    description: (
      <>
        {item.selected.length} of {plural(item.workbook.sheets.length, "sheet")} selected ·{" "}
        {effectiveAppend(item) ? "auto-append" : "sheets kept separate"}
      </>
    ),
    meta: item.selected.length === 0 ? <Badge tone="warning">None selected</Badge> : undefined,
  }));

  return (
    <SectionCard
      id="section-sheets"
      step={2}
      icon={<Table2 />}
      title="Sheet selection"
      description={
        flow.files.length > 1
          ? "Pick a file to see its sheets. Selections and append mode are kept per file."
          : "Choose the sheets to clean. Select all applies to every sheet that matches the search."
      }
    >
      <div className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-end">
        <Select
          value={workbook.id}
          options={fileOptions}
          onChange={flow.setFocusedId}
          label={flow.files.length > 1 ? `File (${flow.files.length})` : "File"}
          icon={<FileSpreadsheet />}
          className="w-full lg:flex-1"
          width={460}
        />
        {sheets.length > 6 && (
          <SearchInput value={query} onChange={setQuery} placeholder="Search sheets…" label="Search sheets" className="w-full lg:w-60" />
        )}
      </div>

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <StatTile icon={<Layers />} label="Sheets in this file" value={formatNumber(sheets.length)} tone="info" />
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
          <TriangleAlert className="h-3.5 w-3.5" aria-hidden /> Select at least one sheet in {workbook.filename} to continue, or remove the file.
        </p>
      )}

      <div className="mt-6 border-t border-ink-200 pt-5">
        <AppendModeOptions entry={entry} />
      </div>
    </SectionCard>
  );
}

/* -------------------------------------------------------------------------- */
/* Summary                                                                     */
/* -------------------------------------------------------------------------- */

function SummaryPanel({ onContinue }: { onContinue: () => void }) {
  const flow = useWorkflow();
  const reason = flow.runBlockedReason;
  const count = flow.files.length;
  const appending = flow.files.filter(effectiveAppend).length;
  const missing = flow.files.filter((entry) => entry.selected.length === 0);

  const rows: { label: string; value: React.ReactNode; ok: boolean }[] = [
    {
      label: count > 1 ? "Workbooks" : "Workbook",
      value: count ? (
        count === 1 ? (
          <span className="block truncate" title={flow.files[0].workbook.filename}>{flow.files[0].workbook.filename}</span>
        ) : (
          plural(count, "file")
        )
      ) : (
        "Not uploaded"
      ),
      ok: count > 0,
    },
    {
      label: "Sheets selected",
      value: count ? `${flow.totalSelected} of ${flow.totalSheets}${count > 1 ? ` across ${plural(count, "file")}` : ""}` : "—",
      ok: count > 0 && missing.length === 0,
    },
    {
      label: "Append mode",
      value: !count
        ? "—"
        : appending === 0
          ? "Sheets kept separate"
          : count === 1
            ? "Auto-detect & append — matching sheets combine during the run"
            : `Auto-detect & append in ${appending} of ${plural(count, "file")}`,
      ok: true,
    },
    { label: "Output", value: "Cleaned CSV per table, column metadata and an audit report — per file", ok: true },
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
      {missing.length > 0 && (
        <Alert tone="warning" className="mt-4" title={missing.length === 1 ? "A file has no sheets selected" : `${missing.length} files have no sheets selected`}>
          <ul className="mt-1 space-y-0.5">
            {missing.map((entry) => (
              <li key={entry.workbook.id}>
                <button type="button" className="truncate text-left underline-offset-2 hover:underline" onClick={() => flow.setFocusedId(entry.workbook.id)}>
                  {entry.workbook.filename}
                </button>
              </li>
            ))}
          </ul>
        </Alert>
      )}
      {flow.stale && flow.batch && !flow.running && (
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
