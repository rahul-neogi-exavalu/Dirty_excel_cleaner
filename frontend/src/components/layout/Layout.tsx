import clsx from "clsx";
import {
  Check,
  ChevronRight,
  ClipboardCheck,
  FileSpreadsheet,
  Home,
  Loader2,
  Lock,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  PlayCircle,
  SlidersHorizontal,
  Trash2,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { formatBytes, plural } from "../../lib/format";
import { useWorkflow, type Page } from "../../state/workflow";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Drawer, Modal, Tooltip } from "../ui/Overlay";

export const PAGE_META: Record<Page, { step: number; label: string; icon: ReactNode }> = {
  configuration: { step: 1, label: "Configuration", icon: <SlidersHorizontal /> },
  run: { step: 2, label: "Run", icon: <PlayCircle /> },
  results: { step: 3, label: "Review & Results", icon: <ClipboardCheck /> },
};

/** The Exavalu mark: a staircase of six squares with a red outline. */
export function ExavaluMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden>
      <rect x="21" y="1" width="10" height="10" fill="#666666" />
      <rect x="11" y="11" width="10" height="10" fill="#B3B3B3" />
      <rect x="21" y="11" width="10" height="10" fill="#D7262E" />
      <rect x="1" y="21" width="10" height="10" fill="#CCCCCC" />
      <rect x="11" y="21" width="10" height="10" fill="#F08080" />
      <rect x="21" y="21" width="10" height="10" fill="#0D0D0D" />
      <path d="M21 1H31V31H1V21H11V11H21Z" fill="none" stroke="#C8102E" strokeWidth="0.8" strokeLinejoin="miter" />
    </svg>
  );
}

