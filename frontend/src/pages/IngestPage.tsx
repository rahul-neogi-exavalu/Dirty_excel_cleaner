import clsx from "clsx";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Columns3,
  Database,
  DatabaseZap,
  FileSpreadsheet,
  History,
  Info,
  ListChecks,
  RefreshCw,
  Rows3,
  ServerOff,
  ShieldCheck,
  Table2,
  TriangleAlert,
  UserCheck,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { BronzeStatus, BronzeTable, IngestAction, IngestPlan, PlanFile, PlanItem } from "../api/types";
import { ColumnChips } from "../components/AppendOutcome";
import { PageHeader, SectionCard } from "../components/layout/Layout";
import { Badge, type Tone } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Checkbox, LabeledCheckbox } from "../components/ui/Controls";
import { Alert, EmptyState, ProgressBar, Skeleton, StatTile, useToast } from "../components/ui/Feedback";
import { Modal, Tooltip } from "../components/ui/Overlay";
import { Pagination, useFitPageSize, usePaged } from "../components/ui/Pagination";
import { Segmented } from "../components/ui/Segmented";
import { Select } from "../components/ui/Select";
import { TabPanel, Tabs } from "../components/ui/Tabs";
import { formatNumber, formatTimestamp, plural } from "../lib/format";
import { useAuth } from "../state/auth";
import { useNavigate, useWorkflow } from "../state/workflow";

const ACTION: Record<IngestAction, { label: string; tone: Tone }> = {
  create: { label: "Create", tone: "brand" },
  append: { label: "Append", tone: "success" },
  reorder: { label: "Reorder", tone: "info" },
  evolve: { label: "Evolve", tone: "warning" },
  replace: { label: "Replace", tone: "danger" },
  new_table: { label: "New table", tone: "info" },
  skip: { label: "Skip", tone: "neutral" },
};

const PLAN_KEY = "exavalu.ingest.plan";
const POLL_MS = 700;

const toError = (error: unknown) => (error instanceof ApiError ? error : new ApiError(0, { code: "unknown", message: String(error) }));

