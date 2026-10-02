import type {
  ApiErrorBody,
  AuthUser,
  HistoryFilters,
  HistoryPage,
  HistoryRun,
  UserDraft,
  BatchFileRequest,
  BatchStatus,
  BronzeStatus,
  BronzeTable,
  ColumnProfile,
  EligibleLoad,
  CleanupLoad,
  IngestPlan,
  SavedMapping,
  SilverCatalog,
  SilverRun,
  SilverSummaryRow,
  JobResults,
  JobStatus,
  OutputSummary,
  Preview,
  Workbook,
} from "./types";

export class ApiError extends Error {
  status: number;
  body: ApiErrorBody;
  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.status = status;
    this.body = body;
  }
}

const NETWORK_ERROR: ApiErrorBody = {
  code: "network",
  message: "Can't reach the cleaning service.",
  advice: "Check that the API server is running, then try again.",
};

async function parseError(response: Response): Promise<ApiError> {
  try {
    const data = await response.json();
    if (data?.error) return new ApiError(response.status, data.error);
  } catch {
    /* fall through */
  }
  return new ApiError(response.status, {
    code: `http_${response.status}`,
    message: `The server responded with an unexpected error (${response.status}).`,
    advice: "Try again. If it keeps happening, check the server logs.",
  });
}

/** Sent on every request. A form on another site can't set it, so the server can
 *  refuse cross-site writes that ride on the session cookie. */
const CLIENT_HEADER = { "X-AHI-Client": "web" };

/** Fired when the server says the session is gone; the auth provider shows sign-in. */
export const AUTH_EXPIRED = "ahi:auth-expired";

/** Answers that mean this person can't go on as they are: back to sign-in, saying why. */
const SIGNED_OUT = new Set(["not_authenticated", "account_inactive", "account_expired"]);

function noticeAuth(error: ApiError): ApiError {
  if (SIGNED_OUT.has(error.body.code)) {
    const reason = error.body.code === "not_authenticated" ? "Session expired. Sign in again." : error.body.message;
    window.dispatchEvent(new CustomEvent(AUTH_EXPIRED, { detail: reason }));
  }
  return error;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      credentials: "same-origin",
      headers: init?.body && !(init.body instanceof FormData)
        ? { "Content-Type": "application/json", ...CLIENT_HEADER, ...init?.headers }
        : { ...CLIENT_HEADER, ...init?.headers },
    });
  } catch {
    throw new ApiError(0, NETWORK_ERROR);
  }
  if (!response.ok) throw noticeAuth(await parseError(response));
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

/** Upload with real byte progress; fetch cannot report upload progress. */
export function uploadWorkbook(
  file: File,
  onProgress: (fraction: number) => void,
  signal?: AbortSignal,
): Promise<Workbook> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/workbooks");
    xhr.setRequestHeader("X-AHI-Client", CLIENT_HEADER["X-AHI-Client"]);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.upload.onload = () => onProgress(1);
    xhr.onload = () => {
      let body: any = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* non-JSON */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as Workbook);
      else
        reject(
          noticeAuth(
            new ApiError(
              xhr.status,
              body?.error ?? { code: `http_${xhr.status}`, message: "The upload failed.", advice: "Try again." },
            ),
          ),
        );
    };
    xhr.onerror = () => reject(new ApiError(0, NETWORK_ERROR));
    xhr.onabort = () => reject(new ApiError(0, { code: "aborted", message: "Upload cancelled." }));
    signal?.addEventListener("abort", () => xhr.abort());
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}

