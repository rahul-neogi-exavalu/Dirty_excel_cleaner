import type {
  ApiErrorBody,
  BatchFileRequest,
  BatchStatus,
  ColumnProfile,
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: init?.body && !(init.body instanceof FormData)
        ? { "Content-Type": "application/json", ...init?.headers }
        : init?.headers,
    });
  } catch {
    throw new ApiError(0, NETWORK_ERROR);
  }
  if (!response.ok) throw await parseError(response);
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
          new ApiError(
            xhr.status,
            body?.error ?? { code: `http_${xhr.status}`, message: "The upload failed.", advice: "Try again." },
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
    response = await fetch(url);
  } catch {
    throw new ApiError(0, NETWORK_ERROR);
  }
  if (!response.ok) throw await parseError(response);
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
