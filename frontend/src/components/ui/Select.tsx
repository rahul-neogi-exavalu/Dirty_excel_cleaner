import clsx from "clsx";
import { Check, ChevronDown } from "lucide-react";
import { useEffect, useId, useState, type ReactNode } from "react";
import { PopoverPanel, usePopover } from "./Overlay";

export interface SelectOption<T extends string> {
  value: T;
  label: string;
  description?: ReactNode;
  icon?: ReactNode;
  meta?: ReactNode;
  disabled?: boolean;
}

/** Accessible single-select listbox: arrow keys, Home/End, Enter, Escape, type-ahead. */
export function Select<T extends string>({
  value,
  options,
  onChange,
  label,
  placeholder = "Select…",
  disabled,
  className,
  icon,
  width = "anchor",
  hideLabel,
}: {
  value: T | null;
  options: SelectOption<T>[];
  onChange: (value: T) => void;
  label: string;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
  icon?: ReactNode;
  width?: number | "anchor";
  hideLabel?: boolean;
}) {
  const { open, setOpen, anchor, panel } = usePopover();
  const [active, setActive] = useState(0);
  const id = useId();
  const selected = options.find((option) => option.value === value) ?? null;

  useEffect(() => {
    if (open) setActive(Math.max(0, options.findIndex((option) => option.value === value)));
  }, [open, options, value]);

  useEffect(() => {
    if (!open) return;
    panel.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active, open, panel]);

  const move = (delta: number) => {
    if (!options.length) return;
    let next = active;
    for (let i = 0; i < options.length; i++) {
      next = (next + delta + options.length) % options.length;
      if (!options[next].disabled) break;
    }
    setActive(next);
  };

  const choose = (index: number) => {
    const option = options[index];
    if (!option || option.disabled) return;
    onChange(option.value);
    setOpen(false);
    anchor.current?.focus();
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (!open && ["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
      event.preventDefault();
      setOpen(true);
      return;
    }
    if (!open) return;
    if (event.key === "ArrowDown") (event.preventDefault(), move(1));
    else if (event.key === "ArrowUp") (event.preventDefault(), move(-1));
    else if (event.key === "Home") (event.preventDefault(), setActive(0));
    else if (event.key === "End") (event.preventDefault(), setActive(options.length - 1));
    else if (event.key === "Enter" || event.key === " ") (event.preventDefault(), choose(active));
    else if (event.key === "Tab") setOpen(false);
    else if (event.key.length === 1) {
      const index = options.findIndex((option) => option.label.toLowerCase().startsWith(event.key.toLowerCase()));
      if (index >= 0) setActive(index);
    }
  };

  return (
    <div className={clsx("flex flex-col gap-1", className)}>
      <span id={`${id}-label`} className={clsx("text-caption font-medium text-ink-600", hideLabel && "sr-only")}>
        {label}
      </span>
      <button
        ref={anchor}
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-labelledby={`${id}-label ${id}-value`}
        aria-controls={open ? `${id}-list` : undefined}
        aria-activedescendant={open ? `${id}-opt-${active}` : undefined}
        onClick={() => setOpen(!open)}
        onKeyDown={onKeyDown}
        className={clsx(
          "flex h-10 w-full items-center gap-2 rounded border bg-white px-3 text-left text-body transition-shadow",
          "focus-visible:outline-none focus-visible:shadow-focus disabled:cursor-not-allowed disabled:bg-ink-50 disabled:text-ink-400",
          open ? "border-brand-500 shadow-focus" : "border-ink-300 hover:border-ink-400",
        )}
      >
        {(selected?.icon ?? icon) && (
          <span className="shrink-0 text-ink-500 [&>svg]:h-4 [&>svg]:w-4" aria-hidden>
            {selected?.icon ?? icon}
          </span>
        )}
        <span id={`${id}-value`} className={clsx("min-w-0 flex-1 truncate", selected ? "font-medium text-ink-900" : "text-ink-400")}>
          {selected?.label ?? placeholder}
        </span>
        {selected?.meta && <span className="hidden shrink-0 sm:inline-flex">{selected.meta}</span>}
        <ChevronDown className={clsx("h-4 w-4 shrink-0 text-ink-500 transition-transform", open && "rotate-180")} aria-hidden />
      </button>
      <PopoverPanel
        open={open}
        anchor={anchor}
        panel={panel}
        width={width}
        role="listbox"
        id={`${id}-list`}
        labelledBy={`${id}-label`}
        className="max-h-80 overflow-y-auto scroll-thin"
      >
        {options.length === 0 && <p className="px-3 py-6 text-center text-body text-ink-500">No options available</p>}
        {options.map((option, index) => {
          const isSelected = option.value === value;
          return (
            <div
              key={option.value}
              id={`${id}-opt-${index}`}
              data-index={index}
              role="option"
              aria-selected={isSelected}
              aria-disabled={option.disabled || undefined}
              onMouseEnter={() => setActive(index)}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => choose(index)}
              className={clsx(
                "flex cursor-pointer items-start gap-2.5 rounded px-3 py-2",
                index === active && "bg-ink-100",
                option.disabled && "cursor-not-allowed opacity-50",
              )}
            >
              {option.icon && <span className="mt-0.5 shrink-0 text-ink-500 [&>svg]:h-4 [&>svg]:w-4">{option.icon}</span>}
              <span className="min-w-0 flex-1">
                <span className={clsx("block truncate text-body", isSelected ? "font-semibold text-ink-900" : "text-ink-800")}>
                  {option.label}
                </span>
                {option.description && <span className="mt-0.5 block text-caption text-ink-500">{option.description}</span>}
              </span>
              {option.meta && <span className="shrink-0">{option.meta}</span>}
              <Check className={clsx("mt-0.5 h-4 w-4 shrink-0 text-brand-600", !isSelected && "invisible")} aria-hidden />
            </div>
          );
        })}
      </PopoverPanel>
    </div>
  );
}
