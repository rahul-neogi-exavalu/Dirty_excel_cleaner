import clsx from "clsx";
import { Check } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

export interface NavSection {
  id: string;
  label: string;
  /** Shown instead of the label on narrow screens. */
  shortLabel?: string;
  icon: ReactNode;
  done?: boolean;
  attention?: boolean;
}

// Gap left between the sticky nav and a section scrolled into place.
const GAP = 16;

/**
 * Scrollspy: a sticky strip of section links. Clicking scrolls to the section; scrolling
 * the page highlights the section currently in view.
 */
export function SectionNav({ sections, label }: { sections: NavSection[]; label: string }) {
  const [active, setActive] = useState(sections[0]?.id);
  const lock = useRef<number | null>(null);
  const nav = useRef<HTMLElement>(null);
  // Where content disappears under the sticky top bar + this nav; measured, so it holds
  // whatever the nav's height is at the current breakpoint.
  const offset = () => (nav.current?.getBoundingClientRect().bottom ?? 128) + GAP;

  const measure = useCallback(() => {
    if (lock.current) return; // a click-initiated scroll is in flight; keep its target
    const atBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4;
    if (atBottom) return setActive(sections[sections.length - 1]?.id);
    let current = sections[0]?.id;
    for (const section of sections) {
      const el = document.getElementById(section.id);
      if (el && el.getBoundingClientRect().top - offset() <= 8) current = section.id;
    }
    setActive(current);
  }, [sections]);

  useEffect(() => {
    let frame = 0;
    const onScroll = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(measure);
    };
    measure();
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
    };
  }, [measure]);

  const go = (id: string) => {
    const el = document.getElementById(id);
    if (!el) return;
    setActive(id);
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - offset(), behavior: reduce ? "auto" : "smooth" });
    // Move focus for keyboard and screen-reader users without a second jump.
    el.focus({ preventScroll: true });
    if (lock.current) clearTimeout(lock.current);
    lock.current = window.setTimeout(() => {
      lock.current = null;
    }, reduce ? 50 : 700);
  };

  return (
    <nav ref={nav} aria-label={label} className="sticky top-14 z-20 -mx-4 mb-6 bg-ink-50/95 px-4 py-2 backdrop-blur md:-mx-8 md:px-8">
      <ol className="card flex gap-1 overflow-x-auto p-1.5 scroll-thin">
        {sections.map((section, index) => {
          const selected = section.id === active;
          return (
            <li key={section.id} className="min-w-0 flex-1">
              <button
                type="button"
                onClick={() => go(section.id)}
                aria-current={selected ? "location" : undefined}
                className={clsx(
                  "relative flex w-full items-center justify-center gap-2 whitespace-nowrap rounded-md px-3 py-2.5 text-body font-medium transition-colors",
                  "focus-visible:outline-none focus-visible:shadow-focus",
                  selected ? "bg-brand-50 text-brand-700" : "text-ink-700 hover:bg-ink-100 hover:text-ink-900",
                )}
              >
                <span className={clsx("[&>svg]:h-[18px] [&>svg]:w-[18px]", selected ? "text-brand-600" : "text-ink-500")} aria-hidden>
                  {section.icon}
                </span>
                <span className="truncate">
                  {index + 1}. <span className="hidden lg:inline">{section.label}</span>
                  <span className="lg:hidden">{section.shortLabel ?? section.label}</span>
                </span>
                {section.done && !section.attention && (
                  <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-emerald-100 text-emerald-600" aria-label="complete">
                    <Check className="h-3 w-3" strokeWidth={3} />
                  </span>
                )}
                {section.attention && <span className="h-2 w-2 shrink-0 rounded-full bg-amber-500" aria-label="needs attention" />}
                <span
                  aria-hidden
                  className={clsx(
                    "absolute inset-x-3 -bottom-1.5 h-0.5 rounded-full bg-brand-600 transition-opacity duration-200",
                    selected ? "opacity-100" : "opacity-0",
                  )}
                />
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
