import clsx from "clsx";
import { Check, Loader2, X } from "lucide-react";
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "link";
type Size = "sm" | "md" | "lg";
export type ButtonState = "idle" | "loading" | "success" | "error";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  icon?: ReactNode;
  iconRight?: ReactNode;
  state?: ButtonState;
  loadingText?: string;
  successText?: string;
  errorText?: string;
}

const variants: Record<Variant, string> = {
  primary:
    "bg-brand-600 text-white hover:bg-brand-700 active:bg-brand-800 disabled:bg-ink-200 disabled:text-ink-400 shadow-sm",
  secondary:
    "bg-white text-ink-800 border border-ink-300 hover:bg-ink-50 hover:border-ink-400 active:bg-ink-100 disabled:text-ink-400 disabled:bg-ink-50 disabled:border-ink-200",
  ghost: "text-ink-700 hover:bg-ink-100 active:bg-ink-200 disabled:text-ink-400",
  danger:
    "bg-white text-brand-700 border border-brand-200 hover:bg-brand-50 active:bg-brand-100 disabled:text-ink-400 disabled:border-ink-200",
  link: "text-brand-700 hover:text-brand-800 underline-offset-4 hover:underline px-0 disabled:text-ink-400",
};

const sizes: Record<Size, string> = {
  sm: "h-8 px-3 text-caption gap-1.5",
  md: "h-9 px-4 text-body gap-2",
  lg: "h-11 px-6 text-[15px] gap-2",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "secondary",
    size = "md",
    icon,
    iconRight,
    state = "idle",
    loadingText,
    successText,
    errorText,
    className,
    children,
    disabled,
    ...rest
  },
  ref,
) {
  const busy = state === "loading";
  let leading = icon;
  let label: ReactNode = children;
  if (state === "loading") {
    leading = <Loader2 className="h-4 w-4 animate-spin" aria-hidden />;
    label = loadingText ?? children;
  } else if (state === "success") {
    leading = <Check className="h-4 w-4" aria-hidden />;
    label = successText ?? children;
  } else if (state === "error") {
    leading = <X className="h-4 w-4" aria-hidden />;
    label = errorText ?? children;
  }

  return (
    <button
      ref={ref}
      type="button"
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      className={clsx(
        "inline-flex select-none items-center justify-center whitespace-nowrap rounded font-medium transition-colors duration-150",
        "focus-visible:outline-none focus-visible:shadow-focus disabled:cursor-not-allowed",
        variants[variant],
        sizes[size],
        state === "success" && variant === "primary" && "!bg-emerald-600",
        state === "success" && variant !== "primary" && "!border-emerald-300 !text-emerald-700",
        state === "error" && "!border-brand-300 !text-brand-700",
        className,
      )}
      {...rest}
    >
      {leading && <span className="flex shrink-0 items-center [&>svg]:h-4 [&>svg]:w-4">{leading}</span>}
      {label && <span>{label}</span>}
      {iconRight && <span className="flex shrink-0 items-center [&>svg]:h-4 [&>svg]:w-4">{iconRight}</span>}
      <span className="sr-only" aria-live="polite">
        {state === "loading" ? "Working" : state === "success" ? "Done" : state === "error" ? "Failed" : ""}
      </span>
    </button>
  );
});

export function IconButton({
  label,
  className,
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={clsx(
        "inline-flex h-8 w-8 items-center justify-center rounded text-ink-500 transition-colors",
        "hover:bg-ink-100 hover:text-ink-800 focus-visible:outline-none focus-visible:shadow-focus",
        "disabled:cursor-not-allowed disabled:opacity-40 [&>svg]:h-4 [&>svg]:w-4",
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}
