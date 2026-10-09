import clsx from "clsx";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  ClipboardList,
  Columns3,
  Database,
  DatabaseZap,
  FileSpreadsheet,
  History,
  ListChecks,
  RefreshCw,
  Rows3,
  ServerOff,
  ShieldCheck,
  Table2,
  TriangleAlert,
  UserCheck,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { BronzeStatus, BronzeTable, ControlRow, IngestAction, IngestPlan, PlanItem } from "../api/types";
import { ColumnChips } from "../components/AppendOutcome";
import { PageHeader, SectionCard } from "../components/layout/Layout";
import { Badge, type Tone } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import { Checkbox, LabeledCheckbox, SearchInput } from "../components/ui/Controls";
import { Alert, EmptyState, ProgressBar, Skeleton, StatTile, useToast } from "../components/ui/Feedback";
import { Modal } from "../components/ui/Overlay";
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

// An ingestion row's status, as the load history shows it.
const LOAD_STATUS: Record<string, string> = { ingested: "Live", superseded: "Superseded", skipped: "Skipped", removed: "Removed" };

export function IngestPage() {
  const [status, setStatus] = useState<BronzeStatus | null>(null);
  const [statusError, setStatusError] = useState<ApiError | null>(null);
  const [tab, setTab] = useState<"plan" | "control" | "tables">("plan");

  const loadStatus = useCallback(() => {
    setStatus(null);
    setStatusError(null);
    api.bronzeStatus().then(setStatus).catch((error) => setStatusError(toError(error)));
  }, []);
  useEffect(loadStatus, [loadStatus]);

  const ready = status?.configured && status.reachable;
  return (
    <>
      <PageHeader page="ingest" title="Ingest" description="Load the staged files the control table lists into Bronze." />
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
                { id: "control", label: "Control table", icon: <ClipboardList /> },
                { id: "tables", label: "Bronze tables", icon: <Database /> },
              ]}
            />
            <span className="num flex items-center gap-1.5 text-caption text-ink-500">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" aria-hidden />
              {status.database} · {status.bronze_schema}
            </span>
          </div>
          <TabPanel idPrefix="ingest" id="plan" active={tab === "plan"}>
            <PlanView onShowTables={() => setTab("tables")} onShowControl={() => setTab("control")} />
          </TabPanel>
          <TabPanel idPrefix="ingest" id="control" active={tab === "control"}>
            {tab === "control" && <ControlView />}
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

function readPlanId(): string | null {
  try {
    return JSON.parse(sessionStorage.getItem(PLAN_KEY) ?? "null")?.planId ?? null;
  } catch {
    return null;
  }
}

function savePlanId(planId: string | null) {
  try {
    sessionStorage.setItem(PLAN_KEY, JSON.stringify({ planId }));
  } catch {
    /* storage unavailable */
  }
}

