import clsx from "clsx";
import { BetweenHorizontalEnd, Info, Lock, Table2, Wand2 } from "lucide-react";
import type { ReactNode } from "react";
import { Badge } from "../components/ui/Badge";
import { appendUnavailableReason, useWorkflow, type FileEntry } from "../state/workflow";

/**
 * How one file's selected sheets are combined. Nothing is cleaned here -- a sheet's real
 * header is only known once the job cleans it, so which sheets actually append is
 * decided (once) during the run and reported with the results. Files never append
 * into each other: each is cleaned on its own.
 */
export function AppendModeOptions({ entry }: { entry: FileEntry }) {
  const flow = useWorkflow();
  const reason = appendUnavailableReason(entry);
  const disabled = Boolean(reason) || flow.running;
  const id = entry.workbook.id;

  return (
    <div id="section-append" className="scroll-mt-40">
      <div className="mb-3 flex items-start gap-2.5">
        <span className="mt-0.5 text-ink-500 [&>svg]:h-4 [&>svg]:w-4" aria-hidden>
          <BetweenHorizontalEnd />
        </span>
        <div className="min-w-0">
          <h3 className="text-card text-ink-900">Append matching sheets</h3>
          <p className="text-caption text-ink-600">
            For <span className="font-medium text-ink-800">{entry.workbook.filename}</span>: choose whether its selected sheets with identical cleaned
            columns are combined into one table. Sheets from different files are never combined.
          </p>
        </div>
      </div>

      {reason && (
        <div className="mb-3 flex items-start gap-3 rounded-lg border border-ink-200 bg-ink-50 px-4 py-3" role="note">
          <Lock className="mt-0.5 h-4 w-4 shrink-0 text-ink-500" aria-hidden />
          <div>
            <p className="text-body font-medium text-ink-800">Appending is not available for this file</p>
            <p className="text-caption text-ink-600">{reason} Each sheet will be exported as its own table.</p>
          </div>
        </div>
      )}

      <div
        role="radiogroup"
        aria-label={`How to combine sheets in ${entry.workbook.filename}`}
        className={clsx("grid gap-3 md:grid-cols-2", disabled && "opacity-60")}
      >
        <ModeCard
          checked={entry.append}
          disabled={disabled}
          onSelect={() => flow.setAppend(id, true)}
          icon={<Wand2 />}
          title="Auto-detect & append"
          badge={<Badge tone="brand">Recommended</Badge>}
          description="While cleaning, each sheet's real header is detected. Sheets whose columns match exactly are appended into one table with a source_sheet column; the rest stay separate."
        />
        <ModeCard
          checked={!entry.append}
          disabled={disabled}
          onSelect={() => flow.setAppend(id, false)}
          icon={<Table2 />}
          title="Keep sheets separate"
          description="Every selected sheet becomes its own table, even when two sheets share the same columns."
        />
      </div>

      {entry.append && !reason && (
        <p className="mt-3 flex animate-fade-in items-start gap-2 text-caption text-ink-600">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-400" aria-hidden />
          Matching is decided while the job cleans the sheets, in a single pass. If some sheets turn out to have different columns,
          you'll see exactly which ones, and why, when the run finishes. Nothing is lost: they are exported as separate tables.
        </p>
      )}
    </div>
  );
}

function ModeCard({
  checked,
  disabled,
  onSelect,
  icon,
  title,
  badge,
  description,
}: {
  checked: boolean;
  disabled: boolean;
  onSelect: () => void;
  icon: ReactNode;
  title: string;
  badge?: ReactNode;
  description: string;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={checked}
      disabled={disabled}
      onClick={onSelect}
      className={clsx(
        "flex items-start gap-3 rounded-lg border p-4 text-left transition-colors",
        "focus-visible:outline-none focus-visible:shadow-focus disabled:cursor-not-allowed",
        checked ? "border-brand-500 bg-brand-50/50 ring-1 ring-brand-500" : "border-ink-200 bg-white hover:border-ink-300 hover:bg-ink-50",
      )}
    >
      <span
        aria-hidden
        className={clsx(
          "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border-2 transition-colors",
          checked ? "border-brand-600" : "border-ink-300",
        )}
      >
        <span className={clsx("h-2 w-2 rounded-full bg-brand-600 transition-transform", checked ? "scale-100" : "scale-0")} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-2">
          <span className={clsx("[&>svg]:h-4 [&>svg]:w-4", checked ? "text-brand-600" : "text-ink-500")} aria-hidden>
            {icon}
          </span>
          <span className="text-body font-semibold text-ink-900">{title}</span>
          {badge}
        </span>
        <span className="mt-1 block text-caption text-ink-600">{description}</span>
      </span>
    </button>
  );
}
