import clsx from "clsx";
import { BetweenHorizontalEnd, Info, Lock, Table2, Wand2 } from "lucide-react";
import type { ReactNode } from "react";
import { SectionCard } from "../components/layout/Layout";
import { Badge } from "../components/ui/Badge";
import { useWorkflow } from "../state/workflow";

/**
 * Section 3: how selected sheets are combined. Nothing is cleaned here -- a sheet's real
 * header is only known once the job cleans it, so which sheets actually append is
 * decided (once) during the run and reported with the results.
 */
export function AppendSection() {
  const flow = useWorkflow();
  const disabled = !flow.appendAvailable || flow.running;

  return (
    <SectionCard
      id="section-append"
      step={3}
      icon={<BetweenHorizontalEnd />}
      title="Append matching sheets"
      description="Choose whether selected sheets with identical cleaned columns are combined into one table."
    >
      {!flow.appendAvailable && (
        <div className="mb-4 flex items-start gap-3 rounded-lg border border-ink-200 bg-ink-50 px-4 py-3" role="note">
          <Lock className="mt-0.5 h-4 w-4 shrink-0 text-ink-500" aria-hidden />
          <div>
            <p className="text-body font-medium text-ink-800">Appending is not available</p>
            <p className="text-caption text-ink-600">{flow.appendUnavailableReason} Each sheet will be exported as its own table.</p>
          </div>
        </div>
      )}

      <div role="radiogroup" aria-label="How to combine sheets" className={clsx("grid gap-3 md:grid-cols-2", disabled && "opacity-60")}>
        <ModeCard
          checked={flow.append}
          disabled={disabled}
          onSelect={() => flow.setAppend(true)}
          icon={<Wand2 />}
          title="Auto-detect & append"
          badge={<Badge tone="brand">Recommended</Badge>}
          description="While cleaning, each sheet's real header is detected. Sheets whose columns match exactly are appended into one table with a source_sheet column; the rest stay separate."
        />
        <ModeCard
          checked={!flow.append}
          disabled={disabled}
          onSelect={() => flow.setAppend(false)}
          icon={<Table2 />}
          title="Keep sheets separate"
          description="Every selected sheet becomes its own table, even when two sheets share the same columns."
        />
      </div>

      {flow.append && flow.appendAvailable && (
        <p className="mt-4 flex animate-fade-in items-start gap-2 text-caption text-ink-600">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-400" aria-hidden />
          Matching is decided while the job cleans the sheets, in a single pass. If some sheets turn out to have different columns,
          you'll see exactly which ones, and why, when the run finishes. Nothing is lost: they are exported as separate tables.
        </p>
      )}
    </SectionCard>
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

