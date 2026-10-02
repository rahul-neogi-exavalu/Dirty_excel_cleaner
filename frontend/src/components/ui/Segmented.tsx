import clsx from "clsx";
import { Check } from "lucide-react";
import type { ReactNode } from "react";

export interface SegmentItem<T extends string> {
  id: T;
  label: string;
  icon?: ReactNode;
  /** Shown as a small mono number before the label: the order to work in. */
  step?: number;
  done?: boolean;
  attention?: boolean;
}

/**
 * One of a few panels on screen at a time, in place of stacking them: the page stays
 * short. Keyboard: Tab to the group, arrow keys between segments.
 */
export function Segmented<T extends string>({
  items,
  value,
  onChange,
  label,
  className,
}: {
  items: SegmentItem<T>[];
  value: T;
  onChange: (value: T) => void;
  label: string;
  className?: string;
}) {
  const move = (from: number, delta: number) => {
    const next = items[(from + delta + items.length) % items.length];
    onChange(next.id);
    requestAnimationFrame(() => document.getElementById(`segment-${next.id}`)?.focus());
  };
  return (
    <div role="tablist" aria-label={label} className={clsx("flex gap-1 rounded-lg border border-ink-200 bg-ink-100 p-1", className)}>
      {items.map((item, index) => {
        const selected = item.id === value;
        return (
          <button
            key={item.id}
            id={`segment-${item.id}`}
            type="button"
            role="tab"
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(item.id)}
            onKeyDown={(event) => {
              if (event.key === "ArrowRight") (event.preventDefault(), move(index, 1));
              if (event.key === "ArrowLeft") (event.preventDefault(), move(index, -1));
            }}
            className={clsx(
              "flex min-w-0 flex-1 items-center justify-center gap-2 rounded-md px-3 py-2 text-body font-medium transition-colors",
              "focus-visible:outline-none focus-visible:shadow-focus",
              selected ? "bg-white text-ink-900 shadow-card ring-1 ring-ink-200" : "text-ink-600 hover:bg-white/60 hover:text-ink-900",
            )}
          >
            {item.icon && (
              <span className={clsx("[&>svg]:h-4 [&>svg]:w-4", selected ? "text-brand-600" : "text-ink-400")} aria-hidden>
                {item.icon}
              </span>
            )}
            {item.step !== undefined && <span className="num font-mono text-caption text-ink-400">{String(item.step).padStart(2, "0")}</span>}
            <span className="truncate">{item.label}</span>
            {item.attention ? (
              <span className="h-2 w-2 shrink-0 rounded-full bg-amber-500" aria-label="needs attention" />
            ) : item.done ? (
              <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-ink-200 text-ink-700" aria-label="complete">
                <Check className="h-3 w-3" strokeWidth={3} />
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
