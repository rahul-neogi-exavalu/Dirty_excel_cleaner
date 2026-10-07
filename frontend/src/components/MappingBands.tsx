import clsx from "clsx";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { formatNumber } from "../lib/format";
import { useWidth } from "../lib/useWidth";
import { Pagination, useFitPageSize, usePaged } from "./ui/Pagination";

/** The row-label column ("Silver column" / "Bronze column"). */
const LABEL_WIDTH = 88;
/** A cell's horizontal padding and border, around its content. */
const CELL_CHROME = 2 * 14 + 1;
/** Room left over a measured width, for rounding and font hinting. */
const SAFETY = 10;
/** How far a column of the last, part-filled band stretches past its content. */
const MAX_STRETCH = 96;
/** The fewest bands a page shows, however short the screen: more where they fit. */
const MIN_BANDS = 3;

/** The text styles a cell uses, so its content can be measured before it is drawn. */
export type TextKind = "name" | "value" | "caption" | "chip";
/** A string's width in pixels, set as ``kind`` sets it. */
export type Measure = (text: string, kind: TextKind) => number;

// As the cells set them: font-mono 12.5px semibold; a select's value (text-body, medium);
// text-caption; the data-type chip (font-mono 10.5px).
const FONT: Record<TextKind, (sans: string, mono: string) => string> = {
  name: (_, mono) => `600 12.5px ${mono}`,
  value: (sans) => `500 14px ${sans}`,
  caption: (sans) => `400 12px ${sans}`,
  chip: (_, mono) => `400 10.5px ${mono}`,
};

