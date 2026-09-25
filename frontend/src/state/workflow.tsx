import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api, ApiError } from "../api/client";
import type { JobResults, JobStatus, OutputSummary, Workbook } from "../api/types";
import { useToast } from "../components/ui/Feedback";

export type Page = "configuration" | "run" | "results";

const STORAGE_KEY = "exavalu.workflow.v1";
const POLL_MS = 700;

interface Persisted {
  workbookId: string | null;
  selected: string[];
  append: boolean;
  jobId: string | null;
  jobConfigKey: string | null;
  outputId: string | null;
}

interface Workflow {
  hydrated: boolean;
  /** Appending needs a workbook with 2+ sheets and 2+ of them selected. */
  appendAvailable: boolean;
  appendUnavailableReason: string | null;
  workbook: Workbook | null;
  selected: string[];
  append: boolean;
  job: JobStatus | null;
  results: JobResults | null;
  resultsError: ApiError | null;
  outputId: string | null;
  /** The job's results no longer match the current configuration. */
  stale: boolean;
  running: boolean;
  canRun: boolean;
  runBlockedReason: string | null;
  canReview: boolean;
  reviewBlockedReason: string | null;

  setWorkbook: (workbook: Workbook) => void;
  removeWorkbook: () => Promise<void>;
  setSelected: (sheets: string[]) => void;
  setAppend: (append: boolean) => void;
  startJob: () => Promise<void>;
  cancelJob: () => Promise<void>;
  resetJob: () => void;
  reloadResults: () => Promise<void>;
  setOutputId: (id: string) => void;
  applyOutputUpdate: (summary: OutputSummary) => void;
}

const WorkflowContext = createContext<Workflow | null>(null);

function readPersisted(): Persisted | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Persisted) : null;
  } catch {
    return null;
  }
}

const configKey = (workbookId: string | undefined, selected: string[], append: boolean) =>
  JSON.stringify([workbookId ?? null, [...selected].sort(), append]);

