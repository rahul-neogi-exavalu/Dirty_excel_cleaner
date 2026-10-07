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
  RefreshCw,
  Rows3,
  ServerOff,
  ShieldCheck,
  Sigma,
  Star,
  Table2,
  Trash2,
  TriangleAlert,
  UserCheck,
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
  SilverColumnDef,
  SilverMappingRow,
  SilverPick,
  SilverRun,
  SilverTableReview,
  SilverVote,
} from "../api/types";
import { PageHeader, SectionCard } from "../components/layout/Layout";
import { PagedBands } from "../components/MappingBands";
import { Badge } from "../components/ui/Badge";
import { Button, IconButton } from "../components/ui/Button";
import { Checkbox, SearchInput } from "../components/ui/Controls";
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
const POLL_MS = 700;
const toError = (error: unknown) => (error instanceof ApiError ? error : new ApiError(0, { code: "unknown", message: String(error) }));
/** The DRT columns: the only Silver columns a bronze column can map to, and all the dropdowns offer. */
const targetsOf = (catalog: SilverCatalog) => catalog.columns.filter((column) => column.target);

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
                // "Load", not "Run": the workflow's own Run step (cleaning) is in the sidebar.
                { id: "run", label: "Load", icon: <ListChecks /> },
                { id: "mapping", label: "Mapping", icon: <BookCheck /> },
                { id: "aggregate", label: "Aggregate", icon: <Sigma /> },
              ]}
            />
            <span className="flex items-center gap-3 text-caption text-ink-500">
              <span title={`${catalog.file}: the mapping offers only the DRT columns`}>{catalog.columns.length} Silver columns · {targetsOf(catalog).length} DRT columns</span>
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
  const picks = run.tables.flatMap((table) => table.targets);
  const mapped = picks.filter((pick) => pick.bronze_column).length;
  const saved = picks.filter((pick) => pick.votes.some((vote) => vote.method === "saved")).length;
  const unloaded = run.tables.reduce((sum, table) => sum + table.mapping.filter((row) => !row.silver_column).length, 0);
  const canApprove = !run.blockers.length;

  const assign = async (table: string, silver: string, bronze: string | null) => {
    setBusy(true);
    try {
      onChange(await api.assignSilverTarget(run.id, { table_name: table, silver_column: silver, bronze_column: bronze }));
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

  return (
    <div className="space-y-4">
      {run.cleanup.length > 0 && (
        <Alert tone="warning" title={`Removes ${plural(run.cleanup.length, "replaced load")} from Silver`}>
          {run.cleanup.map((item) => `${item.file_name} (${item.table_name})`).join(", ")}
        </Alert>
      )}

      <section className="card flex flex-col gap-3 px-4 py-3 md:px-5 xl:flex-row xl:items-center xl:justify-between" aria-labelledby="silver-approve-title">
        <h2 id="silver-approve-title" className="sr-only">Approval</h2>
        <div className="min-w-0">
          <dl className="flex flex-wrap items-center gap-x-5 gap-y-1.5">
            <div className="flex items-center gap-1.5">
              <Columns3 className="h-4 w-4 text-ink-500" aria-hidden />
              <dt className="text-caption text-ink-500">DRT columns mapped</dt>
              <dd className="num text-body font-semibold text-ink-900">{mapped} of {picks.length}</dd>
            </div>
            <div className="flex items-center gap-1.5">
              <Rows3 className="h-4 w-4 text-ink-500" aria-hidden />
              <dt className="text-caption text-ink-500">Rows</dt>
              <dd className="num text-body font-semibold text-ink-900">{formatNumber(rows)}</dd>
            </div>
            {saved > 0 && (
              <div className="flex items-center gap-1.5">
                <BookCheck className="h-4 w-4 text-ink-500" aria-hidden />
                <dt className="text-caption text-ink-500">From saved mappings</dt>
                <dd className="num text-body font-semibold text-ink-900">{saved}</dd>
              </div>
            )}
            <div className="flex items-center gap-1.5">
              <EyeOff className="h-4 w-4 text-ink-500" aria-hidden />
              <dt className="text-caption text-ink-500">Bronze columns not loaded</dt>
              <dd className="num text-body font-semibold text-ink-900">{unloaded}</dd>
            </div>
          </dl>
          <p className="mt-1 flex flex-wrap items-center gap-x-1.5 text-caption text-ink-500">
            <ShieldCheck className="h-3.5 w-3.5" aria-hidden /> All or nothing: the whole mapping is saved on approval.
            <span className="inline-flex items-center gap-1"><UserCheck className="h-3.5 w-3.5" aria-hidden />Approving as <span className="font-medium text-ink-800">{reviewer}</span></span>
            {run.notes.length > 0 && (
              <Tooltip content={<span className="block space-y-1">{run.notes.map((note) => <span key={note} className="block">{note}</span>)}</span>}>
                <span tabIndex={0} className="inline-flex items-center gap-1 rounded text-ink-600 underline decoration-dotted underline-offset-2 focus-visible:outline-none focus-visible:shadow-focus">
                  <Info className="h-3.5 w-3.5" aria-hidden />{plural(run.notes.length, "note")} on the matching
                </span>
              </Tooltip>
            )}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button variant="ghost" onClick={onCancel} disabled={busy}>Back</Button>
          <Button variant="primary" icon={<Check />} disabled={!canApprove || busy} onClick={approve}>Approve & load</Button>
        </div>
      </section>
      {run.blockers.length > 0 && (
        <Alert tone="warning" title={`${plural(run.blockers.length, "issue")} to resolve before approval`}>
          <ul className="space-y-0.5">{run.blockers.map((text) => <li key={text}>{text}</li>)}</ul>
        </Alert>
      )}

      <TablePicker tables={run.tables} catalog={catalog} busy={busy} onAssign={assign} />
    </div>
  );
}

/** Several bronze tables are reviewed one at a time, picked from a list, rather than stacked. */
function TablePicker({
  tables,
  catalog,
  busy,
  onAssign,
}: {
  tables: SilverTableReview[];
  catalog: SilverCatalog;
  busy: boolean;
  onAssign: (table: string, silver: string, bronze: string | null) => void;
}) {
  const [name, setName] = useState(tables[0]?.table_name ?? "");
  const table = tables.find((item) => item.table_name === name) ?? tables[0];
  if (!table) return null;
  const picker =
    tables.length > 1 ? (
      <Select<string>
        label={`Table (${tables.length})`}
        value={table.table_name}
        onChange={setName}
        icon={<Table2 className="h-4 w-4" />}
        className="w-full sm:w-80"
        options={tables.map((item) => {
          const done = item.targets.filter((pick) => pick.bronze_column).length;
          return {
            value: item.table_name,
            label: item.table_name,
            description: `${plural(item.mapping.length, "column")} · ${formatNumber(item.quality.rows)} rows`,
            meta: <Badge tone="neutral">{done} of {item.targets.length} mapped</Badge>,
          };
        })}
      />
    ) : null;
  return (
    <TableMapping
      key={table.table_name}
      table={table}
      catalog={catalog}
      busy={busy}
      picker={picker}
      onAssign={(silver, bronze) => onAssign(table.table_name, silver, bronze)}
    />
  );
}

type MappingTab = "mapped" | "open" | "unloaded" | "quality";
const NONE = "__none__";

/**
 * One bronze table's mapping, made from the Silver side: each DRT column takes a bronze
 * column or none. A table of a hundred bronze columns is still 48 decisions, and the bronze
 * columns no DRT column takes are simply not loaded (listed on their own tab).
 */
function TableMapping({
  table,
  catalog,
  busy,
  onAssign,
  picker,
}: {
  table: SilverTableReview;
  catalog: SilverCatalog;
  busy: boolean;
  onAssign: (silver: string, bronze: string | null) => void;
  picker?: React.ReactNode;
}) {
  const drt = useMemo(() => new Map(targetsOf(catalog).map((column) => [column.name, column])), [catalog]);
  const bronze = useMemo(() => new Map(table.mapping.map((row) => [row.bronze_column, row])), [table.mapping]);
  const mapped = table.targets.filter((pick) => pick.bronze_column);
  const open = table.targets.filter((pick) => !pick.bronze_column);
  const unloaded = table.mapping.filter((row) => !row.silver_column);
  // A table with nothing mapped yet opens on what needs choosing.
  const [tab, setTab] = useState<MappingTab>(mapped.length ? "mapped" : "open");
  const quality = table.quality;
  const invalid = Object.entries(quality.invalid_values ?? {});
  const pcs = Object.entries(quality.profit_center ?? {});
  const issues = invalid.length + pcs.filter(([status]) => !(status === "kept" || status.startsWith("filled") || status === "corrected")).length;

  const silverTop = (pick: SilverPick) => <SilverHead pick={pick} column={drt.get(pick.silver_column)} />;
  const bronzeBottom = (pick: SilverPick) => (
    <BronzePick pick={pick} bronze={bronze} busy={busy} onChange={(value) => onAssign(pick.silver_column, value)} />
  );

  return (
    <SectionCard
      icon={<Table2 />}
      title={table.table_name}
      description={`${table.loads.map((load) => load.file_name).join(", ")} · ${table.pc_id ?? "no profit center"}`}
      actions={
        <span className="flex items-center gap-2">
          {picker}
          <Badge tone="neutral">{plural(table.mapping.length, "bronze column")}</Badge>
          <Badge tone="neutral">{formatNumber(quality.rows)} rows</Badge>
        </span>
      }
    >
      <div className="mb-4">
        <Tabs<MappingTab>
          idPrefix={`map-${table.table_name}`}
          label={`Mapping of ${table.table_name}`}
          value={tab}
          onChange={setTab}
          items={[
            { id: "mapped", label: "Mapped", icon: <Check />, count: mapped.length },
            { id: "open", label: "Not mapped", icon: <TriangleAlert />, count: open.length },
            { id: "unloaded", label: "Bronze not loaded", icon: <EyeOff />, count: unloaded.length },
            { id: "quality", label: "Data quality", icon: <ShieldCheck />, count: issues || undefined },
          ]}
        />
      </div>

      <TabPanel idPrefix={`map-${table.table_name}`} id="mapped" active={tab === "mapped"}>
        {mapped.length ? (
          <PagedBands items={mapped} keyOf={(pick) => pick.silver_column} caption={`DRT columns mapped in ${table.table_name}`}
            noun="DRT column" reset={tab} top={silverTop} bottom={bronzeBottom} />
        ) : (
          <EmptyState compact icon={<Columns3 />} title="No DRT column mapped yet" description="Choose bronze columns on the Not mapped tab." />
        )}
      </TabPanel>
      <TabPanel idPrefix={`map-${table.table_name}`} id="open" active={tab === "open"}>
        {open.length ? (
          <>
            <p className="mb-3 text-caption text-ink-500">
              No bronze column was recommended for these. Choose one where the file has it; a DRT column left here loads empty.
            </p>
            <PagedBands items={open} keyOf={(pick) => pick.silver_column} caption={`DRT columns not mapped in ${table.table_name}`}
              noun="DRT column" reset={tab} top={silverTop} bottom={bronzeBottom} attention={(pick) => pick.candidates.length > 0} />
          </>
        ) : (
          <EmptyState compact icon={<Check />} title="Every DRT column is mapped" description="Nothing left to choose for this table." />
        )}
      </TabPanel>
      <TabPanel idPrefix={`map-${table.table_name}`} id="unloaded" active={tab === "unloaded"}>
        {unloaded.length ? (
          <>
            <p className="mb-3 text-caption text-ink-500">
              No DRT column takes these bronze columns, so they are not loaded to Silver. Load one into a DRT column here if it belongs.
            </p>
            <PagedBands items={unloaded} keyOf={(row) => row.bronze_column} caption={`Bronze columns of ${table.table_name} not loaded`}
              noun="bronze column" reset={tab} topLabel="Bronze column" bottomLabel="Load into"
              top={(row) => <BronzeCell row={row} />}
              bottom={(row) => <LoadInto row={row} picks={table.targets} drt={drt} busy={busy} onAssign={onAssign} />} />
          </>
        ) : (
          <EmptyState compact icon={<Check />} title="Every bronze column is loaded" description="Each one feeds a DRT column." />
        )}
      </TabPanel>
      <TabPanel idPrefix={`map-${table.table_name}`} id="quality" active={tab === "quality"}>
        <div className="grid gap-3 md:grid-cols-3">
          <QualityBlock icon={<TriangleAlert />} title="Unreadable values become NULL" empty="None">
            {invalid.map(([column, count]) => <Badge key={column} tone="warning">{column}: {count}</Badge>)}
          </QualityBlock>
          <QualityBlock icon={<Database />} title="Profit center" empty="Not mapped">
            {pcs.map(([status, count]) => <Badge key={status} tone={status === "kept" || status.startsWith("filled") || status === "corrected" ? "neutral" : "warning"}>{humanize(status)}: {count}</Badge>)}
          </QualityBlock>
          <QualityBlock icon={<Info />} title="Silver columns left empty" empty="None">
            {(quality.unmapped_silver_columns ?? []).map((column) => <Badge key={column} tone="neutral">{column}</Badge>)}
          </QualityBlock>
        </div>
      </TabPanel>
    </SectionCard>
  );
}

/** The Silver side of a pairing, fixed: the DRT column, its business label and its type. */
function SilverHead({ pick, column }: { pick: SilverPick; column?: SilverColumnDef }) {
  return (
    <div className="min-w-0">
      <p className="truncate font-mono text-[12.5px] font-semibold text-ink-900" title={pick.silver_column}>{pick.silver_column}</p>
      <p className="truncate text-caption text-ink-500" title={column ? `DRT column: ${column.drt_name} (${column.data_type})` : undefined}>
        {column ? `${column.drt_name} · ${column.data_type}` : ""}
      </p>
    </div>
  );
}

/** The bronze side, chosen: which bronze column fills this DRT column, and who voted for it. */
function BronzePick({
  pick,
  bronze,
  busy,
  onChange,
}: {
  pick: SilverPick;
  bronze: Map<string, SilverMappingRow>;
  busy: boolean;
  onChange: (bronze: string | null) => void;
}) {
  const options = useMemo(() => {
    const others = (row: SilverMappingRow) =>
      row.bronze_column !== pick.bronze_column && row.silver_column
        ? `loads ${[row.silver_column, ...row.also].join(", ")}`
        : null;
    const voted = new Set(pick.candidates.map((candidate) => candidate.bronze_column));
    const suggested: SelectOption<string>[] = pick.candidates.map((candidate) => {
      const row = bronze.get(candidate.bronze_column);
      return {
        value: candidate.bronze_column,
        label: candidate.bronze_column,
        group: "Suggested by vote",
        description: (
          <span className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
            <Votes votes={candidate.votes} />
            {row?.source_header && row.source_header !== row.bronze_column && <span>“{row.source_header}”</span>}
            {row && others(row) && <span className="text-ink-500">{others(row)}</span>}
          </span>
        ),
        meta: candidate.recommended ? <Badge tone="brand" icon={<Star />}>Recommended</Badge> : undefined,
      };
    });
    const rest: SelectOption<string>[] = [...bronze.values()]
      .filter((row) => !voted.has(row.bronze_column))
      .map((row) => ({
        value: row.bronze_column,
        label: row.bronze_column,
        group: "All bronze columns",
        description: [row.source_header && row.source_header !== row.bronze_column ? `“${row.source_header}”` : null,
          row.samples.slice(0, 2).join(", ") || null, others(row)].filter(Boolean).join(" · ") || undefined,
      }));
    return [...suggested, ...rest, { value: NONE, label: "None", group: "Not loaded", description: "This DRT column loads empty" }];
  }, [pick, bronze]);
  const chosen = pick.bronze_column ? bronze.get(pick.bronze_column) : null;
  const detail = (
    <span className="block space-y-1">
      {pick.votes.map((vote) => (
        <span key={`${vote.method}-${vote.second_choice}`} className="block"><strong className="font-semibold">{METHOD[vote.method]}:</strong> {vote.reason}</span>
      ))}
      {pick.selection === "manual" && <span className="block">Chosen by the reviewer.</span>}
      {pick.candidates.length > 1 && (
        <span className="block text-ink-300">Also voted for: {pick.candidates.filter((c) => c.bronze_column !== pick.bronze_column).map((c) => c.bronze_column).join(", ")}</span>
      )}
    </span>
  );
  return (
    <div className="min-w-0">
      <Select<string>
        value={pick.bronze_column}
        options={options}
        onChange={(value) => onChange(value === NONE ? null : value)}
        label={`Bronze column for ${pick.silver_column}`}
        hideLabel
        placeholder={pick.candidates.length ? `${plural(pick.candidates.length, "suggestion")}: choose…` : "Choose a bronze column…"}
        disabled={busy}
        width={400}
        className="w-full"
        hideSelectedMeta
      />
      {pick.bronze_column ? (
        <>
          <Tooltip content={detail}>
            <span tabIndex={0} className="mt-1.5 inline-flex flex-wrap items-center gap-x-2.5 gap-y-1 rounded focus-visible:outline-none focus-visible:shadow-focus">
              {pick.votes.length ? <Votes votes={pick.votes} /> : <span className="text-caption text-ink-500">No votes</span>}
              {pick.selection === "manual" && <Badge tone="neutral" icon={<UserCheck />}>Manual</Badge>}
            </span>
          </Tooltip>
          {chosen && (
            <p className="mt-0.5 truncate text-caption text-ink-500"
              title={[chosen.source_header && `Header in the file: ${chosen.source_header}`, chosen.samples.join(", ")].filter(Boolean).join("\n")}>
              {[chosen.source_header && chosen.source_header !== chosen.bronze_column ? `“${chosen.source_header}”` : null,
                chosen.samples.join(", ") || null].filter(Boolean).join(" · ")}
            </p>
          )}
        </>
      ) : (
        <p className="mt-1.5 text-caption text-ink-500">
          {pick.candidates.length ? `Votes for ${pick.candidates.map((c) => c.bronze_column).slice(0, 2).join(", ")}${pick.candidates.length > 2 ? "…" : ""}` : "No method found a match"}
        </p>
      )}
    </div>
  );
}

/** A bronze column nothing loads: which DRT column it should fill, if any. Unmapped DRT columns first. */
function LoadInto({
  row,
  picks,
  drt,
  busy,
  onAssign,
}: {
  row: SilverMappingRow;
  picks: SilverPick[];
  drt: Map<string, SilverColumnDef>;
  busy: boolean;
  onAssign: (silver: string, bronze: string | null) => void;
}) {
  const options = useMemo(() => {
    const voted = new Map(row.candidates.filter((c) => c.silver_column && c.support).map((c) => [c.silver_column as string, c]));
    return [...picks]
      .sort((a, b) => Number(Boolean(a.bronze_column)) - Number(Boolean(b.bronze_column)) || Number(voted.has(b.silver_column)) - Number(voted.has(a.silver_column)))
      .map((pick): SelectOption<string> => ({
        value: pick.silver_column,
        label: pick.silver_column,
        group: pick.bronze_column ? "Already mapped" : "Not mapped yet",
        description: (
          <span className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
            <span>{drt.get(pick.silver_column)?.drt_name}</span>
            {voted.has(pick.silver_column) && <Votes votes={voted.get(pick.silver_column)!.votes} />}
            {pick.bronze_column && <span className="text-amber-700">now from {pick.bronze_column}</span>}
          </span>
        ),
      }));
  }, [row, picks, drt]);
  const best = row.candidates.find((candidate) => candidate.silver_column && candidate.support);
  return (
    <div className="min-w-0">
      <Select<string> value={null} options={options} onChange={(silver) => onAssign(silver, row.bronze_column)}
        label={`Load ${row.bronze_column} into`} hideLabel placeholder="Load into…" disabled={busy} width={400} className="w-full" />
      <p className="mt-1.5 truncate text-caption text-ink-500" title={row.reason}>
        {best ? `Closest: ${best.silver_column}` : "No method found a match"}
      </p>
    </div>
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

/** The bronze side of a mapping: the column, its header as the file wrote it, and sample values. */
function BronzeCell({ row }: { row: SilverMappingRow }) {
  return (
    <div className="min-w-0">
      <p className="truncate font-mono text-[12.5px] font-medium text-ink-900" title={row.bronze_column}>{row.bronze_column}</p>
      {row.source_header && row.source_header !== row.bronze_column && (
        <p className="truncate text-caption text-ink-600" title={`Header in the file: ${row.source_header}`}>“{row.source_header}”</p>
      )}
      {row.samples.length > 0 && (
        <p className="truncate text-caption text-ink-500" title={row.samples.join(", ")}>{row.samples.join(", ")}</p>
      )}
    </div>
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
  const load = useCallback(() => {
    setRows(null);
    api.silverMapping().then(setRows).catch((err) => toast({ severity: "error", title: toError(err).body.message }));
  }, [toast]);
  useEffect(load, [load]);
  const options = useMemo(
    () => targetsOf(catalog).map((column) => ({ value: column.name, label: column.name, description: `${column.drt_name} · ${column.data_type}` })),
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
  const visible = useMemo(() => (rows ?? []).filter(
    (row) =>
      (profitCenter === ALL || row.profit_center === profitCenter) &&
      (!needle || [row.profit_center, row.pc_column, row.drt_column ?? "", row.silver_column_name ?? ""].some((v) => v.toLowerCase().includes(needle))),
  ), [rows, profitCenter, needle]);
  const centerCount = useMemo(() => new Set(visible.map((row) => row.profit_center)).size, [visible]);

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
      <p className="mb-3 text-caption text-ink-500">
        {plural(visible.length, "mapping")} across {plural(centerCount, "profit center")}: each source column under the Silver column it fills.
      </p>
      {visible.length ? (
        <PagedBands
          items={visible}
          keyOf={(row) => `${row.profit_center}|${row.pc_column}|${row.silver_column_name}|${row.drt_column}`}
          caption="DRT column mapping"
          noun="mapping"
          reset={`${profitCenter}|${needle}`}
          bottomLabel="Source column"
          attention={(row) => !row.silver_column_name}
          top={(row) => (
            <Select<string> value={row.silver_column_name} options={options} onChange={(value) => save(row, value)}
              label={`Silver column for ${row.pc_column} (${row.profit_center})`} hideLabel placeholder="Not resolved" width={300} className="w-full" />
          )}
          bottom={(row) => (
            <div className="flex min-w-0 items-start gap-1">
              <div className="min-w-0 flex-1">
                <p className="truncate font-mono text-[12.5px] font-medium text-ink-900" title={row.pc_column}>{row.pc_column}</p>
                <p className="truncate text-caption text-ink-500" title={row.drt_column ? `DRT column: ${row.drt_column}` : undefined}>
                  <span className="font-mono">{row.profit_center}</span> · {row.drt_column ? `DRT: ${row.drt_column}` : "No DRT column"}
                </p>
              </div>
              <IconButton label={`Remove the mapping of ${row.pc_column} for ${row.profit_center}`} className="!h-7 !w-7 shrink-0" onClick={() => remove(row)}>
                <Trash2 />
              </IconButton>
            </div>
          )}
        />
      ) : (
        <EmptyState compact icon={<BookCheck />} title="No matching mappings" description="Try another profit center or search." />
      )}
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
