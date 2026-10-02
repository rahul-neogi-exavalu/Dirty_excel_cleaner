import clsx from "clsx";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

/** One cell of the sheet. The card snaps to whole cells, so it reads as a selected range. */
const COL_W = 104;
const ROW_H = 32;
/** Column letters along the top, row numbers down the side. */
const HEAD_H = 28;
const HEAD_W = 56;
/** Below this width the card spans the screen (16px gutters) instead of snapping to columns. */
const NARROW = 640;
const CARD_COLS = 4;

/** What a messy export leaves behind. Scattered faintly outside the selection. */
const DEBRIS = ["#REF!", "n/a", "TOTAL", "Q3 RPT", "1,204.50", "#DIV/0!", "#N/A", "Sheet1", "Grand Total", "0", "Unnamed: 3", "Subtotal"];

export function columnName(index: number): string {
  let name = "";
  for (let n = index + 1; n > 0; n = Math.floor((n - 1) / 26)) name = String.fromCharCode(65 + ((n - 1) % 26)) + name;
  return name;
}

/** Stable pseudo-random per cell, so debris never moves between renders. */
function hash(row: number, col: number): number {
  let h = (row * 374761393 + col * 668265263) ^ 0x5bd1e995;
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
}

interface Range {
  c0: number;
  c1: number;
  r0: number;
  r1: number;
}

interface Layout {
  cols: number;
  rows: number;
  /** The viewport, or taller when the card needs it (short screens scroll). */
  pageHeight: number;
  narrow: boolean;
  /** The card's frame in page pixels. */
  left: number;
  top: number;
  width: number;
  height: number;
  range: Range;
}

function cellOf(x: number, y: number) {
  return { col: Math.max(0, Math.floor((x - HEAD_W) / COL_W)), row: Math.max(0, Math.floor((y - HEAD_H) / ROW_H)) };
}

/**
 * A full-page spreadsheet whose selected range is the content. The headers of the
 * covered columns and rows are highlighted as in a spreadsheet, the Name Box shows the
 * range, and focusing a field inside moves the active cell to that field's row.
 */