export function IngestPage() {
  const [status, setStatus] = useState<BronzeStatus | null>(null);
  const [statusError, setStatusError] = useState<ApiError | null>(null);
  const [tab, setTab] = useState<"plan" | "tables">("plan");

  const loadStatus = useCallback(() => {
    setStatus(null);
    setStatusError(null);
    api.bronzeStatus().then(setStatus).catch((error) => setStatusError(toError(error)));
  }, []);
  useEffect(loadStatus, [loadStatus]);

  const ready = status?.configured && status.reachable;
  return (
    <>
      <PageHeader page="ingest" title="Ingest" description="Load cleaned tables into the bronze layer." />
      {statusError ? (
        <div className="card"><EmptyState icon={<ServerOff />} title="Service unavailable" description={statusError.body.message} action={<Button icon={<RefreshCw />} onClick={loadStatus}>Retry</Button>} /></div>
      ) : !status ? (
        <div className="card space-y-3 p-6"><Skeleton className="h-6 w-48" /><Skeleton className="h-40" /></div>
      ) : !status.enabled ? (
        <div className="card"><EmptyState icon={<ServerOff />} title="Ingestion is off" description="Enable it with AHI_INGEST_ENABLED." /></div>
      ) : !status.configured ? (
        <div className="card">
          <EmptyState
            icon={<Database />}
            title="Database not configured"
            description="Set the AHI_DB_* values in .env (see .env.example), then restart the API."
            action={<Button icon={<RefreshCw />} onClick={loadStatus}>Check again</Button>}
          />
        </div>
      ) : !ready ? (
        <Alert tone="error" title="Database unreachable" action={<Button size="sm" icon={<RefreshCw />} onClick={loadStatus}>Retry</Button>}>
          {status.error ?? `Can't connect to ${status.host}.`}
        </Alert>
      ) : (
        <div className="space-y-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <Tabs
              idPrefix="ingest"
              label="Ingest views"
              value={tab}
              onChange={setTab}
              items={[
                { id: "plan", label: "Plan", icon: <ListChecks /> },
                { id: "tables", label: "Bronze tables", icon: <Database /> },
              ]}
            />
            <span className="num flex items-center gap-1.5 text-caption text-ink-500">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" aria-hidden />
              {status.database} · {status.bronze_schema}
            </span>
          </div>
          <TabPanel idPrefix="ingest" id="plan" active={tab === "plan"}>
            <PlanView onShowTables={() => setTab("tables")} />
          </TabPanel>
          <TabPanel idPrefix="ingest" id="tables" active={tab === "tables"}>
            {tab === "tables" && <TablesView />}
          </TabPanel>
        </div>
      )}
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* Plan                                                                        */
/* -------------------------------------------------------------------------- */

function readPlanId(batchId: string): string | null {
  try {
    const saved = JSON.parse(sessionStorage.getItem(PLAN_KEY) ?? "null");
    return saved?.batchId === batchId ? saved.planId : null;
  } catch {
    return null;
  }
}

function savePlanId(batchId: string, planId: string | null) {
  try {
    sessionStorage.setItem(PLAN_KEY, JSON.stringify({ batchId, planId }));
  } catch {
    /* storage unavailable */
  }
}

function PlanView({ onShowTables }: { onShowTables: () => void }) {
  const flow = useWorkflow();
  const navigate = useNavigate();
  const toast = useToast();
  const batch = flow.batch;
  const jobIds = useMemo(() => (batch?.jobs ?? []).filter((job) => job.status === "succeeded").map((job) => job.id), [batch]);
  const [plan, setPlan] = useState<IngestPlan | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState<Set<string>>(new Set());
  const reviewer = useAuth().user?.user_name ?? "";
  const [finalCheck, setFinalCheck] = useState(false);
  const [schemaFor, setSchemaFor] = useState<PlanItem | null>(null);
  // Files and Plan take turns on screen; null until the first plan picks where to start.
  const [part, setPart] = useState<"files" | "plan" | null>(null);

  const createPlan = useCallback(async () => {
    if (!batch || !jobIds.length) return;
    setError(null);
    setBusy(true);
    try {
      const next = await api.createPlan(jobIds, batch.id);
      setPlan(next);
      setConfirmed(new Set());
      savePlanId(batch.id, next.id);
    } catch (err) {
      setError(toError(err));
    } finally {
      setBusy(false);
    }
  }, [batch, jobIds]);

  // Reuse this batch's plan while it lives on the server; otherwise build a fresh one.
  useEffect(() => {
    if (!batch || !jobIds.length || flow.running) return;
    const saved = readPlanId(batch.id);
    if (!saved) {
      void createPlan();
      return;
    }
    api.getPlan(saved).then(setPlan).catch(() => void createPlan());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batch?.id, jobIds.join(","), flow.running]);

  // Follow a running ingestion.
  useEffect(() => {
    if (plan?.status !== "running") return;
    const timer = setTimeout(async () => {
      try {
        const next = await api.getPlan(plan.id);
        setPlan(next);
        if (next.status === "succeeded") toast({ severity: "success", title: "Ingested", description: plural(Object.keys(next.results).length, "table") });
        if (next.status === "failed") toast({ severity: "error", title: "Ingestion failed", description: next.error?.message });
      } catch {
        setPlan((current) => (current ? { ...current } : current)); // retry
      }
    }, POLL_MS);
    return () => clearTimeout(timer);
  }, [plan, toast]);

  const mutate = async (call: () => Promise<IngestPlan>) => {
    setBusy(true);
    try {
      const next = await call();
      // A confirmation covers what the reviewer saw: drop it when that item changed.
      setConfirmed((current) => keepConfirmed(current, plan, next));
      setPlan(next);
    } catch (err) {
      toast({ severity: "error", title: "Change not saved", description: toError(err).body.message });
    } finally {
      setBusy(false);
    }
  };

  if (flow.running) return <div className="card"><EmptyState icon={<RefreshCw className="animate-spin" />} title="Cleaning in progress" description="Ingest when cleaning finishes." /></div>;
  if (!batch || !jobIds.length)
    return (
      <div className="card">
        <EmptyState icon={<FileSpreadsheet />} title="Nothing to ingest" description="Clean a workbook first." action={<Button variant="primary" icon={<ArrowLeft />} onClick={() => navigate(flow.files.length ? "run" : "configuration")}>{flow.files.length ? "Run" : "Configure"}</Button>} />
      </div>
    );
  if (error)
    return (
      <Alert tone="error" title={error.body.message} action={<Button size="sm" icon={<RefreshCw />} onClick={createPlan}>Retry</Button>}>
        {error.body.advice}
      </Alert>
    );
  if (!plan) return <div className="card space-y-3 p-6" role="status" aria-label="Building plan"><Skeleton className="h-6 w-56" /><Skeleton className="h-32" /><Skeleton className="h-32" /></div>;

  if (plan.status !== "draft") return <RunView plan={plan} onNew={createPlan} onShowTables={onShowTables} />;

  const active = plan.items.filter((item) => item.action !== "skip");
  const needConfirm = active.filter((item) => item.requires_confirmation);
  const unconfirmed = needConfirm.filter((item) => !confirmed.has(item.key));
  const replacing = active.filter((item) => item.replaces.length > 0);
  const rows = active.reduce((sum, item) => sum + item.rows, 0);
  const tables = new Set(active.map((item) => item.table_name)).size;
  const blocked = plan.blockers.length > 0;
  const canApprove = !blocked && !unconfirmed.length && active.length > 0;
  const filesMissing = plan.files.filter(
    (file) =>
      !file.source_system || !file.period_start || !file.pc_id || !file.file_date ||
      (file.division_matches.length > 1 && !file.division_name),
  ).length;
  const itemsToCheck = plan.items.filter(
    (item) => item.action !== "skip" && (item.blockers.length > 0 || (item.requires_confirmation && !confirmed.has(item.key))),
  ).length;
  // Start where the work is: files missing an input first, otherwise the plan.
  const shown = part ?? (filesMissing ? "files" : "plan");

  const approve = async () => {
    setFinalCheck(false);
    setBusy(true);
    try {
      setPlan(await api.approvePlan(plan.id, [...confirmed]));
    } catch (err) {
      const apiError = toError(err);
      toast({ severity: "error", title: apiError.body.message, description: apiError.body.advice ?? undefined });
      // The bronze layer may have moved on; show the plan as it stands now.
      api
        .getPlan(plan.id)
        .then((next) => {
          setConfirmed((current) => keepConfirmed(current, plan, next));
          setPlan(next);
        })
        .catch(() => undefined);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
      <div className="min-w-0 space-y-6">
        {flow.stale && (
          <Alert tone="warning" title="Results are out of date" action={<Button size="sm" onClick={() => navigate("run")}>Run again</Button>}>
            The configuration changed after cleaning. This plan uses the last run.
          </Alert>
        )}
        <Segmented
          label="Plan sections"
          value={shown}
          onChange={setPart}
          items={[
            { id: "files", label: `Files (${plan.files.length})`, step: 1, icon: <FileSpreadsheet />, attention: filesMissing > 0, done: filesMissing === 0 },
            { id: "plan", label: `Plan (${plan.items.length})`, step: 2, icon: <Table2 />, attention: itemsToCheck > 0, done: itemsToCheck === 0 && filesMissing === 0 },
          ]}
        />
        {shown === "files" ? (
        <SectionCard
          step={1}
          icon={<FileSpreadsheet />}
          title="Files"
          description="Confirm profit center, date, division, period."
          actions={<Button size="sm" variant="ghost" icon={<RefreshCw />} onClick={createPlan} disabled={busy}>Re-plan</Button>}
        >
          <FilesTable plan={plan} busy={busy} onChange={(jobId, change) => mutate(() => api.updatePlanFile(plan.id, jobId, change))} />
        </SectionCard>
        ) : (
        <SectionCard step={2} icon={<Table2 />} title="Plan" description="Target table and action per output.">
          <PlanTable
            plan={plan}
            busy={busy}
            confirmed={confirmed}
            onConfirm={(key, on) => setConfirmed((current) => {
              const next = new Set(current);
              if (on) next.add(key);
              else next.delete(key);
              return next;
            })}
            onSchema={setSchemaFor}
            onChange={(key, change) => mutate(() => api.updatePlanItem(plan.id, key, change))}
          />
        </SectionCard>
        )}
      </div>

      <aside className="scroll-thin xl:sticky xl:top-[80px] xl:max-h-[calc(100dvh-96px)] xl:self-start xl:overflow-y-auto">
        <section className="card p-6" aria-labelledby="approve-title">
          <h2 id="approve-title" className="label-caps">Approval</h2>
          <div className="mt-4 grid grid-cols-2 gap-3">
            <StatTile icon={<Table2 />} label="Tables" value={tables} tone="brand" />
            <StatTile icon={<Rows3 />} label="Rows" value={formatNumber(rows)} tone="info" />
          </div>
          <ul className="mt-4 space-y-2 text-body">
            <CheckLine ok={!blocked} label={blocked ? plural(plan.blockers.length, "issue") + " to fix" : "Inputs complete"} />
            <CheckLine ok={!unconfirmed.length} label={needConfirm.length ? `${needConfirm.length - unconfirmed.length} / ${needConfirm.length} confirmed` : "No risky changes"} />
          </ul>
          {blocked && (
            <Alert tone="warning" className="mt-4" title="Fix first">
              <ul className="space-y-0.5">{plan.blockers.slice(0, 4).map((text) => <li key={text}>{text}</li>)}</ul>
            </Alert>
          )}
          <p className="mt-5 flex items-center gap-2 text-body text-ink-600">
            <UserCheck className="h-4 w-4 shrink-0 text-ink-400" aria-hidden />
            Approving as <span className="font-medium text-ink-900">{reviewer}</span>
          </p>
          <Button
            variant="primary"
            size="lg"
            className="mt-5 w-full"
            icon={<DatabaseZap />}
            disabled={!canApprove || busy}
            onClick={() => (replacing.length ? setFinalCheck(true) : approve())}
          >
            Ingest
          </Button>
          <p className="mt-2 flex items-center justify-center gap-1.5 text-caption text-ink-500">
            <ShieldCheck className="h-3.5 w-3.5" aria-hidden /> All or nothing
          </p>
        </section>
      </aside>

      <Modal
        open={finalCheck}
        onClose={() => setFinalCheck(false)}
        title="Replace existing data?"
        description="These earlier loads will be deleted and superseded."
        footer={
          <>
            <Button onClick={() => setFinalCheck(false)}>Cancel</Button>
            <Button variant="primary" icon={<DatabaseZap />} onClick={approve}>Replace and ingest</Button>
          </>
        }
      >
        <ul className="space-y-2">
          {replacing.map((item) => (
            <li key={item.key} className="rounded-md border border-danger-200 bg-danger-50/50 px-3 py-2">
              <p className="font-mono text-caption font-medium text-ink-900">{item.table_name}</p>
              {item.replaces.map((ref) => (
                <p key={ref.id} className="num text-caption text-ink-600">
                  {ref.file_name} · {periodLabel(ref.period_start, ref.period_end)}
                </p>
              ))}
            </li>
          ))}
        </ul>
      </Modal>

      {schemaFor && <SchemaModal item={schemaFor} onClose={() => setSchemaFor(null)} />}
    </div>
  );
}

function CheckLine({ ok, label }: { ok: boolean; label: string }) {
  return (
    <li className="flex items-center gap-2">
      <span className={clsx("flex h-4 w-4 items-center justify-center rounded-full", ok ? "bg-brand-600 text-white" : "bg-ink-100 text-ink-400")} aria-hidden>
        {ok ? <Check className="h-2.5 w-2.5" strokeWidth={3} /> : <span className="h-1.5 w-1.5 rounded-full bg-ink-400" />}
      </span>
      <span className={ok ? "text-ink-800" : "text-ink-500"}>{label}</span>
    </li>
  );
}

/** What a confirmation was given for: action, target and the loads it removes. */
const itemSignature = (item: PlanItem) => [item.action, item.table_name, item.rebuild, item.replaces.map((ref) => ref.id).join(",")].join("|");

function keepConfirmed(current: Set<string>, before: IngestPlan | null, after: IngestPlan): Set<string> {
  const previous = new Map((before?.items ?? []).map((item) => [item.key, itemSignature(item)]));
  return new Set(
    [...current].filter((key) => {
      const item = after.items.find((candidate) => candidate.key === key);
      return item?.requires_confirmation && previous.get(key) === itemSignature(item);
    }),
  );
}

const periodLabel = (start: string | null, end: string | null) => (!start ? "—" : start === end || !end ? start : `${start} → ${end}`);

/* ---- Files ---- */

function FilesTable({ plan, busy, onChange }: { plan: IngestPlan; busy: boolean; onChange: (jobId: string, change: Record<string, string | null>) => void }) {
  const list = useRef<HTMLDivElement>(null);
  const paged = usePaged(plan.files, useFitPageSize(list, { min: 3, max: 20, fallbackRow: 62, reserve: 140 }));
  return (
    <div ref={list} className="overflow-hidden rounded-lg border border-ink-200">
    <div className="relative overflow-x-auto scroll-thin">
      <table className="w-full min-w-[1080px] border-collapse text-table">
        <caption className="sr-only">Files in this plan</caption>
        <thead className="bg-ink-50">
          <tr className="border-b border-ink-200 text-left text-caption font-semibold text-ink-600">
            <th scope="col" className="min-w-[180px] px-4 py-2.5">File</th>
            <th scope="col" className="min-w-[120px] px-3 py-2.5">Profit center</th>
            <th scope="col" className="min-w-[150px] px-3 py-2.5">File date</th>
            <th scope="col" className="min-w-[190px] px-3 py-2.5">Division</th>
            <th scope="col" className="min-w-[120px] px-3 py-2.5">Source system</th>
            <th scope="col" className="min-w-[280px] px-3 py-2.5">Period</th>
          </tr>
        </thead>
        <tbody>
          {paged.slice.map((file) => (
            <FileRow key={file.job_id} file={file} busy={busy} onChange={(change) => onChange(file.job_id, change)} />
          ))}
        </tbody>
      </table>
    </div>
    <Pagination {...paged} onPage={paged.setPage} noun="file" />
    </div>
  );
}

function FileRow({ file, busy, onChange }: { file: PlanFile; busy: boolean; onChange: (change: Record<string, string | null>) => void }) {
  const [source, setSource] = useState(file.source_system ?? "");
  const [pc, setPc] = useState(file.pc_id ?? "");
  const [fileDate, setFileDate] = useState(file.file_date ?? "");
  useEffect(() => setPc(file.pc_id ?? ""), [file.pc_id]);
  useEffect(() => setFileDate(file.file_date ?? ""), [file.file_date]);
  const [start, setStart] = useState(file.period_start ?? "");
  const [end, setEnd] = useState(file.period_end ?? "");
  useEffect(() => setSource(file.source_system ?? ""), [file.source_system]);
  useEffect(() => setStart(file.period_start ?? ""), [file.period_start]);
  useEffect(() => setEnd(file.period_end ?? ""), [file.period_end]);

  const commitPeriod = (nextStart: string, nextEnd: string) => {
    if (nextStart === (file.period_start ?? "") && nextEnd === (file.period_end ?? "")) return;
    onChange({ period_start: nextStart || null, period_end: nextEnd || nextStart || null });
  };
  const input = "h-8 w-full rounded border bg-white px-2 text-body outline-none focus-visible:shadow-focus disabled:bg-ink-50";
  const sourceChanged = file.detected_source_system !== file.source_system;
  const periodChanged = file.detected_period_start !== file.period_start || file.detected_period_end !== file.period_end;
  const fileChanged = file.detected_pc_id !== file.pc_id || file.detected_file_date !== file.file_date;
  // One division: shown. Two: the reviewer picks. None listed: optional, from every division.
  const divisionNeeded = file.division_matches.length > 1 && !file.division_name;

  return (
    <tr data-row className="border-b border-ink-100 align-middle last:border-0">
      <td className="max-w-[280px] px-4 py-2.5">
        <span className="flex items-center gap-2">
          <FileSpreadsheet className="h-4 w-4 shrink-0 text-brand-600" aria-hidden />
          <span className="truncate font-medium text-ink-900" title={file.file_name}>{file.file_name}</span>
          {(sourceChanged || periodChanged || fileChanged) && <Badge tone="info">Edited</Badge>}
        </span>
        <div className="ml-6 min-w-0 overflow-hidden">
        <Tooltip
          content={
            file.period_candidates.length ? (
              <ul className="space-y-0.5">
                {file.period_candidates.map((candidate) => (
                  <li key={candidate.source}>{candidate.source}: {periodLabel(candidate.start, candidate.end)}</li>
                ))}
              </ul>
            ) : "No date columns or month sheets found."
          }
        >
          <span tabIndex={0} className="flex min-w-0 items-center gap-1 text-caption text-ink-500">
            <Info className="h-3 w-3 shrink-0" aria-hidden />
            <span className="truncate">From {file.period_source ?? "— not found"}</span>
          </span>
        </Tooltip>
        {(file.warnings ?? []).map((text) => (
          <p key={text} className="mt-0.5 flex items-start gap-1 text-caption text-amber-800">
            <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
            <span className="whitespace-normal">{text}</span>
          </p>
        ))}
        </div>
      </td>
      <td className="px-3 py-2.5">
        <input
          aria-label={`Profit center for ${file.file_name}`}
          value={pc}
          disabled={busy}
          placeholder="PC0796"
          onChange={(event) => setPc(event.target.value)}
          onBlur={() => pc !== (file.pc_id ?? "") && onChange({ pc_id: pc || null })}
          onKeyDown={(event) => event.key === "Enter" && (event.target as HTMLInputElement).blur()}
          className={clsx(input, "font-mono", file.pc_id ? "border-ink-200" : "border-amber-400")}
        />
      </td>
      <td className="px-3 py-2.5">
        <input
          type="date"
          aria-label={`File date for ${file.file_name}`}
          value={fileDate}
          disabled={busy}
          onChange={(event) => setFileDate(event.target.value)}
          onBlur={() => fileDate !== (file.file_date ?? "") && onChange({ file_date: fileDate || null })}
          className={clsx(input, "num", file.file_date ? "border-ink-200" : "border-amber-400")}
        />
      </td>
      <td className="px-3 py-2.5">
        {file.division_matches.length === 1 ? (
          <span className="block truncate text-body text-ink-800" title="From division_mapping">{file.division_name}</span>
        ) : (
          <select
            aria-label={`Division for ${file.file_name}`}
            value={file.division_name ?? ""}
            disabled={busy}
            onChange={(event) => onChange({ division_name: event.target.value || null })}
            className={clsx(input, "pr-1", divisionNeeded ? "border-amber-400" : "border-ink-200")}
          >
            <option value="">{file.division_matches.length > 1 ? "Choose…" : "— not listed"}</option>
            {file.division_options.map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
        )}
      </td>
      <td className="px-3 py-2.5">
        <input
          aria-label={`Source system for ${file.file_name}`}
          value={source}
          disabled={busy}
          placeholder="pc0515"
          onChange={(event) => setSource(event.target.value)}
          onBlur={() => source !== (file.source_system ?? "") && onChange({ source_system: source || null })}
          onKeyDown={(event) => event.key === "Enter" && (event.target as HTMLInputElement).blur()}
          className={clsx(input, "font-mono", file.source_system ? "border-ink-200" : "border-amber-400")}
        />
      </td>
      <td className="px-3 py-2.5">
        <span className="flex items-center gap-1.5">
          <input type="month" aria-label={`Period start for ${file.file_name}`} value={start} disabled={busy}
            onChange={(event) => setStart(event.target.value)} onBlur={() => commitPeriod(start, end)}
            className={clsx(input, "num min-w-0", file.period_start ? "border-ink-200" : "border-amber-400")} />
          <ArrowRight className="h-3.5 w-3.5 shrink-0 text-ink-400" aria-label="to" />
          <input type="month" aria-label={`Period end for ${file.file_name}`} value={end} disabled={busy}
            onChange={(event) => setEnd(event.target.value)} onBlur={() => commitPeriod(start, end)}
            className={clsx(input, "num min-w-0", file.period_end ? "border-ink-200" : "border-amber-400")} />
        </span>
      </td>
    </tr>
  );
}

/* ---- Plan items ---- */

function PlanTable({
  plan,
  busy,
  confirmed,
  onConfirm,
  onSchema,
  onChange,
}: {
  plan: IngestPlan;
  busy: boolean;
  confirmed: Set<string>;
  onConfirm: (key: string, on: boolean) => void;
  onSchema: (item: PlanItem) => void;
  onChange: (key: string, change: { table_name?: string | null; action?: string | null }) => void;
}) {
  const list = useRef<HTMLUListElement>(null);
  const [file, setFile] = useState("all");
  const [attention, setAttention] = useState(false);
  const needs = (item: PlanItem) =>
    item.action !== "skip" && (item.blockers.length > 0 || (item.requires_confirmation && !confirmed.has(item.key)));
  const files = [...new Set(plan.items.map((item) => item.file_name))];
  const flagged = plan.items.filter(needs);
  const items = plan.items.filter((item) => (file === "all" || item.file_name === file) && (!attention || needs(item)));
  const paged = usePaged(items, useFitPageSize(list, { min: 2, max: 10, fallbackRow: 96, reserve: 140 }), `${file}|${attention}`);
  return (
    <>
    {(files.length > 1 || flagged.length > 0) && (
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        {files.length > 1 ? (
          <Select<string>
            label={`File (${files.length})`}
            value={file}
            onChange={setFile}
            icon={<FileSpreadsheet className="h-4 w-4" />}
            className="w-full sm:w-80"
            options={[
              { value: "all", label: "All files", description: plural(plan.items.length, "table") },
              ...files.map((name) => {
                const own = plan.items.filter((item) => item.file_name === name);
                const open = own.filter(needs).length;
                return {
                  value: name,
                  label: name,
                  description: plural(own.length, "table"),
                  meta: open ? <Badge tone="warning">{open} to check</Badge> : undefined,
                };
              }),
            ]}
          />
        ) : (
          <span />
        )}
        {flagged.length > 0 && (
          <LabeledCheckbox label={`Only tables to check (${flagged.length})`} checked={attention} onChange={setAttention} />
        )}
      </div>
    )}
    <ul ref={list} className="space-y-3" aria-label="Planned tables">
      {paged.slice.map((item) => (
        <PlanRow
          key={item.key}
          item={item}
          busy={busy}
          confirmed={confirmed.has(item.key)}
          onConfirm={(on) => onConfirm(item.key, on)}
          onSchema={() => onSchema(item)}
          onChange={(change) => onChange(item.key, change)}
        />
      ))}
    </ul>
    <Pagination {...paged} onPage={paged.setPage} noun="table" className="mt-3 rounded-b-lg border-x-0 border-b-0 px-0" />
    </>
  );
}

function PlanRow({
  item,
  busy,
  confirmed,
  onConfirm,
  onSchema,
  onChange,
}: {
  item: PlanItem;
  busy: boolean;
  confirmed: boolean;
  onConfirm: (on: boolean) => void;
  onSchema: () => void;
  onChange: (change: { table_name?: string | null; action?: string | null }) => void;
}) {
  const [name, setName] = useState(item.table_name);
  useEffect(() => setName(item.table_name), [item.table_name]);
  const skip = item.action === "skip";
  const risky = item.requires_confirmation && !skip;
  const diff = item.comparison && item.comparison.kind !== "identical";

  return (
    <li
      data-row
      className={clsx(
        "rounded-lg border p-4 transition-colors",
        item.blockers.length && !skip ? "border-amber-300 bg-amber-50/40" : risky && !confirmed ? "border-danger-200" : "border-ink-200",
        skip && "bg-ink-50/60",
      )}
    >
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <Table2 className="h-4 w-4 shrink-0 text-ink-400" aria-hidden />
            <input
              aria-label={`Bronze table for ${item.file_name}`}
              value={name}
              disabled={busy || skip}
              onChange={(event) => setName(event.target.value)}
              onBlur={() => name !== item.table_name && onChange({ table_name: name || null })}
              onKeyDown={(event) => event.key === "Enter" && (event.target as HTMLInputElement).blur()}
              className="h-8 w-full min-w-0 rounded border border-transparent bg-transparent px-1.5 font-mono text-body font-medium text-ink-900 outline-none hover:border-ink-200 focus-visible:border-ink-200 focus-visible:bg-white focus-visible:shadow-focus disabled:hover:border-transparent"
            />
          </div>
          <p className="num ml-6 truncate text-caption text-ink-500" title={item.file_name}>
            {item.file_name} · {item.sheet_names.join(", ")} · {formatNumber(item.rows)} rows · {periodLabel(item.period_start, item.period_end)}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2 pl-6 lg:pl-0">
          {diff && (
            <Button size="sm" variant="ghost" icon={<Columns3 />} onClick={onSchema}>Schema</Button>
          )}
          <Select<IngestAction>
            value={item.action}
            options={item.allowed_actions.map((action) => ({ value: action, label: ACTION[action].label }))}
            onChange={(action) => onChange({ action })}
            label={`Action for ${item.table_name}`}
            hideLabel
            disabled={busy || item.allowed_actions.length < 2}
            className="w-36"
          />
          <Badge tone={ACTION[item.action].tone} className="hidden sm:inline-flex">{ACTION[item.action].label}</Badge>
        </div>
      </div>

      {(item.reasons.length > 0 || item.blockers.length > 0) && (
        <ul className="ml-6 mt-2 space-y-0.5 text-caption">
          {item.blockers.map((text) => (
            <li key={text} className="flex items-start gap-1.5 font-medium text-amber-800"><TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />{text}</li>
          ))}
          {item.reasons.map((text) => (
            <li key={text} className="text-ink-600">{text}</li>
          ))}
        </ul>
      )}

      {risky && (
        <div className="ml-6 mt-3 flex items-center gap-2 rounded-md bg-ink-50 px-3 py-2">
          <Checkbox label={`Confirm ${ACTION[item.action].label} for ${item.table_name}`} checked={confirmed} onChange={onConfirm} disabled={busy} />
          <span className="text-caption font-medium text-ink-800">Confirm {ACTION[item.action].label.toLowerCase()}</span>
        </div>
      )}
    </li>
  );
}

function SchemaModal({ item, onClose }: { item: PlanItem; onClose: () => void }) {
  const comparison = item.comparison!;
  return (
    <Modal open onClose={onClose} size="lg" title={item.table_name} description={`File vs table · ${comparison.kind}`} footer={<Button variant="primary" onClick={onClose}>Done</Button>}>
      <div className="space-y-4">
        {comparison.added.length > 0 && (
          <div><p className="label-caps">Added ({comparison.added.length})</p><ColumnChips names={comparison.added} tone="info" /></div>
        )}
        {comparison.missing.length > 0 && (
          <div><p className="label-caps">Missing — loaded as NULL ({comparison.missing.length})</p><ColumnChips names={comparison.missing} tone="danger" /></div>
        )}
        {item.columns_before && (
          <div><p className="label-caps">Table now</p><ColumnChips names={item.columns_before} tone="neutral" /></div>
        )}
        <div><p className="label-caps">Table after</p><ColumnChips names={item.columns_after} tone="neutral" /></div>
      </div>
    </Modal>
  );
}

/* ---- Running / result ---- */

function RunView({ plan, onNew, onShowTables }: { plan: IngestPlan; onNew: () => void; onShowTables: () => void }) {
  const navigate = useNavigate();
  const running = plan.status === "running";
  const failed = plan.status === "failed";
  const loaded = plan.items.filter((item) => plan.results[item.key]);
  const rows = loaded.reduce((sum, item) => sum + plan.results[item.key].rows_loaded, 0);

  return (
    <section className="card p-5 md:p-6" aria-labelledby="ingest-run-title" aria-busy={running}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className={clsx("flex h-12 w-12 shrink-0 items-center justify-center rounded-full text-white", running ? "bg-brand-600" : failed ? "bg-danger-600" : "bg-emerald-600")} aria-hidden>
          {running ? <RefreshCw className="h-6 w-6 animate-spin" /> : failed ? <TriangleAlert className="h-6 w-6" /> : <Check className="h-6 w-6" strokeWidth={3} />}
        </div>
        <div className="min-w-0 flex-1">
          <p className={clsx("label-caps", failed ? "!text-danger-700" : !running && "!text-emerald-700")}>{running ? "Ingesting" : failed ? "Failed · rolled back" : "Ingested"}</p>
          <h2 id="ingest-run-title" className="text-section text-ink-900">{running ? plan.message : failed ? plan.error?.message : `${plural(loaded.length, "table")} loaded`}</h2>
          <p className="text-caption text-ink-500">
            {plan.reviewed_by && <>Approved by {plan.reviewed_by} · </>}{formatTimestamp(plan.finished_at ?? plan.started_at)}
          </p>
        </div>
        {!running && (
          <div className="flex gap-2">
            <Button icon={<History />} onClick={onShowTables}>Bronze tables</Button>
            <Button variant={failed ? "primary" : "secondary"} icon={<RefreshCw />} onClick={onNew}>New plan</Button>
            {!failed && <Button variant="primary" iconRight={<ArrowRight />} onClick={() => navigate("silver")}>Silver</Button>}
          </div>
        )}
      </div>

      {running && <ProgressBar className="mt-5" value={plan.progress} label="Ingestion progress" active />}
      {failed && plan.error?.advice && <Alert tone="error" className="mt-5" title="Nothing was written">{plan.error.advice}</Alert>}

      {!running && !failed && (
        <>
          <div className="mt-5 grid grid-cols-2 gap-3 md:grid-cols-3">
            <StatTile icon={<Table2 />} label="Tables" value={loaded.length} tone="brand" />
            <StatTile icon={<Rows3 />} label="Rows" value={formatNumber(rows)} tone="success" />
            <StatTile icon={<ShieldCheck />} label="Reconciled" value="100%" tone="success" />
          </div>
          <div className="relative mt-5 overflow-x-auto rounded-lg border border-ink-200 scroll-thin">
            <table className="w-full min-w-[560px] border-collapse text-table">
              <caption className="sr-only">Ingested tables</caption>
              <thead className="bg-ink-50">
                <tr className="border-b border-ink-200 text-left text-caption font-semibold text-ink-600">
                  <th scope="col" className="px-4 py-2.5">Table</th>
                  <th scope="col" className="px-3 py-2.5">Action</th>
                  <th scope="col" className="px-3 py-2.5">File</th>
                  <th scope="col" className="px-4 py-2.5 text-right">Rows</th>
                </tr>
              </thead>
              <tbody>
                {plan.items.map((item) => (
                  <tr key={item.key} className="border-b border-ink-100 last:border-0">
                    <td className="px-4 py-2.5 font-mono font-medium text-ink-900">{item.table_name}</td>
                    <td className="px-3 py-2.5"><Badge tone={ACTION[item.action].tone}>{ACTION[item.action].label}</Badge></td>
                    <td className="max-w-[240px] truncate px-3 py-2.5 text-ink-600" title={item.file_name}>{item.file_name}</td>
                    <td className="num px-4 py-2.5 text-right text-ink-800">{plan.results[item.key] ? formatNumber(plan.results[item.key].rows_loaded) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* Bronze tables registry                                                      */
/* -------------------------------------------------------------------------- */

function TablesView() {
  const [tables, setTables] = useState<BronzeTable[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const list = useRef<HTMLUListElement>(null);
  const paged = usePaged(tables ?? [], useFitPageSize(list, { min: 5, fallbackRow: 49, reserve: 140 }));
  const load = useCallback(() => {
    setError(null);
    setTables(null);
    api.bronzeTables().then(setTables).catch((err) => setError(toError(err)));
  }, []);
  useEffect(load, [load]);

  if (error) return <Alert tone="error" title={error.body.message} action={<Button size="sm" onClick={load}>Retry</Button>} />;
  if (!tables) return <div className="card space-y-2 p-6"><Skeleton className="h-10" /><Skeleton className="h-10" /><Skeleton className="h-10" /></div>;
  if (!tables.length) return <div className="card"><EmptyState icon={<Database />} title="No bronze tables yet" description="Approved plans create them." /></div>;

  return (
    <section className="card" aria-labelledby="tables-title">
      <header className="flex items-center justify-between border-b border-ink-100 px-5 py-4 md:px-6">
        <h2 id="tables-title" className="text-card text-ink-900">{plural(tables.length, "table")}</h2>
        <Button size="sm" variant="ghost" icon={<RefreshCw />} onClick={load}>Refresh</Button>
      </header>
      <ul ref={list} className="divide-y divide-ink-100">
        {paged.slice.map((table) => {
          const expanded = open === table.table_name;
          return (
            <li key={table.table_name} data-row={expanded ? undefined : true}>
              <button
                type="button"
                aria-expanded={expanded}
                onClick={() => setOpen(expanded ? null : table.table_name)}
                className="flex w-full flex-col gap-2 px-5 py-3 text-left hover:bg-ink-50 focus-visible:outline-none focus-visible:shadow-focus sm:flex-row sm:items-center md:px-6"
              >
                <span className="flex min-w-0 flex-1 items-center gap-2">
                  <Database className="h-4 w-4 shrink-0 text-brand-600" aria-hidden />
                  <span className="truncate font-mono text-body font-medium text-ink-900">{table.table_name}</span>
                  <Badge tone="neutral">{table.source_system}</Badge>
                </span>
                <span className="num flex shrink-0 gap-4 pl-6 text-caption text-ink-600 sm:pl-0">
                  <span>{formatNumber(table.rows)} rows</span>
                  <span>{table.columns.length} cols</span>
                  <span>{periodLabel(table.period_start, table.period_end)}</span>
                </span>
              </button>
              {expanded && (
                <div className="animate-fade-in space-y-3 bg-ink-50/50 px-5 pb-4 pt-1 md:px-6">
                  <ColumnChips names={table.columns.map((column) => column.name)} tone="neutral" />
                  <div className="relative overflow-x-auto rounded-md border border-ink-200 bg-white scroll-thin">
                    <table className="w-full min-w-[620px] text-caption">
                      <caption className="sr-only">Ingestion history for {table.table_name}</caption>
                      <thead className="bg-ink-50 text-left text-ink-600">
                        <tr>
                          <th scope="col" className="px-3 py-1.5 font-semibold">File</th>
                          <th scope="col" className="px-3 py-1.5 font-semibold">Period</th>
                          <th scope="col" className="px-3 py-1.5 font-semibold">Action</th>
                          <th scope="col" className="px-3 py-1.5 text-right font-semibold">Rows</th>
                          <th scope="col" className="px-3 py-1.5 font-semibold">Status</th>
                          <th scope="col" className="px-3 py-1.5 font-semibold">Reviewer</th>
                          <th scope="col" className="px-3 py-1.5 font-semibold">When</th>
                        </tr>
                      </thead>
                      <tbody>
                        {table.ingestions.map((load) => (
                          <tr key={load.id} className={clsx("border-t border-ink-100", load.status !== "ingested" && "text-ink-400")}>
                            <td className="max-w-[200px] truncate px-3 py-1.5" title={load.file_name}>{load.file_name}</td>
                            <td className="num px-3 py-1.5">{periodLabel(load.period_start, load.period_end)}</td>
                            <td className="px-3 py-1.5">{ACTION[load.action]?.label ?? load.action}</td>
                            <td className="num px-3 py-1.5 text-right">{formatNumber(load.rows_loaded)}</td>
                            <td className="px-3 py-1.5">
                              <Badge tone={load.status === "ingested" ? "success" : "neutral"}>{load.status === "ingested" ? "Live" : load.status === "superseded" ? "Superseded" : "Skipped"}</Badge>
                            </td>
                            <td className="px-3 py-1.5">{load.reviewed_by ?? "—"}</td>
                            <td className="num px-3 py-1.5">{formatTimestamp(load.created_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ul>
      <Pagination {...paged} onPage={paged.setPage} noun="table" />
      <p className="flex items-center gap-1.5 border-t border-ink-100 px-5 py-3 text-caption text-ink-500 md:px-6">
        <ArrowRight className="h-3.5 w-3.5" aria-hidden /> Every row carries pc_id, file_date, division_name, file_name and processing_date.
      </p>
    </section>
  );
}
