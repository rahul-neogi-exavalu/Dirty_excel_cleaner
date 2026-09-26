import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api, ApiError } from "../api/client";
import type { BatchStatus, JobResults, JobStatus, OutputSummary, Workbook } from "../api/types";
import { useToast } from "../components/ui/Feedback";
import { plural } from "../lib/format";

export type Page = "configuration" | "run" | "results";

const STORAGE_KEY = "exavalu.workflow.v2";
const POLL_MS = 700;
/** Matches the server's AHI_MAX_BATCH_FILES default. */
export const MAX_FILES = 20;

/** One uploaded file and how it should be cleaned. */
export interface FileEntry {
  workbook: Workbook;
  selected: string[];
  append: boolean;
}

interface PersistedFile {
  id: string;
  selected: string[];
  append: boolean;
}

interface Persisted {
  files: PersistedFile[];
  focusedId: string | null;
  batchId: string | null;
  batchConfigKey: string | null;
  resultsJobId: string | null;
  outputIds: Record<string, string>;
}

interface Workflow {
  hydrated: boolean;
  files: FileEntry[];
  /** The file the Sheet selection section is showing. */
  focused: FileEntry | null;
  setFocusedId: (id: string) => void;
  totalSheets: number;
  totalSelected: number;
  /** Uploads still transferring; running waits for them. */
  uploading: number;
  setUploading: (count: number) => void;

  batch: BatchStatus | null;
  /** This file's job in the current batch, if it was part of it. */
  jobFor: (workbookId: string) => JobStatus | null;
  results: Record<string, JobResults>;
  resultsErrors: Record<string, ApiError>;
  /** The job (file) Review & Results is showing. */
  resultsJobId: string | null;
  setResultsJobId: (jobId: string) => void;
  outputIdFor: (jobId: string) => string | null;
  setOutputId: (jobId: string, outputId: string) => void;

  /** The batch no longer matches the current files and settings. */
  stale: boolean;
  running: boolean;
  canRun: boolean;
  runBlockedReason: string | null;
  canReview: boolean;
  reviewBlockedReason: string | null;