/** The plan is built from the control table: every file staged by Validate and not loaded yet. */
function PlanView({ onShowTables, onShowControl }: { onShowTables: () => void; onShowControl: () => void }) {
  const flow = useWorkflow();
  const navigate = useNavigate();
  const toast = useToast();
  const [plan, setPlan] = useState<IngestPlan | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState<Set<string>>(new Set());
  const reviewer = useAuth().user?.user_name ?? "";
  const [finalCheck, setFinalCheck] = useState(false);
  const [schemaFor, setSchemaFor] = useState<PlanItem | null>(null);
  // Staged files and Plan take turns on screen.
  const [part, setPart] = useState<"files" | "plan">("plan");

  const createPlan = useCallback(async () => {
    setError(null);
    setBusy(true);
    try {
      const next = await api.createPlan(null, flow.batch?.id ?? null);
      setPlan(next);
      setConfirmed(new Set());
      savePlanId(next.id);
    } catch (err) {
      setPlan(null);
      setError(toError(err));
    } finally {
      setBusy(false);
    }
  }, [flow.batch?.id]);

  // Reuse the plan while it lives on the server and is still a draft; otherwise build one.
  useEffect(() => {
    const saved = readPlanId();
    if (!saved) {
      void createPlan();
      return;
    }
    api
      .getPlan(saved)
      .then((found) => (found.status === "draft" ? setPlan(found) : void createPlan()))
      .catch(() => void createPlan());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  if (error && error.status === 409 && !plan)
    return (
      <div className="card">
        <EmptyState
          icon={<FileSpreadsheet />}
          title="Nothing staged for Bronze"
          description="Validate cleaned files and stage them first. Rejected files are recorded in the control table and never loaded."
          action={
            <div className="flex flex-wrap justify-center gap-2">
              <Button variant="primary" icon={<ArrowLeft />} onClick={() => navigate("validate")}>Validate</Button>
              <Button icon={<History />} onClick={onShowControl}>Control table</Button>
            </div>
          }
        />
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
  const replacing = active.filter((item) => item.replaces.length > 0 || item.month_replaces.length > 0);
  const rows = active.reduce((sum, item) => sum + item.rows, 0);
  const tables = new Set(active.map((item) => item.table_name)).size;
  const blocked = plan.blockers.length > 0;
  const canApprove = !blocked && !unconfirmed.length && active.length > 0;
  const itemsToCheck = plan.items.filter(
    (item) => item.action !== "skip" && (item.blockers.length > 0 || (item.requires_confirmation && !confirmed.has(item.key))),
  ).length;

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
        <Segmented
          label="Plan sections"
          value={part}
          onChange={setPart}
          items={[
            { id: "files", label: `Staged files (${plan.files.length})`, step: 1, icon: <FileSpreadsheet />, done: true },
            { id: "plan", label: `Plan (${plan.items.length})`, step: 2, icon: <Table2 />, attention: itemsToCheck > 0, done: itemsToCheck === 0 },
          ]}
        />
        {part === "files" ? (
          <SectionCard
            step={1}
            icon={<FileSpreadsheet />}
            title="Staged files"
            description="Decided in Validate and recorded in the control table. Change them there."
            actions={<Button size="sm" variant="ghost" icon={<RefreshCw />} onClick={createPlan} disabled={busy}>Re-plan</Button>}
          >
            <StagedFilesTable plan={plan} />
          </SectionCard>
        ) : (
          <SectionCard step={2} icon={<Table2 />} title="Plan" description="Target table and action per staged file."
            actions={<Button size="sm" variant="ghost" icon={<RefreshCw />} onClick={createPlan} disabled={busy}>Re-plan</Button>}>
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
            <ShieldCheck className="h-3.5 w-3.5" aria-hidden /> All or nothing · control rows marked loaded
          </p>
        </section>
      </aside>

      <Modal
        open={finalCheck}
        onClose={() => setFinalCheck(false)}
        title="Replace existing data?"
        description="These earlier loads lose their rows: whole files, or the one month named."
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
                  {ref.file_name} · {periodLabel(ref.period_start, ref.period_end)} · whole file
                </p>
              ))}
              {item.month_replaces.map((ref) => (
                <p key={ref.id} className="num text-caption text-ink-600">
                  {ref.file_name} · only {item.replace_month}
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

/* ---- Staged files ---- */

const ACTION_TONE: Record<string, Tone> = { INSERT: "brand", APPEND: "success", REJECTED: "danger" };

function StagedFilesTable({ plan }: { plan: IngestPlan }) {
  const list = useRef<HTMLDivElement>(null);
  const paged = usePaged(plan.files, useFitPageSize(list, { min: 3, max: 20, fallbackRow: 58, reserve: 140 }));
  return (
    <div ref={list} className="overflow-hidden rounded-lg border border-ink-200">
      <div className="relative overflow-x-auto scroll-thin">
        <table className="w-full min-w-[1080px] border-collapse text-table">
          <caption className="sr-only">Files staged for this plan</caption>
          <thead className="bg-ink-50">
            <tr className="border-b border-ink-200 text-left text-caption font-semibold text-ink-600">
              <th scope="col" className="px-4 py-2.5">Control</th>
              <th scope="col" className="min-w-[220px] px-3 py-2.5">File</th>
              <th scope="col" className="px-3 py-2.5">Source system</th>
              <th scope="col" className="px-3 py-2.5">File received date</th>
              <th scope="col" className="px-3 py-2.5">Reporting start date</th>
              <th scope="col" className="px-3 py-2.5">Reporting end date</th>
              <th scope="col" className="px-3 py-2.5">Type</th>
              <th scope="col" className="px-3 py-2.5">Action</th>
              <th scope="col" className="px-4 py-2.5 text-right">Rows</th>
            </tr>
          </thead>
          <tbody>
            {paged.slice.map((file) => (
              <tr key={file.key} data-row className="border-b border-ink-100 align-middle last:border-0">
                <td className="num px-4 py-2.5 font-mono text-ink-700">{file.control_id}</td>
                <td className="max-w-[300px] px-3 py-2.5">
                  <span className="block truncate font-medium text-ink-900" title={file.file_name}>{file.file_name}</span>
                  <span className="block truncate text-caption text-ink-500">
                    {[file.output_name, file.division_name, file.date_detail ? `dates from ${file.date_detail}` : "dates entered"].filter(Boolean).join(" · ")}
                  </span>
                </td>
                <td className="px-3 py-2.5 font-mono text-ink-800">{file.source_system}</td>
                <td className="num px-3 py-2.5">{file.file_received_date ?? "—"}</td>
                <td className="num px-3 py-2.5">{file.reporting_start_date}</td>
                <td className="num px-3 py-2.5">{file.reporting_end_date}</td>
                <td className="px-3 py-2.5">{file.reporting_period_type ?? "—"}</td>
                <td className="px-3 py-2.5">
                  <Badge tone={ACTION_TONE[file.processing_action] ?? "neutral"}>{file.processing_action}</Badge>
                  {file.replace_month && <span className="block text-caption text-ink-500">replaces {file.replace_month}</span>}
                  {!file.replace_month && file.file_replaced && <span className="block max-w-[200px] truncate text-caption text-ink-500" title={file.file_replaced}>replaces {file.file_replaced}</span>}
                </td>
                <td className="num px-4 py-2.5 text-right">{formatNumber(file.rows)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pagination {...paged} onPage={paged.setPage} noun="file" />
    </div>
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
            {item.file_name} · {item.sheet_names.join(", ")} · {formatNumber(item.rows)} rows · reporting {periodLabel(item.period_start, item.period_end)}
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
                  <span>Reporting {periodLabel(table.period_start, table.period_end)}</span>
                </span>
              </button>
              {expanded && (
                <div className="animate-fade-in space-y-3 bg-ink-50/50 px-5 pb-4 pt-1 md:px-6">
                  <ColumnChips names={table.columns.map((column) => column.name)} tone="neutral" />
                  <div className="relative overflow-x-auto rounded-md border border-ink-200 bg-white scroll-thin">
                    <table className="w-full min-w-[720px] text-caption">
                      <caption className="sr-only">Ingestion history for {table.table_name}</caption>
                      <thead className="bg-ink-50 text-left text-ink-600">
                        <tr>
                          <th scope="col" className="px-3 py-1.5 font-semibold">File</th>
                          <th scope="col" className="px-3 py-1.5 font-semibold">Control</th>
                          <th scope="col" className="px-3 py-1.5 font-semibold">Reporting start date</th>
                          <th scope="col" className="px-3 py-1.5 font-semibold">Reporting end date</th>
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
                            <td className="num px-3 py-1.5 font-mono">{load.control_id ?? "—"}</td>
                            <td className="num px-3 py-1.5">{load.reporting_start_date ?? load.period_start ?? "—"}</td>
                            <td className="num px-3 py-1.5">{load.reporting_end_date ?? load.period_end ?? load.period_start ?? "—"}</td>
                            <td className="px-3 py-1.5">{ACTION[load.action]?.label ?? load.action}</td>
                            <td className="num px-3 py-1.5 text-right">{formatNumber(load.rows_loaded)}</td>
                            <td className="px-3 py-1.5">
                              <Badge tone={load.status === "ingested" ? "success" : "neutral"}>{LOAD_STATUS[load.status] ?? load.status}</Badge>
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
        <ArrowRight className="h-3.5 w-3.5" aria-hidden /> Every row carries pc_id, file_received_date, reporting_start_date, reporting_end_date, division_name, file_name, processing_date and its _reporting_month.
      </p>
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* Control table                                                               */
/* -------------------------------------------------------------------------- */

type ControlFilter = "all" | "pending" | "loaded" | "rejected" | "listed";

const FLAG_TONE: Record<string, Tone> = { Y: "success", N: "neutral" };

/** The control table: the business's columns first, in their order, then what the app adds. */
function ControlView() {
  const [rows, setRows] = useState<ControlRow[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [filter, setFilter] = useState<ControlFilter>("all");
  const [query, setQuery] = useState("");
  const list = useRef<HTMLDivElement>(null);
  const load = useCallback(() => {
    setError(null);
    setRows(null);
    api.bronzeControl(filter === "all" ? {} : { status: filter }).then(setRows).catch((err) => setError(toError(err)));
  }, [filter]);
  useEffect(load, [load]);
  const needle = query.trim().toLowerCase();
  const shown = (rows ?? []).filter((row) => !needle || `${row.file_name} ${row.sheet_name ?? ""} ${row.source_system}`.toLowerCase().includes(needle));
  const paged = usePaged(shown, useFitPageSize(list, { min: 5, max: 50, fallbackRow: 45, reserve: 150 }), `${filter}|${needle}`);

  return (
    <section className="card" aria-labelledby="control-title">
      <header className="flex flex-col gap-3 border-b border-ink-100 px-5 py-4 md:px-6 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <h2 id="control-title" className="text-card text-ink-900">Control table</h2>
          <p className="text-caption text-ink-500">Every file Bronze has been told about: listed by the business, staged by Validate, loaded or rejected.</p>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
          <SearchInput value={query} onChange={setQuery} placeholder="File, sheet or source system" label="Search the control table" className="w-full sm:w-64" />
          <Select<ControlFilter>
            label="Show"
            hideLabel
            value={filter}
            onChange={setFilter}
            className="w-full sm:w-48"
            options={[
              { value: "all", label: "All rows" },
              { value: "pending", label: "Staged, not loaded" },
              { value: "loaded", label: "Loaded (Y)" },
              { value: "rejected", label: "Rejected" },
              { value: "listed", label: "Listed, not received" },
            ]}
          />
          <Button size="md" variant="ghost" icon={<RefreshCw />} onClick={load}>Refresh</Button>
        </div>
      </header>
      {error ? (
        <div className="p-5"><Alert tone="error" title={error.body.message} action={<Button size="sm" onClick={load}>Retry</Button>}>{error.body.advice}</Alert></div>
      ) : !rows ? (
        <div className="space-y-2 p-6"><Skeleton className="h-8" /><Skeleton className="h-8" /><Skeleton className="h-8" /></div>
      ) : !shown.length ? (
        <EmptyState compact icon={<ListChecks />} title="No rows" description={needle ? `Nothing matches “${query}”.` : "Nothing in the control table for this filter."} />
      ) : (
        <div ref={list}>
          <div className="relative overflow-x-auto scroll-thin">
            <table className="w-full min-w-[1560px] border-collapse text-table">
              <caption className="sr-only">The control table</caption>
              <thead className="bg-ink-50">
                <tr className="border-b border-ink-200 text-left text-caption font-semibold text-ink-600">
                  {["control_id", "source_system", "file_name", "sheet_name", "reporting_period_type", "processing_action", "bronze_load_flag",
                    "file_received_date", "drt_reporting_start_date", "drt_reporting_end_date", "date_detail", "file_replaced", "is_active"].map((name) => (
                    <th key={name} scope="col" className="whitespace-nowrap px-3 py-2.5 font-mono text-[11px]">{name}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {paged.slice.map((row) => {
                  const rejected = row.processing_action === "REJECTED";
                  return (
                    <tr key={row.control_id} data-row className={clsx("border-b border-ink-100 align-top last:border-0", rejected && "bg-danger-50/30 text-ink-500")}>
                      <td className="num px-3 py-2 font-mono">{row.control_id}</td>
                      <td className="px-3 py-2 font-mono">{row.source_system}</td>
                      <td className="max-w-[340px] px-3 py-2">
                        <span className="block truncate" title={row.file_name}>{row.file_name}</span>
                        {rejected && row.rejection_reason && (
                          <span className="block text-caption text-danger-700" title={row.rejection_reason}>{row.rejection_reason}</span>
                        )}
                        {!row.staging_table && row.bronze_load_flag === "N" && !rejected && (
                          <span className="block text-caption text-ink-400">Listed by the business; not received here yet</span>
                        )}
                      </td>
                      <td className="max-w-[200px] px-3 py-2">
                        <span className="block truncate" title={row.sheet_name ?? undefined}>{row.sheet_name ?? "—"}</span>
                      </td>
                      <td className="px-3 py-2">{row.reporting_period_type ?? "—"}</td>
                      <td className="px-3 py-2">{row.processing_action ? <Badge tone={rejected ? "danger" : row.processing_action === "APPEND" ? "success" : "brand"}>{row.processing_action}</Badge> : "—"}</td>
                      <td className="px-3 py-2"><Badge tone={FLAG_TONE[row.bronze_load_flag]}>{row.bronze_load_flag}</Badge></td>
                      <td className="num whitespace-nowrap px-3 py-2">{row.file_received_date ?? "—"}</td>
                      <td className="num whitespace-nowrap px-3 py-2">{row.drt_reporting_start_date ?? "—"}</td>
                      <td className="num whitespace-nowrap px-3 py-2">{row.drt_reporting_end_date ?? "—"}</td>
                      <td className="px-3 py-2">{row.date_detail ?? "—"}</td>
                      <td className="max-w-[220px] truncate px-3 py-2" title={row.file_replaced ?? undefined}>{row.file_replaced ?? "—"}</td>
                      <td className="px-3 py-2"><Badge tone={row.is_active === "Y" ? "success" : "neutral"}>{row.is_active}</Badge></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <Pagination {...paged} onPage={paged.setPage} noun="row" />
        </div>
      )}
    </section>
  );
}
