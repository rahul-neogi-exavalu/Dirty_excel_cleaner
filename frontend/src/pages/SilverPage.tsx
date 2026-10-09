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
  Link2,
  ListChecks,
  Plus,
  RefreshCw,
  Rows3,
  ServerOff,
  ShieldCheck,
  Sigma,
  Star,
  Table2,
  Trash2,
  TriangleAlert,
  Undo2,
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
  SilverColumnDef,
  SilverJoinHow,
  SilverLink,
  SilverMappingRow,
  SilverPick,
  SilverRun,
  SilverTableReview,
  SilverVote,
} from "../api/types";
import { PageHeader, SectionCard } from "../components/layout/Layout";
import { PagedBands, type Measure } from "../components/MappingBands";
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

const METHOD: Record<MatchMethod, string> = {
  saved: "Saved", exact: "Exact", fuzzy: "Fuzzy", semantic: "Semantic", ai: "AI", overlap: "Shared values",
};
const DOT: Record<MatchMethod, string> = {
  saved: "bg-ink-500",
  exact: "bg-emerald-500",
  fuzzy: "bg-sky-400",
  semantic: "bg-sky-700",
  ai: "bg-brand-600",
  overlap: "bg-amber-500",
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
                  <td className="px-2 py-2.5">
                    <span className="block font-mono font-medium text-ink-900">{load.table_name}</span>
                    {load.aggregated && (
                      <span className="mt-0.5 inline-flex items-center gap-1 text-caption text-amber-800" title="The profit center's own aggregates: loaded into silver_aggregate as reported">
                        <Sigma className="h-3 w-3" aria-hidden />Aggregated · to silver_aggregate
                      </span>
                    )}
                  </td>
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
  const reviewed = run.tables.filter((table) => !table.joined_into);
  const rows = reviewed.reduce((sum, table) => sum + (table.quality.rows_loaded ?? table.quality.rows ?? 0), 0);
  const picks = reviewed.filter((table) => !table.aggregated).flatMap((table) => table.targets);
  const mapped = picks.filter((pick) => pick.bronze_column).length;
  const aggregatePicks = reviewed.filter((table) => table.aggregated).flatMap((table) => table.targets);
  const aggregateMapped = aggregatePicks.filter((pick) => pick.bronze_column).length;
  const saved = [...picks, ...aggregatePicks].filter((pick) => pick.votes.some((vote) => vote.method === "saved")).length;
  const unloaded = reviewed.reduce((sum, table) => sum + table.mapping.filter((row) => !row.silver_column).length, 0);
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

  const mutate = async (call: () => Promise<SilverRun>) => {
    setBusy(true);
    try {
      onChange(await call());
    } catch (err) {
      const apiError = toError(err);
      toast({ severity: "error", title: apiError.body.message, description: apiError.body.advice ?? undefined });
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
            {picks.length > 0 && (
              <div className="flex items-center gap-1.5">
                <Columns3 className="h-4 w-4 text-ink-500" aria-hidden />
                <dt className="text-caption text-ink-500">DRT columns mapped</dt>
                <dd className="num text-body font-semibold text-ink-900">{mapped} of {picks.length}</dd>
              </div>
            )}
            {aggregatePicks.length > 0 && (
              <div className="flex items-center gap-1.5">
                <Sigma className="h-4 w-4 text-ink-500" aria-hidden />
                <dt className="text-caption text-ink-500">Aggregate columns mapped</dt>
                <dd className="num text-body font-semibold text-ink-900">{aggregateMapped}</dd>
              </div>
            )}
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

      {run.links.map((link) => (
        <JoinPanel key={`${link.left_table}|${link.right_table}`} run={run} link={link} busy={busy}
          onChange={(change) => mutate(() => api.joinSilverTables(run.id, change))} />
      ))}

      <TablePicker tables={run.tables} catalog={catalog} busy={busy} onAssign={assign}
        aggregateTargets={run.aggregate_targets}
        onSpread={(change) => mutate(() => api.spreadSilverTable(run.id, change))} />
    </div>
  );
}

/* ---- files that came together: joined ------------------------------------------- */

const HOW: { id: SilverJoinHow; label: string; hint: string }[] = [
  { id: "left", label: "Left", hint: "Every row of the data table; list columns empty where nothing matches" },
  { id: "inner", label: "Inner", hint: "Only the rows that match" },
  { id: "right", label: "Right", hint: "Every row of the list table" },
];

/**
 * Two tables of files that came together -- the transactions and the broker list sent with
 * them -- joined into one before Silver. The keys are suggested by the same votes as the
 * mapping, and by the values themselves (a column whose values are the other's).
 */
function JoinPanel({ run, link, busy, onChange }: {
  run: SilverRun;
  link: SilverLink;
  busy: boolean;
  onChange: (change: { left_table: string; right_table: string; how: SilverJoinHow | null; keys: { left: string; right: string }[]; ignore_case: boolean }) => void;
}) {
  const tables = new Map(run.tables.map((table) => [table.table_name, table]));
  const left = tables.get(link.left_table);
  const right = tables.get(link.right_table);
  const keeper = left?.join ? left : right?.join ? right : null;
  const asked = keeper?.join?.asked;
  const initial = asked ? asked.keys.map(([a, b]) => ({ left: a, right: b }))
    : link.keys.length ? [{ left: link.keys[0].left, right: link.keys[0].right }] : [{ left: "", right: "" }];
  const [how, setHow] = useState<SilverJoinHow>(asked?.how ?? "left");
  const [keys, setKeys] = useState(initial);
  const [ignoreCase, setIgnoreCase] = useState(keeper?.join?.ignore_case ?? true);
  useEffect(() => {
    setHow(asked?.how ?? "left");
    setKeys(initial);
    // Reset when the join itself changes, not on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [asked?.how, JSON.stringify(asked?.keys)]);
  if (!left || !right) return null;
  const own = (table: SilverTableReview) => table.columns.filter((column) => !column.includes("."));
  const leftOptions: SelectOption<string>[] = own(left).map((column) => ({ value: column, label: column }));
  const rightOptions: SelectOption<string>[] = own(right).map((column) => {
    const vote = link.keys.find((pair) => pair.right === column);
    return {
      value: column, label: column,
      description: vote ? `${vote.methods.map((method) => METHOD[method]).join(" + ")}${vote.overlap !== null ? ` · ${pct(vote.overlap)} of values shared` : ""}` : undefined,
    };
  });
  const stats = keeper?.join?.stats && "rows" in keeper.join.stats ? keeper.join.stats : null;
  const ready = keys.every((key) => key.left && key.right);
  const apply = () => onChange({ left_table: link.left_table, right_table: link.right_table, how, keys, ignore_case: ignoreCase });
  return (
    <SectionCard
      icon={<Link2 />}
      title={`${link.left_table} ⟷ ${link.right_table}`}
      description={`Files that came together${link.saved ? " · joined as approved before" : ""}: ${right.loads.map((load) => load.file_name).join(", ")} came with ${left.loads.map((load) => load.file_name).join(", ")}.`}
      actions={keeper ? <Badge tone="success">Joined</Badge> : <Badge tone="warning">Join to load</Badge>}
    >
      <div className="flex flex-wrap items-end gap-4">
        <div role="radiogroup" aria-label="Join type" className="inline-flex rounded-lg border border-ink-200 bg-ink-50 p-0.5">
          {HOW.map((option) => (
            <button key={option.id} type="button" role="radio" aria-checked={how === option.id} title={option.hint} disabled={busy}
              onClick={() => setHow(option.id)}
              className={clsx("rounded-md px-3 py-1.5 text-body font-medium transition-colors disabled:opacity-50",
                how === option.id ? "bg-white text-ink-900 shadow-sm" : "text-ink-600 hover:text-ink-900")}>
              {option.label}
            </button>
          ))}
        </div>
        <Checkbox label="Match ignoring case and extra spaces" checked={ignoreCase} onChange={setIgnoreCase} disabled={busy} />
      </div>
      <p className="mt-2 text-caption text-ink-500">{HOW.find((option) => option.id === how)?.hint}.</p>

      <div className="mt-4 space-y-2">
        {keys.map((key, index) => (
          <div key={index} className="grid items-end gap-2 sm:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)_auto]">
            <Select<string> label={index ? `${link.left_table} (key ${index + 1})` : link.left_table} value={key.left}
              options={leftOptions} placeholder="Key column" disabled={busy}
              onChange={(value) => setKeys(keys.map((item, at) => (at === index ? { ...item, left: value } : item)))} />
            <span className="pb-2 text-center text-body font-semibold text-ink-400" aria-hidden>=</span>
            <Select<string> label={index ? `${link.right_table} (key ${index + 1})` : link.right_table} value={key.right}
              options={rightOptions} placeholder="Key column" disabled={busy}
              onChange={(value) => setKeys(keys.map((item, at) => (at === index ? { ...item, right: value } : item)))} />
            <IconButton label="Remove this key" disabled={busy || keys.length === 1}
              onClick={() => setKeys(keys.filter((_, at) => at !== index))}><X className="h-4 w-4" /></IconButton>
          </div>
        ))}
      </div>
      {link.keys.length > 0 && (
        <p className="mt-2 flex flex-wrap items-center gap-1.5 text-caption text-ink-500">
          Suggested:
          {link.keys.slice(0, 4).map((pair) => (
            <button key={`${pair.left}=${pair.right}`} type="button" disabled={busy}
              onClick={() => setKeys([{ left: pair.left, right: pair.right }])}
              className="rounded-md border border-ink-200 bg-white px-2 py-0.5 font-mono text-[11.5px] text-ink-700 hover:border-brand-300">
              {pair.left} = {pair.right}
              <span className="ml-1 font-sans text-ink-400">{pair.methods.map((method) => METHOD[method]).join(" + ")}</span>
            </button>
          ))}
        </p>
      )}

      {stats && (
        <div className="mt-4 flex flex-wrap gap-2">
          <Badge tone="success">{formatNumber(stats.matched)} of {formatNumber(stats.rows)} rows matched</Badge>
          {stats.unmatched > 0 && <Badge tone={keeper?.join?.how === "inner" ? "warning" : "neutral"}>{formatNumber(stats.unmatched)} {keeper?.join?.how === "inner" ? "left out" : "without a match"}</Badge>}
          {stats.repeated_keys > 0 && <Badge tone="danger">{plural(stats.repeated_keys, "key")} repeated in {keeper?.join?.table}</Badge>}
        </div>
      )}
      {stats && stats.duplicates.length > 0 && (
        <Alert tone="error" className="mt-3" title="A key repeats in the joined table">
          Each repeat would count its rows twice: {stats.duplicates.slice(0, 5).map((item) => `${item.key} (${item.rows} rows)`).join(", ")}. Add a key column so each row matches one.
        </Alert>
      )}

      <div className="mt-4 flex flex-wrap gap-2 border-t border-ink-100 pt-3">
        <Button size="sm" variant="ghost" icon={<Plus />} disabled={busy} onClick={() => setKeys([...keys, { left: "", right: "" }])}>Add a key column</Button>
        <span className="flex-1" />
        {keeper && (
          <Button size="sm" variant="ghost" icon={<Undo2 />} disabled={busy}
            onClick={() => onChange({ left_table: link.left_table, right_table: link.right_table, how: null, keys: [], ignore_case: ignoreCase })}>
            Undo the join
          </Button>
        )}
        <Button size="sm" variant="primary" icon={<Link2 />} disabled={busy || !ready} onClick={apply}>
          {keeper ? "Join again" : "Join"}
        </Button>
      </div>
    </SectionCard>
  );
}

/** Several bronze tables are reviewed one at a time, picked from a list, rather than stacked. */
function TablePicker({
  tables: all,
  catalog,
  busy,
  onAssign,
  aggregateTargets,
  onSpread,
}: {
  tables: SilverTableReview[];
  catalog: SilverCatalog;
  busy: boolean;
  onAssign: (table: string, silver: string, bronze: string | null) => void;
  aggregateTargets: { name: string; measure: boolean }[];
  onSpread: (change: { table_name: string; columns: string[]; measure: string | null; dimension: string | null; label?: string | null; rollups?: string[] }) => void;
}) {
  // A table joined into another is mapped there: one review for the pair.
  const tables = all.filter((item) => !item.joined_into);
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
            description: `${plural(item.mapping.length, "column")} · ${formatNumber(item.quality.rows)} rows${item.aggregated ? " · aggregated" : item.join ? ` · joined with ${item.join.table}` : ""}`,
            meta: <Badge tone="neutral">{done} mapped</Badge>,
          };
        })}
      />
    ) : null;
  return (
    <div className="space-y-4">
      {table.aggregated && (
        <SpreadPanel key={`spread-${table.table_name}`} table={table} targets={aggregateTargets} busy={busy}
          onChange={(change) => onSpread({ table_name: table.table_name, ...change })} />
      )}
      <TableMapping
        key={table.table_name}
        table={table}
        catalog={catalog}
        busy={busy}
        picker={picker}
        onAssign={(silver, bronze) => onAssign(table.table_name, silver, bronze)}
      />
    </div>
  );
}

