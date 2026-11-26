import clsx from "clsx";
import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import { formatNumber } from "../../lib/format";
import { IconButton } from "./Button";

/**
 * Rows per page that fit the screen below a list, so the page itself barely scrolls:
 * a 720px laptop gets fewer rows than a 1440px monitor. Measured from the list's own
 * position and its first row, and re-measured on resize.
 */
export function useFitPageSize(
  anchor: RefObject<HTMLElement>,
  { min = 5, max = 50, fallbackRow = 44, reserve = 120 }: { min?: number; max?: number; fallbackRow?: number; reserve?: number } = {},
): number {
  const [size, setSize] = useState(min);
  // A row's height is read once and then kept. Re-reading it from whichever page is
  // showing would make the size depend on the page, and rows of different heights could
  // flip the size back and forth forever.
  const rowHeight = useRef(0);
  useLayoutEffect(() => {
    const measure = () => {
      const node = anchor.current;
      if (!node) return;
      const top = node.getBoundingClientRect().top + window.scrollY;
      if (!rowHeight.current) rowHeight.current = node.querySelector<HTMLElement>("[data-row]")?.offsetHeight ?? 0;
      const row = rowHeight.current || fallbackRow;
      const fits = Math.floor((window.innerHeight - top - reserve) / row);
      setSize(Math.max(min, Math.min(max, fits)));
    };
    measure();
    // Content above the list can load later and push it down; the result only depends
    // on the list's position and row height, so re-measuring on layout changes is stable.
    const observer = new ResizeObserver(measure);
    observer.observe(document.body);
    window.addEventListener("resize", measure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [anchor, min, max, fallbackRow, reserve]);
  return size;
}

/** One page of `items`; back to the first page whenever `reset` changes (a filter, a search). */
export function usePaged<T>(items: T[], size: number, reset?: unknown) {
  const [page, setPage] = useState(0);
  const pages = Math.max(1, Math.ceil(items.length / size));
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    setPage(0);
  }, [reset]);
  // A shorter list (or a bigger page) can leave the current page past the end.
  const current = Math.min(page, pages - 1);
  const slice = useMemo(() => items.slice(current * size, current * size + size), [items, current, size]);
  return {
    page: current,
    pages,
    size,
    total: items.length,
    from: items.length ? current * size + 1 : 0,
    to: Math.min(items.length, current * size + size),
    slice,
    setPage,
  };
}

export function Pagination({
  page,
  pages,
  total,
  from,
  to,
  onPage,
  noun = "row",
  className,
}: {
  page: number;
  pages: number;
  total: number;
  from: number;
  to: number;
  onPage: (page: number) => void;
  noun?: string;
  className?: string;
}) {
  if (total === 0) return null;
  return (
    <nav aria-label="Pagination" className={clsx("flex items-center justify-between gap-3 border-t border-ink-200 px-4 py-2", className)}>
      <span className="num text-caption text-ink-500">
        {pages > 1 ? `${formatNumber(from)}–${formatNumber(to)} of ` : ""}
        {formatNumber(total)} {total === 1 ? noun : `${noun}s`}
      </span>
      {pages > 1 && (
        <div className="flex items-center gap-1">
          <IconButton label="First page" disabled={page === 0} onClick={() => onPage(0)} className="hidden sm:inline-flex">
            <ChevronsLeft />
          </IconButton>
          <IconButton label="Previous page" disabled={page === 0} onClick={() => onPage(page - 1)}>
            <ChevronLeft />
          </IconButton>
          <span className="num min-w-[72px] text-center text-caption font-medium text-ink-700" aria-live="polite">
            {page + 1} / {pages}
          </span>
          <IconButton label="Next page" disabled={page >= pages - 1} onClick={() => onPage(page + 1)}>
            <ChevronRight />
          </IconButton>
          <IconButton label="Last page" disabled={page >= pages - 1} onClick={() => onPage(pages - 1)} className="hidden sm:inline-flex">
            <ChevronsRight />
          </IconButton>
        </div>
      )}
    </nav>
  );
}
