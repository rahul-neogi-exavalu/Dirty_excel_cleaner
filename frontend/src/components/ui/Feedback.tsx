import clsx from "clsx";
import {
  AlertTriangle,
  CheckCircle2,
  Info,
  X,
  XCircle,
} from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

/* -------------------------------------------------------------------------- */
/* Toasts: confirmation of things that already happened. Never for decisions. */
/* -------------------------------------------------------------------------- */

type Severity = "success" | "info" | "warning" | "error";
interface ToastItem {
  id: number;
  severity: Severity;
  title: string;
  description?: string;
}

const ToastContext = createContext<(toast: Omit<ToastItem, "id">) => void>(() => {});

const severityStyle: Record<Severity, { icon: ReactNode; bar: string; text: string }> = {
  success: { icon: <CheckCircle2 />, bar: "bg-emerald-500", text: "text-emerald-600" },
  info: { icon: <Info />, bar: "bg-sky-500", text: "text-sky-600" },
  warning: { icon: <AlertTriangle />, bar: "bg-amber-500", text: "text-amber-600" },
  error: { icon: <XCircle />, bar: "bg-brand-600", text: "text-brand-600" },
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const counter = useRef(0);

  const dismiss = useCallback((id: number) => setToasts((all) => all.filter((toast) => toast.id !== id)), []);
  const push = useCallback(
    (toast: Omit<ToastItem, "id">) => {
      const id = ++counter.current;
      setToasts((all) => [...all.slice(-3), { ...toast, id }]);
      setTimeout(() => dismiss(id), toast.severity === "error" ? 8000 : 4500);
    },
    [dismiss],
  );

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div
        aria-live="polite"
        aria-relevant="additions"
        className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-[min(380px,calc(100vw-32px))] flex-col gap-2"
      >
        {toasts.map((toast) => {
          const style = severityStyle[toast.severity];
          return (
            <div
              key={toast.id}
              role={toast.severity === "error" ? "alert" : "status"}
              className="pointer-events-auto relative flex animate-slide-in-right gap-3 overflow-hidden rounded-lg border border-ink-200 bg-white py-3 pl-4 pr-10 shadow-pop"
            >
              <span className={clsx("absolute inset-y-0 left-0 w-1", style.bar)} aria-hidden />
              <span className={clsx("mt-0.5 [&>svg]:h-4 [&>svg]:w-4", style.text)} aria-hidden>
                {style.icon}
              </span>
              <div className="min-w-0">
                <p className="text-body font-medium text-ink-900">{toast.title}</p>
                {toast.description && <p className="mt-0.5 text-caption text-ink-600">{toast.description}</p>}
              </div>
              <button
                type="button"
                aria-label="Dismiss notification"
                onClick={() => dismiss(toast.id)}
                className="absolute right-2 top-2 rounded p-1 text-ink-400 hover:bg-ink-100 hover:text-ink-700"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          );
        })}
      </div>
    </ToastContext.Provider>
  );
}

export const useToast = () => useContext(ToastContext);

/* -------------------------------------------------------------------------- */
/* Inline alerts: information the user must read or act on.                   */
/* -------------------------------------------------------------------------- */