/** Measures text in the page's own fonts; measures again once the web fonts have loaded. */
function useMeasure(): Measure {
  const [fontsReady, setFontsReady] = useState(0);
  useEffect(() => {
    let live = true;
    document.fonts?.ready.then(() => live && setFontsReady((count) => count + 1));
    return () => {
      live = false;
    };
  }, []);
  return useMemo(() => {
    const context = document.createElement("canvas").getContext("2d");
    const sans = getComputedStyle(document.body).fontFamily;
    const probe = document.createElement("span");
    probe.className = "font-mono";
    document.body.appendChild(probe);
    const mono = getComputedStyle(probe).fontFamily;
    probe.remove();
    const cache = new Map<string, number>();
    return (text: string, kind: TextKind) => {
      const key = `${kind}\u0000${text}`;
      let width = cache.get(key);
      if (width === undefined) {
        if (!context) return text.length * 8;
        context.font = FONT[kind](sans, mono);
        width = context.measureText(text).width;
        cache.set(key, width);
      }
      return width;
    };
    // fontsReady: a new measurer once the real fonts can be measured.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fontsReady]);
}

interface Band<T> {
  items: T[];
  /** Each column's width in px, in order; ``filler`` is what is left at the end. */
  widths: number[];
  filler: number;
  first: number;
}

/**
 * Columns auto-fitted like a spreadsheet's: as many go in a band as fit at the width
 * their content needs, so every name shows whole on one line. A full band shares its
 * spare width out evenly; the last one stretches a little and leaves the rest empty.
 */
function pack<T>(items: T[], needs: number[], available: number): Band<T>[] {
  const bands: Band<T>[] = [];
  let start = 0;
  while (start < items.length) {
    let end = start;
    let sum = 0;
    // At least one column, even one wider than the band.
    while (end < items.length && (end === start || sum + needs[end] <= available)) sum += needs[end++];
    const full = end < items.length;
    const count = end - start;
    const spare = Math.max(0, available - sum);
    const stretch = full ? spare / count : Math.min(spare / count, MAX_STRETCH);
    bands.push({
      items: items.slice(start, end),
      widths: needs.slice(start, end).map((need) => need + stretch),
      filler: full ? 0 : spare - stretch * count,
      first: start,
    });
    start = end;
  }
  return bands;
}

interface BandProps<T> {
  items: T[];
  keyOf: (item: T) => string;
  top: (item: T) => ReactNode;
  bottom: (item: T) => ReactNode;
  /** The width the item's content needs (the wider of its two cells), without padding. */
  width: (item: T, measure: Measure) => number;
  /** Columns that still need a decision are tinted. */
  attention?: (item: T) => boolean;
  caption: string;
  topLabel?: string;
  bottomLabel?: string;
  noun?: string;
  /** Back to the first page when this changes (a tab, a filter, a search). */
  reset?: unknown;
}

/**
 * Mappings the way the business lays them out in a sheet: bands of columns, each with the
 * top row's value above the bottom row's (the Silver column above the bronze column that
 * fills it). Columns are auto-fitted to their content, a band holds as many as fit, and
 * a page holds at least three bands (more where the screen allows): nothing wraps and
 * nothing is cut short, and every band is the same height.
 */
export function PagedBands<T>({ items, width, noun = "column", reset, ...props }: BandProps<T>) {
  const [ref, containerWidth] = useWidth();
  const measure = useMeasure();
  const available = Math.max(0, containerWidth - LABEL_WIDTH);
  const bands = useMemo(
    () => (available ? pack(items, items.map((item) => Math.ceil(width(item, measure)) + CELL_CHROME + SAFETY), available) : []),
    // width is a fresh function each render; what it reads arrives through items.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [items, available, measure],
  );
  const list = useRef<HTMLDivElement>(null);
  const perPage = useFitPageSize(list, { min: MIN_BANDS, max: 8, fallbackRow: 132, reserve: 96 });
  const paged = usePaged(bands, perPage, reset);
  const shown = paged.slice;
  const from = shown.length ? shown[0].first + 1 : 0;
  const last = shown[shown.length - 1];
  const to = last ? last.first + last.items.length : 0;
  return (
    <div ref={ref}>
      <div ref={list} className="space-y-5">
        {shown.map((band) => (
          <BandTable key={props.keyOf(band.items[0])} band={band} total={items.length} {...props} />
        ))}
      </div>
      {paged.pages > 1 ? (
        <Pagination page={paged.page} pages={paged.pages} total={items.length} from={from} to={to}
          onPage={paged.setPage} noun={noun} className="mt-3 rounded-lg border border-ink-200" />
      ) : items.length > 0 && (
        <p className="mt-2 text-right text-caption text-ink-400">{formatNumber(items.length)} {noun}{items.length === 1 ? "" : "s"}</p>
      )}
    </div>
  );
}

function BandTable<T>({
  band,
  total,
  keyOf,
  top,
  bottom,
  attention,
  caption,
  topLabel = "Silver column",
  bottomLabel = "Bronze column",
}: Omit<BandProps<T>, "items" | "width" | "noun" | "reset"> & { band: Band<T>; total: number }) {
  return (
    <div data-row className="overflow-hidden rounded-xl border border-ink-200 bg-white shadow-[0_1px_2px_rgba(15,23,42,0.05)]">
      <table className="w-full table-fixed border-collapse whitespace-nowrap text-table">
        <caption className="sr-only">
          {caption}: {band.first + 1} to {band.first + band.items.length} of {total}
        </caption>
        <colgroup>
          <col style={{ width: LABEL_WIDTH }} />
          {band.widths.map((width, index) => <col key={index} style={{ width }} />)}
          {band.filler > 0 && <col style={{ width: band.filler }} />}
        </colgroup>
        <tbody>
          <tr className="border-b border-ink-200">
            <RowLabel>{topLabel}</RowLabel>
            {band.items.map((item) => (
              <td key={keyOf(item)} className={clsx("overflow-hidden border-l border-ink-200/70 px-3.5 pb-2 pt-2.5 align-top", attention?.(item) ? "bg-amber-50/80" : "bg-ink-50/70")}>
                {top(item)}
              </td>
            ))}
            {band.filler > 0 && <td className="border-l border-ink-200/70 bg-ink-50/70" aria-hidden />}
          </tr>
          <tr>
            <RowLabel>{bottomLabel}</RowLabel>
            {band.items.map((item) => (
              <td key={keyOf(item)} className={clsx("overflow-hidden border-l border-ink-200/70 px-3.5 pb-2.5 pt-2.5 align-top", attention?.(item) && "bg-amber-50/30")}>
                {bottom(item)}
              </td>
            ))}
            {band.filler > 0 && <td className="border-l border-ink-200/70" aria-hidden />}
          </tr>
        </tbody>
      </table>
    </div>
  );
}

function RowLabel({ children }: { children: ReactNode }) {
  return (
    <th scope="row" className="whitespace-normal bg-ink-100/60 px-3 py-3 text-left align-middle text-caption font-semibold leading-4 text-ink-600">
      {children}
    </th>
  );
}
