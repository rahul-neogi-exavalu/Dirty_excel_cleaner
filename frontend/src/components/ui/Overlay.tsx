import clsx from "clsx";
import { X } from "lucide-react";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

/* -------------------------------------------------------------------------- */
/* Tooltip: supplementary text on hover *and* keyboard focus.                 */
/* -------------------------------------------------------------------------- */

export function Tooltip({
  content,
  children,
  side = "top",
  className,
}: {
  content: ReactNode;
  children: ReactElement;
  side?: "top" | "bottom";
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  if (!content) return children;
  return (
    <span
      className={clsx("relative inline-flex", className)}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
      onKeyDown={(event) => event.key === "Escape" && setOpen(false)}
      aria-describedby={open ? id : undefined}
    >
      {children}
      {open && (
        <span
          id={id}
          role="tooltip"
          className={clsx(
            "pointer-events-none absolute left-1/2 z-50 w-max max-w-[260px] -translate-x-1/2 animate-fade-in rounded bg-ink-900 px-2.5 py-1.5 text-caption font-normal normal-case tracking-normal text-white shadow-pop",
            side === "top" ? "bottom-full mb-2" : "top-full mt-2",
          )}
        >
          {content}
        </span>
      )}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Modal: focus-trapped dialog for confirmations.                             */
/* -------------------------------------------------------------------------- */

export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = "md",
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children?: ReactNode;
  footer?: ReactNode;
  size?: "md" | "lg";
}) {
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useFocusTrap(open, panel, onClose);
  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 animate-fade-in bg-ink-900/40" onClick={onClose} aria-hidden />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={clsx(
          "relative flex max-h-[calc(100vh-32px)] w-full animate-scale-in flex-col rounded-lg bg-white shadow-pop",
          size === "md" ? "max-w-md" : "max-w-2xl",
        )}
      >
        <div className="flex shrink-0 items-start justify-between gap-4 border-b border-ink-200 px-6 py-4">
          <div>
            <h2 id={titleId} className="text-section text-ink-900">
              {title}
            </h2>
            {description && <p className="mt-1 text-body text-ink-600">{description}</p>}
          </div>
          <button
            type="button"
            aria-label="Close dialog"
            onClick={onClose}
            className="rounded p-1 text-ink-400 hover:bg-ink-100 hover:text-ink-700"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        {children && <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4 scroll-thin">{children}</div>}
        {footer && <div className="flex shrink-0 flex-wrap justify-end gap-2 rounded-b-lg border-t border-ink-200 bg-ink-50 px-6 py-3">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}

/* Side drawer used for the mobile navigation. */
export function Drawer({
  open,
  onClose,
  label,
  children,
}: {
  open: boolean;
  onClose: () => void;
  label: string;
  children: ReactNode;
}) {
  const panel = useRef<HTMLDivElement>(null);
  useFocusTrap(open, panel, onClose);
  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-50 md:hidden">
      <div className="absolute inset-0 animate-fade-in bg-ink-900/50" onClick={onClose} aria-hidden />
      <div ref={panel} role="dialog" aria-modal="true" aria-label={label} className="absolute inset-y-0 left-0 w-72 animate-slide-in-right">
        {children}
      </div>
    </div>,
    document.body,
  );
}

function useFocusTrap(open: boolean, panel: React.RefObject<HTMLElement>, onClose: () => void) {
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const selector = 'button:not([disabled]), [href], input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])';
    const focusables = () => Array.from(panel.current?.querySelectorAll<HTMLElement>(selector) ?? []);
    focusables()[0]?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
      }
      if (event.key !== "Tab") return;
      const items = focusables();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus?.();
    };
  }, [open, panel, onClose]);
}

/* -------------------------------------------------------------------------- */
/* Popover: anchored floating panel (menus, dropdowns).                       */
/* -------------------------------------------------------------------------- */

export function usePopover() {
  const [open, setOpen] = useState(false);
  const anchor = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!anchor.current?.contains(target) && !panel.current?.contains(target)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        anchor.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return { open, setOpen, anchor, panel };
}

export function PopoverPanel({
  open,
  anchor,
  panel,
  children,
  align = "left",
  width,
  className,
  role,
  id,
  labelledBy,
}: {
  open: boolean;
  anchor: React.RefObject<HTMLElement>;
  panel: React.RefObject<HTMLDivElement>;
  children: ReactNode;
  align?: "left" | "right";
  width?: number | "anchor";
  className?: string;
  role?: string;
  id?: string;
  labelledBy?: string;
}) {
  const [style, setStyle] = useState<React.CSSProperties>({});
  useLayoutEffect(() => {
    if (!open || !anchor.current) return;
    const place = () => {
      const rect = anchor.current!.getBoundingClientRect();
      const panelWidth = width === "anchor" ? rect.width : width ?? 240;
      let left = align === "left" ? rect.left : rect.right - panelWidth;
      left = Math.max(8, Math.min(left, window.innerWidth - panelWidth - 8));
      const below = window.innerHeight - rect.bottom;
      const top = below < 280 && rect.top > below ? undefined : rect.bottom + 4;
      const bottom = top === undefined ? window.innerHeight - rect.top + 4 : undefined;
      setStyle({ position: "fixed", left, top, bottom, width: panelWidth });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, anchor, align, width]);
  if (!open) return null;
  return createPortal(
    <div
      ref={panel}
      id={id}
      role={role}
      aria-labelledby={labelledBy}
      style={style}
      className={clsx("z-50 animate-scale-in rounded-lg border border-ink-200 bg-white p-1 shadow-pop", className)}
    >
      {children}
    </div>,
    document.body,
  );
}