/**
 * A profit center's own aggregates: columns that are one measure across a dimension (premium
 * by product line, a column each) are unpivoted, their headers becoming the dimension's values;
 * columns totalling others are roll-ups, left out. Silver keeps the figures as reported.
 */
function SpreadPanel({ table, targets, busy, onChange }: {
  table: SilverTableReview;
  targets: { name: string; measure: boolean }[];
  busy: boolean;
  onChange: (change: { columns: string[]; measure: string | null; dimension: string | null; label?: string | null; rollups?: string[] }) => void;
}) {
  const spread = table.spread;
  const [label, setLabel] = useState(spread?.label ?? "category");
  useEffect(() => setLabel(spread?.label ?? "category"), [spread?.label]);
  const measures: SelectOption<string>[] = targets.filter((item) => item.measure).map((item) => ({ value: item.name, label: item.name }));
  const dimensions: SelectOption<string>[] = [
    { value: NONE, label: "A generic dimension", description: `aggregation_dimension, named “${label}”` },
    ...targets.filter((item) => !item.measure).map((item) => ({ value: item.name, label: item.name })),
  ];
  const own = table.columns.filter((column) => !table.rollups.includes(column) && !table.lineage.includes(column));
  const send = (change: Partial<{ columns: string[]; measure: string | null; dimension: string | null; label: string | null; rollups: string[] }>) =>
    onChange({ columns: spread?.columns ?? [], measure: spread?.measure ?? null, dimension: spread?.dimension ?? null,
      label: spread?.label ?? null, ...change });
  const toggle = (column: string) => {
    const columns = spread?.columns ?? [];
    send({ columns: columns.includes(column) ? columns.filter((item) => item !== column) : [...columns, column] });
  };
  return (
    <SectionCard
      icon={<Sigma />}
      title="Aggregated figures"
      description={`${table.table_name} holds the profit center's own aggregates: it loads into silver_aggregate as reported, never into the transaction table.`}
      actions={<Badge tone="warning">{formatNumber(table.quality.rows_loaded ?? 0)} aggregate rows</Badge>}
    >
      <div className="grid gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div>
          <p className="text-caption font-semibold text-ink-700">One measure spread across columns</p>
          <p className="mb-2 text-caption text-ink-500">Their headers are the values of a dimension (a product line each): one row per cell.</p>
          <div className="flex flex-wrap gap-1.5">
            {own.map((column) => {
              const on = spread?.columns.includes(column) ?? false;
              return (
                <button key={column} type="button" aria-pressed={on} disabled={busy} onClick={() => toggle(column)}
                  className={clsx("rounded-md border px-2 py-1 font-mono text-[12px] transition-colors disabled:opacity-50",
                    on ? "border-brand-400 bg-brand-50 text-brand-800" : "border-ink-200 bg-white text-ink-600 hover:border-ink-300")}>
                  {on && <Check className="mr-1 inline h-3 w-3" aria-hidden />}{column}
                </button>
              );
            })}
          </div>
          {table.rollups.length > 0 && (
            <p className="mt-3 flex flex-wrap items-center gap-1.5 text-caption text-ink-500">
              Roll-ups, left out:
              {table.rollups.map((column) => (
                <span key={column} className="inline-flex items-center gap-1 rounded-md border border-ink-200 bg-ink-50 px-2 py-0.5 font-mono text-[11.5px] text-ink-600">
                  {column}
                  <button type="button" aria-label={`Load ${column} after all`} disabled={busy} className="text-ink-400 hover:text-ink-700"
                    onClick={() => send({ rollups: table.rollups.filter((item) => item !== column) })}><X className="h-3 w-3" /></button>
                </span>
              ))}
              {table.quality.rollup_rows ? <span>· {plural(table.quality.rollup_rows, "total row")} too</span> : null}
            </p>
          )}
        </div>
        <div className="space-y-3">
          <Select<string> label="The measure the cells are" value={spread?.measure ?? ""} options={measures} placeholder="Choose the measure"
            disabled={busy || !spread?.columns.length} onChange={(value) => send({ measure: value })} />
          <Select<string> label="The dimension the headers fill" value={spread?.dimension ?? NONE} options={dimensions}
            disabled={busy || !spread?.columns.length} onChange={(value) => send({ dimension: value === NONE ? null : value })} />
          {!spread?.dimension && spread?.columns.length ? (
            <label className="block">
              <span className="mb-1 block text-caption font-medium text-ink-600">Name of the generic dimension</span>
              <input value={label} disabled={busy} onChange={(event) => setLabel(event.target.value)}
                onBlur={() => label.trim() && label !== spread?.label && send({ label: label.trim() })}
                className="h-9 w-full rounded-lg border border-ink-200 px-3 text-body" />
            </label>
          ) : null}
        </div>
      </div>
      {table.quality.measures && Object.keys(table.quality.measures).length > 0 && (
        <p className="mt-4 border-t border-ink-100 pt-3 text-caption text-ink-500">
          Totals as loaded: {Object.entries(table.quality.measures).map(([name, value]) => `${name} ${formatNumber(Math.round(value * 100) / 100)}`).join(" · ")}
        </p>
      )}
    </SectionCard>
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
  const noun = table.aggregated ? "aggregate column" : "DRT column";
  const Noun = table.aggregated ? "Aggregate column" : "DRT column";
  const bronze = useMemo(() => new Map(table.mapping.map((row) => [row.bronze_column, row])), [table.mapping]);
  const mapped = useMemo(() => table.targets.filter((pick) => pick.bronze_column), [table.targets]);
  const open = useMemo(() => table.targets.filter((pick) => !pick.bronze_column), [table.targets]);
  const unloaded = useMemo(() => table.mapping.filter((row) => !row.silver_column), [table.mapping]);
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
  const pickWidth = (pick: SilverPick, measure: Measure) =>
    Math.max(silverHeadWidth(pick, drt.get(pick.silver_column), measure), bronzePickWidth(pick, bronze, measure));

  return (
    <SectionCard
      icon={<Table2 />}
      title={table.table_name}
      description={`${table.loads.map((load) => load.file_name).join(", ")} · ${table.pc_id ?? "no profit center"}${table.join ? ` · joined with ${table.join.table} (its columns are prefixed with its name)` : ""}`}
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
          <PagedBands items={mapped} keyOf={(pick) => pick.silver_column} caption={`${Noun}s mapped in ${table.table_name}`}
            noun={noun} reset={tab} top={silverTop} bottom={bronzeBottom} width={pickWidth} />
        ) : (
          <EmptyState compact icon={<Columns3 />} title={`No ${noun} mapped yet`} description="Choose bronze columns on the Not mapped tab." />
        )}
      </TabPanel>
      <TabPanel idPrefix={`map-${table.table_name}`} id="open" active={tab === "open"}>
        {open.length ? (
          <>
            <p className="mb-3 text-caption text-ink-500">
              No bronze column was recommended for these. Choose one where the file has it; a {noun} left here loads empty.
            </p>
            <PagedBands items={open} keyOf={(pick) => pick.silver_column} caption={`${Noun}s not mapped in ${table.table_name}`}
              noun={noun} reset={tab} top={silverTop} bottom={bronzeBottom} width={pickWidth}
              attention={(pick) => pick.candidates.length > 0} />
          </>
        ) : (
          <EmptyState compact icon={<Check />} title={`Every ${noun} is mapped`} description="Nothing left to choose for this table." />
        )}
      </TabPanel>
      <TabPanel idPrefix={`map-${table.table_name}`} id="unloaded" active={tab === "unloaded"}>
        {unloaded.length ? (
          <>
            <p className="mb-3 text-caption text-ink-500">
              No {noun} takes these bronze columns, so they are not loaded to Silver. Load one into a {noun} here if it belongs.
            </p>
            <PagedBands items={unloaded} keyOf={(row) => row.bronze_column} caption={`Bronze columns of ${table.table_name} not loaded`}
              noun="bronze column" reset={tab} topLabel="Bronze column" bottomLabel="Load into"
              width={(row, measure) => Math.max(bronzeCellWidth(row, measure), loadIntoWidth(row, measure))}
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

/* ---- what a cell needs, measured before it is drawn ------------------------ */

/** A closed select around its value: padding, gap, chevron, border. */
const SELECT_CHROME = 2 * 12 + 8 + 16 + 2;
/** Sample values are data, not names: shown in full up to this width. */
const SAMPLES_MAX = 220;
/** A small badge around its text: padding, icon, gap, border. */
const BADGE_CHROME = 2 * 8 + 12 + 4 + 2;

const votesWidth = (votes: SilverVote[], measure: Measure) =>
  votes.reduce((sum, vote, index) => {
    const score = vote.second_choice ? "2nd choice" : SCORED.has(vote.method) && vote.score < 0.995 ? pct(vote.score) : "";
    return sum + (index ? 10 : 0) + 6 + 4 + measure(METHOD[vote.method], "caption") + (score ? 4 + measure(score, "caption") : 0);
  }, 0);
const samplesWidth = (samples: string[], measure: Measure) => Math.min(measure(samples.join(", "), "caption"), SAMPLES_MAX);
const quoted = (header: string) => `“${header}”`;
/** The header in full beside the samples, on one line. */
const sourceLineWidth = (row: SilverMappingRow, measure: Measure) => {
  const header = row.source_header && row.source_header !== row.bronze_column ? measure(quoted(row.source_header), "caption") : 0;
  const samples = row.samples.length ? samplesWidth(row.samples, measure) : 0;
  return header && samples ? header + 6 + samples : header + samples;
};
const openLine = (pick: SilverPick) =>
  pick.candidates.length
    ? `Votes for ${pick.candidates[0].bronze_column}${pick.candidates.length > 1 ? ` +${pick.candidates.length - 1} more` : ""}`
    : "No method found a match";
const pickPlaceholder = (pick: SilverPick) =>
  pick.candidates.length ? `${plural(pick.candidates.length, "suggestion")}: choose…` : "Choose a bronze column…";

function silverHeadWidth(pick: SilverPick, column: SilverColumnDef | undefined, measure: Measure) {
  const label = column ? measure(column.drt_name, "caption") + 6 + measure(column.data_type, "chip") + 14 : 0;
  return Math.max(measure(pick.silver_column, "name"), label);
}

function bronzePickWidth(pick: SilverPick, bronze: Map<string, SilverMappingRow>, measure: Measure) {
  const select = measure(pick.bronze_column ?? pickPlaceholder(pick), "value") + SELECT_CHROME;
  if (!pick.bronze_column) return Math.max(select, measure(openLine(pick), "caption"));
  const chosen = bronze.get(pick.bronze_column);
  const votes = (pick.votes.length ? votesWidth(pick.votes, measure) : measure("No votes", "caption"))
    + (pick.selection === "manual" ? 10 + measure("Manual", "caption") + BADGE_CHROME : 0);
  return Math.max(select, votes, chosen ? sourceLineWidth(chosen, measure) : 0);
}

function bronzeCellWidth(row: SilverMappingRow, measure: Measure) {
  return Math.max(measure(row.bronze_column, "name"), sourceLineWidth(row, measure));
}

const closestLine = (row: SilverMappingRow) => {
  const best = row.candidates.find((candidate) => candidate.silver_column && candidate.support);
  return best?.silver_column ? `Closest: ${best.silver_column}` : "No method found a match";
};

function loadIntoWidth(row: SilverMappingRow, measure: Measure) {
  return Math.max(measure("Load into…", "value") + SELECT_CHROME, measure(closestLine(row), "caption"));
}

/* ---- the cells: one line per fact, sized to fit --------------------------- */

/** The Silver side of a pairing, fixed: the DRT column, its business label and its type. */
function SilverHead({ pick, column }: { pick: SilverPick; column?: SilverColumnDef }) {
  return (
    <div className="min-w-0">
      <p className="truncate font-mono text-[12.5px] font-semibold leading-5 text-ink-900">{pick.silver_column}</p>
      {column && (
        <p className="mt-1 flex items-center gap-1.5 text-caption leading-4 text-ink-500">
          <span className="truncate" title="DRT column">{column.drt_name}</span>
          <span className="shrink-0 rounded bg-white px-1.5 py-px font-mono text-[10.5px] text-ink-500 ring-1 ring-inset ring-ink-200">{column.data_type}</span>
        </p>
      )}
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
            {row?.source_header && row.source_header !== row.bronze_column && <span>{quoted(row.source_header)}</span>}
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
        description: [row.source_header && row.source_header !== row.bronze_column ? quoted(row.source_header) : null,
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
        placeholder={pickPlaceholder(pick)}
        disabled={busy}
        width={400}
        className="w-full"
        hideSelectedMeta
      />
      {pick.bronze_column ? (
        <>
          <Tooltip content={detail}>
            <span tabIndex={0} className="mt-1.5 flex items-center gap-x-2.5 rounded focus-visible:outline-none focus-visible:shadow-focus">
              {pick.votes.length ? <Votes votes={pick.votes} className="flex-nowrap" /> : <span className="text-caption text-ink-500">No votes</span>}
              {pick.selection === "manual" && <Badge tone="neutral" icon={<UserCheck />}>Manual</Badge>}
            </span>
          </Tooltip>
          {chosen && <SourceLine row={chosen} />}
        </>
      ) : (
        <p className="mt-1.5 truncate text-caption leading-4 text-ink-500"
          title={pick.candidates.length ? `Voted for: ${pick.candidates.map((c) => c.bronze_column).join(", ")}` : undefined}>
          {openLine(pick)}
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
  return (
    <div className="min-w-0">
      <Select<string> value={null} options={options} onChange={(silver) => onAssign(silver, row.bronze_column)}
        label={`Load ${row.bronze_column} into`} hideLabel placeholder="Load into…" disabled={busy} width={400} className="w-full" />
      <p className="mt-1.5 truncate text-caption leading-4 text-ink-500" title={row.reason}>{closestLine(row)}</p>
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

/** A bronze column's header as the file wrote it, in full, then its sample values. */
function SourceLine({ row }: { row: SilverMappingRow }) {
  const header = row.source_header && row.source_header !== row.bronze_column ? row.source_header : null;
  if (!header && !row.samples.length) return null;
  return (
    <p className="mt-1 flex min-w-0 items-baseline gap-1.5 text-caption leading-4">
      {header && <span className="shrink-0 text-ink-600" title="Header in the file">{quoted(header)}</span>}
      {row.samples.length > 0 && (
        <span className="min-w-0 truncate text-ink-400" title={row.samples.join(", ")}>{row.samples.join(", ")}</span>
      )}
    </p>
  );
}

/** The bronze side of a mapping: the column, its header as the file wrote it, and sample values. */
function BronzeCell({ row }: { row: SilverMappingRow }) {
  return (
    <div className="min-w-0">
      <p className="truncate font-mono text-[12.5px] font-semibold leading-5 text-ink-900">{row.bronze_column}</p>
      <SourceLine row={row} />
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
          width={(row, measure) => Math.max(
            measure(row.silver_column_name ?? "Not resolved", "value") + SELECT_CHROME,
            measure(row.pc_column, "name") + 4 + 28,
            measure(`${row.profit_center} · ${row.drt_column ? `DRT: ${row.drt_column}` : "No DRT column"}`, "caption"),
          )}
          top={(row) => (
            <Select<string> value={row.silver_column_name} options={options} onChange={(value) => save(row, value)}
              label={`Silver column for ${row.pc_column} (${row.profit_center})`} hideLabel placeholder="Not resolved" width={300} className="w-full" />
          )}
          bottom={(row) => (
            <div className="flex min-w-0 items-start gap-1">
              <div className="min-w-0 flex-1">
                <p className="truncate font-mono text-[12.5px] font-semibold leading-5 text-ink-900">{row.pc_column}</p>
                <p className="mt-1 truncate text-caption leading-4 text-ink-500">
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
