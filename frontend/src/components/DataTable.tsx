import clsx from "clsx";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Calendar,
  Check,
  ChevronLeft,
  ChevronRight,
  Columns3,
  Hash,
  Pencil,
  RotateCcw,
  SearchX,
  ToggleLeft,
  Type,
  X,
  TableProperties,
  AlertOctagon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { OutputSummary, Preview, PreviewColumn } from "../api/types";
import { formatNumber } from "../lib/format";
import { Button, IconButton } from "./ui/Button";
import { Checkbox, SearchInput } from "./ui/Controls";
import { Alert, EmptyState, Skeleton, useToast } from "./ui/Feedback";
import { PopoverPanel, Tooltip, usePopover } from "./ui/Overlay";
import { Select } from "./ui/Select";

const PAGE_SIZES = ["20", "50", "100"] as const;
const MAX_HEADER = 128;

function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}

function DtypeIcon({ dtype }: { dtype: string }) {
  const lower = dtype.toLowerCase();
  const icon = /int|float|decimal/.test(lower) ? <Hash /> : /date|time/.test(lower) ? <Calendar /> : /bool/.test(lower) ? <ToggleLeft /> : <Type />;
  return (
    <span className="text-ink-400 [&>svg]:h-3.5 [&>svg]:w-3.5" title={dtype} aria-label={`Type ${dtype}`}>
      {icon}
    </span>
  );
}

const isNumeric = (dtype: string) => /int|float|decimal/i.test(dtype);