export const api = {
  me: () => request<{ user: AuthUser }>("/api/auth/me"),
  login: (email: string, password: string) =>
    request<{ user: AuthUser }>("/api/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  logout: () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  changePassword: (current_password: string, new_password: string) =>
    request<{ user: AuthUser }>("/api/auth/password", {
      method: "POST",
      body: JSON.stringify({ current_password, new_password }),
    }),

  listUsers: () => request<{ users: AuthUser[] }>("/api/users"),
  createUser: (draft: UserDraft) =>
    request<{ user: AuthUser; temporary_password: string }>("/api/users", { method: "POST", body: JSON.stringify(draft) }),
  updateUser: (id: string, draft: UserDraft) =>
    request<{ user: AuthUser }>(`/api/users/${id}`, { method: "PATCH", body: JSON.stringify(draft) }),
  resetPassword: (id: string) =>
    request<{ user: AuthUser; temporary_password: string }>(`/api/users/${id}/reset-password`, { method: "POST" }),

  history: (filters: HistoryFilters, signal?: AbortSignal) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== null && value !== "") query.set(key, String(value));
    }
    return request<HistoryPage>(`/api/history?${query}`, { signal });
  },
  historyRun: (jobId: string) => request<HistoryRun>(`/api/history/${jobId}`),

  getWorkbook: (id: string) => request<Workbook>(`/api/workbooks/${id}`),
  deleteWorkbook: (id: string) => request<void>(`/api/workbooks/${id}`, { method: "DELETE" }),

  startJob: (workbook_id: string, sheets: string[], append: boolean) =>
    request<JobStatus>("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ workbook_id, sheets, append }),
    }),
  getJob: (id: string) => request<JobStatus>(`/api/jobs/${id}`),

  startBatch: (files: BatchFileRequest[]) =>
    request<BatchStatus>("/api/batches", { method: "POST", body: JSON.stringify({ files }) }),
  getBatch: (id: string) => request<BatchStatus>(`/api/batches/${id}`),
  cancelBatch: (id: string) => request<BatchStatus>(`/api/batches/${id}/cancel`, { method: "POST" }),
  cancelJob: (id: string) => request<JobStatus>(`/api/jobs/${id}/cancel`, { method: "POST" }),
  getResults: (id: string) => request<JobResults>(`/api/jobs/${id}/results`),

  getPreview: (
    jobId: string,
    outputId: string,
    params: { offset: number; limit: number; q?: string; sort?: string; desc?: boolean },
    signal?: AbortSignal,
  ) => {
    const query = new URLSearchParams({ offset: String(params.offset), limit: String(params.limit) });
    if (params.q) query.set("q", params.q);
    if (params.sort) query.set("sort", params.sort);
    if (params.desc) query.set("desc", "true");
    return request<Preview>(`/api/jobs/${jobId}/outputs/${outputId}/preview?${query}`, { signal });
  },
  getColumns: (jobId: string, outputId: string) =>
    request<ColumnProfile[]>(`/api/jobs/${jobId}/outputs/${outputId}/columns`),
  renameHeaders: (jobId: string, outputId: string, renames: Record<string, string>) =>
    request<OutputSummary>(`/api/jobs/${jobId}/outputs/${outputId}/headers`, {
      method: "PUT",
      body: JSON.stringify({ renames }),
    }),
  resetHeaders: (jobId: string, outputId: string, columns: string[] | null) =>
    request<OutputSummary>(`/api/jobs/${jobId}/outputs/${outputId}/headers/reset`, {
      method: "POST",
      body: JSON.stringify({ columns }),
    }),

  bronzeStatus: () => request<BronzeStatus>("/api/bronze/status"),
  bronzeTables: () => request<BronzeTable[]>("/api/bronze/tables"),
  createPlan: (job_ids: string[], batch_id: string | null) =>
    request<IngestPlan>("/api/bronze/plans", { method: "POST", body: JSON.stringify({ job_ids, batch_id }) }),
  getPlan: (id: string) => request<IngestPlan>(`/api/bronze/plans/${id}`),
  updatePlanFile: (id: string, jobId: string, change: { source_system?: string | null; period_start?: string | null; period_end?: string | null }) =>
    request<IngestPlan>(`/api/bronze/plans/${id}/files/${jobId}`, { method: "PATCH", body: JSON.stringify(change) }),
  updatePlanItem: (id: string, key: string, change: { table_name?: string | null; action?: string | null }) =>
    request<IngestPlan>(`/api/bronze/plans/${id}/items/${encodeURIComponent(key)}`, { method: "PATCH", body: JSON.stringify(change) }),
  /** The signed-in user is recorded as the reviewer. */
  approvePlan: (id: string, confirmed: string[]) =>
    request<IngestPlan>(`/api/bronze/plans/${id}/approve`, { method: "POST", body: JSON.stringify({ confirmed }) }),

  silverCatalog: () => request<SilverCatalog>("/api/silver/catalog"),
  silverEligible: () => request<{ loads: EligibleLoad[]; cleanup: CleanupLoad[] }>("/api/silver/eligible"),
  createSilverRun: (ingestion_ids: string[]) =>
    request<SilverRun>("/api/silver/runs", { method: "POST", body: JSON.stringify({ ingestion_ids }) }),
  getSilverRun: (id: string) => request<SilverRun>(`/api/silver/runs/${id}`),
  editSilverRunMapping: (id: string, change: { table_name: string; bronze_column: string; silver_column: string | null; ignored: boolean }) =>
    request<SilverRun>(`/api/silver/runs/${id}/mapping`, { method: "PATCH", body: JSON.stringify(change) }),
  /** The signed-in user is recorded as the reviewer. */
  approveSilverRun: (id: string) =>
    request<SilverRun>(`/api/silver/runs/${id}/approve`, { method: "POST", body: JSON.stringify({}) }),
  silverMapping: () => request<SavedMapping[]>("/api/silver/mapping"),
  editSilverMapping: (row: { pc_id: string; bronze_table_name: string; bronze_column_name: string; silver_column_name: string | null }) =>
    request<SavedMapping>("/api/silver/mapping", { method: "PATCH", body: JSON.stringify(row) }),
  silverSummary: () => request<SilverSummaryRow[]>("/api/silver/summary"),
};

export const exportUrls = {
  csv: (jobId: string, outputId: string) => `/api/jobs/${jobId}/outputs/${outputId}/export/csv`,
  metadata: (jobId: string, outputId: string) => `/api/jobs/${jobId}/outputs/${outputId}/export/metadata`,
  audit: (jobId: string) => `/api/jobs/${jobId}/export/audit`,
  zip: (jobId: string) => `/api/jobs/${jobId}/export/zip`,
  batchZip: (batchId: string) => `/api/batches/${batchId}/export/zip`,
};

/** Fetch a file and hand it to the browser, so the UI can show preparing/done/error. */
export async function download(url: string): Promise<string> {
  let response: Response;
  try {
    response = await fetch(url, { credentials: "same-origin", headers: CLIENT_HEADER });
  } catch {
    throw new ApiError(0, NETWORK_ERROR);
  }
  if (!response.ok) throw noticeAuth(await parseError(response));
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
  const filename = match ? decodeURIComponent(match[1]) : url.split("/").pop() ?? "download";
  const blob = await response.blob();
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
  return filename;
}
