import clsx from "clsx";
import {
  ArrowRight,
  BookCheck,
  Check,
  Columns3,
  Database,
  Info,
  Layers3,
  ListChecks,
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
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type {
  BronzeStatus,
  CleanupLoad,
  EligibleLoad,
  MatchMethod,
  SavedMapping,
  SilverCatalog,
  SilverCandidate,
  SilverMappingRow,
  SilverRun,
  SilverSummaryRow,
  SilverTableReview,
  SilverVote,
} from "../api/types";
import { PageHeader, SectionCard } from "../components/layout/Layout";
import { Badge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
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
const period = (start: string | null, end: string | null) => (!start ? "—" : start === end || !end ? start : `${start} → ${end}`);

export function SilverPage() {
  const [status, setStatus] = useState<BronzeStatus | null>(null);
  const [catalog, setCatalog] = useState<SilverCatalog | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [tab, setTab] = useState<"run" | "mapping" | "summary">("run");

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
                { id: "summary", label: "Summary", icon: <Sigma /> },
              ]}
            />
            <span className="flex items-center gap-3 text-caption text-ink-500">
              <span>{catalog.columns.length} Silver columns · {catalog.file}</span>
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
          <TabPanel idPrefix="silver" id="summary" active={tab === "summary"}>
            {tab === "summary" && <SummaryView />}
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
        <table className="w-full min-w-[680px] border-collapse text-table">
          <caption className="sr-only">Bronze loads eligible for Silver</caption>
          <thead className="bg-ink-50">
            <tr className="border-b border-ink-200 text-left text-caption font-semibold text-ink-600">
              <th scope="col" className="w-12 px-4 py-2.5">
                <Checkbox label={all ? "Clear all" : "Select all"} checked={all} indeterminate={!all && selected.size > 0}
                  onChange={(on) => setSelected(new Set(on ? loads.map((load) => load.ingestion_id) : []))} />
              </th>
              <th scope="col" className="px-2 py-2.5">Bronze table</th>
              <th scope="col" className="px-3 py-2.5">File</th>
              <th scope="col" className="px-3 py-2.5">Period</th>
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
                  <td className="num px-3 py-2.5 text-ink-700">{period(load.period_start, load.period_end)}</td>
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
        <TablePicker tables={run.tables} catalog={catalog} busy={busy} onEdit={edit} />
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
}: {
  tables: SilverTableReview[];
  catalog: SilverCatalog;
  busy: boolean;
  onEdit: (table: string, column: string, value: string) => void;
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
    />
  );
}

