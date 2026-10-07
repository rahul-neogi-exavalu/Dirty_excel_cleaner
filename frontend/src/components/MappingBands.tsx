import clsx from "clsx";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useWidth } from "../lib/useWidth";

/**
 * Renders its children once they come within ``margin`` of the viewport, and keeps them.
 * Until then it holds ``estimate`` pixels, so hundreds of mappings cost only what is near.
 */
export function WhenNear({ estimate, margin = 800, children }: { estimate: number; margin?: number; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [near, setNear] = useState(false);
  useEffect(() => {
    const element = ref.current;
    if (near || !element) return;
    const observer = new IntersectionObserver((entries) => entries.some((entry) => entry.isIntersecting) && setNear(true), {
      rootMargin: `${margin}px 0px`,
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [near, margin]);
  return (
    <div ref={ref} style={near ? undefined : { minHeight: estimate }} aria-busy={!near || undefined}>
      {near ? children : null}
    </div>
  );
}

/** The narrowest a mapping column gets before a band wraps to the next. */
const MIN_CELL = 190;
/** The row-label column ("Silver column" / "Bronze column"). */
const LABEL_WIDTH = 96;

/** How many mapping columns fit across the element, kept current as it resizes. */
function useColumns(min: number): [React.RefObject<HTMLDivElement>, number] {
  const [ref, width] = useWidth();
  return [ref, width ? Math.max(1, Math.floor((width - LABEL_WIDTH) / min)) : 4];
}

/**
 * Mappings the way the business lays them out in a sheet: bands of columns, each with the
 * Silver column above the bronze column it is filled from. As many columns as fit go across;
 * the rest wrap into the next band, so every mapping is on the page at once, no paging.
 */
export function MappingBands<T>({
  items,
  keyOf,
  top,
  bottom,
  attention,
  caption,
  topLabel = "Silver column",
  bottomLabel = "Bronze column",
  minCell = MIN_CELL,
}: {
  items: T[];
  keyOf: (item: T) => string;
  top: (item: T) => ReactNode;
  bottom: (item: T) => ReactNode;
  /** Columns that still need a decision are tinted. */
  attention?: (item: T) => boolean;
  caption: string;
  topLabel?: string;
  bottomLabel?: string;
  minCell?: number;
}) {
  const [ref, columns] = useColumns(minCell);
  const bands: T[][] = [];
  for (let start = 0; start < items.length; start += columns) bands.push(items.slice(start, start + columns));
  return (
    <div ref={ref} className="space-y-3">
      {bands.map((band, index) => (
        <div key={keyOf(band[0])} className="overflow-hidden rounded-lg border border-ink-200">
          <table className="w-full table-fixed border-collapse text-table">
            <caption className="sr-only">
              {caption}: columns {index * columns + 1} to {index * columns + band.length} of {items.length}
            </caption>
            <colgroup>
              <col style={{ width: LABEL_WIDTH }} />
              {Array.from({ length: columns }, (_, i) => <col key={i} />)}
            </colgroup>
            <tbody>
              <tr className="border-b border-ink-200">
                <RowLabel>{topLabel}</RowLabel>
                {band.map((item) => (
                  <td key={keyOf(item)} className={clsx("border-l border-ink-100 px-2.5 py-2 align-top", attention?.(item) && "bg-amber-50/70")}>
                    {top(item)}
                  </td>
                ))}
                <Filler count={columns - band.length} />
              </tr>
              <tr>
                <RowLabel muted>{bottomLabel}</RowLabel>
                {band.map((item) => (
                  <td key={keyOf(item)} className={clsx("border-l border-ink-100 px-2.5 py-2 align-top", attention?.(item) ? "bg-amber-50/40" : "bg-ink-50/50")}>
                    {bottom(item)}
                  </td>
                ))}
                <Filler count={columns - band.length} muted />
              </tr>
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}

function RowLabel({ children, muted }: { children: ReactNode; muted?: boolean }) {
  return (
    <th scope="row" className={clsx("px-3 py-2 text-left align-middle text-caption font-semibold leading-4 text-ink-600", muted ? "bg-ink-100/70" : "bg-ink-50")}>
      {children}
    </th>
  );
}

/** The empty end of the last band, so its columns line up with the bands above. */
function Filler({ count, muted }: { count: number; muted?: boolean }) {
  if (count <= 0) return null;
  return <td colSpan={count} className={clsx("border-l border-ink-100", muted && "bg-ink-50/50")} aria-hidden />;
}
