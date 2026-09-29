import clsx from "clsx";
import { ArrowRight, CheckCircle2, BetweenHorizontalEnd, Rows3, Table2, TriangleAlert } from "lucide-react";
import type { AppendCheck, AppendGroup } from "../api/types";
import { formatNumber, plural } from "../lib/format";
import { Badge } from "./ui/Badge";
import { Button } from "./ui/Button";
import { Alert } from "./ui/Feedback";
import { Modal } from "./ui/Overlay";

/**
 * How "Auto-detect & append" turned out. Known only after the run: sheets are cleaned
 * once, by the job, and their real headers compared then.
 */

export function appendIncomplete(check: AppendCheck | null | undefined): boolean {
  return Boolean(check && (check.status === "partial" || check.status === "none_match"));
}

export function AppendOutcomeAlert({ check, onDetails }: { check: AppendCheck; onDetails?: () => void }) {
  const appended = check.groups.filter((group) => group.appended);
  const appendedTables = appended.reduce((total, group) => total + group.tables.length, 0);
  const rest = check.tables - appendedTables;
  const details = onDetails ? <Button size="sm" onClick={onDetails}>View details</Button> : undefined;

  switch (check.status) {
    case "all_match":
      return (
        <Alert tone="success" title={`All ${check.tables} tables had identical columns and were appended`}>
          One table of {formatNumber(appended[0]?.rows ?? 0)} rows and {appended[0]?.columns.length ?? 0} columns, with a source_sheet column.
        </Alert>
      );
    case "partial":
      return (
        <Alert tone="warning" title="Some sheets couldn't be appended" action={details}>
          {plural(appendedTables, "table")} {appendedTables === 1 ? "was" : "were"} appended.{" "}
          {rest === 1
            ? "The other table has different columns, so it was exported separately."
            : `The other ${rest} tables have different columns, so they were exported separately.`}
        </Alert>
      );
    case "none_match":
      return (
        <Alert tone="warning" title="Append failed: schemas didn't match" action={details}>
          None of the selected sheets had the same columns after cleaning, so nothing was appended. Each was exported as its own table.
        </Alert>
      );
    case "single_table":
      return <Alert tone="info" title="Nothing to append">Only one of the selected sheets held a table.</Alert>;
    default:
      return null;
  }
}

export function AppendPlanList({ check }: { check: AppendCheck }) {
  return (
    <ul className="divide-y divide-ink-100 rounded-lg border border-ink-200">
      {check.groups.map((group) => (
        <GroupRow key={group.tables.join("|")} group={group} />
      ))}
      {check.headerless.map((table) => (
        <li key={table.label} className="flex flex-wrap items-center gap-2 px-4 py-2.5 text-body">
          <Table2 className="h-4 w-4 text-ink-400" aria-hidden />
          <span className="font-medium text-ink-900">{table.label}</span>
          <Badge tone="warning">No header row</Badge>
          <span className="text-caption text-ink-500">Columns were named by position, so it couldn't be matched.</span>
        </li>
      ))}
    </ul>
  );
}

function GroupRow({ group }: { group: AppendGroup }) {
  return (
    <li className="flex flex-col gap-1.5 px-4 py-2.5 sm:flex-row sm:items-center sm:gap-3">
      <span className="flex min-w-0 flex-1 items-center gap-2 text-body">
        {group.appended ? <BetweenHorizontalEnd className="h-4 w-4 shrink-0 text-brand-600" aria-hidden /> : <Table2 className="h-4 w-4 shrink-0 text-ink-400" aria-hidden />}
        <span className="truncate font-medium text-ink-900" title={group.tables.join(" + ")}>
          {group.tables.join(" + ")}
        </span>
        {group.appended && <ArrowRight className="h-3.5 w-3.5 shrink-0 text-ink-400" aria-label="became" />}
        {group.appended && <span className="shrink-0 text-caption text-ink-600">1 table</span>}
      </span>
      <span className="num flex shrink-0 items-center gap-3 text-caption text-ink-500">
        <span className="inline-flex items-center gap-1">
          <Rows3 className="h-3.5 w-3.5" aria-hidden /> {formatNumber(group.rows)} rows
        </span>
        <span>{group.columns.length} columns</span>
        {group.appended ? (
          <Badge tone="success" icon={<CheckCircle2 />}>Appended</Badge>
        ) : (
          <Badge tone={group.is_reference ? "neutral" : "warning"} icon={group.is_reference ? undefined : <TriangleAlert />}>
            {group.is_reference ? "Separate" : "Different columns"}
          </Badge>
        )}
      </span>
    </li>
  );
}

