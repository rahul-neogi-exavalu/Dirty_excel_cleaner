import clsx from "clsx";
import { useRef, type ReactNode } from "react";
import { useWidth } from "../lib/useWidth";
import { Pagination, useFitPageSize, usePaged } from "./ui/Pagination";

/** The narrowest a mapping column gets before a band wraps to the next. */
const MIN_CELL = 175;
/** The row-label column ("Silver column" / "Bronze column"). */
const LABEL_WIDTH = 96;
/** The fewest bands a page shows, however short the screen: more where they fit. */
const MIN_BANDS = 3;

/** How many mapping columns fit across a width. */
const columnsFor = (width: number, min: number) => (width ? Math.max(1, Math.floor((width - LABEL_WIDTH) / min)) : 4);

interface BandProps<T> {
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
}

/**
 * Mappings the way the business lays them out in a sheet: bands of columns, each with the
 * top row's value above the bottom row's (the Silver column above the bronze column that
 * fills it). As many columns as fit go across; the rest wrap into the next band.
 */
export function MappingBands<T>({ columns: fixed, ...props }: BandProps<T> & { columns?: number }) {
  const [ref, width] = useWidth();
  const columns = fixed ?? columnsFor(width, props.minCell ?? MIN_CELL);
  return (
    <div ref={ref}>
      <Bands {...props} columns={columns} first={0} total={props.items.length} />
    </div>
  );
}

/**
 * The bands a page at a time: at least three, and as many more as fit below them on this
 * screen (re-measured as it resizes); the rest are a page away.
 */
export function PagedBands<T>({ noun = "column", reset, ...props }: BandProps<T> & { noun?: string; reset?: unknown }) {
  const [ref, width] = useWidth();
  const columns = columnsFor(width, props.minCell ?? MIN_CELL);
  const list = useRef<HTMLDivElement>(null);
  const bands = useFitPageSize(list, { min: MIN_BANDS, max: 8, fallbackRow: 132, reserve: 96 });
  const paged = usePaged(props.items, columns * bands, reset);
  return (
    <div ref={ref}>
      <div ref={list}>
        <Bands {...props} items={paged.slice} columns={columns} first={paged.from ? paged.from - 1 : 0} total={props.items.length} />
      </div>
      {paged.pages > 1 && <Pagination {...paged} onPage={paged.setPage} noun={noun} className="mt-2 rounded-lg border border-ink-200" />}
    </div>
  );
}

function Bands<T>({
  items,
  keyOf,
  top,
  bottom,
  attention,
  caption,
  topLabel = "Silver column",
  bottomLabel = "Bronze column",
  columns,
  first,
  total,
}: BandProps<T> & { columns: number; first: number; total: number }) {
  const bands: T[][] = [];
  for (let start = 0; start < items.length; start += columns) bands.push(items.slice(start, start + columns));
  return (
    <div className="space-y-3">
      {bands.map((band, index) => (
        <div key={keyOf(band[0])} data-row className="overflow-hidden rounded-lg border border-ink-200">
          <table className="w-full table-fixed border-collapse text-table">
            <caption className="sr-only">
              {caption}: {first + index * columns + 1} to {first + index * columns + band.length} of {total}
            </caption>
            <colgroup>
              <col style={{ width: LABEL_WIDTH }} />
              {Array.from({ length: columns }, (_, i) => <col key={i} />)}
            </colgroup>
            <tbody>
              <tr className="border-b border-ink-200">
                <RowLabel>{topLabel}</RowLabel>
                {band.map((item) => (
                  <td key={keyOf(item)} className={clsx("border-l border-ink-100 px-2.5 py-1.5 align-top", attention?.(item) && "bg-amber-50/70")}>
                    {top(item)}
                  </td>
                ))}
                <Filler count={columns - band.length} />
              </tr>
              <tr>
                <RowLabel muted>{bottomLabel}</RowLabel>
                {band.map((item) => (
                  <td key={keyOf(item)} className={clsx("border-l border-ink-100 px-2.5 py-1.5 align-top", attention?.(item) ? "bg-amber-50/40" : "bg-ink-50/50")}>
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
