import clsx from "clsx";
import type { ReactNode } from "react";

export type Tone = "neutral" | "success" | "warning" | "danger" | "info" | "brand";

const tones: Record<Tone, string> = {
  neutral: "bg-ink-100 text-ink-700 ring-ink-200",
  success: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  warning: "bg-amber-50 text-amber-800 ring-amber-200",
  danger: "bg-brand-50 text-brand-700 ring-brand-200",
  info: "bg-sky-50 text-sky-700 ring-sky-200",
  brand: "bg-brand-50 text-brand-700 ring-brand-200",
};

const dots: Record<Tone, string> = {
  neutral: "bg-ink-400",
  success: "bg-emerald-500",
  warning: "bg-amber-500",
  danger: "bg-brand-600",
  info: "bg-sky-500",
  brand: "bg-brand-600",
};

export function Badge({
  tone = "neutral",
  icon,
  dot,
  children,
  className,
  title,
}: {
  tone?: Tone;
  icon?: ReactNode;
  dot?: boolean;
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={clsx(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-caption font-medium ring-1 ring-inset",
        tones[tone],
        className,
      )}
    >
      {dot && <span className={clsx("h-1.5 w-1.5 rounded-full", dots[tone])} aria-hidden />}
      {icon && <span className="flex [&>svg]:h-3.5 [&>svg]:w-3.5" aria-hidden>{icon}</span>}
      {children}
    </span>
  );
}