function TableMapping({
  table,
  catalog,
  busy,
  onEdit,
  picker,
}: {
  table: SilverTableReview;
  catalog: SilverCatalog;
  busy: boolean;
  onEdit: (column: string, value: string) => void;
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
    for (const row of table.mapping) if (row.silver_column) map.set(row.silver_column, row.bronze_column);
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
      description={`${table.loads.map((load) => load.file_name).join(", ")} · pc_id ${table.pc_id ?? "—"}`}
      actions={
        <span className="flex items-center gap-2">
          {split > 0 && <Badge tone="warning" icon={<Split />}>{split} split</Badge>}
          <Badge tone="neutral">{formatNumber(quality.rows)} rows</Badge>
        </span>
      }
    >
      {(picker || openRows.length > 0) && (
        <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          {picker ?? <span />}
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
              <MappingRow key={row.bronze_column} row={row} catalog={catalog} usedBy={usedBy} busy={busy} onEdit={(value) => onEdit(row.bronze_column, value)} />
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

function MappingRow({ row, catalog, usedBy, busy, onEdit }: { row: SilverMappingRow; catalog: SilverCatalog; usedBy: Map<string, string>; busy: boolean; onEdit: (value: string) => void }) {
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
    const rest: SelectOption<string>[] = catalog.columns
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
          <StatTile icon={<BookCheck />} label="Mappings saved" value={run.tables.reduce((sum, table) => sum + table.mapping.length, 0)} />
        </div>
      )}
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* Saved mapping (editable) and summary                                         */
/* -------------------------------------------------------------------------- */

function MappingView({ catalog }: { catalog: SilverCatalog }) {
  const toast = useToast();
  const [rows, setRows] = useState<SavedMapping[] | null>(null);
  const [query, setQuery] = useState("");
  const [tableKey, setTableKey] = useState("all");
  const list = useRef<HTMLDivElement>(null);
  const pageSize = useFitPageSize(list, { reserve: 130 });
  const load = useCallback(() => {
    setRows(null);
    api.silverMapping().then(setRows).catch((err) => toast({ severity: "error", title: toError(err).body.message }));
  }, [toast]);
  useEffect(load, [load]);
  const options = useMemo(
    () => [...catalog.columns.map((column) => ({ value: column.name, label: column.name, description: column.drt_name })), { value: IGNORE, label: "Ignore" }],
    [catalog],
  );

  const keyOf = (row: SavedMapping) => `${row.pc_id}|${row.bronze_table_name}`;
  const tableOptions = useMemo(() => {
    const counts = new Map<string, { row: SavedMapping; count: number }>();
    for (const row of rows ?? []) {
      const found = counts.get(keyOf(row));
      counts.set(keyOf(row), { row, count: (found?.count ?? 0) + 1 });
    }
    return [
      { value: "all", label: "All tables", description: plural(rows?.length ?? 0, "column") },
      ...[...counts.entries()].map(([value, { row, count }]) => ({
        value,
        label: row.bronze_table_name,
        description: `pc_id ${row.pc_id || "—"} · ${plural(count, "column")}`,
      })),
    ];
  }, [rows]);
  const needle = query.trim().toLowerCase();
  const visible = (rows ?? []).filter(
    (row) =>
      (tableKey === "all" || keyOf(row) === tableKey) &&
      (!needle || [row.pc_id, row.bronze_table_name, row.bronze_column_name, row.silver_column_name ?? ""].some((v) => v.toLowerCase().includes(needle))),
  );
  const paged = usePaged(visible, pageSize, `${tableKey}|${needle}`);

  if (!rows) return <div className="card space-y-2 p-6"><Skeleton className="h-10" /><Skeleton className="h-10" /></div>;
  if (!rows.length) return <div className="card"><EmptyState icon={<BookCheck />} title="No saved mappings yet" description="Approved Silver runs save their mapping here." /></div>;

  const save = async (row: SavedMapping, value: string) => {
    const silver = value === IGNORE ? null : value;
    try {
      await api.editSilverMapping({ pc_id: row.pc_id, bronze_table_name: row.bronze_table_name, bronze_column_name: row.bronze_column_name, silver_column_name: silver });
      setRows((current) => current?.map((item) => (item === row ? { ...item, silver_column_name: silver } : item)) ?? null);
      toast({ severity: "success", title: "Mapping updated", description: "Applies from the next Silver run." });
    } catch (err) {
      toast({ severity: "error", title: "Change not saved", description: toError(err).body.message });
    }
  };

  return (
    <SectionCard
      icon={<BookCheck />}
      title="Saved mapping"
      description="Approved bronze → Silver columns."
      actions={
        <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
          {tableOptions.length > 2 && (
            <Select<string> label="Bronze table" hideLabel value={tableKey} options={tableOptions} onChange={setTableKey} icon={<Table2 className="h-4 w-4" />} className="sm:w-64" />
          )}
          <SearchInput value={query} onChange={setQuery} placeholder="Search" label="Search mappings" className="sm:w-56" />
        </div>
      }
    >
      <div ref={list} className="overflow-hidden rounded-lg border border-ink-200">
      <div className="relative overflow-x-auto scroll-thin">
        <table className="w-full min-w-[760px] border-separate border-spacing-0 text-table">
          <caption className="sr-only">Saved column mapping</caption>
          <thead>
            <tr className="text-left text-caption font-semibold text-ink-600">
              {["pc_id", "Bronze table", "Bronze column", "DRT column", "Silver column"].map((label) => (
                <th key={label} scope="col" className="border-b border-ink-200 bg-ink-50 px-3 py-2.5">{label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {paged.slice.map((row) => (
              <tr key={`${row.pc_id}|${row.bronze_table_name}|${row.bronze_column_name}`} data-row>
                <td className="num border-b border-ink-100 px-3 py-2 text-ink-700">{row.pc_id}</td>
                <td className="border-b border-ink-100 px-3 py-2 font-mono text-ink-800">{row.bronze_table_name}</td>
                <td className="border-b border-ink-100 px-3 py-2 font-mono font-medium text-ink-900">{row.bronze_column_name}</td>
                <td className="border-b border-ink-100 px-3 py-2 text-ink-600">{row.drt_column_name ?? "—"}</td>
                <td className="w-[240px] border-b border-ink-100 px-3 py-1.5">
                  <Select<string> value={row.silver_column_name ?? IGNORE} options={options} onChange={(value) => save(row, value)} label={`Silver column for ${row.bronze_column_name}`} hideLabel width={300} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!visible.length && <EmptyState compact icon={<BookCheck />} title="No matching mappings" description="Try another table or search." />}
      </div>
      <Pagination {...paged} onPage={paged.setPage} noun="column" />
      </div>
    </SectionCard>
  );
}

function SummaryView() {
  const [rows, setRows] = useState<SilverSummaryRow[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const list = useRef<HTMLDivElement>(null);
  const paged = usePaged(rows ?? [], useFitPageSize(list, { fallbackRow: 37, reserve: 130 }));
  useEffect(() => {
    api.silverSummary().then(setRows).catch((err) => setError(toError(err)));
  }, []);
  if (error) return <Alert tone="error" title={error.body.message} />;
  if (!rows) return <div className="card space-y-2 p-6"><Skeleton className="h-10" /><Skeleton className="h-10" /></div>;
  if (!rows.length) return <div className="card"><EmptyState icon={<Sigma />} title="No summary yet" description="Load data to Silver first." /></div>;
  const total = rows.reduce((sum, row) => sum + row.row_count, 0);
  const premium = rows.reduce((sum, row) => sum + (row.premium_total ?? 0), 0);
  return (
    <SectionCard icon={<Sigma />} title="Summary" description="Rows and premium by profit center and accounting month." actions={<span className="num text-caption text-ink-600">{formatNumber(total)} rows · {premium.toLocaleString("en-US", { style: "currency", currency: "USD" })}</span>}>
      <div ref={list} className="overflow-hidden rounded-lg border border-ink-200">
      <div className="relative overflow-x-auto scroll-thin">
        <table className="w-full min-w-[640px] border-separate border-spacing-0 text-table">
          <caption className="sr-only">Silver summary</caption>
          <thead>
            <tr className="text-left text-caption font-semibold text-ink-600">
              {["pc_id", "Profit center", "Number", "Month", "Rows", "Premium"].map((label) => (
                <th key={label} scope="col" className={clsx("border-b border-ink-200 bg-ink-50 px-3 py-2.5", ["Rows", "Premium"].includes(label) && "text-right")}>{label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {paged.slice.map((row, index) => (
              <tr key={`${paged.page}-${index}`} data-row>
                <td className="num border-b border-ink-100 px-3 py-2 text-ink-700">{row.pc_id}</td>
                <td className="border-b border-ink-100 px-3 py-2 text-ink-900">{row.profit_center_name ?? "—"}</td>
                <td className="num border-b border-ink-100 px-3 py-2 text-ink-700">{row.profit_center_number ?? "—"}</td>
                <td className="num border-b border-ink-100 px-3 py-2 text-ink-700">{row.accounting_month?.slice(0, 7) ?? "—"}</td>
                <td className="num border-b border-ink-100 px-3 py-2 text-right text-ink-800">{formatNumber(row.row_count)}</td>
                <td className="num border-b border-ink-100 px-3 py-2 text-right text-ink-800">{row.premium_total?.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) ?? "—"}</td>
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