export function Alert({
  tone = "info",
  title,
  children,
  action,
  className,
}: {
  tone?: Severity;
  title?: string;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  const styles: Record<Severity, string> = {
    success: "border-emerald-200 bg-emerald-50 text-emerald-900",
    info: "border-sky-200 bg-sky-50/70 text-sky-900",
    warning: "border-amber-200 bg-amber-50 text-amber-900",
    error: "border-brand-200 bg-brand-50 text-brand-800",
  };
  return (
    <div
      role={tone === "error" ? "alert" : "note"}
      className={clsx("flex items-start gap-3 rounded-lg border px-4 py-3", styles[tone], className)}
    >
      <span className={clsx("mt-0.5 shrink-0 [&>svg]:h-4 [&>svg]:w-4", severityStyle[tone].text)} aria-hidden>
        {severityStyle[tone].icon}
      </span>
      <div className="min-w-0 flex-1 text-body">
        {title && <p className="font-medium">{title}</p>}
        {children && <div className={clsx(title && "mt-0.5", "text-[13px] opacity-90")}>{children}</div>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}

/* -------------------------------------------------------------------------- */

export function EmptyState({
  icon,
  title,
  description,
  action,
  compact,
}: {
  icon: ReactNode;
  title: string;
  description: string;
  action?: ReactNode;
  compact?: boolean;
}) {
  return (
    <div className={clsx("flex flex-col items-center text-center", compact ? "px-6 py-10" : "px-6 py-16")}>
      <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-lg bg-ink-100 text-ink-500 [&>svg]:h-6 [&>svg]:w-6">
        {icon}
      </div>
      <h3 className="text-card text-ink-900">{title}</h3>
      <p className="mt-1 max-w-sm text-body text-ink-600">{description}</p>
      {action && <div className="mt-6">{action}</div>}
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx("skeleton", className)} aria-hidden />;
}

export function ProgressBar({
  value,
  label,
  active,
  indeterminate,
  tone = "brand",
  className,
}: {
  value: number;
  label: string;
  active?: boolean;
  /** Work is happening but its size is unknown (e.g. opening a workbook). */
  indeterminate?: boolean;
  tone?: "brand" | "success" | "danger";
  className?: string;
}) {
  const percent = Math.max(0, Math.min(100, Math.round(value * 100)));
  if (indeterminate)
    return (
      <div
        role="progressbar"
        aria-label={label}
        aria-valuetext="In progress"
        className={clsx("relative h-2 w-full overflow-hidden rounded-full bg-ink-100", className)}
      >
        <div className="absolute inset-y-0 w-1/3 animate-indeterminate rounded-full bg-brand-600" />
      </div>
    );
  const fill = { brand: "bg-brand-600", success: "bg-emerald-500", danger: "bg-brand-700" }[tone];
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent}
      className={clsx("h-2 w-full overflow-hidden rounded-full bg-ink-100", className)}
    >
      <div
        className={clsx("h-full rounded-full transition-[width] duration-500 ease-out", fill, active && "animate-stripes")}
        style={{
          width: `${percent}%`,
          backgroundImage: active
            ? "linear-gradient(45deg, rgba(255,255,255,.18) 25%, transparent 25%, transparent 50%, rgba(255,255,255,.18) 50%, rgba(255,255,255,.18) 75%, transparent 75%, transparent)"
            : undefined,
          backgroundSize: active ? "24px 24px" : undefined,
        }}
      />
    </div>
  );
}

export function StatTile({
  icon,
  label,
  value,
  hint,
  tone = "neutral",
  loading,
  className,
}: {
  className?: string;
  icon?: ReactNode;
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "neutral" | "brand" | "success" | "warning" | "info" | "danger";
  loading?: boolean;
}) {
  const iconTone = {
    neutral: "bg-ink-100 text-ink-600",
    brand: "bg-brand-50 text-brand-600",
    success: "bg-emerald-50 text-emerald-600",
    warning: "bg-amber-50 text-amber-600",
    info: "bg-sky-50 text-sky-600",
    danger: "bg-brand-50 text-brand-600",
  }[tone];
  return (
    <div className={clsx("flex items-center gap-3 rounded-lg border border-ink-200 bg-white px-4 py-3", className)}>
      {icon && (
        <div className={clsx("flex h-9 w-9 shrink-0 items-center justify-center rounded [&>svg]:h-[18px] [&>svg]:w-[18px]", iconTone)} aria-hidden>
          {icon}
        </div>
      )}
      <div className="min-w-0">
        {loading ? (
          <>
            <Skeleton className="mb-1.5 h-5 w-16" />
            <Skeleton className="h-3 w-24" />
          </>
        ) : (
          <>
            <p className="num truncate text-[18px] font-semibold leading-6 text-ink-900">{value}</p>
            <p className="text-caption leading-4 text-ink-500">{label}</p>
            {hint && <p className="mt-0.5 text-caption leading-4 text-ink-400">{hint}</p>}
          </>
        )}
      </div>
    </div>
  );
}

export function useStableCallback<T extends (...args: any[]) => any>(fn: T): T {
  const ref = useRef(fn);
  ref.current = fn;
  return useMemo(() => ((...args: any[]) => ref.current(...args)) as T, []);
}
