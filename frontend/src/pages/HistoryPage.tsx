import clsx from "clsx";
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  Copy,
  FileText,
  History as HistoryIcon,
  RefreshCw,
  ScrollText,
} from "lucide-react";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { AuthUser, HistoryJobType, HistoryRun, HistoryStatus } from "../api/types";
import { PageHeader } from "../components/layout/Layout";
import { Badge, type Tone } from "../components/ui/Badge";
import { Button, IconButton } from "../components/ui/Button";
import { useFitPageSize } from "../components/ui/Pagination";
import { SearchInput } from "../components/ui/Controls";
import { Alert, EmptyState, Skeleton, useToast } from "../components/ui/Feedback";
import { SidePanel } from "../components/ui/Overlay";
import { Select, type SelectOption } from "../components/ui/Select";
import { formatDuration, formatIso, formatNumber, plural, secondsBetween } from "../lib/format";
import { useDebounced } from "../lib/hooks";
import { useAuth } from "../state/auth";

/** While something is queued or running, the page on screen refreshes itself. */
const LIVE_MS = 3000;

const TYPE_LABEL: Record<HistoryJobType, string> = {
  clean: "Clean",
  bronze_ingest: "Bronze ingest",
  silver_load: "Silver load",
};

const STATUS: Record<HistoryStatus, { label: string; tone: Tone }> = {
  queued: { label: "Queued", tone: "neutral" },
  running: { label: "Running", tone: "info" },
  succeeded: { label: "Succeeded", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  cancelled: { label: "Cancelled", tone: "neutral" },
  skipped: { label: "Skipped", tone: "neutral" },
  interrupted: { label: "Interrupted", tone: "warning" },
};

type Any = "all";
const typeOptions: SelectOption<HistoryJobType | Any>[] = [
  { value: "all", label: "All types" },
  ...(Object.keys(TYPE_LABEL) as HistoryJobType[]).map((value) => ({ value, label: TYPE_LABEL[value] })),
];
const statusOptions: SelectOption<HistoryStatus | Any>[] = [
  { value: "all", label: "Any status" },
  ...(Object.keys(STATUS) as HistoryStatus[]).map((value) => ({ value, label: STATUS[value].label })),
];

export function HistoryPage() {
  const { user } = useAuth();
  const toast = useToast();
  const [query, setQuery] = useState("");
  const [type, setType] = useState<HistoryJobType | Any>("all");
  const [status, setStatus] = useState<HistoryStatus | Any>("all");
  const [owner, setOwner] = useState<string>("all");
  const [people, setPeople] = useState<AuthUser[]>([]);
  const [runs, setRuns] = useState<HistoryRun[] | null>(null);
  // Keyset pages: the cursor that opens each page seen so far (page 1 has none).
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [page, setPage] = useState(0);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const table = useRef<HTMLDivElement>(null);
  const pageSize = useFitPageSize(table, { min: 5, max: 50, fallbackRow: 49, reserve: 110 });
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [logFor, setLogFor] = useState<HistoryRun | null>(null);
  const q = useDebounced(query.trim(), 300);
  const request = useRef(0);

  const filters = useMemo(
    () => ({
      type: type === "all" ? null : type,
      status: status === "all" ? null : status,
      q,
      user_id: owner === "all" ? null : owner,
      limit: pageSize,
    }),
    [type, status, q, owner, pageSize],
  );
  // New filters (or a new page size) start again from the newest jobs.
  useEffect(() => {
    setCursors([null]);
    setPage(0);
  }, [filters]);
  const before = cursors[page] ?? null;

  const load = useCallback(
    async (quiet = false) => {
      const ticket = ++request.current;
      if (!quiet) setRuns(null);
      try {
        const found = await api.history({ ...filters, before });
        if (ticket !== request.current) return;
        setRuns(found.jobs);
        setNext(found.next_before);
        setError(null);
      } catch (failure) {
        if (ticket !== request.current) return;
        setError(failure instanceof ApiError ? failure.body.message : "History could not be loaded.");
        if (!quiet) setRuns([]);
      }
    },
    [filters, before],
  );

  useEffect(() => {
    void load();
  }, [load]);

  const goNext = () => {
    if (!next) return;
    setCursors((current) => [...current.slice(0, page + 1), next]);
    setPage(page + 1);
  };

  const live = runs?.some((run) => run.status === "running" || run.status === "queued") ?? false;
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => void load(true), LIVE_MS);
    return () => window.clearInterval(timer);
  }, [live, load]);

  useEffect(() => {
    if (!user?.is_admin) return;
    api.listUsers().then(({ users }) => setPeople(users)).catch(() => setPeople([]));
  }, [user?.is_admin]);

  const toggle = (id: string) =>
    setOpen((current) => {
      const copy = new Set(current);
      copy.has(id) ? copy.delete(id) : copy.add(id);
      return copy;
    });

  const copy = async (text: string, what: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast({ severity: "success", title: `${what} copied` });
    } catch {
      toast({ severity: "error", title: "Copy is blocked in this browser" });
    }
  };

  const filtered = Boolean(q || type !== "all" || status !== "all" || owner !== "all");
  const ownerOptions: SelectOption<string>[] = [
    { value: "all", label: "Everyone" },
    ...people.map((person) => ({ value: person.user_id, label: person.user_name })),
  ];

  return (
    <>
      <PageHeader
        page="history"
        title="History"
        description={user?.is_admin ? "Every clean, ingest and Silver load." : "Your cleans, ingests and Silver loads."}
        actions={
          <Button icon={<RefreshCw />} onClick={() => void load()} aria-label="Refresh history">
            Refresh
          </Button>
        }
      />

      <div className="mb-4 grid gap-3 md:grid-cols-[minmax(0,1fr)_auto_auto_auto] md:items-end">
        <SearchInput value={query} onChange={setQuery} label="Search history" placeholder="Job id, file name or SHA-256" />
        <Select label="Type" hideLabel value={type} options={typeOptions} onChange={setType} className="md:w-44" />
        <Select label="Status" hideLabel value={status} options={statusOptions} onChange={setStatus} className="md:w-40" />
        {user?.is_admin && (
          <Select label="Run by" hideLabel value={owner} options={ownerOptions} onChange={setOwner} className="md:w-44" />
        )}
      </div>

      {error && (
        <Alert tone="error" title={error} className="mb-4">
          Jobs keep running; their history appears once the app database is reachable.
        </Alert>
      )}

      <div ref={table} className="card overflow-hidden">
        <div className="relative overflow-x-auto scroll-thin">
          <table className="w-full min-w-[880px] text-table">
            <thead>
              <tr className="border-b border-ink-200 bg-ink-50 text-left text-caption font-semibold text-ink-600">
                <th scope="col" className="w-10 px-3 py-2.5"><span className="sr-only">Expand</span></th>
                <th scope="col" className="px-3 py-2.5">Job</th>
                <th scope="col" className="px-3 py-2.5">Type</th>
                <th scope="col" className="px-3 py-2.5">Status</th>
                <th scope="col" className="px-3 py-2.5">Source</th>
                <th scope="col" className="px-3 py-2.5 text-right">Outputs</th>
                <th scope="col" className="px-3 py-2.5">Run by</th>
                <th scope="col" className="px-3 py-2.5">Started</th>
                <th scope="col" className="px-3 py-2.5 text-right">Took</th>
                <th scope="col" className="w-12 px-3 py-2.5"><span className="sr-only">Log</span></th>
              </tr>
            </thead>
            <tbody>
              {runs === null &&
                Array.from({ length: 6 }, (_, index) => (
                  <tr key={index} className="border-b border-ink-100 last:border-0">
                    <td colSpan={10} className="px-3 py-3">
                      <Skeleton className="h-5 w-full" />
                    </td>
                  </tr>
                ))}
              {runs?.map((run) => {
                const expanded = open.has(run.job_id);
                const took = run.started_at ? formatDuration(secondsBetween(run.started_at, run.ended_at)) : "—";
                const sources = run.source_files;
                return (
                  <Fragment key={run.job_id}>
                    <tr data-row className={clsx("border-b border-ink-100 transition-colors hover:bg-ink-50/70", expanded && "bg-ink-50/70")}>
                      <td className="px-3 py-2">
                        <IconButton
                          label={expanded ? "Hide outputs" : "Show outputs"}
                          aria-expanded={expanded}
                          onClick={() => toggle(run.job_id)}
                          disabled={!run.outputs.length}
                        >
                          {expanded ? <ChevronDown /> : <ChevronRight />}
                        </IconButton>
                      </td>
                      <td className="px-3 py-2">
                        <span className="inline-flex items-center gap-1">
                          <span className="font-mono text-[12px] text-ink-800" title={run.job_id}>
                            {run.job_id.slice(0, 8)}
                          </span>
                          <IconButton label="Copy job id" className="h-7 w-7" onClick={() => void copy(run.job_id, "Job id")}>
                            <Copy />
                          </IconButton>
                        </span>
                      </td>
                      <td className="px-3 py-2 text-ink-700">{TYPE_LABEL[run.job_type]}</td>
                      <td className="px-3 py-2">
                        <Badge tone={STATUS[run.status].tone} dot={run.status === "running"}>
                          {STATUS[run.status].label}
                        </Badge>
                      </td>
                      <td className="max-w-[260px] px-3 py-2">
                        {sources.length ? (
                          <span className="block truncate text-ink-800" title={sources.join("\n")}>
                            {sources[0]}
                            {sources.length > 1 && <span className="text-ink-500"> +{sources.length - 1}</span>}
                          </span>
                        ) : (
                          <span className="text-ink-400">—</span>
                        )}
                      </td>
                      <td className="num px-3 py-2 text-right text-ink-800">{run.outputs.length || "—"}</td>
                      <td className="px-3 py-2 text-ink-700">{run.created_by?.user_name ?? "—"}</td>
                      <td className="num whitespace-nowrap px-3 py-2 text-ink-700">{formatIso(run.started_at ?? run.created_at)}</td>
                      <td className="num px-3 py-2 text-right text-ink-700">{took}</td>
                      <td className="px-3 py-2">
                        <IconButton label="Open log" onClick={() => setLogFor(run)}>
                          <ScrollText />
                        </IconButton>
                      </td>
                    </tr>
                    {expanded &&
                      run.outputs.map((output) => (
                        <tr key={output.job_row_id} className="border-b border-ink-100 bg-ink-50/40 text-ink-700">
                          <td />
                          <td colSpan={4} className="py-2 pl-3 pr-3">
                            <span className="flex min-w-0 items-center gap-2">
                              <FileText className="h-3.5 w-3.5 shrink-0 text-ink-400" aria-hidden />
                              <span className="truncate font-mono text-[12px]" title={output.output_file ?? output.output_name}>
                                {output.output_file ?? output.output_name}
                              </span>
                              {output.status !== run.status && (
                                <Badge tone={STATUS[output.status].tone}>{STATUS[output.status].label}</Badge>
                              )}
                            </span>
                          </td>
                          <td className="num px-3 py-2 text-right" title="Rows">
                            {output.row_count == null ? "—" : formatNumber(output.row_count)}
                          </td>
                          <td colSpan={4} className="px-3 py-2 text-caption text-ink-500">
                            {run.job_type === "clean"
                              ? output.rows_removed
                                ? `${plural(output.rows_removed, "row")} removed`
                                : "No rows removed"
                              : output.output_name}
                          </td>
                        </tr>
                      ))}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
        {runs?.length === 0 && !error && (
          <EmptyState
            icon={<HistoryIcon />}
            title={filtered ? "No matching jobs" : "No jobs yet"}
            description={filtered ? "Try another search or clear the filters." : "Clean a workbook and it appears here."}
            compact
          />
        )}
        {runs && runs.length > 0 && (page > 0 || next) && (
          <nav aria-label="Pagination" className="flex items-center justify-between gap-3 border-t border-ink-200 px-4 py-2">
            <span className="num text-caption text-ink-500">Page {page + 1} · newest first</span>
            <div className="flex items-center gap-1">
              <IconButton label="Newest jobs" disabled={page === 0} onClick={() => setPage(0)} className="hidden sm:inline-flex">
                <ChevronsLeft />
              </IconButton>
              <IconButton label="Newer jobs" disabled={page === 0} onClick={() => setPage(page - 1)}>
                <ChevronLeft />
              </IconButton>
              <IconButton label="Older jobs" disabled={!next} onClick={goNext}>
                <ChevronRight />
              </IconButton>
            </div>
          </nav>
        )}
      </div>

      <LogPanel run={logFor} onClose={() => setLogFor(null)} onCopy={copy} />
    </>
  );
}

function LogPanel({
  run,
  onClose,
  onCopy,
}: {
  run: HistoryRun | null;
  onClose: () => void;
  onCopy: (text: string, what: string) => Promise<void>;
}) {
  const [detail, setDetail] = useState<HistoryRun | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDetail(null);
    setError(null);
    if (!run) return;
    let live = true;
    api
      .historyRun(run.job_id)
      .then((found) => live && setDetail(found))
      .catch((failure) => live && setError(failure instanceof ApiError ? failure.body.message : "The log could not be loaded."));
    return () => {
      live = false;
    };
  }, [run]);

  const shown = detail ?? run;
  return (
    <SidePanel
      open={Boolean(run)}
      onClose={onClose}
      wide
      title={shown ? `${TYPE_LABEL[shown.job_type]} log` : "Log"}
      description={shown && <span className="font-mono text-[12px] text-ink-500">{shown.job_id}</span>}
      footer={
        detail?.log ? (
          <Button icon={<Copy />} onClick={() => void onCopy(detail.log ?? "", "Log")}>
            Copy log
          </Button>
        ) : undefined
      }
    >
      {shown && (
        <dl className="mb-5 grid grid-cols-[auto_minmax(0,1fr)] gap-x-6 gap-y-2 text-body">
          <dt className="text-ink-500">Status</dt>
          <dd>
            <Badge tone={STATUS[shown.status].tone}>{STATUS[shown.status].label}</Badge>
          </dd>
          <dt className="text-ink-500">Run by</dt>
          <dd className="text-ink-800">{shown.created_by?.user_name ?? "—"}</dd>
          {shown.modified_by && shown.modified_by.user_id !== shown.created_by?.user_id && (
            <>
              <dt className="text-ink-500">Changed by</dt>
              <dd className="text-ink-800">{shown.modified_by.user_name}</dd>
            </>
          )}
          <dt className="text-ink-500">Started</dt>
          <dd className="num text-ink-800">{formatIso(shown.started_at ?? shown.created_at)}</dd>
          <dt className="text-ink-500">Ended</dt>
          <dd className="num text-ink-800">{formatIso(shown.ended_at)}</dd>
          {shown.source_sheets.length > 0 && (
            <>
              <dt className="text-ink-500">Sheets</dt>
              <dd className="text-ink-800">{shown.source_sheets.join(", ")}</dd>
            </>
          )}
          {shown.source_sha256.map((sha) => (
            <Fragment key={sha}>
              <dt className="text-ink-500">SHA-256</dt>
              <dd className="truncate font-mono text-[12px] text-ink-700" title={sha}>{sha}</dd>
            </Fragment>
          ))}
          {shown.source_job_ids.map((id) => (
            <Fragment key={id}>
              <dt className="text-ink-500">From job</dt>
              <dd className="font-mono text-[12px] text-ink-700">{id}</dd>
            </Fragment>
          ))}
        </dl>
      )}
      {error && <Alert tone="error">{error}</Alert>}
      {!detail && !error && <Skeleton className="h-40 w-full" />}
      {detail &&
        (detail.log ? (
          <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-words rounded-md border border-ink-200 bg-ink-50 p-4 font-mono text-[12px] leading-5 text-ink-800 scroll-thin">
            {detail.log}
          </pre>
        ) : (
          <p className="text-body text-ink-500">No log lines were recorded for this job.</p>
        ))}
    </SidePanel>
  );
}
