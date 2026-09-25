import clsx from "clsx";
import { Check, Minus, Search, X } from "lucide-react";
import { forwardRef, useEffect, useRef, type InputHTMLAttributes } from "react";

export function Switch({
  checked,
  onChange,
  label,
  description,
  disabled,
  id,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  description?: string;
  disabled?: boolean;
  id: string;
}) {
  return (
    <div className="flex items-center gap-3">
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={checked}
        aria-describedby={description ? `${id}-desc` : undefined}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={clsx(
          "relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors duration-200",
          "focus-visible:outline-none focus-visible:shadow-focus disabled:cursor-not-allowed disabled:opacity-50",
          checked ? "bg-brand-600" : "bg-ink-300",
        )}
      >
        <span className="sr-only">{label}</span>
        <span
          aria-hidden
          className={clsx(
            "inline-block h-5 w-5 rounded-full bg-white shadow transition-transform duration-200",
            checked ? "translate-x-[22px]" : "translate-x-0.5",
          )}
        />
      </button>
      <label htmlFor={id} className="cursor-pointer select-none text-body font-medium text-ink-800">
        {checked ? "On" : "Off"}
      </label>
      {description && (
        <span id={`${id}-desc`} className="sr-only">
          {description}
        </span>
      )}
    </div>
  );
}

export function Checkbox({
  checked,
  indeterminate,
  onChange,
  label,
  disabled,
  className,
}: {
  checked: boolean;
  indeterminate?: boolean;
  onChange: (value: boolean) => void;
  label: string;
  disabled?: boolean;
  className?: string;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = Boolean(indeterminate);
  }, [indeterminate]);
  const on = checked || indeterminate;
  return (
    <span className={clsx("relative inline-flex h-4 w-4 shrink-0", className)}>
      <input
        ref={ref}
        type="checkbox"
        aria-label={label}
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        className="peer absolute inset-0 z-10 h-4 w-4 cursor-pointer opacity-0 disabled:cursor-not-allowed"
      />
      <span
        aria-hidden
        className={clsx(
          "pointer-events-none flex h-4 w-4 items-center justify-center rounded-[4px] border transition-all duration-150",
          "peer-focus-visible:shadow-focus peer-disabled:opacity-50",
          on ? "border-brand-600 bg-brand-600 text-white" : "border-ink-300 bg-white peer-hover:border-ink-500",
        )}
      >
        {indeterminate ? (
          <Minus className="h-3 w-3" strokeWidth={3} />
        ) : (
          <Check className={clsx("h-3 w-3 transition-transform duration-150", checked ? "scale-100" : "scale-0")} strokeWidth={3} />
        )}
      </span>
    </span>
  );
}

export const TextInput = forwardRef<
  HTMLInputElement,
  InputHTMLAttributes<HTMLInputElement> & { invalid?: boolean; valid?: boolean }
>(function TextInput({ invalid, valid, className, ...rest }, ref) {
  return (
    <input
      ref={ref}
      aria-invalid={invalid || undefined}
      className={clsx(
        "h-9 w-full rounded border bg-white px-3 text-body text-ink-900 placeholder:text-ink-400 transition-shadow",
        "focus:outline-none focus:shadow-focus read-only:bg-ink-50 read-only:text-ink-600",
        "disabled:cursor-not-allowed disabled:bg-ink-50 disabled:text-ink-400",
        invalid ? "border-brand-500 focus:border-brand-600" : valid ? "border-emerald-500" : "border-ink-300 focus:border-brand-500",
        className,
      )}
      {...rest}
    />
  );
});

export function SearchInput({
  value,
  onChange,
  placeholder,
  label,
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  label: string;
  className?: string;
}) {
  return (
    <div className={clsx("relative", className)}>
      <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-ink-400" aria-hidden />
      <input
        type="search"
        aria-label={label}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        className={clsx(
          "h-9 w-full rounded border border-ink-300 bg-white pl-9 pr-8 text-body placeholder:text-ink-400",
          "focus:border-brand-500 focus:outline-none focus:shadow-focus [&::-webkit-search-cancel-button]:hidden",
        )}
      />
      {value && (
        <button
          type="button"
          aria-label="Clear search"
          onClick={() => onChange("")}
          className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-ink-400 hover:bg-ink-100 hover:text-ink-700"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );
}