export function WorkflowProvider({ children }: { children: ReactNode }) {
  const toast = useToast();
  const [hydrated, setHydrated] = useState(false);
  const [workbook, setWorkbookState] = useState<Workbook | null>(null);
  const [selected, setSelectedState] = useState<string[]>([]);
  const [append, setAppendState] = useState(true);
  const [job, setJob] = useState<JobStatus | null>(null);
  const [jobConfigKey, setJobConfigKey] = useState<string | null>(null);
  const [results, setResults] = useState<JobResults | null>(null);
  const [resultsError, setResultsError] = useState<ApiError | null>(null);
  const [outputId, setOutputIdState] = useState<string | null>(null);
  const announced = useRef<string | null>(null);

  // ---- hydrate from the session, verifying each id still exists on the server ----
  useEffect(() => {
    const saved = readPersisted();
    (async () => {
      if (saved?.workbookId) {
        try {
          const found = await api.getWorkbook(saved.workbookId);
          setWorkbookState(found);
          const names = new Set(found.sheets.map((sheet) => sheet.name));
          setSelectedState(saved.selected.filter((name) => names.has(name)));
          setAppendState(saved.append);
          if (saved.jobId) {
            try {
              const status = await api.getJob(saved.jobId);
              announced.current = status.status === "running" || status.status === "queued" ? null : status.id;
              setJob(status);
              setJobConfigKey(saved.jobConfigKey);
              setOutputIdState(saved.outputId);
            } catch {
              /* job expired with a server restart */
            }
          }
        } catch {
          /* upload expired; start fresh */
        }
      }
      setHydrated(true);
    })();
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    const data: Persisted = {
      workbookId: workbook?.id ?? null,
      selected,
      append,
      jobId: job?.id ?? null,
      jobConfigKey,
      outputId,
    };
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(data));
    } catch {
      /* storage unavailable: state simply won't survive a reload */
    }
  }, [hydrated, workbook, selected, append, job?.id, jobConfigKey, outputId]);

  const running = job?.status === "queued" || job?.status === "running";

  // ---- poll the running job; lives here so progress continues across pages ----
  useEffect(() => {
    if (!job || !running) return;
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const next = await api.getJob(job.id);
        if (!cancelled) setJob(next);
      } catch (error) {
        if (!cancelled && error instanceof ApiError && error.status === 404) {
          setJob((current) =>
            current && {
              ...current,
              status: "failed",
              error: {
                kind: "not_found",
                message: "The cleaning job is no longer available.",
                advice: "The server may have restarted. Run the job again.",
              },
            },
          );
        } else if (!cancelled) setJob((current) => (current ? { ...current } : current)); // retry
      }
    }, POLL_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [job, running]);

  const loadResults = useCallback(async (jobId: string) => {
    setResultsError(null);
    try {
      const data = await api.getResults(jobId);
      setResults(data);
      setOutputIdState((current) =>
        current && data.outputs.some((output) => output.id === current) ? current : data.outputs[0]?.id ?? null,
      );
    } catch (error) {
      setResultsError(error instanceof ApiError ? error : new ApiError(0, { code: "unknown", message: String(error) }));
    }
  }, []);

  // ---- react to the job finishing ----
  useEffect(() => {
    if (!job) return;
    if (job.status === "succeeded" && results?.job.id !== job.id) void loadResults(job.id);
    if (announced.current === job.id || running) return;
    announced.current = job.id;
    if (job.status === "succeeded") toast({ severity: "success", title: "Cleaning completed", description: job.source_name });
    if (job.status === "failed") toast({ severity: "error", title: "Cleaning could not be completed", description: job.error?.message });
    if (job.status === "cancelled") toast({ severity: "info", title: "Cleaning job cancelled" });
  }, [job, running, results?.job.id, loadResults, toast]);

  // ---- append is only meaningful with two or more sheets ----
  const appendUnavailableReason = !workbook
    ? "Upload a workbook first."
    : workbook.sheets.length < 2
      ? "This workbook has only one sheet, so there is nothing to append."
      : selected.length < 2
        ? "Select two or more sheets to append them."
        : null;
  const appendAvailable = !appendUnavailableReason;

  // ---- actions ----
  const clearJob = () => {
    setJob(null);
    setJobConfigKey(null);
    setResults(null);
    setResultsError(null);
    setOutputIdState(null);
  };

  const setWorkbook = useCallback((next: Workbook) => {
    setWorkbookState(next);
    // Pre-select every sheet that holds something; hidden or empty sheets stay opt-in.
    setSelectedState(next.sheets.filter((sheet) => sheet.has_content && !sheet.hidden).map((sheet) => sheet.name));
    clearJob();
  }, []);

  const removeWorkbook = useCallback(async () => {
    if (workbook) {
      try {
        await api.deleteWorkbook(workbook.id);
      } catch {
        /* already gone on the server; clear locally anyway */
      }
    }
    setWorkbookState(null);
    setSelectedState([]);
    clearJob();
    toast({ severity: "info", title: "Workbook removed" });
  }, [workbook, toast]);

  const startJob = useCallback(async () => {
    if (!workbook || !selected.length) return;
    const status = await api.startJob(workbook.id, selected, append && selected.length > 1);
    announced.current = null;
    setResults(null);
    setResultsError(null);
    setOutputIdState(null);
    setJob(status);
    setJobConfigKey(configKey(workbook.id, selected, append));
  }, [workbook, selected, append]);

  const cancelJob = useCallback(async () => {
    if (!job) return;
    try {
      setJob(await api.cancelJob(job.id));
    } catch (error) {
      if (error instanceof ApiError) toast({ severity: "warning", title: error.body.message });
    }
  }, [job, toast]);

  const applyOutputUpdate = useCallback((summary: OutputSummary) => {
    setResults((current) =>
      current && { ...current, outputs: current.outputs.map((output) => (output.id === summary.id ? summary : output)) },
    );
  }, []);

  const currentKey = configKey(workbook?.id, selected, append);
  const stale = Boolean(job && jobConfigKey && jobConfigKey !== currentKey);

  const runBlockedReason = !workbook
    ? "Upload a workbook first."
    : !selected.length
      ? "Select at least one sheet to clean."
      : null;
  const reviewBlockedReason = results
    ? null
    : running
      ? "Results will be available when the cleaning job finishes."
      : "Run a cleaning job to generate results.";

  const value: Workflow = {
    hydrated,
    appendAvailable,
    appendUnavailableReason,
    workbook,
    selected,
    append,
    job,
    results,
    resultsError,
    outputId,
    stale,
    running,
    canRun: !runBlockedReason && !running,
    runBlockedReason,
    canReview: !reviewBlockedReason,
    reviewBlockedReason,
    setWorkbook,
    removeWorkbook,
    setSelected: setSelectedState,
    setAppend: setAppendState,
    startJob,
    cancelJob,
    resetJob: clearJob,
    reloadResults: async () => {
      if (job) await loadResults(job.id);
    },
    setOutputId: setOutputIdState,
    applyOutputUpdate,
  };

  return <WorkflowContext.Provider value={value}>{children}</WorkflowContext.Provider>;
}

export function useWorkflow(): Workflow {
  const context = useContext(WorkflowContext);
  if (!context) throw new Error("useWorkflow must be used inside WorkflowProvider");
  return context;
}

/* ---- hash routing: three pages, deep-linkable, back button works ---- */

const PAGES: Page[] = ["configuration", "run", "results"];

export const NavContext = createContext<(page: Page) => void>(() => {});
export const useNavigate = () => useContext(NavContext);

export function usePage(): [Page, (page: Page) => void] {
  const read = (): Page => {
    const hash = window.location.hash.replace(/^#\/?/, "") as Page;
    return PAGES.includes(hash) ? hash : "configuration";
  };
  const [page, setPage] = useState<Page>(read);
  useEffect(() => {
    const onHash = () => setPage(read());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const navigate = useCallback((next: Page) => {
    if (window.location.hash !== `#/${next}`) window.location.hash = `/${next}`;
    setPage(next);
    document.getElementById("main")?.focus({ preventScroll: true });
    window.scrollTo({ top: 0 });
  }, []);
  return [page, navigate];
}
