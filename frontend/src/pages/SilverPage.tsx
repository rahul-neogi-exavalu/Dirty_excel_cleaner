import clsx from "clsx";
import {
  ArrowRight,
  BookCheck,
  Building2,
  Check,
  Columns3,
  Database,
  EyeOff,
  Info,
  Layers3,
  ListChecks,
  Plus,
  RefreshCw,
  Rows3,
  ServerOff,
  ShieldCheck,
  Sigma,
  Split,
  Star,
  Table2,
  Trash2,
  TriangleAlert,
  UserCheck,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type {
  BronzeStatus,
  CleanupLoad,
  EligibleLoad,
  DrtMapping,
  MatchMethod,
  SilverAggregateRow,
  SilverCatalog,
  SilverCandidate,
  SilverMappingRow,
  SilverRun,
  SilverTableReview,
  SilverVote,
} from "../api/types";
import { PageHeader, SectionCard } from "../components/layout/Layout";
import { Badge } from "../components/ui/Badge";
import { Button, IconButton } from "../components/ui/Button";
import { Checkbox, LabeledCheckbox, SearchInput } from "../components/ui/Controls";
import { Alert, EmptyState, ProgressBar, Skeleton, StatTile, useToast } from "../components/ui/Feedback";
import { Tooltip } from "../components/ui/Overlay";
import { Pagination, useFitPageSize, usePaged } from "../components/ui/Pagination";
import { Select, type SelectOption } from "../components/ui/Select";
import { TabPanel, Tabs } from "../components/ui/Tabs";
import { formatNumber, humanize, plural } from "../lib/format";
import { useAuth } from "../state/auth";

const METHOD: Record<MatchMethod, string> = { saved: "Saved", exact: "Exact", fuzzy: "Fuzzy", semantic: "Semantic", ai: "AI" };
const DOT: Record<MatchMethod, string> = {
  saved: "bg-ink-500",
  exact: "bg-emerald-500",
  fuzzy: "bg-sky-400",
  semantic: "bg-sky-700",
  ai: "bg-brand-600",
};
// Saved and exact votes are certain by definition; the others carry a score.
const SCORED = new Set<MatchMethod>(["fuzzy", "semantic", "ai"]);
const pct = (score: number) => `${Math.round(score * 100)}%`;
const IGNORE = "__ignore__";
const POLL_MS = 700;
const toError = (error: unknown) => (error instanceof ApiError ? error : new ApiError(0, { code: "unknown", message: String(error) }));
/** The Silver columns a bronze column can map to (system columns are filled by the pipeline). */
const targetsOf = (catalog: SilverCatalog) => catalog.columns.filter((column) => column.role !== "system");

export function SilverPage() {
  const [status, setStatus] = useState<BronzeStatus | null>(null);
  const [catalog, setCatalog] = useState<SilverCatalog | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [tab, setTab] = useState<"run" | "mapping" | "aggregate">("run");

  const load = useCallback(() => {
    setError(null);
    setStatus(null);
    Promise.all([api.bronzeStatus(), api.silverCatalog()])
      .then(([nextStatus, nextCatalog]) => {
        setStatus(nextStatus);
        setCatalog(nextCatalog);
      })
      .catch((err) => setError(toError(err)));
  }, []);
  useEffect(load, [load]);

  return (
    <>
      <PageHeader page="silver" title="Silver" description="Map bronze columns to the common schema and load." />
      {error ? (
        <div className="card"><EmptyState icon={<ServerOff />} title="Service unavailable" description={error.body.message} action={<Button icon={<RefreshCw />} onClick={load}>Retry</Button>} /></div>
      ) : !status || !catalog ? (
        <div className="card space-y-3 p-6"><Skeleton className="h-6 w-48" /><Skeleton className="h-40" /></div>
      ) : !status.configured ? (
        <div className="card"><EmptyState icon={<Database />} title="Database not configured" description="Set the AHI_DB_* values in .env, then restart the API." /></div>
      ) : !status.reachable ? (
        <Alert tone="error" title="Database unreachable" action={<Button size="sm" icon={<RefreshCw />} onClick={load}>Retry</Button>}>{status.error}</Alert>
      ) : (
        <div className="space-y-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <Tabs
              idPrefix="silver"
              label="Silver views"
              value={tab}
              onChange={setTab}
              items={[
                { id: "run", label: "Run", icon: <ListChecks /> },
                { id: "mapping", label: "Mapping", icon: <BookCheck /> },
                { id: "aggregate", label: "Aggregate", icon: <Sigma /> },
              ]}
            />
            <span className="flex items-center gap-3 text-caption text-ink-500">
              <span title={catalog.file}>{catalog.columns.length} Silver columns · {targetsOf(catalog).length} mappable</span>
              <span className={clsx("inline-flex items-center gap-1", catalog.semantic ? "text-ink-600" : "text-ink-400")}>
                <span className={clsx("h-1.5 w-1.5 rounded-full", catalog.semantic ? "bg-emerald-500" : "bg-ink-300")} aria-hidden />word2vec
              </span>
              <span className={clsx("inline-flex items-center gap-1", catalog.ai ? "text-ink-600" : "text-ink-400")} title={catalog.ai ? catalog.ai_label : "AI matching off"}>
                <span className={clsx("h-1.5 w-1.5 rounded-full", catalog.ai ? "bg-emerald-500" : "bg-ink-300")} aria-hidden />{catalog.ai ? catalog.ai_label : "AI"}
              </span>
            </span>
          </div>
          <TabPanel idPrefix="silver" id="run" active={tab === "run"}>
            <RunView catalog={catalog} />
          </TabPanel>
          <TabPanel idPrefix="silver" id="mapping" active={tab === "mapping"}>
            {tab === "mapping" && <MappingView catalog={catalog} />}
          </TabPanel>
          <TabPanel idPrefix="silver" id="aggregate" active={tab === "aggregate"}>
            {tab === "aggregate" && <AggregateView />}
          </TabPanel>
        </div>
      )}
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* Run: pick loads -> review the whole mapping -> approve                       */
/* -------------------------------------------------------------------------- */

function RunView({ catalog }: { catalog: SilverCatalog }) {
  const toast = useToast();
  const [eligible, setEligible] = useState<{ loads: EligibleLoad[]; cleanup: CleanupLoad[] } | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [run, setRun] = useState<SilverRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const refresh = useCallback(() => {
    setError(null);
    api
      .silverEligible()
      .then((data) => {
        setEligible(data);
        setSelected(new Set(data.loads.map((load) => load.ingestion_id)));
      })
      .catch((err) => setError(toError(err)));
  }, []);
  useEffect(refresh, [refresh]);

  useEffect(() => {
    if (run?.status !== "running") return;
    const timer = setTimeout(async () => {
      try {
        const next = await api.getSilverRun(run.id);
        setRun(next);
        if (next.status === "succeeded") toast({ severity: "success", title: "Loaded to Silver", description: `${formatNumber(next.result.rows_loaded ?? 0)} rows` });
        if (next.status === "failed") toast({ severity: "error", title: "Silver load failed", description: next.error?.message });
      } catch {
        setRun((current) => (current ? { ...current } : current));
      }
    }, POLL_MS);
    return () => clearTimeout(timer);
  }, [run, toast]);

  const start = async () => {
    setBusy(true);
    try {
      setRun(await api.createSilverRun([...selected]));
    } catch (err) {
      const apiError = toError(err);
      toast({ severity: "error", title: apiError.body.message, description: apiError.body.advice ?? undefined });
    } finally {
      setBusy(false);
    }
  };

  const reset = () => {
    setRun(null);
    refresh();
  };

  if (error) return <Alert tone="error" title={error.body.message} action={<Button size="sm" onClick={refresh}>Retry</Button>} />;
  if (!eligible) return <div className="card space-y-2 p-6"><Skeleton className="h-10" /><Skeleton className="h-10" /></div>;
  if (run && run.status === "draft") return <ReviewView run={run} catalog={catalog} onChange={setRun} onCancel={reset} />;
  if (run) return <ResultView run={run} onNew={reset} />;

  const loads = eligible.loads;
  if (!loads.length && !eligible.cleanup.length)
    return <div className="card"><EmptyState icon={<Layers3 />} title="Nothing to load" description="Every bronze load is already in Silver." action={<Button icon={<RefreshCw />} onClick={refresh}>Refresh</Button>} /></div>;

  const all = loads.length > 0 && loads.every((load) => selected.has(load.ingestion_id));
  return (
    <EligibleLoads
      loads={loads}
      cleanup={eligible.cleanup}
      selected={selected}
      setSelected={setSelected}
      all={all}
      busy={busy}
      refresh={refresh}
      start={start}
    />
  );
}

function EligibleLoads({
  loads,
  cleanup,
  selected,
  setSelected,
  all,
  busy,
  refresh,
  start,
}: {
  loads: EligibleLoad[];
  cleanup: CleanupLoad[];
  selected: Set<string>;
  setSelected: React.Dispatch<React.SetStateAction<Set<string>>>;
  all: boolean;
  busy: boolean;
  refresh: () => void;
  start: () => void;
}) {
  const list = useRef<HTMLDivElement>(null);
  const paged = usePaged(loads, useFitPageSize(list, { reserve: 150 }));
  const eligible = { cleanup };
  return (
    <SectionCard
      icon={<Table2 />}
      title="Eligible loads"
      description="Ingested to bronze, not yet in Silver."
      actions={<Button size="sm" variant="ghost" icon={<RefreshCw />} onClick={refresh}>Refresh</Button>}
    >
      {eligible.cleanup.length > 0 && (
        <Alert tone="info" className="mb-4" title={`${plural(eligible.cleanup.length, "replaced load")} will be removed from Silver`}>
          {eligible.cleanup.map((item) => item.file_name).join(", ")}
        </Alert>
      )}
      <div ref={list} className="overflow-hidden rounded-lg border border-ink-200">
      <div className="relative overflow-x-auto scroll-thin">
        <table className="w-full min-w-[900px] border-collapse text-table">
          <caption className="sr-only">Bronze loads eligible for Silver</caption>
          <thead className="bg-ink-50">
            <tr className="border-b border-ink-200 text-left text-caption font-semibold text-ink-600">
              <th scope="col" className="w-12 px-4 py-2.5">
                <Checkbox label={all ? "Clear all" : "Select all"} checked={all} indeterminate={!all && selected.size > 0}
                  onChange={(on) => setSelected(new Set(on ? loads.map((load) => load.ingestion_id) : []))} />
              </th>
              <th scope="col" className="px-2 py-2.5">Bronze table</th>
              <th scope="col" className="px-3 py-2.5">File</th>
              <th scope="col" className="px-3 py-2.5">Profit center</th>
              <th scope="col" className="px-3 py-2.5">Reporting start date</th>
              <th scope="col" className="px-3 py-2.5">Reporting end date</th>
              <th scope="col" className="px-4 py-2.5 text-right">Rows</th>
            </tr>
          </thead>
          <tbody>
            {paged.slice.map((load) => {
              const on = selected.has(load.ingestion_id);
              const toggle = (value: boolean) =>
                setSelected((current) => {
                  const next = new Set(current);
                  if (value) next.add(load.ingestion_id);
                  else next.delete(load.ingestion_id);
                  return next;
                });
              return (
                <tr key={load.ingestion_id} data-row onClick={() => toggle(!on)} className={clsx("cursor-pointer border-b border-ink-100 last:border-0", on ? "bg-brand-50/50" : "hover:bg-ink-50")}>
                  <td className="px-4 py-2.5" onClick={(event) => event.stopPropagation()}>
                    <Checkbox label={`Select ${load.file_name}`} checked={on} onChange={toggle} />
                  </td>
                  <td className="px-2 py-2.5 font-mono font-medium text-ink-900">{load.table_name}</td>
                  <td className="max-w-[260px] truncate px-3 py-2.5 text-ink-700" title={load.file_name}>{load.file_name}</td>
                  <td className="px-3 py-2.5">
                    <span className="block font-mono text-ink-800">{load.pc_id ?? "—"}</span>
                    {load.division_name && <span className="block text-caption text-ink-500">{load.division_name}</span>}
                  </td>
                  <td className="num px-3 py-2.5 text-ink-700">{load.reporting_start_date ?? load.period_start ?? "—"}</td>
                  <td className="num px-3 py-2.5 text-ink-700">
                    {load.reporting_end_date ?? load.period_end ?? load.period_start ?? "—"}
                    {load.reporting_period_type && <span className="block text-caption text-ink-500">{load.reporting_period_type}</span>}
                  </td>
                  <td className="num px-4 py-2.5 text-right text-ink-800">{formatNumber(load.rows)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <Pagination {...paged} onPage={paged.setPage} noun="load" />
      </div>
      <div className="mt-5 flex items-center justify-between gap-3">
        <p className="text-caption text-ink-500">{selected.size} of {plural(loads.length, "load")} selected</p>
        <Button variant="primary" iconRight={<ArrowRight />} state={busy ? "loading" : "idle"} loadingText="Matching…" disabled={!selected.size && !eligible.cleanup.length} onClick={start}>
          Review mapping
        </Button>
      </div>
    </SectionCard>
  );
}

function ReviewView({ run, catalog, onChange, onCancel }: { run: SilverRun; catalog: SilverCatalog; onChange: (run: SilverRun) => void; onCancel: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const reviewer = useAuth().user?.user_name ?? "";
  const rows = run.tables.reduce((sum, table) => sum + table.quality.rows, 0);
  const mapped = run.tables.reduce((sum, table) => sum + table.mapping.filter((row) => row.silver_column).length, 0);
  const prefilled = run.tables.reduce((sum, table) => sum + table.mapping.filter((row) => row.methods.includes("saved")).length, 0);
  const split = run.tables.reduce((sum, table) => sum + table.mapping.filter((row) => row.split).length, 0);
  const canApprove = !run.blockers.length;

  const edit = async (table: string, column: string, value: string) => {
    setBusy(true);
    try {
      onChange(await api.editSilverRunMapping(run.id, {
        table_name: table, bronze_column: column, silver_column: value === IGNORE ? null : value, ignored: value === IGNORE,
      }));
    } catch (err) {
      toast({ severity: "error", title: "Change not saved", description: toError(err).body.message });
    } finally {
      setBusy(false);
    }
  };

  const editAlso = async (table: string, column: string, also: string[]) => {
    setBusy(true);
    try {
      onChange(await api.editSilverRunMapping(run.id, { table_name: table, bronze_column: column, also }));
    } catch (err) {
      toast({ severity: "error", title: "Change not saved", description: toError(err).body.message });
    } finally {
      setBusy(false);
    }
  };

  const approve = async () => {
    setBusy(true);
    try {
      onChange(await api.approveSilverRun(run.id));
    } catch (err) {
      const apiError = toError(err);
      toast({ severity: "error", title: apiError.body.message, description: apiError.body.advice ?? undefined });
    } finally {
      setBusy(false);
    }
  };

  const ignoreAll = async (tableName: string) => {
    setBusy(true);
    try {
      onChange(await api.ignoreUnmapped(run.id, tableName));
      toast({ severity: "success", title: "Unmapped columns ignored", description: `All undecided columns in ${tableName} set to Ignore.` });
    } catch (err) {
      toast({ severity: "error", title: "Could not ignore columns", description: toError(err).body.message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
      <div className="min-w-0 space-y-6">
        {run.notes.map((note) => (
          <Alert key={note} tone="info" title={note} />
        ))}
        {run.cleanup.length > 0 && (
          <Alert tone="warning" title={`Removes ${plural(run.cleanup.length, "replaced load")} from Silver`}>
            {run.cleanup.map((item) => `${item.file_name} (${item.table_name})`).join(", ")}
          </Alert>
        )}
        <TablePicker tables={run.tables} catalog={catalog} busy={busy} onEdit={edit} onAlso={editAlso} onIgnoreAll={ignoreAll} />
      </div>

      <aside className="scroll-thin xl:sticky xl:top-[80px] xl:max-h-[calc(100dvh-96px)] xl:self-start xl:overflow-y-auto">
        <section className="card p-6" aria-labelledby="silver-approve-title">
          <h2 id="silver-approve-title" className="label-caps">Approval</h2>
          <div className="mt-4 grid grid-cols-2 gap-3">
            <StatTile icon={<Columns3 />} label="Mapped" value={mapped} />
            <StatTile icon={<Rows3 />} label="Rows" value={formatNumber(rows)} />
          </div>
          <p className="mt-3 text-caption text-ink-500">
            {prefilled ? `${prefilled} pre-filled from saved mappings. ` : ""}The whole mapping is saved on approval.
          </p>
          {split > 0 && (
            <p className="mt-2 flex items-center gap-1.5 text-caption text-amber-800">
              <Split className="h-3.5 w-3.5" aria-hidden /> {plural(split, "column")} where methods disagree
            </p>
          )}
          {run.blockers.length > 0 && (
            <Alert tone="warning" className="mt-4" title={`${plural(run.blockers.length, "issue")} to resolve`}>
              <ul className="space-y-0.5">{run.blockers.slice(0, 4).map((text) => <li key={text}>{text}</li>)}</ul>
            </Alert>
          )}
          <p className="mt-5 flex items-center gap-2 text-body text-ink-600">
            <UserCheck className="h-4 w-4 shrink-0 text-ink-400" aria-hidden />
            Approving as <span className="font-medium text-ink-900">{reviewer}</span>
          </p>
          <Button variant="primary" size="lg" className="mt-5 w-full" icon={<Check />} disabled={!canApprove || busy} onClick={approve}>
            Approve & load
          </Button>
          <Button variant="ghost" className="mt-2 w-full" onClick={onCancel} disabled={busy}>
            Back
          </Button>
          <p className="mt-2 flex items-center justify-center gap-1.5 text-caption text-ink-500">
            <ShieldCheck className="h-3.5 w-3.5" aria-hidden /> All or nothing
          </p>
        </section>
      </aside>
    </div>
  );
}

/** Several bronze tables are reviewed one at a time, picked from a list, rather than stacked. */
function TablePicker({
  tables,
  catalog,
  busy,
  onEdit,
  onAlso,
  onIgnoreAll,
}: {
  tables: SilverTableReview[];
  catalog: SilverCatalog;
  busy: boolean;
  onEdit: (table: string, column: string, value: string) => void;
  onAlso: (table: string, column: string, also: string[]) => void;
  onIgnoreAll: (table: string) => void;
}) {
  const [name, setName] = useState(tables[0]?.table_name ?? "");
  const table = tables.find((item) => item.table_name === name) ?? tables[0];
  if (!table) return null;
  const open = (item: SilverTableReview) => item.mapping.filter((row) => !row.silver_column && !row.ignored).length;
  const picker =
    tables.length > 1 ? (
      <Select<string>
        label={`Table (${tables.length})`}
        value={table.table_name}
        onChange={setName}
        icon={<Table2 className="h-4 w-4" />}
        className="w-full sm:w-80"
        options={tables.map((item) => ({
          value: item.table_name,
          label: item.table_name,
          description: `${plural(item.mapping.length, "column")} · ${formatNumber(item.quality.rows)} rows`,
          meta: open(item) ? <Badge tone="warning">{open(item)} to choose</Badge> : <Badge tone="success">Mapped</Badge>,
        }))}
      />
    ) : null;
  return (
    <TableMapping
      key={table.table_name}
      table={table}
      catalog={catalog}
      busy={busy}
      picker={picker}
      onEdit={(column, value) => onEdit(table.table_name, column, value)}
      onAlso={(column, also) => onAlso(table.table_name, column, also)}
      onIgnoreAll={() => onIgnoreAll(table.table_name)}
    />
  );
}

function TableMapping({
  table,
  catalog,
  busy,
  onEdit,
  onAlso,
  onIgnoreAll,
  picker,
}: {
  table: SilverTableReview;
  catalog: SilverCatalog;
  busy: boolean;
  onEdit: (column: string, value: string) => void;
  onAlso: (column: string, also: string[]) => void;
  onIgnoreAll: () => void;
  picker?: React.ReactNode;
}) {
  const list = useRef<HTMLDivElement>(null);
  const [onlyOpen, setOnlyOpen] = useState(false);
  const openRows = table.mapping.filter((row) => !row.silver_column && !row.ignored);
  const rows = onlyOpen && openRows.length ? openRows : table.mapping;
  const paged = usePaged(rows, useFitPageSize(list, { min: 4, fallbackRow: 76, reserve: 140 }), onlyOpen);
  // Which bronze column currently holds each Silver column, to flag a second use.
  const usedBy = useMemo(() => {
    const map = new Map<string, string>();
    for (const row of table.mapping) {
      if (row.silver_column) map.set(row.silver_column, row.bronze_column);
      for (const extra of row.also) map.set(extra, row.bronze_column);
    }
    return map;
  }, [table.mapping]);
  const quality = table.quality;
  const invalid = Object.entries(quality.invalid_values ?? {});
  const pcs = Object.entries(quality.profit_center ?? {});
  const split = table.mapping.filter((row) => row.split).length;
  return (
    <SectionCard
      icon={<Table2 />}
      title={table.table_name}
      description={`${table.loads.map((load) => load.file_name).join(", ")} · ${table.pc_id ?? "no profit center"}`}
      actions={
        <span className="flex items-center gap-2">
          {split > 0 && <Badge tone="warning" icon={<Split />}>{split} split</Badge>}
          <Badge tone="neutral">{formatNumber(quality.rows)} rows</Badge>
        </span>
      }
    >
      {(picker || openRows.length > 0) && (
        <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div className="flex flex-wrap items-end gap-3">
            {picker}
            {openRows.length > 0 && (
              <Button
                size="sm"
                variant="ghost"
                icon={<EyeOff />}
                disabled={busy}
                onClick={onIgnoreAll}
              >
                Ignore all unmapped ({openRows.length})
              </Button>
            )}
          </div>
          {openRows.length > 0 && (
            <LabeledCheckbox label={`Only columns to choose (${openRows.length})`} checked={onlyOpen} onChange={setOnlyOpen} />
          )}
        </div>
      )}
      <div ref={list} className="overflow-hidden rounded-lg border border-ink-200">
      <div className="relative overflow-x-auto scroll-thin">
        <table className="w-full min-w-[560px] border-collapse text-table">
          <caption className="sr-only">Column mapping for {table.table_name}</caption>
          <thead className="bg-ink-50">
            <tr className="border-b border-ink-200 text-left text-caption font-semibold text-ink-600">
              <th scope="col" className="px-4 py-2.5">Bronze column</th>
              <th scope="col" className="px-3 py-2.5">Silver column · votes</th>
            </tr>
          </thead>
          <tbody>
            {paged.slice.map((row) => (
              <MappingRow key={row.bronze_column} row={row} catalog={catalog} usedBy={usedBy} busy={busy}
                onEdit={(value) => onEdit(row.bronze_column, value)} onAlso={(also) => onAlso(row.bronze_column, also)} />
            ))}
          </tbody>
        </table>
      </div>
      <Pagination {...paged} onPage={paged.setPage} noun="column" />
      </div>
      {(invalid.length > 0 || pcs.length > 0 || quality.unmapped_silver_columns?.length > 0) && (
        <div className="mt-4 grid gap-3 md:grid-cols-3">
          <QualityBlock icon={<TriangleAlert />} title="Unreadable → NULL" empty="None">
            {invalid.map(([column, count]) => <Badge key={column} tone="warning">{column}: {count}</Badge>)}
          </QualityBlock>
          <QualityBlock icon={<Database />} title="Profit center" empty="Not mapped">
            {pcs.map(([status, count]) => <Badge key={status} tone={status === "kept" || status.startsWith("filled") || status === "corrected" ? "neutral" : "warning"}>{humanize(status)}: {count}</Badge>)}
          </QualityBlock>
          <QualityBlock icon={<Info />} title="Silver columns left empty" empty="None">
            {quality.unmapped_silver_columns.map((column) => <Badge key={column} tone="neutral">{column}</Badge>)}
          </QualityBlock>
        </div>
      )}
    </SectionCard>
  );
}

/** Who voted: a colour dot and the method's name, with its score where the score means something. */
function Votes({ votes, className }: { votes: SilverVote[]; className?: string }) {
  return (
    <span className={clsx("inline-flex flex-wrap items-center gap-x-2.5 gap-y-0.5 text-caption text-ink-600", className)}>
      {votes.map((vote) => (
        <span key={`${vote.method}-${vote.second_choice}`} className={clsx("inline-flex items-center gap-1", vote.second_choice && "text-ink-400")}>
          <span className={clsx("h-1.5 w-1.5 rounded-full", vote.second_choice ? "ring-1 ring-inset ring-ink-400" : DOT[vote.method])} aria-hidden />
          {METHOD[vote.method]}
          {vote.second_choice ? (
            <span>2nd choice</span>
          ) : (
            SCORED.has(vote.method) && vote.score < 0.995 && <span className="num text-ink-400">{pct(vote.score)}</span>
          )}
        </span>
      ))}
    </span>
  );
}

function candidateLabel(candidate: SilverCandidate) {
  const methods = candidate.votes.filter((vote) => !vote.second_choice).map((vote) => METHOD[vote.method]);
  return `${candidate.silver_column ?? "Ignore"} (${methods.join(" + ") || "2nd choice"})`;
}

function MappingRow({
  row,
  catalog,
  usedBy,
  busy,
  onEdit,
  onAlso,
}: {
  row: SilverMappingRow;
  catalog: SilverCatalog;
  usedBy: Map<string, string>;
  busy: boolean;
  onEdit: (value: string) => void;
  onAlso: (also: string[]) => void;
}) {
  const [adding, setAdding] = useState(false);
  const options = useMemo(() => {
    const inUse = (name: string) => {
      const owner = usedBy.get(name);
      return owner && owner !== row.bronze_column ? <span className="text-amber-700">Used by {owner}</span> : null;
    };
    // The voted candidates first, best first; then every other Silver column; then Ignore.
    const suggested: SelectOption<string>[] = row.candidates.map((candidate) => ({
      value: candidate.silver_column ?? IGNORE,
      label: candidate.silver_column ?? "Ignore",
      group: "Suggested by vote",
      description: (
        <span className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
          {candidate.recommended && <Badge tone="brand" icon={<Star />}>Recommended</Badge>}
          <Votes votes={candidate.votes} />
          {candidate.silver_column && inUse(candidate.silver_column)}
        </span>
      ),
    }));
    const voted = new Set(row.candidates.map((candidate) => candidate.silver_column ?? IGNORE));
    const rest: SelectOption<string>[] = targetsOf(catalog)
      .filter((column) => !voted.has(column.name))
      .map((column) => ({
        value: column.name,
        label: column.name,
        group: "All Silver columns",
        description: <>{column.drt_name} · {column.data_type}{inUse(column.name) && <> · {inUse(column.name)}</>}</>,
      }));
    if (!voted.has(IGNORE)) rest.push({ value: IGNORE, label: "Ignore", group: "All Silver columns", description: "Not loaded to Silver" });
    return [...suggested, ...rest];
  }, [row, catalog, usedBy]);

  const value = row.ignored ? IGNORE : row.silver_column;
  // "Also load into": every other target column, not the main one, not Ignore.
  const alsoOptions = options.filter((option) => option.value !== IGNORE && option.value !== row.silver_column && !row.also.includes(option.value));
  const current = row.candidates.find((candidate) => (candidate.silver_column ?? IGNORE) === value);
  const backing = current?.votes.filter((vote) => !vote.second_choice) ?? [];
  const others = row.candidates.filter((candidate) => candidate.support > 0 && candidate !== current);
  const open = !row.silver_column && !row.ignored;
  const manual = row.selection === "manual";
  const detail = (
    <span className="block space-y-1">
      {open && <span className="block">{row.reason}</span>}
      {backing.map((vote) => (
        <span key={vote.method} className="block"><strong className="font-semibold">{METHOD[vote.method]}:</strong> {vote.reason}</span>
      ))}
      {manual && <span className="block">Chosen by the reviewer.</span>}
      {!open && others.length > 0 && <span className="block text-ink-300">Other votes: {others.map(candidateLabel).join(", ")}</span>}
    </span>
  );

  return (
    <tr data-row className={clsx("border-b border-ink-100 last:border-0", open && "bg-amber-50/50")}>
      <td className="max-w-0 px-4 py-3 align-top">
        <p className="truncate font-mono font-medium text-ink-900" title={row.bronze_column}>{row.bronze_column}</p>
        {row.source_header && row.source_header !== row.bronze_column && (
          <p className="truncate text-caption text-ink-600" title={`Header in the file: ${row.source_header}`}>“{row.source_header}”</p>
        )}
        {row.samples.length > 0 && <p className="truncate text-caption text-ink-500" title={row.samples.join(" · ")}>{row.samples.join(" · ")}</p>}
      </td>
      <td className="w-[58%] px-3 py-2.5 align-top">
        <Select<string>
          value={value}
          options={options}
          onChange={onEdit}
          label={`Silver column for ${row.bronze_column}`}
          hideLabel
          placeholder={row.candidates.length ? "Choose a suggestion…" : "Choose…"}
          disabled={busy}
          width={400}
          className="max-w-[360px]"
        />
        <Tooltip content={detail}>
          <span tabIndex={0} className="mt-1.5 inline-flex flex-wrap items-center gap-x-2.5 gap-y-1 rounded focus-visible:outline-none focus-visible:shadow-focus">
            {open ? (
              <span className="text-caption font-medium text-amber-800">{row.candidates.length ? `${plural(row.candidates.length, "suggestion")} · choose one` : "No method found a match"}</span>
            ) : backing.length ? (
              <Votes votes={backing} />
            ) : (
              <span className="text-caption text-ink-500">{row.ignored ? "Not loaded" : "No votes"}</span>
            )}
            {manual && <Badge tone="neutral" icon={<UserCheck />}>Manual</Badge>}
            {row.split && !open && <Badge tone="warning" icon={<Split />}>Split</Badge>}
          </span>
        </Tooltip>
        {row.silver_column && !row.ignored && (
          <div className="mt-1.5 flex max-w-[360px] flex-wrap items-center gap-1.5">
            {row.also.map((extra) => (
              <span key={extra} className="inline-flex items-center gap-0.5 rounded bg-ink-100 py-0.5 pl-2 pr-0.5 text-caption text-ink-700">
                <span className="text-ink-500">Also</span> <span className="font-mono">{extra}</span>
                <IconButton label={`Stop loading ${row.bronze_column} into ${extra}`} className="!h-5 !w-5" disabled={busy}
                  onClick={() => onAlso(row.also.filter((item) => item !== extra))}>
                  <X />
                </IconButton>
              </span>
            ))}
            {adding ? (
              <Select<string>
                value={null}
                options={alsoOptions}
                onChange={(extra) => {
                  setAdding(false);
                  onAlso([...row.also, extra]);
                }}
                label={`Also load ${row.bronze_column} into`}
                hideLabel
                placeholder="Also load into…"
                disabled={busy}
                width={400}
                className="w-full"
              />
            ) : (
              <button type="button" disabled={busy} onClick={() => setAdding(true)}
                className="inline-flex items-center gap-1 rounded px-1 text-caption text-ink-500 hover:text-brand-700 focus-visible:outline-none focus-visible:shadow-focus disabled:opacity-40">
                <Plus className="h-3 w-3" aria-hidden /> Also load into
              </button>
            )}
          </div>
        )}
      </td>
    </tr>
  );
}

function QualityBlock({ icon, title, empty, children }: { icon: React.ReactNode; title: string; empty: string; children: React.ReactNode[] }) {
  return (
    <div className="rounded-lg border border-ink-200 p-3">
      <p className="mb-2 flex items-center gap-1.5 text-caption font-semibold text-ink-600 [&>svg]:h-3.5 [&>svg]:w-3.5">{icon}{title}</p>
      <div className="flex flex-wrap gap-1.5">{children.length ? children : <span className="text-caption text-ink-400">{empty}</span>}</div>
    </div>
  );
}

function ResultView({ run, onNew }: { run: SilverRun; onNew: () => void }) {
  const running = run.status === "running";
  const failed = run.status === "failed";
  return (
    <section className="card p-5 md:p-6" aria-busy={running}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className={clsx("flex h-12 w-12 shrink-0 items-center justify-center rounded-full text-white", running ? "bg-brand-600" : failed ? "bg-danger-600" : "bg-emerald-600")} aria-hidden>
          {running ? <RefreshCw className="h-6 w-6 animate-spin" /> : failed ? <TriangleAlert className="h-6 w-6" /> : <Check className="h-6 w-6" strokeWidth={3} />}
        </div>
        <div className="min-w-0 flex-1">
          <p className={clsx("label-caps", failed ? "!text-danger-700" : !running && "!text-emerald-700")}>{running ? "Loading" : failed ? "Failed · rolled back" : "Loaded"}</p>
          <h2 className="text-section text-ink-900">{running ? run.message : failed ? run.error?.message : `${formatNumber(run.result.rows_loaded ?? 0)} rows in Silver`}</h2>
          {run.reviewed_by && <p className="text-caption text-ink-500">Approved by {run.reviewed_by}</p>}
        </div>
        {!running && <Button variant={failed ? "primary" : "secondary"} icon={<RefreshCw />} onClick={onNew}>New run</Button>}
      </div>
      {running && <ProgressBar className="mt-5" value={run.progress} label="Silver load progress" active />}
      {failed && run.error?.advice && <Alert tone="error" className="mt-5" title="Nothing was written">{run.error.advice}</Alert>}
      {!running && !failed && (
        <div className="mt-5 grid grid-cols-2 gap-3 md:grid-cols-3">
          <StatTile icon={<Rows3 />} label="Rows loaded" value={formatNumber(run.result.rows_loaded ?? 0)} tone="success" />
          <StatTile icon={<Trash2 />} label="Replaced rows removed" value={formatNumber(run.result.rows_removed ?? 0)} />
          <StatTile icon={<BookCheck />} label="New mappings saved" value={formatNumber(run.result.mappings_saved ?? 0)} />
        </div>
      )}
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* DRT column mapping (editable) and the aggregate                              */
/* -------------------------------------------------------------------------- */

const ALL = "__all__";

function MappingView({ catalog }: { catalog: SilverCatalog }) {
  const toast = useToast();
  const [rows, setRows] = useState<DrtMapping[] | null>(null);
  const [query, setQuery] = useState("");
  const [profitCenter, setProfitCenter] = useState(ALL);
  const list = useRef<HTMLDivElement>(null);
  const pageSize = useFitPageSize(list, { reserve: 130 });
  const load = useCallback(() => {
    setRows(null);
    api.silverMapping().then(setRows).catch((err) => toast({ severity: "error", title: toError(err).body.message }));
  }, [toast]);
  useEffect(load, [load]);
  const options = useMemo(
    () => targetsOf(catalog).map((column) => ({ value: column.name, label: column.name, description: column.drt_name })),
    [catalog],
  );
  const centers = useMemo(() => {
    const counts = new Map<string, number>();
    for (const row of rows ?? []) counts.set(row.profit_center, (counts.get(row.profit_center) ?? 0) + 1);
    return [
      { value: ALL, label: "All profit centers", description: plural(rows?.length ?? 0, "row") },
      ...[...counts.entries()].map(([value, count]) => ({ value, label: value, description: plural(count, "row") })),
    ];
  }, [rows]);
  const needle = query.trim().toLowerCase();
  const visible = (rows ?? []).filter(
    (row) =>
      (profitCenter === ALL || row.profit_center === profitCenter) &&
      (!needle || [row.profit_center, row.pc_column, row.drt_column ?? "", row.silver_column_name ?? ""].some((v) => v.toLowerCase().includes(needle))),
  );
  const paged = usePaged(visible, pageSize, `${profitCenter}|${needle}`);

  if (!rows) return <div className="card space-y-2 p-6"><Skeleton className="h-10" /><Skeleton className="h-10" /></div>;
  if (!rows.length) return <div className="card"><EmptyState icon={<BookCheck />} title="No mappings" description="Add drt_column_mapping.xlsx to assets/." /></div>;

  const save = async (row: DrtMapping, value: string) => {
    try {
      const saved = await api.editSilverMapping({ ...row, new_silver_column_name: value });
      setRows((current) => current?.map((item) => (item === row ? { ...item, silver_column_name: value, drt_column: saved.drt_column } : item)) ?? null);
      toast({ severity: "success", title: "Mapping updated", description: "Applies from the next Silver run." });
    } catch (err) {
      toast({ severity: "error", title: "Change not saved", description: toError(err).body.message });
    }
  };

  const remove = async (row: DrtMapping) => {
    try {
      await api.deleteSilverMapping(row);
      setRows((current) => current?.filter((item) => item !== row) ?? null);
      toast({ severity: "success", title: "Mapping removed", description: "The column will be asked about again in the next Silver run." });
    } catch (err) {
      toast({ severity: "error", title: "Not removed", description: toError(err).body.message });
    }
  };

  return (
    <SectionCard
      icon={<BookCheck />}
      title="Mapping"
      description="DRT column mapping by profit center."
      actions={
        <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
          <Select<string> label="Profit center" hideLabel value={profitCenter} options={centers} onChange={setProfitCenter} icon={<Building2 className="h-4 w-4" />} className="sm:w-56" />
          <SearchInput value={query} onChange={setQuery} placeholder="Search" label="Search mappings" className="sm:w-56" />
        </div>
      }
    >
      <div ref={list} className="overflow-hidden rounded-lg border border-ink-200">
      <div className="relative overflow-x-auto scroll-thin">
        <table className="w-full min-w-[760px] border-separate border-spacing-0 text-table">
          <caption className="sr-only">DRT column mapping</caption>
          <thead>
            <tr className="text-left text-caption font-semibold text-ink-600">
              {["Profit center", "Source column", "DRT column", "Silver column", ""].map((label) => (
                <th key={label} scope="col" className="border-b border-ink-200 bg-ink-50 px-3 py-2.5">{label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {paged.slice.map((row) => (
              <tr key={`${row.profit_center}|${row.pc_column}|${row.silver_column_name}|${row.drt_column}`} data-row>
                <td className="border-b border-ink-100 px-3 py-2 font-mono text-ink-700">{row.profit_center}</td>
                <td className="max-w-[280px] truncate border-b border-ink-100 px-3 py-2 font-mono font-medium text-ink-900" title={row.pc_column}>{row.pc_column}</td>
                <td className="border-b border-ink-100 px-3 py-2 text-ink-600">{row.drt_column ?? ""}</td>
                <td className="w-[240px] border-b border-ink-100 px-3 py-1.5">
                  <Select<string> value={row.silver_column_name} options={options} onChange={(value) => save(row, value)} label={`Silver column for ${row.pc_column}`} hideLabel placeholder="Not resolved" width={300} />
                </td>
                <td className="w-10 border-b border-ink-100 px-2 py-1.5 text-right">
                  <IconButton label={`Remove the mapping of ${row.pc_column} for ${row.profit_center}`} onClick={() => remove(row)}>
                    <Trash2 />
                  </IconButton>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!visible.length && <EmptyState compact icon={<BookCheck />} title="No matching mappings" description="Try another profit center or search." />}
      </div>
      <Pagination {...paged} onPage={paged.setPage} noun="row" />
      </div>
    </SectionCard>
  );
}

const money = (value: number | null | undefined) =>
  value == null ? "—" : value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function AggregateView() {
  const [rows, setRows] = useState<SilverAggregateRow[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [source, setSource] = useState(ALL);
  const list = useRef<HTMLDivElement>(null);
  const visible = (rows ?? []).filter((row) => source === ALL || row.source_system === source);
  const paged = usePaged(visible, useFitPageSize(list, { fallbackRow: 37, reserve: 130 }), source);
  useEffect(() => {
    api.silverAggregate().then(setRows).catch((err) => setError(toError(err)));
  }, []);
  const sources = useMemo(() => {
    const names = [...new Set((rows ?? []).map((row) => row.source_system ?? "—"))];
    return [{ value: ALL, label: "All sources" }, ...names.map((name) => ({ value: name, label: name }))];
  }, [rows]);
  if (error) return <Alert tone="error" title={error.body.message} />;
  if (!rows) return <div className="card space-y-2 p-6"><Skeleton className="h-10" /><Skeleton className="h-10" /></div>;
  if (!rows.length) return <div className="card"><EmptyState icon={<Sigma />} title="No aggregate yet" description="Load data to Silver first." /></div>;
  const policies = visible.reduce((sum, row) => sum + (row.policy_count ?? 0), 0);
  const premium = visible.reduce((sum, row) => sum + (row.premium ?? 0), 0);
  return (
    <SectionCard
      icon={<Sigma />}
      title="Aggregate"
      description="Profit center by accounting month."
      actions={
        <div className="flex w-full flex-col items-stretch gap-2 sm:w-auto sm:flex-row sm:items-center">
          <span className="num text-caption text-ink-600">{formatNumber(policies)} policies · {premium.toLocaleString("en-US", { style: "currency", currency: "USD" })}</span>
          {sources.length > 2 && <Select<string> label="Source" hideLabel value={source} options={sources} onChange={setSource} className="sm:w-44" />}
        </div>
      }
    >
      <div ref={list} className="overflow-hidden rounded-lg border border-ink-200">
      <div className="relative overflow-x-auto scroll-thin">
        <table className="w-full min-w-[820px] border-separate border-spacing-0 text-table">
          <caption className="sr-only">Silver aggregate</caption>
          <thead>
            <tr className="text-left text-caption font-semibold text-ink-600">
              {["Source", "Profit center", "Month", "Policies", "Premium", "Commission", "Revenue"].map((label, index) => (
                <th key={label} scope="col" className={clsx("border-b border-ink-200 bg-ink-50 px-3 py-2.5", index > 2 && "text-right")}>{label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {paged.slice.map((row, index) => (
              <tr key={`${paged.page}-${index}`} data-row>
                <td className="border-b border-ink-100 px-3 py-2 font-mono text-ink-700">{row.source_system ?? "—"}</td>
                <td className="border-b border-ink-100 px-3 py-2 text-ink-900">
                  <span className="num text-ink-600">{row.profit_center_number ?? "—"}</span> {row.profit_center_name ?? ""}
                </td>
                <td className="num border-b border-ink-100 px-3 py-2 text-ink-700">{row.reporting_period ?? "—"}</td>
                <td className="num border-b border-ink-100 px-3 py-2 text-right text-ink-800">{row.policy_count == null ? "—" : formatNumber(row.policy_count)}</td>
                <td className="num border-b border-ink-100 px-3 py-2 text-right text-ink-800">{money(row.premium)}</td>
                <td className="num border-b border-ink-100 px-3 py-2 text-right text-ink-800">{money(row.gross_commission_amount)}</td>
                <td className="num border-b border-ink-100 px-3 py-2 text-right text-ink-800">{money(row.revenue)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pagination {...paged} onPage={paged.setPage} noun="row" />
      </div>
    </SectionCard>
  );
}
