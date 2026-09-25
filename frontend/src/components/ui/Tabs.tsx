import clsx from "clsx";
import { useRef, type ReactNode } from "react";

export interface TabItem<T extends string> {
  id: T;
  label: string;
  icon?: ReactNode;
  count?: number | string;
}

/** WAI-ARIA tabs with roving focus (arrow keys, Home, End). */
export function Tabs<T extends string>({
  items,
  value,
  onChange,
  label,
  idPrefix,
}: {
  items: TabItem<T>[];
  value: T;
  onChange: (value: T) => void;
  label: string;
  idPrefix: string;
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const focus = (index: number) => {
    const next = (index + items.length) % items.length;
    refs.current[next]?.focus();
    onChange(items[next].id);
  };
  return (
    <div role="tablist" aria-label={label} className="flex gap-1 overflow-x-auto overflow-y-hidden border-b border-ink-200 scroll-thin">
      {items.map((item, index) => {
        const selected = item.id === value;
        return (
          <button
            key={item.id}
            ref={(el) => (refs.current[index] = el)}
            role="tab"
            id={`${idPrefix}-tab-${item.id}`}
            aria-selected={selected}
            aria-controls={`${idPrefix}-panel-${item.id}`}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(item.id)}
            onKeyDown={(event) => {
              if (event.key === "ArrowRight") (event.preventDefault(), focus(index + 1));
              if (event.key === "ArrowLeft") (event.preventDefault(), focus(index - 1));
              if (event.key === "Home") (event.preventDefault(), focus(0));
              if (event.key === "End") (event.preventDefault(), focus(items.length - 1));
            }}
            className={clsx(
              "relative flex shrink-0 items-center gap-2 border-b-2 px-3 py-2.5 text-body font-medium transition-colors",
              "focus-visible:outline-none focus-visible:bg-ink-100 rounded-t",
              selected ? "border-brand-600 text-brand-700" : "border-transparent text-ink-600 hover:text-ink-900 hover:border-ink-300",
            )}
          >
            {item.icon && <span className="[&>svg]:h-4 [&>svg]:w-4" aria-hidden>{item.icon}</span>}
            {item.label}
            {item.count !== undefined && (
              <span
                className={clsx(
                  "num rounded-full px-1.5 text-caption",
                  selected ? "bg-brand-50 text-brand-700" : "bg-ink-100 text-ink-600",
                )}
              >
                {item.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({
  id,
  idPrefix,
  active,
  children,
}: {
  id: string;
  idPrefix: string;
  active: boolean;
  children: ReactNode;
}) {
  if (!active) return null;
  return (
    <div
      role="tabpanel"
      id={`${idPrefix}-panel-${id}`}
      aria-labelledby={`${idPrefix}-tab-${id}`}
      tabIndex={0}
      className="animate-fade-in focus-visible:outline-none"
    >
      {children}
    </div>
  );
}