export function DataTable({
  jobId,
  output,
  onOutputUpdated,
}: {
  jobId: string;
  output: OutputSummary;
  onOutputUpdated: (summary: OutputSummary) => void;
}) {
  const toast = useToast();
  const [data, setData] = useState<Preview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState<(typeof PAGE_SIZES)[number]>("20");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<{ original: string; desc: boolean } | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [widths, setWidths] = useState<Record<string, number>>({});
  const [reload, setReload] = useState(0);
  const debouncedQuery = useDebounced(query, 300);

  // Reset view state when switching tables.
  useEffect(() => {
    setOffset(0);
    setQuery("");
    setSort(null);
    setHidden(new Set());
    setWidths({});
    setData(null);
  }, [output.id]);

  useEffect(() => setOffset(0), [debouncedQuery, limit, sort]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    api
      .getPreview(jobId, output.id, { offset, limit: Number(limit), q: debouncedQuery || undefined, sort: sort?.original, desc: sort?.desc }, controller.signal)
      .then((next) => setData(next))
      .catch((err) => {
        if (controller.signal.aborted) return;
        setError(err instanceof ApiError ? err : new ApiError(0, { code: "unknown", message: String(err) }));
      })
      .finally(() => !controller.signal.aborted && setLoading(false));
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, output.id, offset, limit, debouncedQuery, sort?.original, sort?.desc, reload]);

  const columns = useMemo(() => (data?.columns ?? []).filter((column) => !hidden.has(column.original)), [data, hidden]);

  const onRenamed = (summary: OutputSummary) => {
    onOutputUpdated(summary);
    setReload((n) => n + 1);
  };

  const cycleSort = (column: PreviewColumn) => {
    setSort((current) =>
      current?.original !== column.original ? { original: column.original, desc: false } : current.desc ? null : { original: column.original, desc: true },
    );
  };

  const total = data?.total ?? 0;
  const firstRow = total ? offset + 1 : 0;
  const lastRow = Math.min(offset + Number(limit), total);
  const filtered = Boolean(debouncedQuery);

  return (
    <div className="overflow-hidden rounded-lg border border-ink-200 bg-white">
      {/* Toolbar */}
      <div className="flex flex-col gap-3 border-b border-ink-200 px-4 py-3 lg:flex-row lg:items-center">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <p className="text-body font-medium text-ink-900">Editable headers</p>
          <Tooltip content="Select the pencil beside a column name to rename it. Saved names are used in every export.">
            <span tabIndex={0} className="rounded text-ink-400 hover:text-ink-600" aria-label="About editable headers">
              <Pencil className="h-3.5 w-3.5" />
            </span>
          </Tooltip>
          {output.renamed_columns > 0 && (
            <span className="rounded-full bg-sky-50 px-2 py-0.5 text-caption font-medium text-sky-700 ring-1 ring-inset ring-sky-200">
              {output.renamed_columns} renamed
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <SearchInput value={query} onChange={setQuery} placeholder="Search in this table…" label="Search rows" className="w-full sm:w-64" />
          <ColumnMenu columns={data?.columns ?? []} hidden={hidden} onChange={setHidden} />
          <div className="flex items-center gap-2">
            <span className="hidden text-caption text-ink-500 sm:inline">Rows</span>
            <Select
              value={limit}
              onChange={(value) => setLimit(value)}
              options={PAGE_SIZES.map((size) => ({ value: size, label: size }))}
              label="Rows per page"
              hideLabel
              className="w-20"
            />
          </div>
        </div>
      </div>

      {/* Status line */}
      <div className="flex flex-wrap items-center justify-between gap-2 bg-ink-50/70 px-4 py-2 text-caption text-ink-600" aria-live="polite">
        <span className="num">
          {data ? (
            <>
              Previewing rows <strong className="text-ink-900">{formatNumber(firstRow)}–{formatNumber(lastRow)}</strong> of{" "}
              <strong className="text-ink-900">{formatNumber(total)}</strong>
              {filtered && <> matching “{debouncedQuery}” (of {formatNumber(data.total_unfiltered)} total)</>}
              {hidden.size > 0 && <> · {hidden.size} column{hidden.size > 1 ? "s" : ""} hidden</>}
            </>
          ) : (
            "Loading rows…"
          )}
        </span>
        <span>Sorting and search run on the full table, not just this page.</span>
      </div>

      {/* Body */}
      {error ? (
        <div className="p-4">
          <Alert tone="error" title="The preview couldn't be loaded" action={<Button size="sm" onClick={() => setReload((n) => n + 1)}>Retry</Button>}>
            {error.body.message}
          </Alert>
        </div>
      ) : !data ? (
        <TableSkeleton />
      ) : (
        <div className={clsx("relative max-h-[560px] overflow-auto scroll-thin", loading && "opacity-60 transition-opacity")} aria-busy={loading}>
          {loading && <div className="absolute inset-x-0 top-0 z-30 h-0.5 animate-pulse bg-brand-500" aria-hidden />}
          <table className="border-separate border-spacing-0 text-table" style={{ minWidth: "100%" }}>
            <caption className="sr-only">Cleaned data for {output.name}</caption>
            <thead>
              <tr>
                <th scope="col" className="sticky left-0 top-0 z-20 w-12 min-w-[48px] border-b border-r border-ink-200 bg-ink-50 px-3 py-2 text-right text-caption font-medium text-ink-400">
                  #
                </th>
                {columns.map((column) => (
                  <HeaderCell
                    key={column.original}
                    column={column}
                    width={widths[column.original]}
                    onResize={(width) => setWidths((all) => ({ ...all, [column.original]: width }))}
                    sort={sort?.original === column.original ? (sort.desc ? "desc" : "asc") : null}
                    onSort={() => cycleSort(column)}
                    allNames={data.columns.map((c) => c.name)}
                    jobId={jobId}
                    outputId={output.id}
                    onRenamed={(summary, message) => {
                      onRenamed(summary);
                      toast({ severity: "success", title: message });
                    }}
                  />
                ))}
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row, rowIndex) => (
                <tr key={offset + rowIndex} className="group">
                  <td className="num sticky left-0 z-10 border-b border-r border-ink-100 bg-white px-3 py-2 text-right text-caption text-ink-400 group-hover:bg-ink-50">
                    {offset + rowIndex + 1}
                  </td>
                  {columns.map((column) => {
                    const index = data.columns.indexOf(column);
                    const value = row[index];
                    const numeric = isNumeric(column.dtype);
                    return (
                      <td
                        key={column.original}
                        className={clsx(
                          "max-w-[420px] truncate border-b border-ink-100 px-3 py-2 group-hover:bg-ink-50",
                          numeric ? "num text-right" : "text-left",
                          value === null ? "text-ink-300" : "text-ink-800",
                        )}
                        title={value === null ? "Empty" : String(value)}
                      >
                        {value === null ? "—" : String(value)}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
          {data.rows.length === 0 && (
            <EmptyState
              compact
              icon={filtered ? <SearchX /> : <TableProperties />}
              title={filtered ? "No matching rows" : "This table has no rows"}
              description={filtered ? `No rows contain “${debouncedQuery}”. Try a different search.` : "The cleaner kept the headers but found no data rows."}
              action={filtered ? <Button size="sm" onClick={() => setQuery("")}>Clear search</Button> : undefined}
            />
          )}
          {columns.length === 0 && data.columns.length > 0 && (
            <EmptyState compact icon={<Columns3 />} title="All columns are hidden" description="Choose columns to show from the Columns menu." action={<Button size="sm" onClick={() => setHidden(new Set())}>Show all columns</Button>} />
          )}
        </div>
      )}

      {/* Pagination */}
      {data && total > 0 && (
        <nav aria-label="Pagination" className="flex items-center justify-between gap-3 border-t border-ink-200 px-4 py-2.5">
          <span className="num text-caption text-ink-500">
            Page {Math.floor(offset / Number(limit)) + 1} of {Math.max(1, Math.ceil(total / Number(limit)))}
          </span>
          <div className="flex items-center gap-1">
            <IconButton label="Previous page" disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - Number(limit)))}>
              <ChevronLeft />
            </IconButton>
            <IconButton label="Next page" disabled={offset + Number(limit) >= total || loading} onClick={() => setOffset(offset + Number(limit))}>
              <ChevronRight />
            </IconButton>
          </div>
        </nav>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function HeaderCell({
  column,
  width,
  onResize,
  sort,
  onSort,
  allNames,
  jobId,
  outputId,
  onRenamed,
}: {
  column: PreviewColumn;
  width?: number;
  onResize: (width: number) => void;
  sort: "asc" | "desc" | null;
  onSort: () => void;
  allNames: string[];
  jobId: string;
  outputId: string;
  onRenamed: (summary: OutputSummary, message: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(column.name);
  const [saving, setSaving] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);
  const [justSaved, setJustSaved] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const cell = useRef<HTMLTableCellElement>(null);

  useEffect(() => {
    if (editing) {
      setValue(column.name);
      setServerError(null);
      requestAnimationFrame(() => input.current?.select());
    }
  }, [editing, column.name]);

  const trimmed = value.trim();
  const clientError = !trimmed
    ? "Column name cannot be empty."
    : trimmed.length > MAX_HEADER
      ? `Column name cannot be longer than ${MAX_HEADER} characters.`
      : allNames.some((name) => name !== column.name && name.toLowerCase() === trimmed.toLowerCase())
        ? "Column name already exists."
        : null;
  const errorText = clientError ?? serverError;
  const unchanged = trimmed === column.name;

  const save = async () => {
    if (clientError || saving) return;
    if (unchanged) return setEditing(false);
    setSaving(true);
    try {
      const summary = await api.renameHeaders(jobId, outputId, { [column.original]: trimmed });
      setEditing(false);
      setJustSaved(true);
      setTimeout(() => setJustSaved(false), 2000);
      onRenamed(summary, `Header updated to “${trimmed}”`);
    } catch (err) {
      setServerError(err instanceof ApiError ? err.body.message : "The header could not be saved.");
    } finally {
      setSaving(false);
    }
  };

  const reset = async () => {
    try {
      const summary = await api.resetHeaders(jobId, outputId, [column.original]);
      onRenamed(summary, `Header restored to “${column.original}”`);
    } catch (err) {
      setServerError(err instanceof ApiError ? err.body.message : "The header could not be restored.");
    }
  };

  const startResize = (event: React.PointerEvent) => {
    event.preventDefault();
    event.stopPropagation();
    const startX = event.clientX;
    const startWidth = cell.current?.offsetWidth ?? 160;
    const move = (e: PointerEvent) => onResize(Math.max(96, Math.min(640, startWidth + e.clientX - startX)));
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const ariaSort = sort === "asc" ? "ascending" : sort === "desc" ? "descending" : "none";

  return (
    <th
      ref={cell}
      scope="col"
      aria-sort={ariaSort}
      style={{ width, minWidth: width ?? 150, maxWidth: width }}
      className={clsx(
        "group/th sticky top-0 z-10 border-b border-r border-ink-200 px-3 py-2 text-left align-top font-medium transition-colors",
        editing ? "bg-brand-50" : column.renamed ? "bg-sky-50" : "bg-ink-50",
        justSaved && "!bg-emerald-50",
      )}
    >
      {editing ? (
        <div className="min-w-[200px]">
          <label className="sr-only" htmlFor={`rename-${column.original}`}>
            Rename column {column.name}
          </label>
          <input
            ref={input}
            id={`rename-${column.original}`}
            value={value}
            maxLength={MAX_HEADER + 20}
            aria-invalid={Boolean(errorText) || undefined}
            aria-describedby={errorText ? `rename-err-${column.original}` : undefined}
            onChange={(event) => {
              setValue(event.target.value);
              setServerError(null);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter") (event.preventDefault(), void save());
              if (event.key === "Escape") (event.preventDefault(), setEditing(false));
            }}
            className={clsx(
              "h-8 w-full rounded border bg-white px-2 text-table font-medium text-ink-900 focus:outline-none focus:shadow-focus",
              errorText ? "border-brand-500" : "border-ink-300 focus:border-brand-500",
            )}
          />
          {errorText && (
            <p id={`rename-err-${column.original}`} role="alert" className="mt-1 text-[11px] font-normal text-brand-700">
              {errorText}
            </p>
          )}
          <div className="mt-1.5 flex gap-1">
            <Button size="sm" variant="primary" icon={<Check />} onClick={save} disabled={Boolean(clientError)} state={saving ? "loading" : "idle"} className="!h-7">
              Save
            </Button>
            <Button size="sm" variant="ghost" icon={<X />} onClick={() => setEditing(false)} className="!h-7">
              Cancel
            </Button>
          </div>
          {column.renamed && <p className="mt-1 truncate text-[11px] font-normal text-ink-500">Original: {column.original}</p>}
        </div>
      ) : (
        <div className="flex items-center gap-1.5">
          <DtypeIcon dtype={column.dtype} />
          <button
            type="button"
            onClick={onSort}
            className="flex min-w-0 flex-1 items-center gap-1 rounded text-left text-table font-semibold text-ink-800 hover:text-ink-900 focus-visible:outline-none focus-visible:shadow-focus"
            aria-label={`${column.name}, sort ${sort === "asc" ? "descending" : sort === "desc" ? "off" : "ascending"}`}
            title={column.renamed ? `Renamed from “${column.original}”` : column.name}
          >
            <span className="truncate">{column.name}</span>
            {sort === "asc" ? <ArrowUp className="h-3.5 w-3.5 shrink-0 text-brand-600" /> : sort === "desc" ? <ArrowDown className="h-3.5 w-3.5 shrink-0 text-brand-600" /> : <ArrowUpDown className="h-3.5 w-3.5 shrink-0 text-ink-300 opacity-0 group-hover/th:opacity-100" />}
          </button>
          {justSaved && <Check className="h-3.5 w-3.5 shrink-0 animate-scale-in text-emerald-600" aria-label="Saved" />}
          {column.renamed && !justSaved && (
            <IconButton label={`Restore original name ${column.original}`} className="!h-6 !w-6 opacity-70 hover:opacity-100" onClick={reset}>
              <RotateCcw />
            </IconButton>
          )}
          <IconButton label={`Rename column ${column.name}`} className="!h-6 !w-6 opacity-60 hover:opacity-100 group-hover/th:opacity-100" onClick={() => setEditing(true)}>
            <Pencil />
          </IconButton>
        </div>
      )}
      {serverError && !editing && (
        <p role="alert" className="mt-1 flex items-center gap-1 text-[11px] font-normal text-brand-700">
          <AlertOctagon className="h-3 w-3" /> {serverError}
        </p>
      )}
      <span
        role="separator"
        aria-orientation="vertical"
        aria-label={`Resize ${column.name}`}
        onPointerDown={startResize}
        className="absolute right-0 top-0 h-full w-1.5 cursor-col-resize hover:bg-brand-300"
      />
    </th>
  );
}

/* -------------------------------------------------------------------------- */

function ColumnMenu({ columns, hidden, onChange }: { columns: PreviewColumn[]; hidden: Set<string>; onChange: (hidden: Set<string>) => void }) {
  const { open, setOpen, anchor, panel } = usePopover();
  return (
    <>
      <Button
        ref={anchor}
        size="md"
        icon={<Columns3 />}
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        disabled={!columns.length}
      >
        Columns{hidden.size ? ` (${columns.length - hidden.size}/${columns.length})` : ""}
      </Button>
      <PopoverPanel open={open} anchor={anchor} panel={panel} align="right" width={260} className="max-h-80 overflow-y-auto scroll-thin">
        <div className="flex items-center justify-between px-2 py-1.5">
          <span className="text-caption font-semibold text-ink-600">Visible columns</span>
          <button type="button" className="text-caption font-medium text-brand-700 hover:underline" onClick={() => onChange(new Set())}>
            Show all
          </button>
        </div>
        {columns.map((column) => {
          const visible = !hidden.has(column.original);
          return (
            <label key={column.original} className="flex cursor-pointer items-center gap-2.5 rounded px-2 py-1.5 hover:bg-ink-100">
              <Checkbox
                label={`Show ${column.name}`}
                checked={visible}
                onChange={(on) => {
                  const next = new Set(hidden);
                  if (on) next.delete(column.original);
                  else next.add(column.original);
                  onChange(next);
                }}
              />
              <span className="truncate text-body text-ink-800">{column.name}</span>
            </label>
          );
        })}
      </PopoverPanel>
    </>
  );
}

function TableSkeleton() {
  return (
    <div className="space-y-0 p-4" aria-label="Loading table" role="status">
      <div className="mb-3 flex gap-3">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} className="h-6 flex-1" />
        ))}
      </div>
      {Array.from({ length: 8 }).map((_, row) => (
        <div key={row} className="flex gap-3 border-t border-ink-100 py-2.5">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-4 flex-1" />
          ))}
        </div>
      ))}
    </div>
  );
}
