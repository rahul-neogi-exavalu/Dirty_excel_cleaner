import clsx from "clsx";
import {
  ArrowLeft,
  ArrowRight,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  Columns3,
  EyeOff,
  FileSpreadsheet,
  FileText,
  Info,
  Rows3,
  ScanEye,
  Sheet,
  TableCellsMerge,
  TriangleAlert,
  Upload,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { SheetInfo, SourcePreview } from "../api/types";
import { PageHeader } from "../components/layout/Layout";
import { Badge } from "../components/ui/Badge";
import { Button, IconButton } from "../components/ui/Button";
import { Checkbox } from "../components/ui/Controls";
import { Alert, EmptyState, Skeleton } from "../components/ui/Feedback";
import { Tooltip } from "../components/ui/Overlay";
import { Select } from "../components/ui/Select";
import { formatNumber, plural } from "../lib/format";
import { effectiveAppend, useNavigate, useWorkflow, type FileEntry } from "../state/workflow";

const ROW_SIZES = ["10", "20", "50", "100"] as const;
const COLUMN_SIZES = ["10", "20", "50", "100"] as const;
type RowSize = (typeof ROW_SIZES)[number];
type ColumnSize = (typeof COLUMN_SIZES)[number];

/** Excel's error values: shown as the file holds them, which the cleaner then empties. */
const ERRORS = new Set(["#VALUE!", "#REF!", "#DIV/0!", "#N/A", "#NAME?", "#NULL!", "#NUM!", "#SPILL!", "#CALC!", "#GETTING_DATA"]);
/** Number-like text in a CSV, which a spreadsheet would right-align as a number. */
const NUMBER_TEXT = /^[-+]?(\d{1,3}(,\d{3})+|\d+)?([.,]\d+)?%?$/;

/** Excel's column letters: 1 -> A, 27 -> AA. */
function columnLetter(index: number): string {
  let letters = "";
  for (let n = index; n > 0; n = Math.floor((n - 1) / 26)) letters = String.fromCharCode(65 + ((n - 1) % 26)) + letters;
  return letters;
}

/** "B3:D4" -> 1-based bounds. */
function bounds(range: string) {
  const match = /^([A-Z]+)(\d+):([A-Z]+)(\d+)$/.exec(range);
  if (!match) return null;
  const col = (letters: string) => [...letters].reduce((sum, ch) => sum * 26 + ch.charCodeAt(0) - 64, 0);
  return { minCol: col(match[1]), minRow: Number(match[2]), maxCol: col(match[3]), maxRow: Number(match[4]) };
}

/** The sheet to open for a file: the last one shown, else the first to be cleaned, else the first with data. */
function defaultSheet(entry: FileEntry, remembered: string | null): string | null {
  const sheets = entry.workbook.sheets;
  if (remembered) return remembered;
  const selected = new Set(entry.selected);
  return (
    sheets.find((sheet) => selected.has(sheet.name))?.name ??
    sheets.find((sheet) => sheet.has_content && !sheet.hidden)?.name ??
    sheets[0]?.name ??
    null
  );
}

export function PreviewPage() {
  const flow = useWorkflow();
  const navigate = useNavigate();
  const entry = flow.focused;
  const reason = flow.runBlockedReason;

  const header = (
    <PageHeader
      page="preview"
      title="Preview"
      description="See each uploaded sheet as it is, before cleaning."
      actions={
        entry && (
          <Tooltip content={reason}>
            <Button variant="primary" iconRight={<ArrowRight />} disabled={Boolean(reason)} onClick={() => navigate("run")}>
              Run
            </Button>
          </Tooltip>
        )
      }
    />
  );

  if (!entry) {
    return (
      <>
        {header}
        <div className="card">
          <EmptyState
            icon={<Upload />}
            title={flow.hydrated ? "No files to preview" : "Loading files"}
            description="Upload a workbook or CSV on the Configure page first."
            action={<Button variant="primary" icon={<ArrowLeft />} onClick={() => navigate("configuration")}>Configure</Button>}
          />
        </div>
      </>
    );
  }

  return (
    <>
      {header}
      <SourceViewer entry={entry} />
      {reason && (
        <p className="mt-3 flex items-center gap-1.5 text-caption text-amber-700" role="status">
          <TriangleAlert className="h-3.5 w-3.5 shrink-0" aria-hidden /> {reason}
        </p>
      )}
    </>
  );
}

/* -------------------------------------------------------------------------- */

function SourceViewer({ entry }: { entry: FileEntry }) {
  const flow = useWorkflow();
  const navigate = useNavigate();
  const { workbook } = entry;
  const sheetName = defaultSheet(entry, flow.previewSheetFor(workbook.id));
  const sheetInfo = workbook.sheets.find((sheet) => sheet.name === sheetName) ?? null;
  const selected = new Set(entry.selected);

  const [rowSize, setRowSize] = useState<RowSize>("20");
  const [columnSize, setColumnSize] = useState<ColumnSize>("20");
  const [offset, setOffset] = useState(0);
  const [colOffset, setColOffset] = useState(0);
  const [data, setData] = useState<SourcePreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [reload, setReload] = useState(0);
  const grid = useRef<HTMLDivElement>(null);
  const limit = Number(rowSize);
  const colLimit = Number(columnSize);

  // Looking at a file is what the Preview step asks for.
  useEffect(() => flow.markPreviewed(workbook.id), [workbook.id, flow.markPreviewed]);

  // Another file or sheet starts at its top-left corner, with nothing stale on screen.
  useEffect(() => {
    setOffset(0);
    setColOffset(0);
    setData(null);
  }, [workbook.id, sheetName]);
  useEffect(() => setOffset(0), [rowSize]);
  useEffect(() => setColOffset(0), [columnSize]);

  useEffect(() => {
    if (!sheetName) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    api
      .getSourcePreview(workbook.id, { sheet: sheetName, offset, limit, colOffset, colLimit }, controller.signal)
      .then((next) => {
        setData(next);
        grid.current?.scrollTo({ top: 0, left: 0 });
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        setError(err instanceof ApiError ? err : new ApiError(0, { code: "unknown", message: String(err) }));
      })
      .finally(() => !controller.signal.aborted && setLoading(false));
    return () => controller.abort();
  }, [workbook.id, sheetName, offset, limit, colOffset, colLimit, reload]);

  // Merged ranges drawn as Excel draws them: the part on screen is one cell spanning its
  // rows and columns, holding the value when the range's top-left cell is on screen too.
  // Keyed "row:col" (sheet positions); every other cell the part covers is skipped.
  const merges = useMemo(() => {
    const spans = new Map<string, { range: string; rowSpan: number; colSpan: number; anchor: boolean }>();
    const skip = new Set<string>();
    if (!data || !data.rows.length) return { spans, skip };
    const top = data.offset + 1;
    const bottom = data.offset + data.rows.length;
    const left = data.col_offset + 1;
    const right = data.col_offset + data.columns.length;
    for (const range of data.merged) {
      const box = bounds(range);
      if (!box) continue;
      const r0 = Math.max(box.minRow, top);
      const r1 = Math.min(box.maxRow, bottom);
      const c0 = Math.max(box.minCol, left);
      const c1 = Math.min(box.maxCol, right);
      if (r0 > r1 || c0 > c1) continue;
      spans.set(`${r0}:${c0}`, { range, rowSpan: r1 - r0 + 1, colSpan: c1 - c0 + 1, anchor: r0 === box.minRow && c0 === box.minCol });
      for (let row = r0; row <= r1; row++) for (let col = c0; col <= c1; col++) if (row !== r0 || col !== c0) skip.add(`${row}:${col}`);
    }
    return { spans, skip };
  }, [data]);

  const fileOptions = flow.files.map((item) => ({
    value: item.workbook.id,
    label: item.workbook.filename,
    icon: item.workbook.kind === "delimited" ? <FileText /> : <FileSpreadsheet />,
    description: (
      <>
        {plural(item.workbook.sheets.length, "sheet")} · {item.selected.length} selected for cleaning
        {effectiveAppend(item) ? " · append" : ""}
      </>
    ),
    meta: flow.previewed.has(item.workbook.id) ? <Badge tone="success">Viewed</Badge> : undefined,
  }));

  const sheetOptions = workbook.sheets.map((sheet) => ({
    value: sheet.name,
    label: sheet.name,
    icon: <Sheet />,
    description: [
      selected.has(sheet.name) ? "Selected for cleaning" : "Not selected",
      sheet.hidden ? "hidden in Excel" : null,
      !sheet.has_content ? "no data found" : null,
    ]
      .filter(Boolean)
      .join(" · "),
    meta: sheet.hidden ? <Badge tone="info">Hidden</Badge> : !sheet.has_content ? <Badge tone="warning">Empty</Badge> : undefined,
  }));

  const delimited = workbook.kind === "delimited";
  const available = data?.available_rows ?? 0;
  const rowPages = Math.max(1, Math.ceil(available / limit));
  const rowPage = Math.floor(offset / limit);
  const totalColumns = data?.total_columns ?? 0;
  const firstRow = data && data.rows.length ? data.offset + 1 : 0;
  const lastRow = data ? data.offset + data.rows.length : 0;
  const firstCol = data?.columns[0];
  const lastCol = data?.columns[data.columns.length - 1];
  const usedRange = data && data.total_rows && data.total_columns ? `A1:${columnLetter(data.total_columns)}${data.total_rows}` : null;
  const toggleSheet = (on: boolean) => {
    if (!sheetName) return;
    flow.setSelected(
      workbook.id,
      on ? workbook.sheets.map((sheet) => sheet.name).filter((name) => name === sheetName || selected.has(name)) : entry.selected.filter((name) => name !== sheetName),
    );
  };

  return (
    <section className="card" aria-labelledby="source-title">
      {/* What is shown: file, sheet, and how much of it at once */}
      <div className="flex flex-col gap-4 border-b border-ink-100 px-5 py-4 md:px-6">
        <div className="flex min-w-0 items-start gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-ink-200 bg-ink-50 text-ink-600" aria-hidden>
            <ScanEye className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <p className="label-caps">Source · before cleaning</p>
            <h2 id="source-title" className="truncate text-section text-ink-900" title={workbook.filename}>{workbook.filename}</h2>
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3 xl:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)_112px_112px]">
          <Select
            value={workbook.id}
            options={fileOptions}
            onChange={flow.setFocusedId}
            label={flow.files.length > 1 ? `File (${flow.files.length})` : "File"}
            icon={<FileSpreadsheet />}
            className="col-span-2 sm:col-span-1"
            width={460}
          />
          <Select
            value={sheetName}
            options={sheetOptions}
            onChange={(name) => flow.setPreviewSheet(workbook.id, name)}
            label={delimited ? "Sheet (the whole file)" : `Sheet (${workbook.sheets.length})`}
            icon={<Sheet />}
            disabled={workbook.sheets.length < 2}
            className="col-span-2 sm:col-span-1"
            width={360}
          />
          <Select value={rowSize} options={ROW_SIZES.map((size) => ({ value: size, label: size }))} onChange={setRowSize} label="Rows" icon={<Rows3 />} />
          <Select value={columnSize} options={COLUMN_SIZES.map((size) => ({ value: size, label: size }))} onChange={setColumnSize} label="Columns" icon={<Columns3 />} />
        </div>
      </div>

      {/* About this sheet */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 bg-ink-50/70 px-5 py-2.5 text-caption text-ink-600 md:px-6" aria-live="polite">
        {data ? (
          <>
            {usedRange ? (
              <span className="num">
                Used range <code className="rounded bg-white px-1.5 py-0.5 font-mono text-[11px] text-ink-800 ring-1 ring-inset ring-ink-200">{usedRange}</code>
              </span>
            ) : (
              <span>Empty sheet</span>
            )}
            {usedRange && (
              <span className="num">
                <strong className="text-ink-900">{formatNumber(data.total_rows)}</strong> rows × <strong className="text-ink-900">{formatNumber(data.total_columns)}</strong> columns
              </span>
            )}
            {data.merged_total > 0 && (
              <span className="inline-flex items-center gap-1">
                <TableCellsMerge className="h-3.5 w-3.5 text-ink-400" aria-hidden /> {plural(data.merged_total, "merged range")}
              </span>
            )}
            {delimited && <span>{data.source_format}</span>}
            {data.hidden && <Badge tone="info" icon={<EyeOff />}>Hidden in Excel</Badge>}
          </>
        ) : (
          <Skeleton className="h-4 w-64" />
        )}
        {sheetInfo && (
          <label className={clsx("ml-auto flex items-center gap-2 font-medium", flow.running ? "cursor-not-allowed text-ink-400" : "cursor-pointer text-ink-800")}>
            <Checkbox
              label={`Clean sheet ${sheetInfo.name}`}
              checked={selected.has(sheetInfo.name)}
              onChange={toggleSheet}
              disabled={flow.running}
            />
            Clean this sheet
          </label>
        )}
      </div>

      {data && available < data.total_rows && (
        <div className="px-5 pt-4 md:px-6">
          <Alert tone="info" title={`Showing the first ${formatNumber(available)} of ${formatNumber(data.total_rows)} rows`}>
            The sheet is too large to page through here in full. Cleaning still reads all of it.
          </Alert>
        </div>
      )}

      {/* The cells */}
      <div className="p-5 md:p-6">
        <div className="overflow-hidden rounded-lg border border-ink-200">
          {error ? (
            <div className="p-4">
              <Alert tone="error" title={error.body.message} action={<Button size="sm" onClick={() => setReload((n) => n + 1)}>Retry</Button>}>
                {error.body.advice}
              </Alert>
            </div>
          ) : !data ? (
            <GridSkeleton />
          ) : data.total_rows === 0 ? (
            <EmptyState compact icon={<Sheet />} title="This sheet is empty" description="It has no values, so cleaning finds nothing in it." />
          ) : (
            <div ref={grid} className={clsx("relative max-h-[560px] overflow-auto scroll-thin", loading && "opacity-60 transition-opacity")} aria-busy={loading}>
              {loading && <div className="absolute inset-x-0 top-0 z-30 h-0.5 animate-pulse bg-brand-500" aria-hidden />}
              <table className="border-separate border-spacing-0 text-table" style={{ minWidth: "100%" }}>
                <caption className="sr-only">
                  {sheetName} in {workbook.filename}, rows {firstRow} to {lastRow}, columns {firstCol} to {lastCol}, as uploaded
                </caption>
                <thead>
                  <tr>
                    <th scope="col" className="sticky left-0 top-0 z-20 w-14 min-w-[56px] border-b border-r border-ink-200 bg-ink-100 px-2 py-1.5">
                      <span className="sr-only">Row</span>
                    </th>
                    {data.columns.map((letter) => (
                      <th
                        key={letter}
                        scope="col"
                        className="sticky top-0 z-10 min-w-[96px] border-b border-r border-ink-200 bg-ink-50 px-3 py-1.5 text-center font-mono text-[11px] font-semibold text-ink-500"
                      >
                        {letter}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((row, index) => {
                    const number = data.offset + index + 1;
                    return (
                      <tr key={number} className="group">
                        <th
                          scope="row"
                          className="num sticky left-0 z-10 border-b border-r border-ink-200 bg-ink-50 px-2 py-1.5 text-right font-mono text-[11px] font-medium text-ink-500 group-hover:bg-ink-100"
                        >
                          {number}
                        </th>
                        {row.map((raw, column) => {
                          const col = data.col_offset + column + 1;
                          const key = `${number}:${col}`;
                          if (merges.skip.has(key)) return null;
                          const merge = merges.spans.get(key);
                          // The part of a range whose top-left is off screen is shown empty.
                          const value = merge && !merge.anchor ? null : raw;
                          const text = value === null ? "" : typeof value === "boolean" ? (value ? "TRUE" : "FALSE") : String(value);
                          const isError = typeof value === "string" && ERRORS.has(value.trim());
                          const numeric = typeof value === "number" || (delimited && typeof value === "string" && value.trim() !== "" && NUMBER_TEXT.test(value.trim()));
                          return (
                            <td
                              key={col}
                              rowSpan={merge && merge.rowSpan > 1 ? merge.rowSpan : undefined}
                              colSpan={merge && merge.colSpan > 1 ? merge.colSpan : undefined}
                              title={merge ? `${text ? `${text}\n` : ""}Merged cells ${merge.range}` : text || undefined}
                              className={clsx(
                                "truncate whitespace-pre border-b border-r border-ink-100 px-3 py-1.5",
                                merge ? "max-w-0 bg-sky-50/60 align-top" : "max-w-[320px] group-hover:bg-ink-50",
                                numeric ? "num text-right" : "text-left",
                                isError ? "font-medium text-danger-700" : "text-ink-800",
                              )}
                            >
                              {text}
                            </td>
                          );
                        })}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {data.rows.length === 0 && (
                <EmptyState compact icon={<Rows3 />} title="No rows here" description="This page is past the end of the sheet." action={<Button size="sm" onClick={() => setOffset(0)}>First page</Button>} />
              )}
            </div>
          )}

          {/* Rows and columns page separately */}
          {data && data.total_rows > 0 && !error && (
            <div className="flex flex-col gap-2 border-t border-ink-200 px-4 py-2 md:flex-row md:items-center md:justify-between">
              <nav aria-label="Rows" className="flex items-center gap-3">
                <span className="num text-caption text-ink-500">
                  Rows <strong className="text-ink-800">{formatNumber(firstRow)}–{formatNumber(lastRow)}</strong> of {formatNumber(available)}
                </span>
                {rowPages > 1 && (
                  <div className="flex items-center gap-1">
                    <IconButton label="First rows" disabled={rowPage === 0 || loading} onClick={() => setOffset(0)} className="hidden sm:inline-flex">
                      <ChevronsLeft />
                    </IconButton>
                    <IconButton label="Previous rows" disabled={rowPage === 0 || loading} onClick={() => setOffset(Math.max(0, offset - limit))}>
                      <ChevronLeft />
                    </IconButton>
                    <span className="num min-w-[72px] text-center text-caption font-medium text-ink-700" aria-live="polite">
                      {rowPage + 1} / {rowPages}
                    </span>
                    <IconButton label="Next rows" disabled={rowPage >= rowPages - 1 || loading} onClick={() => setOffset(offset + limit)}>
                      <ChevronRight />
                    </IconButton>
                    <IconButton label="Last rows" disabled={rowPage >= rowPages - 1 || loading} onClick={() => setOffset((rowPages - 1) * limit)} className="hidden sm:inline-flex">
                      <ChevronsRight />
                    </IconButton>
                  </div>
                )}
              </nav>
              <nav aria-label="Columns" className="flex items-center gap-3">
                <span className="num text-caption text-ink-500">
                  Columns <strong className="text-ink-800">{firstCol}–{lastCol}</strong> of {formatNumber(totalColumns)}
                </span>
                {totalColumns > colLimit && (
                  <div className="flex items-center gap-1">
                    <IconButton label="Previous columns" disabled={colOffset === 0 || loading} onClick={() => setColOffset(Math.max(0, colOffset - colLimit))}>
                      <ChevronLeft />
                    </IconButton>
                    <IconButton label="Next columns" disabled={colOffset + colLimit >= totalColumns || loading} onClick={() => setColOffset(colOffset + colLimit)}>
                      <ChevronRight />
                    </IconButton>
                  </div>
                )}
              </nav>
            </div>
          )}
        </div>

        <p className="mt-3 flex items-start gap-1.5 text-caption text-ink-500">
          <Info className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden />
          {delimited
            ? "Each value as the file holds it. Cleaning finds the table, types the columns and drops blank or repeated rows."
            : "Values as the workbook stores them: formulas show their last saved result, and Excel number formats (%, currency) are not applied. Cleaning finds the table, trims text, empties error cells and drops banners, subtotals and blank rows."}
        </p>
      </div>

      <div className="flex flex-col-reverse gap-2 border-t border-ink-100 px-5 py-4 sm:flex-row sm:justify-between md:px-6">
        <Button variant="ghost" icon={<ArrowLeft />} onClick={() => navigate("configuration")}>
          Configure
        </Button>
        <SheetSteps entry={entry} sheet={sheetName} onPick={(name) => flow.setPreviewSheet(workbook.id, name)} />
      </div>
    </section>
  );
}

/** Previous / next sheet of this file, so a reviewer can walk through them in order. */
function SheetSteps({ entry, sheet, onPick }: { entry: FileEntry; sheet: string | null; onPick: (name: string) => void }) {
  const sheets: SheetInfo[] = entry.workbook.sheets;
  const index = sheets.findIndex((item) => item.name === sheet);
  if (sheets.length < 2 || index < 0) return null;
  const previous = sheets[index - 1];
  const next = sheets[index + 1];
  return (
    <div className="flex items-center gap-2">
      <Button icon={<ChevronLeft />} disabled={!previous} onClick={() => previous && onPick(previous.name)} title={previous ? `Previous sheet: ${previous.name}` : undefined}>
        Previous sheet
      </Button>
      <Button iconRight={<ChevronRight />} disabled={!next} onClick={() => next && onPick(next.name)} title={next ? `Next sheet: ${next.name}` : undefined}>
        Next sheet
      </Button>
    </div>
  );
}

function GridSkeleton() {
  return (
    <div className="space-y-0 p-4" aria-label="Loading sheet" role="status">
      <div className="mb-3 flex gap-3">
        {Array.from({ length: 7 }).map((_, i) => (
          <Skeleton key={i} className="h-5 flex-1" />
        ))}
      </div>
      {Array.from({ length: 8 }).map((_, row) => (
        <div key={row} className="flex gap-3 border-t border-ink-100 py-2.5">
          {Array.from({ length: 7 }).map((_, i) => (
            <Skeleton key={i} className="h-4 flex-1" />
          ))}
        </div>
      ))}
    </div>
  );
}