export function Logo({ compact }: { compact?: boolean }) {
  return (
    <div className="flex items-center gap-3">
      {/* The mark is designed on white; a white tile keeps its greys legible on the dark sidebar. */}
      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-white p-1.5 shadow-sm ring-1 ring-black/5">
        <ExavaluMark className="h-full w-full" />
      </span>
      {!compact && (
        <div className="leading-none">
          <p className="text-[19px] font-bold tracking-[0.02em] text-white">EXAVALU</p>
          <p className="mt-1 text-[10px] font-medium uppercase tracking-[0.14em] text-ink-400">Data Cleaning Studio</p>
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function useStepState(page: Page): { done: boolean; locked: string | null; busy: boolean } {
  const flow = useWorkflow();
  const configured = flow.files.length > 0 && flow.files.every((entry) => entry.selected.length > 0);
  if (page === "configuration") return { done: configured, locked: null, busy: false };
  if (page === "run")
    return {
      done: (flow.batch?.status === "succeeded" || flow.batch?.status === "partial") && !flow.stale,
      locked: flow.files.length ? null : flow.runBlockedReason,
      busy: flow.running,
    };
  return { done: false, locked: flow.reviewBlockedReason, busy: false };
}

function NavItem({ page, active, compact, onNavigate }: { page: Page; active: boolean; compact: boolean; onNavigate: (page: Page) => void }) {
  const meta = PAGE_META[page];
  const { done, locked, busy } = useStepState(page);
  const button = (
    <button
      type="button"
      onClick={() => !locked && onNavigate(page)}
      aria-current={active ? "page" : undefined}
      aria-disabled={locked ? true : undefined}
      className={clsx(
        "group relative flex w-full items-center gap-3 rounded-md px-3 py-2.5 text-left text-body transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500",
        active ? "bg-nav-raised text-white" : locked ? "cursor-not-allowed text-ink-500" : "text-ink-300 hover:bg-nav-raised/60 hover:text-white",
        compact && "justify-center px-0",
      )}
    >
      {active && <span className="absolute inset-y-1.5 left-0 w-[3px] rounded-r bg-brand-500" aria-hidden />}
      <span className={clsx("flex shrink-0 [&>svg]:h-[18px] [&>svg]:w-[18px]", active && "text-brand-300")} aria-hidden>
        {meta.icon}
      </span>
      {!compact && (
        <>
          <span className="num w-5 text-caption text-ink-500">{String(meta.step).padStart(2, "0")}</span>
          <span className="flex-1 font-medium">{meta.label}</span>
          {busy ? (
            <Loader2 className="h-4 w-4 animate-spin text-brand-300" aria-label="Running" />
          ) : locked ? (
            <Lock className="h-3.5 w-3.5 text-ink-500" aria-label="Locked" />
          ) : done ? (
            <span className="flex h-4 w-4 items-center justify-center rounded-full bg-emerald-500/20 text-emerald-400" aria-label="Complete">
              <Check className="h-3 w-3" strokeWidth={3} />
            </span>
          ) : null}
        </>
      )}
    </button>
  );
  const tip = compact ? `${meta.label}${locked ? ` — ${locked}` : ""}` : locked;
  return tip ? (
    <Tooltip content={tip} side={compact ? "top" : "bottom"} className="w-full">
      {button}
    </Tooltip>
  ) : (
    button
  );
}

function WorkbookCard({ compact }: { compact: boolean }) {
  const flow = useWorkflow();
  const [confirm, setConfirm] = useState(false);
  if (!flow.files.length || compact) return null;
  const shown = flow.files.slice(0, 4);
  const hidden = flow.files.length - shown.length;
  const size = flow.files.reduce((sum, entry) => sum + entry.workbook.size, 0);
  const status = flow.running
    ? <Badge tone="info" dot>Cleaning · {Math.round((flow.batch?.progress ?? 0) * 100)}%</Badge>
    : flow.stale || !flow.batch
      ? <Badge tone="neutral" dot>Uploaded</Badge>
      : flow.batch.status === "succeeded"
        ? <Badge tone="success" dot>Cleaned</Badge>
        : flow.batch.status === "partial"
          ? <Badge tone="warning" dot>Partly cleaned</Badge>
          : flow.batch.status === "failed"
            ? <Badge tone="danger" dot>Failed</Badge>
            : <Badge tone="neutral" dot>Cancelled</Badge>;
  const dot = (id: string) => {
    const job = flow.stale && !flow.running ? null : flow.jobFor(id);
    return job?.status === "succeeded"
      ? "bg-emerald-400"
      : job?.status === "failed"
        ? "bg-brand-500"
        : job?.status === "running"
          ? "bg-sky-400 animate-pulse"
          : "bg-ink-500";
  };
  return (
    <div className="rounded-lg border border-nav-line bg-nav-raised p-3">
      <div className="flex items-start gap-2.5">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded bg-emerald-600 text-white" aria-hidden>
          <FileSpreadsheet className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] font-medium text-white">
            {flow.files.length === 1 ? flow.files[0].workbook.filename : plural(flow.files.length, "file")}
          </p>
          <p className="num text-caption text-ink-400">
            {formatBytes(size)} · {plural(flow.totalSheets, "sheet")}
          </p>
        </div>
        <button
          type="button"
          aria-label="Remove all files"
          title="Remove all files"
          disabled={flow.running}
          onClick={() => setConfirm(true)}
          className="rounded p-1 text-ink-400 hover:bg-nav-line hover:text-white disabled:opacity-40"
        >
          <Trash2 className="h-4 w-4" />
        </button>
      </div>
      {flow.files.length > 1 && (
        <ul className="mt-2.5 space-y-1">
          {shown.map((entry) => (
            <li key={entry.workbook.id} className="flex items-center gap-2 text-caption text-ink-300">
              <span className={clsx("h-1.5 w-1.5 shrink-0 rounded-full", dot(entry.workbook.id))} aria-hidden />
              <span className="truncate" title={entry.workbook.filename}>{entry.workbook.filename}</span>
            </li>
          ))}
          {hidden > 0 && <li className="pl-3.5 text-caption text-ink-500">+{hidden} more</li>}
        </ul>
      )}
      <div className="mt-2.5">{status}</div>
      <Modal
        open={confirm}
        onClose={() => setConfirm(false)}
        title={flow.files.length === 1 ? "Remove this workbook?" : `Remove all ${flow.files.length} files?`}
        description="The files and any cleaning results for them will be removed from this workspace. Files you already downloaded are not affected."
        footer={
          <>
            <Button onClick={() => setConfirm(false)}>Keep files</Button>
            <Button
              variant="primary"
              icon={<Trash2 />}
              onClick={async () => {
                setConfirm(false);
                await flow.removeAll();
              }}
            >
              Remove
            </Button>
          </>
        }
      />
    </div>
  );
}

function SidebarContent({ page, compact, onNavigate, onToggle }: { page: Page; compact: boolean; onNavigate: (page: Page) => void; onToggle?: () => void }) {
  const flow = useWorkflow();
  return (
    <nav aria-label="Workflow" className="flex h-full flex-col bg-nav px-3 py-5 text-ink-300">
      <div className={clsx("mb-8 flex items-center", compact ? "justify-center" : "justify-between px-3")}>
        <Logo compact={compact} />
      </div>
      {!compact && <p className="label-caps mb-2 px-3 !text-ink-500">Workflow</p>}
      <ol className="space-y-1">
        {(Object.keys(PAGE_META) as Page[]).map((key) => (
          <li key={key}>
            <NavItem page={key} active={key === page} compact={compact} onNavigate={onNavigate} />
          </li>
        ))}
      </ol>
      <div className="my-6 border-t border-nav-line" />
      {!compact && flow.files.length > 0 && (
        <p className="label-caps mb-2 px-3 !text-ink-500">{flow.files.length > 1 ? "Workbooks" : "Workbook"}</p>
      )}
      <WorkbookCard compact={compact} />
      <div className="mt-auto flex items-center justify-between gap-2 px-2 pt-6 text-caption text-ink-500">
        {!compact && <span>v1.0.0 · Enterprise workspace</span>}
        {onToggle && (
          <button
            type="button"
            onClick={onToggle}
            aria-label={compact ? "Expand sidebar" : "Collapse sidebar"}
            title={compact ? "Expand sidebar" : "Collapse sidebar"}
            className={clsx("hidden rounded p-1.5 hover:bg-nav-raised hover:text-white lg:inline-flex", compact && "mx-auto")}
          >
            {compact ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
          </button>
        )}
      </div>
    </nav>
  );
}

export function AppShell({ page, onNavigate, children }: { page: Page; onNavigate: (page: Page) => void; children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const flow = useWorkflow();

  return (
    <div className="flex min-h-screen">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[70] focus:rounded focus:bg-white focus:px-4 focus:py-2 focus:shadow-pop">
        Skip to content
      </a>
      {/* Desktop: full sidebar, collapsible. Tablet: icon rail. Mobile: drawer. */}
      <aside
        className={clsx(
          "sticky top-0 hidden h-screen shrink-0 transition-[width] duration-200 md:block",
          collapsed ? "w-[72px]" : "w-[72px] lg:w-[264px]",
        )}
      >
        <div className="hidden h-full lg:block">
          <SidebarContent page={page} compact={collapsed} onNavigate={onNavigate} onToggle={() => setCollapsed(!collapsed)} />
        </div>
        <div className="h-full lg:hidden">
          <SidebarContent page={page} compact onNavigate={onNavigate} />
        </div>
      </aside>
      <Drawer open={drawer} onClose={() => setDrawer(false)} label="Navigation">
        <SidebarContent
          page={page}
          compact={false}
          onNavigate={(next) => {
            setDrawer(false);
            onNavigate(next);
          }}
        />
      </Drawer>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-ink-200 bg-white/95 px-4 backdrop-blur md:px-8">
          <button
            type="button"
            className="rounded p-1.5 text-ink-600 hover:bg-ink-100 md:hidden"
            aria-label="Open navigation"
            onClick={() => setDrawer(true)}
          >
            <Menu className="h-5 w-5" />
          </button>
          <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-2 text-body text-ink-500">
            <button type="button" aria-label="Home" onClick={() => onNavigate("configuration")} className="rounded p-1 hover:bg-ink-100 hover:text-ink-800">
              <Home className="h-4 w-4" />
            </button>
            <ChevronRight className="h-3.5 w-3.5 text-ink-300" aria-hidden />
            <span aria-current="page" className="truncate font-medium text-ink-800">
              {PAGE_META[page].label}
            </span>
          </nav>
          <div className="ml-auto flex items-center gap-3">
            {flow.running && flow.batch && (
              <button
                type="button"
                onClick={() => onNavigate("run")}
                className="flex items-center gap-2 rounded-full border border-sky-200 bg-sky-50 px-3 py-1 text-caption font-medium text-sky-700 hover:bg-sky-100"
              >
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                <span className="hidden sm:inline">
                  Cleaning{flow.batch.files_total > 1 ? ` · ${flow.batch.files_done}/${flow.batch.files_total} files done` : ""} ·
                </span>
                <span className="num">{Math.round(flow.batch.progress * 100)}%</span>
              </button>
            )}
          </div>
        </header>
        <main id="main" tabIndex={-1} className="flex-1 px-4 py-6 focus:outline-none md:px-8 md:py-8">
          <div key={page} className="mx-auto w-full max-w-[1320px] animate-fade-in">
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

export function PageHeader({
  page,
  title,
  description,
  actions,
}: {
  page: Page;
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  const meta = PAGE_META[page];
  return (
    <div className="mb-6 flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
      <div className="flex items-start gap-4">
        <div className="hidden h-12 w-12 shrink-0 items-center justify-center rounded-lg bg-brand-50 text-brand-600 sm:flex [&>svg]:h-6 [&>svg]:w-6" aria-hidden>
          {meta.icon}
        </div>
        <div>
          <h1 className="text-page text-ink-900">{title}</h1>
          <p className="mt-1 text-body text-ink-600">{description}</p>
        </div>
      </div>
      <div className="flex items-center gap-4">
        {actions}
        <WorkflowStepper current={page} />
      </div>
    </div>
  );
}

function WorkflowStepper({ current }: { current: Page }) {
  const pages = Object.keys(PAGE_META) as Page[];
  const index = pages.indexOf(current);
  return (
    <ol className="hidden shrink-0 items-center xl:flex" aria-label="Workflow progress">
      {pages.map((page, i) => {
        const state = i < index ? "done" : i === index ? "current" : "upcoming";
        return (
          <li key={page} className="flex items-center">
            <div className="flex flex-col items-center gap-1.5">
              <span
                aria-current={state === "current" ? "step" : undefined}
                className={clsx(
                  "num flex h-7 w-7 items-center justify-center rounded-full text-caption font-semibold",
                  state === "current" && "bg-brand-600 text-white ring-4 ring-brand-100",
                  state === "done" && "bg-emerald-600 text-white",
                  state === "upcoming" && "border border-ink-300 bg-white text-ink-500",
                )}
              >
                {state === "done" ? <Check className="h-3.5 w-3.5" strokeWidth={3} /> : i + 1}
              </span>
              <span className={clsx("whitespace-nowrap text-caption", state === "current" ? "font-semibold text-ink-900" : "text-ink-500")}>
                {PAGE_META[page].label}
              </span>
            </div>
            {i < pages.length - 1 && (
              <span className={clsx("mx-2 mb-5 h-px w-12", i < index ? "bg-emerald-500" : "bg-ink-300")} aria-hidden />
            )}
          </li>
        );
      })}
    </ol>
  );
}

export function SectionCard({
  icon,
  title,
  description,
  actions,
  children,
  className,
  step,
  id,
}: {
  id?: string;
  icon?: ReactNode;
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  step?: number;
}) {
  return (
    <section id={id} tabIndex={id ? -1 : undefined} className={clsx("card focus:outline-none", className)} aria-label={title}>
      <header className="flex flex-col gap-3 px-5 pb-4 pt-5 sm:flex-row sm:items-start sm:justify-between md:px-6">
        <div className="flex items-start gap-3">
          {icon && <span className="mt-0.5 text-ink-500 [&>svg]:h-5 [&>svg]:w-5" aria-hidden>{icon}</span>}
          <div>
            <h2 className="text-section text-ink-900">
              {step !== undefined && <span className="num mr-1.5 text-ink-400">{step}.</span>}
              {title}
            </h2>
            {description && <p className="mt-0.5 text-body text-ink-600">{description}</p>}
          </div>
        </div>
        {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
      </header>
      <div className="px-5 pb-5 md:px-6 md:pb-6">{children}</div>
    </section>
  );
}