export function SheetBackdrop({ children }: { children: ReactNode }) {
  const inner = useRef<HTMLDivElement>(null);
  const frame = useRef<HTMLDivElement>(null);
  const [contentHeight, setContentHeight] = useState(0);
  const [viewport, setViewport] = useState(() => ({ w: window.innerWidth, h: window.innerHeight }));
  const [active, setActive] = useState<{ col: number; row: number } | null>(null);

  useEffect(() => {
    const onResize = () => setViewport({ w: window.innerWidth, h: window.innerHeight });
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  useLayoutEffect(() => {
    const node = inner.current;
    if (!node) return;
    const observer = new ResizeObserver(() => setContentHeight(node.offsetHeight));
    observer.observe(node);
    setContentHeight(node.offsetHeight);
    return () => observer.disconnect();
  }, []);

  const layout = computeLayout(viewport.w, viewport.h, contentHeight);

  // Measured by offsets inside the frame, not the screen: the frame may still be
  // scaling in, and the layout may move it once the content has been measured.
  const locate = useCallback(
    (target: EventTarget | null) => {
      if (!(target instanceof HTMLElement) || !frame.current?.contains(target)) return setActive(null);
      let x = 0;
      let y = 0;
      for (let node: HTMLElement | null = target; node && node !== frame.current; node = node.offsetParent as HTMLElement | null) {
        x += node.offsetLeft;
        y += node.offsetTop;
      }
      const page = cellOf(layout.left + x + 12, layout.top + y + target.offsetHeight / 2);
      setActive({ col: Math.min(Math.max(page.col, layout.range.c0), layout.range.c1), row: page.row });
    },
    [layout.left, layout.top, layout.range.c0, layout.range.c1],
  );

  // The active cell follows keyboard focus inside the card, and follows the card when it moves.
  useEffect(() => {
    const onFocus = (event: FocusEvent) => locate(event.target);
    document.addEventListener("focusin", onFocus);
    locate(document.activeElement);
    return () => document.removeEventListener("focusin", onFocus);
  }, [locate]);

  const { range } = layout;
  const nameBox = active ? `${columnName(active.col)}${active.row + 1}` : `${columnName(range.c0)}${range.r0 + 1}:${columnName(range.c1)}${range.r1 + 1}`;
  return (
    <div className="relative min-h-[100dvh] overflow-clip bg-[#fafbfc]" style={{ height: layout.pageHeight }}>
      {/* Grid lines: one hairline per column and row, starting under the headers. */}
      <div
        aria-hidden
        className="absolute inset-0"
        style={{
          backgroundImage:
            "linear-gradient(to right, rgba(20,23,28,0.06) 1px, transparent 1px), linear-gradient(to bottom, rgba(20,23,28,0.06) 1px, transparent 1px)",
          backgroundSize: `${COL_W}px ${ROW_H}px`,
          backgroundPosition: `${HEAD_W - 1}px ${HEAD_H - 1}px`,
        }}
      />
      <Debris layout={layout} />

      {/* Column letters */}
      <div aria-hidden className="absolute inset-x-0 top-0 flex border-b border-ink-200 bg-ink-50/95" style={{ height: HEAD_H, paddingLeft: HEAD_W }}>
        {Array.from({ length: layout.cols }, (_, col) => {
          const covered = col >= range.c0 && col <= range.c1;
          const current = active?.col === col;
          return (
            <span
              key={col}
              className={clsx(
                "relative flex shrink-0 items-center justify-center border-r border-ink-200 font-mono text-[11px] transition-colors duration-200",
                current ? "bg-brand-50 font-semibold text-brand-700" : covered ? "bg-ink-200/60 text-ink-800" : "text-ink-400",
              )}
              style={{ width: COL_W }}
            >
              {columnName(col)}
              {current && <span className="absolute inset-x-0 bottom-0 h-0.5 bg-brand-600" />}
            </span>
          );
        })}
      </div>

      {/* Row numbers */}
      <div aria-hidden className="absolute bottom-0 left-0 overflow-clip border-r border-ink-200 bg-ink-50/95" style={{ top: HEAD_H, width: HEAD_W }}>
        {Array.from({ length: layout.rows }, (_, row) => {
          const covered = row >= range.r0 && row <= range.r1;
          const current = active?.row === row;
          return (
            <span
              key={row}
              className={clsx(
                "num relative flex items-center justify-center border-b border-ink-200 font-mono text-[11px] transition-colors duration-200",
                current ? "bg-brand-50 font-semibold text-brand-700" : covered ? "bg-ink-200/60 text-ink-800" : "text-ink-400",
              )}
              style={{ height: ROW_H }}
            >
              {row + 1}
              {current && <span className="absolute inset-y-0 right-0 w-0.5 bg-brand-600" />}
            </span>
          );
        })}
      </div>

      {/* Name Box: the corner cell, as in a spreadsheet. */}
      <div
        aria-hidden
        className="absolute left-0 top-0 flex items-center justify-center border-b border-r border-ink-200 bg-white font-mono text-[10.5px] font-medium text-ink-600"
        style={{ width: HEAD_W, height: HEAD_H }}
      >
        <span className="num truncate px-1">{nameBox}</span>
      </div>

      {/* The selected range: the content, snapped to the grid. A ring, not a border, so
          the frame's size stays exactly whole cells. */}
      <div
        ref={frame}
        className="absolute animate-scale-in rounded-[3px] bg-white shadow-pop ring-1 ring-ink-300"
        style={{ left: layout.left, top: layout.top, width: layout.width, minHeight: layout.height }}
      >
        <div ref={inner}>{children}</div>
        <span aria-hidden className="absolute -bottom-[4px] -right-[4px] h-[7px] w-[7px] border border-white bg-brand-600" />
      </div>
    </div>
  );
}

function computeLayout(vw: number, vh: number, contentHeight: number): Layout {
  const narrow = vw < NARROW;
  const usableW = vw - HEAD_W;
  const cols = Math.max(1, Math.ceil(usableW / COL_W));
  const spanRows = Math.max(1, Math.ceil(contentHeight / ROW_H));
  const viewRows = Math.floor((vh - HEAD_H) / ROW_H);
  // A little above centre reads as centred; never closer than one row to the headers.
  const r0 = Math.max(1, Math.floor((viewRows - spanRows) * 0.42));
  const pageHeight = Math.max(vh, HEAD_H + (r0 + spanRows + 2) * ROW_H);
  // Enough rows to fill the page; the last one may be cut off, as in a real sheet.
  const rows = Math.ceil((pageHeight - HEAD_H) / ROW_H);

  let left: number;
  let width: number;
  if (narrow) {
    left = HEAD_W + 8;
    width = vw - left - 16;
  } else {
    const span = Math.min(CARD_COLS, cols);
    const c0 = Math.max(0, Math.floor((cols - span) / 2));
    left = HEAD_W + c0 * COL_W;
    width = span * COL_W;
  }
  const top = HEAD_H + r0 * ROW_H;
  const height = spanRows * ROW_H;
  const start = cellOf(left + 1, top + 1);
  const end = cellOf(left + width - 1, top + height - 1);
  return {
    cols, rows, pageHeight, narrow, left, top, width, height,
    range: { c0: start.col, c1: end.col, r0: start.row, r1: end.row },
  };
}

function Debris({ layout }: { layout: Layout }) {
  const { range } = layout;
  const cells: { row: number; col: number; text: string }[] = [];
  for (let row = 0; row < layout.rows; row++) {
    for (let col = 0; col < layout.cols; col++) {
      // Keep a one-cell margin of clean sheet around the selection.
      if (row >= range.r0 - 1 && row <= range.r1 + 1 && col >= range.c0 - 1 && col <= range.c1 + 1) continue;
      const roll = hash(row, col);
      if (roll < 0.075) cells.push({ row, col, text: DEBRIS[Math.floor(roll * 1000) % DEBRIS.length] });
    }
  }
  return (
    <div aria-hidden className="pointer-events-none absolute inset-0 select-none">
      {cells.map(({ row, col, text }) => (
        <span
          key={`${row}:${col}`}
          className="absolute truncate px-2 font-mono text-[11px] leading-8 text-ink-300"
          style={{ left: HEAD_W + col * COL_W, top: HEAD_H + row * ROW_H, width: COL_W, height: ROW_H }}
        >
          {text}
        </span>
      ))}
    </div>
  );
}
