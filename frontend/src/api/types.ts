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

/* ---- Bronze ingestion ---------------------------------------------------- */

export type IngestAction = "create" | "append" | "reorder" | "evolve" | "replace" | "new_table" | "skip";

export interface BronzeStatus {
  enabled: boolean;
  configured: boolean;
  reachable: boolean;
  host: string | null;
  database: string | null;
  bronze_schema: string;
  error: string | null;
}

export interface PeriodCandidate {
  source: string;
  column: string | null;
  start: string | null;
  end: string | null;
  rows: number | null;
  months?: number[];
}

export interface PlanFile {
  job_id: string;
  file_name: string;
  source_system: string | null;
  detected_source_system: string | null;
  period_start: string | null;
  period_end: string | null;
  detected_period_start: string | null;
  detected_period_end: string | null;
  period_source: string | null;
  period_candidates: PeriodCandidate[];
  /** PC + 4-digit number, e.g. PC0796 (from the file name, or entered). */
  pc_id: string | null;
  detected_pc_id: string | null;
  /** YYYY-MM-DD. */
  file_date: string | null;
  detected_file_date: string | null;
  division_name: string | null;
  /** The divisions division_mapping lists for the profit center (two for a few). */
  division_matches: string[];
  /** What the reviewer may choose from. */
  division_options: string[];
}

export interface PlanReplaceRef {
  id: string;
  file_name: string;
  period_start: string | null;
  period_end: string | null;
}

export interface PlanItem {
  key: string;
  file_name: string;
  source_system: string | null;
  sheet_names: string[];
  rows: number;
  period_start: string | null;
  period_end: string | null;
  suggested_table: string;
  table_name: string;
  action: IngestAction;
  allowed_actions: IngestAction[];
  comparison: { kind: "identical" | "reordered" | "evolved" | "different"; added: string[]; missing: string[] } | null;
  columns_after: string[];
  columns_before: string[] | null;
  replaces: PlanReplaceRef[];
  rebuild: boolean;
  requires_confirmation: boolean;
  reasons: string[];
  blockers: string[];
}

export interface IngestPlan {
  id: string;
  batch_id: string | null;
  status: "draft" | "running" | "succeeded" | "failed";
  progress: number;
  message: string;
  error: { message: string; advice?: string | null; technical?: string | null } | null;
  reviewed_by: string | null;
  files: PlanFile[];
  items: PlanItem[];
  blockers: string[];
  results: Record<string, { ingestion_id: string; rows_loaded: number; table_name: string }>;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
}

export interface BronzeIngestion {
  id: string;
  file_name: string;
  period_start: string | null;
  period_end: string | null;
  action: IngestAction;
  rows_loaded: number;
  status: "ingested" | "superseded" | "skipped";
  reviewed_by: string | null;
  created_at: number;
  superseded_by: string | null;
  pc_id: string | null;
  file_date: string | null;
  division_name: string | null;
  processing_date: number | null;
}

export interface BronzeTable {
  table_name: string;
  schema_name: string;
  source_system: string;
  columns: { name: string; datatype: string | null }[];
  rows: number;
  period_start: string | null;
  period_end: string | null;
  created_at: number;
  updated_at: number;
  ingestions: BronzeIngestion[];
}

/* ---- Silver ---------------------------------------------------------------- */

/** A matching method; each one votes independently on every bronze column. */
export type MatchMethod = "saved" | "exact" | "fuzzy" | "semantic" | "ai";
/** How the current choice was made: pre-selected from the votes, by the reviewer, or not yet. */
export type MappingSelection = "recommended" | "manual" | "none";

export interface SilverVote {
  method: MatchMethod;
  score: number;
  reason: string;
  /** The AI's second choice: offered, not counted. */
  second_choice: boolean;
}

export interface SilverCandidate {
  /** null: do not load the column (Ignore). */
  silver_column: string | null;
  recommended: boolean;
  /** Number of methods that voted for it. */
  support: number;
  votes: SilverVote[];
}

export interface SilverColumnDef {
  name: string;
  drt_name: string;
  /** As the business's silver_schema states it: string, int, bigint, boolean, date, timestamp, decimal(p,s). */
  data_type: string;
  business_key: boolean;
  description: string;
  /** "mapped": a bronze column can map to it; "system": the pipeline fills it. */
  role: "mapped" | "system";
}

export interface SilverCatalog {
  file: string;
  /** Every silver_detail column, in table order. */
  columns: SilverColumnDef[];
  aggregate_columns: string[];
  semantic: boolean;
  ai: boolean;
  /** Provider and model, e.g. "Azure OpenAI · gpt-4.1-mini"; empty when off. */
  ai_label: string;
}