export function AppendMismatchModal({
  open,
  check,
  onClose,
  onChangeConfiguration,
}: {
  open: boolean;
  check: AppendCheck;
  onClose: () => void;
  onChangeConfiguration: () => void;
}) {
  const reference = check.groups.find((group) => group.is_reference);
  const others = check.groups.filter((group) => !group.is_reference);
  const none = check.status === "none_match";

  return (
    <Modal
      open={open}
      onClose={onClose}
      size="lg"
      title={none ? "Append failed: schemas didn't match" : "Some sheets couldn't be appended"}
      description={
        none
          ? "After cleaning, the selected sheets had different columns, so none of them could be appended. Nothing was lost: each was exported as its own table."
          : "Only sheets with exactly the same cleaned columns are appended. The sheets below differed and were exported as separate tables."
      }
      footer={
        <>
          <Button variant="ghost" onClick={onChangeConfiguration}>Change configuration</Button>
          <Button variant="primary" onClick={onClose}>OK, review results</Button>
        </>
      }
    >
      <div className="space-y-4">
        {reference && (
          <div className="rounded-lg border border-ink-200 p-3">
            <p className="text-caption font-semibold text-ink-700">
              Compared against {reference.tables.join(" + ")} · {reference.columns.length} columns
            </p>
            <ColumnChips names={reference.columns} tone="neutral" />
          </div>
        )}
        {others.map((group) => (
          <div key={group.tables.join("|")} className="rounded-lg border border-amber-200 bg-amber-50/40 p-3">
            <p className="flex flex-wrap items-center gap-2 text-body font-semibold text-ink-900">
              <TriangleAlert className="h-4 w-4 text-amber-600" aria-hidden />
              {group.tables.join(" + ")}
              <span className="text-caption font-normal text-ink-500">{group.columns.length} columns</span>
            </p>
            {group.missing_columns.length > 0 && (
              <div className="mt-2">
                <p className="text-caption text-ink-600">Missing {plural(group.missing_columns.length, "column")}:</p>
                <ColumnChips names={group.missing_columns} tone="danger" />
              </div>
            )}
            {group.extra_columns.length > 0 && (
              <div className="mt-2">
                <p className="text-caption text-ink-600">Has {plural(group.extra_columns.length, "extra column")}:</p>
                <ColumnChips names={group.extra_columns} tone="info" />
              </div>
            )}
          </div>
        ))}
        {check.headerless.length > 0 && (
          <Alert tone="info" title="Tables without a header row">
            {check.headerless.map((table) => table.label).join(", ")} had no header, so they couldn't be matched by column name.
          </Alert>
        )}
      </div>
    </Modal>
  );
}

function ColumnChips({ names, tone }: { names: string[]; tone: "neutral" | "danger" | "info" }) {
  const shown = names.slice(0, 24);
  return (
    <div className="mt-1.5 flex flex-wrap gap-1">
      {shown.map((name) => (
        <code
          key={name}
          className={clsx(
            "rounded px-1.5 py-0.5 font-mono text-[11px] ring-1 ring-inset",
            tone === "neutral" && "bg-ink-50 text-ink-700 ring-ink-200",
            tone === "danger" && "bg-brand-50 text-brand-700 ring-brand-200",
            tone === "info" && "bg-sky-50 text-sky-700 ring-sky-200",
          )}
        >
          {name}
        </code>
      ))}
      {names.length > shown.length && <span className="text-caption text-ink-500">+{names.length - shown.length} more</span>}
    </div>
  );
}