  addWorkbook: (workbook: Workbook) => void;
  removeWorkbook: (id: string) => Promise<void>;
  removeAll: () => Promise<void>;
  setSelected: (id: string, sheets: string[]) => void;
  setAppend: (id: string, append: boolean) => void;
  startBatch: () => Promise<void>;
  cancelBatch: () => Promise<void>;
  cancelJob: (jobId: string) => Promise<void>;
  reloadResults: (jobId: string) => Promise<void>;
  applyOutputUpdate: (jobId: string, summary: OutputSummary) => void;
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

/** Appending needs two sheets to compare: in the file, and in the selection. */
export function appendUnavailableReason(entry: FileEntry | null): string | null {
  if (!entry) return "Upload a workbook first.";
  if (entry.workbook.sheets.length < 2)
    return entry.workbook.kind === "delimited"
      ? "A CSV or text file holds a single table, so there is nothing to append."
      : "This workbook has only one sheet, so there is nothing to append.";
  if (entry.selected.length < 2) return "Select two or more sheets to append them.";
  return null;
}

export const effectiveAppend = (entry: FileEntry) => entry.append && !appendUnavailableReason(entry);

const configKey = (files: FileEntry[]) =>
  JSON.stringify(files.map((entry) => [entry.workbook.id, [...entry.selected].sort(), effectiveAppend(entry)]));

/** Pre-select every sheet that holds something; hidden or empty sheets stay opt-in. */
const defaultSelection = (workbook: Workbook) =>
  workbook.sheets.filter((sheet) => sheet.has_content && !sheet.hidden).map((sheet) => sheet.name);

const isActive = (status: string | undefined) => status === "queued" || status === "running";

export function WorkflowProvider({ children }: { children: ReactNode }) {
  const toast = useToast();
  const [hydrated, setHydrated] = useState(false);
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const [uploading, setUploading] = useState(0);
  const [batch, setBatch] = useState<BatchStatus | null>(null);
  const [batchConfigKey, setBatchConfigKey] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, JobResults>>({});
  const [resultsErrors, setResultsErrors] = useState<Record<string, ApiError>>({});
  const [resultsJobId, setResultsJobId] = useState<string | null>(null);
  const [outputIds, setOutputIds] = useState<Record<string, string>>({});
  const announced = useRef<string | null>(null);
  const requested = useRef(new Set<string>());

  // ---- hydrate from the session, verifying each id still exists on the server ----
  useEffect(() => {
    const saved = readPersisted();
    (async () => {
      if (saved?.files?.length) {
        const found = await Promise.all(
          saved.files.map(async (item): Promise<FileEntry | null> => {
            try {
              const workbook = await api.getWorkbook(item.id);
              const names = new Set(workbook.sheets.map((sheet) => sheet.name));
              return { workbook, selected: item.selected.filter((name) => names.has(name)), append: item.append };
            } catch {
              return null; // upload expired with a server restart
            }
          }),
        );
        const kept = found.filter((entry): entry is FileEntry => entry !== null);
        setFiles(kept);
        setFocusedId(kept.some((entry) => entry.workbook.id === saved.focusedId) ? saved.focusedId : kept[0]?.workbook.id ?? null);
        if (saved.batchId) {
          try {
            const status = await api.getBatch(saved.batchId);
            announced.current = isActive(status.status) ? null : status.id;
            setBatch(status);
            setBatchConfigKey(saved.batchConfigKey);
            setResultsJobId(saved.resultsJobId);
            setOutputIds(saved.outputIds ?? {});
          } catch {
            /* batch expired with a server restart */
          }
        }
      }
      setHydrated(true);
    })();
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    const data: Persisted = {
      files: files.map((entry) => ({ id: entry.workbook.id, selected: entry.selected, append: entry.append })),
      focusedId,
      batchId: batch?.id ?? null,
      batchConfigKey,
      resultsJobId,
      outputIds,
    };
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(data));
    } catch {
      /* storage unavailable: state simply won't survive a reload */
    }
  }, [hydrated, files, focusedId, batch?.id, batchConfigKey, resultsJobId, outputIds]);

  const running = isActive(batch?.status);

  // ---- poll the running batch; lives here so progress continues across pages ----
  useEffect(() => {
    if (!batch || !running) return;
    let cancelled = false;
    const timer = setTimeout(async () => {
      try {
        const next = await api.getBatch(batch.id);
        if (!cancelled) setBatch(next);
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 404) {
          // The server restarted mid-run: every unfinished file is lost.
          setBatch((current) =>
            current && {
              ...current,
              status: "failed",
              finished_at: Date.now() / 1000,
              jobs: current.jobs.map((job) =>
                isActive(job.status)
                  ? {
                      ...job,
                      status: "failed",
                      error: {
                        kind: "not_found",
                        message: "The cleaning job is no longer available.",
                        advice: "The server may have restarted. Upload the files again and rerun.",
                      },
                    }
                  : job,
              ),
            },
          );
        } else setBatch((current) => (current ? { ...current } : current)); // retry
      }
    }, POLL_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [batch, running]);

  const loadResults = useCallback(async (jobId: string) => {
    requested.current.add(jobId);
    setResultsErrors(({ [jobId]: _, ...rest }) => rest);
    try {
      const data = await api.getResults(jobId);
      setResults((all) => ({ ...all, [jobId]: data }));
      setOutputIds((all) =>
        all[jobId] && data.outputs.some((output) => output.id === all[jobId])
          ? all
          : data.outputs[0]
            ? { ...all, [jobId]: data.outputs[0].id }
            : all,
      );
    } catch (error) {
      const apiError = error instanceof ApiError ? error : new ApiError(0, { code: "unknown", message: String(error) });
      setResultsErrors((all) => ({ ...all, [jobId]: apiError }));
    }
  }, []);

  // ---- load each file's results as soon as its job succeeds, even mid-batch ----
  useEffect(() => {
    if (!batch) return;
    for (const job of batch.jobs) {
      if (job.status === "succeeded" && !requested.current.has(job.id)) void loadResults(job.id);
    }
  }, [batch, loadResults]);

  // ---- keep Review & Results pointed at a file that has results ----
  useEffect(() => {
    if (!batch) return;
    const succeeded = batch.jobs.filter((job) => job.status === "succeeded");
    if (!succeeded.some((job) => job.id === resultsJobId) && succeeded[0]) setResultsJobId(succeeded[0].id);
  }, [batch, resultsJobId]);

  // ---- announce the batch finishing, once ----
  useEffect(() => {
    if (!batch || running || announced.current === batch.id) return;
    announced.current = batch.id;
    const files = plural(batch.files_total, "file");
    if (batch.status === "succeeded")
      toast({ severity: "success", title: "Cleaning completed", description: batch.files_total === 1 ? batch.jobs[0]?.source_name : `All ${files} were cleaned.` });
    else if (batch.status === "partial")
      toast({
        severity: "warning",
        title: "Cleaning finished with errors",
        description: `${batch.files_succeeded} of ${files} cleaned; ${plural(batch.files_total - batch.files_succeeded, "file")} did not.`,
      });
    else if (batch.status === "failed")
      toast({ severity: "error", title: "Cleaning could not be completed", description: batch.files_total === 1 ? batch.jobs[0]?.error?.message : `None of the ${files} could be cleaned.` });
    else if (batch.status === "cancelled") toast({ severity: "info", title: "Cleaning cancelled" });
  }, [batch, running, toast]);

  // ---- actions ----
  const clearBatch = () => {
    setBatch(null);
    setBatchConfigKey(null);
    setResults({});
    setResultsErrors({});
    setResultsJobId(null);
    setOutputIds({});
    requested.current.clear();
  };

  const addWorkbook = useCallback((workbook: Workbook) => {
    setFiles((current) => [...current.filter((entry) => entry.workbook.id !== workbook.id), { workbook, selected: defaultSelection(workbook), append: true }]);
    setFocusedId(workbook.id);
  }, []);

  const removeWorkbook = useCallback(
    async (id: string) => {
      const entry = files.find((item) => item.workbook.id === id);
      try {
        await api.deleteWorkbook(id);
      } catch (error) {
        // 404: already gone on the server, so clear it locally anyway. Anything else
        // (a run still using it) means the file must stay.
        if (!(error instanceof ApiError && error.status === 404)) {
          toast({ severity: "error", title: "The file couldn't be removed", description: error instanceof ApiError ? error.body.message : String(error) });
          return;
        }
      }
      const remaining = files.filter((item) => item.workbook.id !== id);
      setFiles((current) => current.filter((item) => item.workbook.id !== id));
      setFocusedId((focus) => (focus === id ? remaining[0]?.workbook.id ?? null : focus));
      toast({ severity: "info", title: "File removed", description: entry?.workbook.filename });
    },
    [files, toast],
  );

  const removeAll = useCallback(async () => {
    await Promise.allSettled(files.map((entry) => api.deleteWorkbook(entry.workbook.id)));
    setFiles([]);
    setFocusedId(null);
    clearBatch();
    toast({ severity: "info", title: "Workspace cleared" });
  }, [files, toast]);

  const update = (id: string, change: (entry: FileEntry) => FileEntry) =>
    setFiles((current) => current.map((entry) => (entry.workbook.id === id ? change(entry) : entry)));

  const setSelected = useCallback((id: string, sheets: string[]) => update(id, (entry) => ({ ...entry, selected: sheets })), []);
  const setAppend = useCallback((id: string, append: boolean) => update(id, (entry) => ({ ...entry, append })), []);

  const startBatch = useCallback(async () => {
    if (!files.length) return;
    const status = await api.startBatch(
      files.map((entry) => ({ workbook_id: entry.workbook.id, sheets: entry.selected, append: effectiveAppend(entry) })),
    );
    announced.current = null;
    clearBatch();
    setBatch(status);
    setBatchConfigKey(configKey(files));
  }, [files]);

  const cancelBatch = useCallback(async () => {
    if (!batch) return;
    try {
      setBatch(await api.cancelBatch(batch.id));
    } catch (error) {
      if (error instanceof ApiError) toast({ severity: "warning", title: error.body.message });
    }
  }, [batch, toast]);

  const cancelJob = useCallback(
    async (jobId: string) => {
      try {
        const job = await api.cancelJob(jobId);
        setBatch((current) => current && { ...current, jobs: current.jobs.map((item) => (item.id === job.id ? job : item)) });
      } catch (error) {
        if (error instanceof ApiError) toast({ severity: "warning", title: error.body.message });
      }
    },
    [toast],
  );

  const applyOutputUpdate = useCallback((jobId: string, summary: OutputSummary) => {
    setResults((all) => {
      const current = all[jobId];
      if (!current) return all;
      return { ...all, [jobId]: { ...current, outputs: current.outputs.map((output) => (output.id === summary.id ? summary : output)) } };
    });
  }, []);

  // ---- derived ----
  const focused = files.find((entry) => entry.workbook.id === focusedId) ?? files[0] ?? null;
  const totalSheets = files.reduce((sum, entry) => sum + entry.workbook.sheets.length, 0);
  const totalSelected = files.reduce((sum, entry) => sum + entry.selected.length, 0);
  const stale = Boolean(batch && batchConfigKey && batchConfigKey !== configKey(files));

  const jobsByWorkbook = useMemo(() => new Map((batch?.jobs ?? []).map((job) => [job.workbook_id, job])), [batch]);
  const jobFor = useCallback((workbookId: string) => jobsByWorkbook.get(workbookId) ?? null, [jobsByWorkbook]);
  const outputIdFor = useCallback((jobId: string) => outputIds[jobId] ?? null, [outputIds]);
  const setOutputId = useCallback((jobId: string, outputId: string) => setOutputIds((all) => ({ ...all, [jobId]: outputId })), []);

  const unselected = files.find((entry) => entry.selected.length === 0);
  const runBlockedReason = !files.length
    ? "Upload a workbook first."
    : uploading
      ? "Wait for the uploads to finish."
      : unselected
        ? `${unselected.workbook.filename} has no sheets selected. Select at least one, or remove the file.`
        : null;
  const anySucceeded = Boolean(batch?.jobs.some((job) => job.status === "succeeded"));
  const reviewBlockedReason = anySucceeded
    ? null
    : running
      ? "Results will be available when the first file finishes cleaning."
      : batch && !running
        ? "No file was cleaned successfully. Fix the errors and run again."
        : "Run a cleaning job to generate results.";

  const value: Workflow = {
    hydrated,
    files,
    focused,
    setFocusedId,
    totalSheets,
    totalSelected,
    uploading,
    setUploading,
    batch,
    jobFor,
    results,
    resultsErrors,
    resultsJobId,
    setResultsJobId,
    outputIdFor,
    setOutputId,
    stale,
    running,
    canRun: !runBlockedReason && !running,
    runBlockedReason,
    canReview: !reviewBlockedReason,
    reviewBlockedReason,
    addWorkbook,
    removeWorkbook,
    removeAll,
    setSelected,
    setAppend,
    startBatch,
    cancelBatch,
    cancelJob,
    reloadResults: loadResults,
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
