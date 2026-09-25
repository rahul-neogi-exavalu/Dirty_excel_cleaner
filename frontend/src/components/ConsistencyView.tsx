import clsx from "clsx";
import { ChevronDown, CircleCheck, CircleMinus, CircleX, Equal, Minus, ShieldAlert, ShieldCheck } from "lucide-react";
import { useState } from "react";
import type { ConsistencyCheck, ConsistencyReport, RowAccounting } from "../api/types";
import { formatNumber, humanize, plural } from "../lib/format";
import { Badge } from "./ui/Badge";
import { Button } from "./ui/Button";

/**
 * Whether a cleaned table is safe to load: every check with its status, and the row
 * accounting that proves no source row vanished.
 */
export function ConsistencyView({ report }: { report: ConsistencyReport | undefined }) {
  if (!report) {
    return <p className="text-body text-ink-600">No consistency report is available for this table.</p>;
  }
  const failed = report.status === "failed";

  return (
    <div className="space-y-6">
      <div
        role={failed ? "alert" : "status"}
        className={clsx(
          "flex items-start gap-3 rounded-lg border px-4 py-3",
          failed ? "border-brand-200 bg-brand-50" : "border-emerald-200 bg-emerald-50",
        )}
      >
        {failed ? <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-brand-600" aria-hidden /> : <ShieldCheck className="mt-0.5 h-5 w-5 shrink-0 text-emerald-600" aria-hidden />}
        <div>
          <p className={clsx("text-body font-semibold", failed ? "text-brand-800" : "text-emerald-800")}>
            {failed ? `${plural(report.issues, "consistency issue")} — review before loading this table` : "All consistency checks passed — safe to load"}
          </p>
          <p className="text-caption text-ink-700">
            {failed
              ? "The file was still produced so you can inspect the evidence, but loading it as-is may carry missing or misplaced data."
              : "Every source row is accounted for and the cleaned table reconciles with the source."}
          </p>
        </div>
      </div>

      <section aria-labelledby="checks-title">
        <h3 id="checks-title" className="mb-2 text-card text-ink-900">Checks</h3>
        <ul className="divide-y divide-ink-100 rounded-lg border border-ink-200">
          {report.checks.map((check) => (
            <CheckRow key={check.id} check={check} />
          ))}
        </ul>
      </section>

      <section aria-labelledby="accounting-title">
        <h3 id="accounting-title" className="text-card text-ink-900">Row accounting</h3>
        <p className="mb-3 text-caption text-ink-600">
          Every row of the source sheet must end up somewhere: blank, header, removed with a reason, or kept. Anything else is unaccounted for.
        </p>
        <div className="space-y-4">
          {report.accounting.map((sheet) => (
            <AccountingCard key={sheet.sheet} sheet={sheet} />
          ))}
        </div>
      </section>
    </div>
  );
}

function StatusIcon({ status }: { status: ConsistencyCheck["status"] }) {
  if (status === "passed") return <CircleCheck className="h-5 w-5 shrink-0 text-emerald-600" aria-label="Passed" />;
  if (status === "failed") return <CircleX className="h-5 w-5 shrink-0 text-brand-600" aria-label="Failed" />;
  return <CircleMinus className="h-5 w-5 shrink-0 text-ink-400" aria-label="Not applicable" />;
}

function CheckRow({ check }: { check: ConsistencyCheck }) {
  return (
    <li className={clsx("flex gap-3 px-4 py-3", check.status === "failed" && "bg-brand-50/40")}>
      <StatusIcon status={check.status} />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-body font-medium text-ink-900">{check.title}</p>
          <Badge tone={check.status === "passed" ? "success" : check.status === "failed" ? "danger" : "neutral"}>
            {check.status === "passed" ? "Passed" : check.status === "failed" ? "Failed" : "Not applicable"}
          </Badge>
        </div>
        <p className="text-caption text-ink-600">{check.description}</p>
        {check.details.length > 0 && (
          <ul className="mt-1.5 space-y-0.5">
            {check.details.map((detail, index) => (
              <li key={index} className="text-caption font-medium text-brand-700">{detail}</li>
            ))}
          </ul>
        )}
      </div>
    </li>
  );
}

function AccountingCard({ sheet }: { sheet: RowAccounting }) {
  const [open, setOpen] = useState(false);
  const unit = sheet.axis;
  const failed = sheet.status === "failed";

  const terms: { label: string; value: number; tone?: string }[] = [
    { label: "Blank", value: sheet.blank },
    { label: "Header", value: sheet.header },
    { label: "Removed", value: sheet.removed },
    { label: "Kept", value: sheet.kept, tone: "text-emerald-700" },
  ];

  return (
    <div className={clsx("rounded-lg border", failed ? "border-brand-200" : "border-ink-200")}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ink-100 px-4 py-2.5">
        <p className="flex items-center gap-2 text-body font-semibold text-ink-900">
          {sheet.sheet}
          {sheet.axis === "columns" && <Badge tone="info">Turned upright — counted by column</Badge>}
        </p>
        <Badge tone={sheet.status === "passed" ? "success" : failed ? "danger" : "neutral"}>
          {sheet.status === "passed" ? "Balanced" : failed ? `${formatNumber(Math.abs(sheet.unaccounted))} unaccounted` : "Checked inside the table"}
        </Badge>
      </div>

      {/* raw = blank + header + removed + kept (+ unaccounted) */}
      <div className="num flex flex-wrap items-center gap-2 px-4 py-3 text-body" aria-label="Row accounting equation">
        <Term label={`Raw ${unit}`} value={sheet.raw} strong />
        <Equal className="h-4 w-4 text-ink-400" aria-label="equals" />
        {terms.map((term, index) => (
          <span key={term.label} className="flex items-center gap-2">
            {index > 0 && <span className="text-ink-400" aria-label="plus">+</span>}
            <Term label={term.label} value={term.value} className={term.tone} />
          </span>
        ))}
        {sheet.unaccounted !== 0 && (
          <>
            {sheet.unaccounted > 0 ? <span className="text-ink-400">+</span> : <Minus className="h-4 w-4 text-ink-400" />}
            <Term label="Unaccounted" value={Math.abs(sheet.unaccounted)} className="text-brand-700" />
          </>
        )}
      </div>

      {sheet.note && <p className="px-4 pb-3 text-caption text-ink-600">{sheet.note}</p>}

      {Object.keys(sheet.removed_by_reason).length > 0 && (
        <div className="flex flex-wrap gap-1.5 px-4 pb-3">
          {Object.entries(sheet.removed_by_reason).map(([reason, count]) => (
            <Badge key={reason} tone="neutral">
              {humanize(reason)}: {count}
            </Badge>
          ))}
        </div>
      )}

      {sheet.unaccounted_rows.length > 0 && (
        <div className="mx-4 mb-3 overflow-x-auto rounded-md border border-brand-200 scroll-thin">
          <table className="w-full min-w-[480px] text-caption">
            <caption className="bg-brand-50 px-3 py-1.5 text-left font-semibold text-brand-800">
              {unit === "rows" ? "Rows" : "Columns"} neither kept nor removed with a reason
            </caption>
            <thead className="bg-ink-50 text-left text-ink-600">
              <tr>
                <th scope="col" className="w-28 px-3 py-1.5 font-semibold">Sheet {unit === "rows" ? "row" : "column"}</th>
                <th scope="col" className="px-3 py-1.5 font-semibold">Content</th>
              </tr>
            </thead>
            <tbody>
              {sheet.unaccounted_rows.map((row) => (
                <tr key={row.sheet_row} className="border-t border-ink-100">
                  <td className="num px-3 py-1.5 text-ink-700">{row.sheet_row}</td>
                  <td className="max-w-[520px] truncate px-3 py-1.5 font-mono text-[11px] text-ink-800" title={row.content}>{row.content}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {sheet.removed_rows.length > 0 && (
        <div className="border-t border-ink-100">
          <Button
            variant="ghost"
            size="sm"
            className="m-2"
            aria-expanded={open}
            iconRight={<ChevronDown className={clsx("transition-transform", open && "rotate-180")} />}
            onClick={() => setOpen(!open)}
          >
            {open ? "Hide" : "Show"} removed {unit} ({formatNumber(sheet.removed)})
          </Button>
          {open && (
            <div className="mx-4 mb-3 overflow-x-auto rounded-md border border-ink-200 scroll-thin">
              <table className="w-full min-w-[560px] text-caption">
                <caption className="sr-only">Removed {unit} on {sheet.sheet}</caption>
                <thead className="bg-ink-50 text-left text-ink-600">
                  <tr>
                    <th scope="col" className="w-24 px-3 py-1.5 font-semibold">Sheet row</th>
                    <th scope="col" className="w-40 px-3 py-1.5 font-semibold">Removed as</th>
                    <th scope="col" className="px-3 py-1.5 font-semibold">Content</th>
                  </tr>
                </thead>
                <tbody>
                  {sheet.removed_rows.map((row, index) => (
                    <tr key={index} className="border-t border-ink-100">
                      <td className="num px-3 py-1.5 text-ink-600">{row.sheet_row ?? "—"}</td>
                      <td className="px-3 py-1.5" title={row.reason}>{humanize(row.classification)}</td>
                      <td className="max-w-[420px] truncate px-3 py-1.5 font-mono text-[11px] text-ink-700" title={row.content}>{row.content || "(blank)"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {sheet.removed > sheet.removed_rows.length && (
                <p className="border-t border-ink-100 px-3 py-1.5 text-caption text-ink-500">
                  Showing {sheet.removed_rows.length} of {formatNumber(sheet.removed)}. The audit report lists every removed row.
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Term({ label, value, strong, className }: { label: string; value: number; strong?: boolean; className?: string }) {
  return (
    <span className="inline-flex flex-col rounded-md bg-ink-50 px-2.5 py-1 ring-1 ring-inset ring-ink-200">
      <span className="text-[11px] leading-4 text-ink-500">{label}</span>
      <span className={clsx("text-body leading-5", strong ? "font-bold text-ink-900" : "font-semibold text-ink-800", className)}>{formatNumber(value)}</span>
    </span>
  );
}
