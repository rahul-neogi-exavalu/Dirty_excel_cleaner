export interface SheetInfo {
  name: string;
  hidden: boolean;
  has_content: boolean;
}

export interface Workbook {
  id: string;
  filename: string;
  size: number;
  kind: "workbook" | "delimited";
  sheets: SheetInfo[];
  uploaded_at: number;
}

export type JobState = "queued" | "running" | "succeeded" | "failed" | "cancelled";
export type Stage = "read" | "clean" | "append" | "validate" | "write";

export interface JobError {
  kind: string;
  message: string;
  detail?: string | null;
  advice?: string | null;
  stage?: Stage | null;
  sheet?: string | null;
  technical?: string | null;
}

export interface JobStatus {
  id: string;
  batch_id: string | null;
  workbook_id: string;
  source_name: string;
  sheets: string[];
  append: boolean;
  status: JobState;
  stage: Stage | null;
  stages: Stage[];
  progress: number;
  message: string;
  current_sheet: string | null;
  /** Sheets being read and cleaned right now; several when they run in parallel. */
  active_sheets: string[];
  /** Worker processes this job's sheets are spread over; 0 when cleaned in-process. */
  parallel_workers: number;
  sheets_done: number;
  sheets_total: number;
  rows_kept: number;
  rows_removed: number;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  elapsed_seconds: number | null;
  error: JobError | null;
}

export type BatchState = "queued" | "running" | "succeeded" | "partial" | "failed" | "cancelled";

/** Several files cleaned together: one job per file, run one after another. */
export interface BatchStatus {
  id: string;
  status: BatchState;
  progress: number;
  files_total: number;
  files_done: number;
  files_succeeded: number;
  files_failed: number;
  files_cancelled: number;
  current_job_id: string | null;
  /** In the order the files were submitted. */
  jobs: JobStatus[];
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  elapsed_seconds: number | null;
}

export interface BatchFileRequest {
  workbook_id: string;
  sheets: string[];
  append: boolean;
}

export interface OutputSummary {
  id: string;
  name: string;
  kind: "stacked" | "standalone";
  rows: number;
  columns: number;
  column_names: string[];
  tables: string[];
  sheet_names: string[];
  flagged_columns: number;
  renamed_columns: number;
  headers_updated_at: number | null;
  file: string;
  metadata_file: string;
}

export interface RemovedRow {
  sheet_row: number | null;
  classification: string;
  reason: string;
  content: string;
}

export interface TableReport {
  label: string;
  rows: number;
  columns: number;
  region_rows: number | null;
  region_columns: number | null;
  header_detected: boolean;
  header_row: number | null;
  header_adopted_from: string | null;
  orientation: string;
  removed_rows: number;
  removed_by_reason: Record<string, number>;
  removed_examples: RemovedRow[];
  coercion_failures: number;
  empty_columns: string[];
  notes: string[];
  output_id: string | null;
}

export interface SheetReport {
  sheet: string;
  raw_rows: number;
  raw_columns: number;
  rows: number;
  removed_rows: number;
  status: "cleaned" | "no_table";
  tables: TableReport[];
}

export interface Relationship {
  tables: string[];
  decision: "appended" | "kept_separate";
  reason: string;
}

export interface Violation {
  table: string;
  check: string;
  detail: string;
}

export interface ResultsSummary {
  source_name: string;
  append: boolean;
  sheets_selected: number;
  sheets_with_tables: number;
  tables_found: number;
  outputs: number;
  appended_outputs: number;
  rows: number;
  raw_rows: number;
  rows_removed: number;
  removed_by_reason: Record<string, number>;
  coercion_failures: number;
  validation_findings: number;
  contract_violations: number;
  consistency_issues: number;
  headerless_tables: number;
  flagged_columns: number;
  embedded_images: number;
  completed_at: number;
}

export interface JobResults {
  job: JobStatus;
  summary: ResultsSummary;
  outputs: OutputSummary[];
  sheets: SheetReport[];
  relationships: Relationship[];
  violations: Violation[];
  append_check: AppendCheck | null;
  /** Keyed by output id. */
  consistency: Record<string, ConsistencyReport>;
}

export interface PreviewColumn {
  name: string;
  original: string;
  dtype: string;
  renamed: boolean;
}

export interface Preview {
  columns: PreviewColumn[];
  rows: (string | number | boolean | null)[][];
  offset: number;
  limit: number;
  total: number;
  total_unfiltered: number;
}

export interface ColumnProfile {
  header_name: string;
  datatype: string;
  inferred_datatype: string | null;
  distinct_count: number;
  count: number;
  total_row_count: number;
  min: string;
  max: string;
  sum: string;
  null_percentage: string;
  excel_name: string;
  sheet_name: string;
  type_flag: string;
}

export interface ApiErrorBody {
  code: string;
  message: string;
  advice?: string | null;
  detail?: string | null;
  field?: string | null;
}

export interface AppendGroup {
  tables: string[];
  sheets: string[];
  columns: string[];
  rows: number;
  appended: boolean;
  is_reference: boolean;
  missing_columns: string[];
  extra_columns: string[];
}

/** How auto-detect & append turned out, reported by the run. */
export interface AppendCheck {
  status: "all_match" | "partial" | "none_match" | "single_table" | "no_tables";
  tables: number;
  outputs: number;
  appended_groups: number;
  groups: AppendGroup[];
  headerless: { label: string; sheet: string; columns: number; rows: number }[];
}

export type CheckStatus = "passed" | "failed" | "not_applicable";

export interface ConsistencyCheck {
  id: string;
  title: string;
  description: string;
  status: CheckStatus;
  details: string[];
}

/** raw = blank + header + removed + kept, per source sheet (or column, for a sheet turned upright). */
export interface RowAccounting {
  sheet: string;
  axis: "rows" | "columns";
  raw: number;
  blank: number;
  header: number;
  removed: number;
  removed_by_reason: Record<string, number>;
  kept: number;
  unaccounted: number;
  unaccounted_rows: { sheet_row: number; content: string }[];
  removed_rows: RemovedRow[];
  status: CheckStatus;
  note: string | null;
}

export interface ConsistencyReport {
  output_id: string;
  status: "passed" | "failed";
  issues: number;
  checks: ConsistencyCheck[];
  accounting: RowAccounting[];
}