export interface EligibleLoad {
  ingestion_id: string;
  table_name: string;
  file_name: string;
  source_system: string | null;
  period_start: string | null;
  period_end: string | null;
  rows: number;
  ingested_at: number;
  silver_status: string | null;
  pc_id: string | null;
  division_name: string | null;
  file_date: string | null;
  processing_date: number;
}

export interface CleanupLoad {
  ingestion_id: string;
  table_name: string;
  file_name: string;
  source_system: string | null;
  pc_id: string | null;
  processing_date: string;
}

export interface SilverMappingRow {
  bronze_column: string;
  /** The header as the source file wrote it ("Net Premium"); null for older loads. */
  source_header?: string | null;
  silver_column: string | null;
  /** More Silver columns the same bronze column also loads into. */
  also: string[];
  ignored: boolean;
  selection: MappingSelection;
  /** Methods that voted for the current choice. */
  methods: MatchMethod[];
  /** Methods disagree. */
  split: boolean;
  reason: string;
  samples: string[];
  /** Best first. */
  candidates: SilverCandidate[];
}

export interface SilverQuality {
  rows: number;
  invalid_values: Record<string, number>;
  profit_center: Record<string, number>;
  unmapped_silver_columns: string[];
}

export interface SilverTableReview {
  table_name: string;
  source_system: string | null;
  pc_id: string | null;
  /** Every profit center the table's loads carry (each row keeps its own). */
  pc_ids?: string[];
  loads: { ingestion_id: string; file_name: string; rows: number; period_start: string | null; period_end: string | null }[];
  quality: SilverQuality;
  mapping: SilverMappingRow[];
}

export interface SilverRun {
  id: string;
  status: "draft" | "running" | "succeeded" | "failed";
  progress: number;
  message: string;
  error: { message: string; advice?: string | null } | null;
  reviewed_by: string | null;
  notes: string[];
  cleanup: CleanupLoad[];
  lotl_rows: number;
  blockers: string[];
  result: { rows_loaded?: number; rows_removed?: number; mappings_saved?: number };
  tables: SilverTableReview[];
}

/** A row of the business's DRT column mapping. One source column can have two rows. */
export interface DrtMapping {
  /** PC0077 */
  profit_center: string;
  /** The source column as the file writes it. */
  pc_column: string;
  /** The business's DRT label for the Silver column; null when the business has none for it. */
  drt_column: string | null;
  /** null: a DRT label that names no Silver column. Ignores are never saved. */
  silver_column_name: string | null;
}

/** A silver_aggregate row: every column of the business's aggregate schema. */
export interface SilverAggregateRow {
  source_system: string | null;
  profit_center_number: string | null;
  profit_center_name: string | null;
  record_grain: string | null;
  reporting_period: string | null;
  reporting_year: number | null;
  reporting_month: number | null;
  premium: number | null;
  policy_fees: number | null;
  gross_commission_amount: number | null;
  producer_commission_amount: number | null;
  revenue: number | null;
  policy_count: number | null;
  file_date: string | null;
  [column: string]: string | number | boolean | null;
}

/* ---- Sign-in, users and job history ---- */

export type UserState = "active" | "inactive" | "expired";

export interface AuthUser {
  user_id: string;
  user_name: string;
  user_email_id: string;
  user_phone_number: string | null;
  is_admin: boolean;
  is_active: boolean;
  state: UserState;
  must_change_password: boolean;
  last_login_at: string | null;
  expiry_date: string | null;
  created_at: string | null;
  modified_at: string | null;
}

export interface UserDraft {
  user_name?: string;
  user_email_id?: string;
  user_phone_number?: string | null;
  is_admin?: boolean;
  is_active?: boolean;
  expiry_date?: string | null;
}

export type HistoryJobType = "clean" | "bronze_ingest" | "silver_load";
export type HistoryStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled" | "skipped" | "interrupted";

export interface HistoryOutput {
  job_row_id: string;
  output_name: string;
  output_file: string | null;
  row_count: number | null;
  rows_removed: number | null;
  status: HistoryStatus;
  source_file: string | null;
}

export interface HistoryRun {
  job_id: string;
  job_type: HistoryJobType;
  status: HistoryStatus;
  batch_id: string | null;
  source_job_ids: string[];
  source_files: string[];
  source_sha256: string[];
  source_sheets: string[];
  created_at: string | null;
  started_at: string | null;
  ended_at: string | null;
  created_by: { user_id: string; user_name: string } | null;
  modified_by: { user_id: string; user_name: string } | null;
  outputs: HistoryOutput[];
  log?: string | null;
}

export interface HistoryPage {
  jobs: HistoryRun[];
  next_before: string | null;
}

export interface HistoryFilters {
  type?: HistoryJobType | null;
  status?: HistoryStatus | null;
  q?: string;
  user_id?: string | null;
  before?: string | null;
  limit?: number;
}
