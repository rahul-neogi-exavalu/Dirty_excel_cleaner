import clsx from "clsx";
import {
  BadgeCheck,
  Check,
  ChevronRight,
  ClipboardCheck,
  DatabaseZap,
  Layers3,
  FileSpreadsheet,
  History,
  Home,
  Loader2,
  Lock,
  LogOut,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  PlayCircle,
  ScanEye,
  SlidersHorizontal,
  Trash2,
  UsersRound,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { formatBytes, initials, plural } from "../../lib/format";
import { useAuth } from "../../state/auth";
import { useWorkflow, type Page, type StepPage } from "../../state/workflow";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Drawer, Modal, Tooltip } from "../ui/Overlay";

export const PAGE_META: Record<StepPage, { step: number; label: string; icon: ReactNode }> = {
  configuration: { step: 1, label: "Configure", icon: <SlidersHorizontal /> },
  preview: { step: 2, label: "Preview", icon: <ScanEye /> },
  run: { step: 3, label: "Run", icon: <PlayCircle /> },
  results: { step: 4, label: "Results", icon: <ClipboardCheck /> },
  validate: { step: 5, label: "Validate", icon: <BadgeCheck /> },
  ingest: { step: 6, label: "Ingest", icon: <DatabaseZap /> },
  silver: { step: 7, label: "Silver", icon: <Layers3 /> },
};

/** Pages beside the workflow: not steps, so no number and no place in the stepper. */
export const WORKSPACE_META: Record<Exclude<Page, StepPage>, { label: string; icon: ReactNode; admin: boolean }> = {
  history: { label: "History", icon: <History />, admin: false },
  users: { label: "Users", icon: <UsersRound />, admin: true },
};

const isStep = (page: Page): page is StepPage => page in PAGE_META;

export const pageLabel = (page: Page) => (isStep(page) ? PAGE_META[page] : WORKSPACE_META[page]).label;

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

/** The mark on its white tile with the wordmark; `light` for use on a white surface. */
export function Logo({ compact, light }: { compact?: boolean; light?: boolean }) {
  return (
    <div className="flex items-center gap-3">
      <span className={clsx("flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-white p-1.5", light && "ring-1 ring-ink-200")}>
        <ExavaluMark className="h-full w-full" />
      </span>
      {!compact && (
        <div className="leading-none">
          <p className={clsx("font-display text-[15px] font-semibold tracking-[0.08em]", light ? "text-ink-900" : "text-white")}>EXAVALU</p>
          <p className={clsx("mt-1 text-[11px] font-medium", light ? "text-ink-500" : "text-white/50")}>Data Processing Studio</p>
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function useStepState(page: StepPage): { done: boolean; locked: string | null; busy: boolean } {
  const flow = useWorkflow();
  const configured = flow.files.length > 0 && flow.files.every((entry) => entry.selected.length > 0);
  if (page === "configuration") return { done: configured, locked: null, busy: false };
  // Preview reads the uploaded files themselves: done once every one has been looked at.
  if (page === "preview")
    return {
      done: flow.files.length > 0 && flow.files.every((entry) => flow.previewed.has(entry.workbook.id)),
      locked: flow.files.length ? null : "Upload a workbook first.",
      busy: false,
    };
  if (page === "run")
    return {
      done: (flow.batch?.status === "succeeded" || flow.batch?.status === "partial") && !flow.stale,
      locked: flow.files.length ? null : flow.runBlockedReason,
      busy: flow.running,
    };
  // Every table of the batch staged (or recorded as rejected) in the control table.
  if (page === "validate") {
    const progress = flow.validation;
    return {
      done: Boolean(progress && progress.total > 0 && progress.staged >= progress.total && !flow.stale),
      locked: flow.reviewBlockedReason,
      busy: false,
    };
  }
  // Ingest and Silver read the control table and the bronze loads, not this session's files.
  if (page === "silver" || page === "ingest") return { done: false, locked: null, busy: false };
  // Results and Ingest both need at least one cleaned file.
  return { done: false, locked: flow.reviewBlockedReason, busy: false };
}

function NavItem({ page, active, compact, onNavigate }: { page: StepPage; active: boolean; compact: boolean; onNavigate: (page: Page) => void }) {
  const meta = PAGE_META[page];
  const { done, locked, busy } = useStepState(page);
  const button = (
    <button
      type="button"
      onClick={() => !locked && onNavigate(page)}
      aria-current={active ? "page" : undefined}
      aria-disabled={locked ? true : undefined}
      className={clsx(
        "group relative flex w-full items-center gap-3 rounded-lg px-3 py-3 text-left text-[15px] transition-colors [@media(max-height:820px)]:py-2",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400",
        active
          ? "bg-white/[0.08] text-white ring-1 ring-inset ring-white/[0.06]"
          : locked
            ? "cursor-not-allowed text-white/40"
            : "text-white/70 hover:bg-white/[0.05] hover:text-white",
        compact && "justify-center px-0",
      )}
    >
      {active && <span className="absolute inset-y-2.5 left-0 w-[2px] rounded-r bg-white" aria-hidden />}
      <span className={clsx("flex shrink-0 [&>svg]:h-[18px] [&>svg]:w-[18px]", active && "text-white")} aria-hidden>
        {meta.icon}
      </span>
      {!compact && (
        <>
          <span className="num w-5 font-display text-caption text-white/35">{String(meta.step).padStart(2, "0")}</span>
          <span className="flex-1 font-medium">{meta.label}</span>
          {busy ? (
            <Loader2 className="h-4 w-4 animate-spin text-white/70" aria-label="Running" />
          ) : locked ? (
            <Lock className="h-3.5 w-3.5 text-white/40" aria-label="Locked" />
          ) : done ? (
            <span className="flex h-5 w-5 items-center justify-center rounded-full bg-white/10 text-white/80" aria-label="Complete">
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

function WorkspaceItem({
  page,
  active,
  compact,
  onNavigate,
}: {
  page: Exclude<Page, StepPage>;
  active: boolean;
  compact: boolean;
  onNavigate: (page: Page) => void;
}) {
  const meta = WORKSPACE_META[page];
  const button = (
    <button
      type="button"
      onClick={() => onNavigate(page)}
      aria-current={active ? "page" : undefined}
      className={clsx(
        "relative flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left text-[14px] transition-colors [@media(max-height:820px)]:py-2",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400",
        active ? "bg-white/[0.08] text-white ring-1 ring-inset ring-white/[0.06]" : "text-white/60 hover:bg-white/[0.05] hover:text-white",
        compact && "justify-center px-0",
      )}
    >
      {active && <span className="absolute inset-y-2 left-0 w-[2px] rounded-r bg-white" aria-hidden />}
      <span className="flex shrink-0 [&>svg]:h-[17px] [&>svg]:w-[17px]" aria-hidden>
        {meta.icon}
      </span>
      {!compact && <span className="flex-1 font-medium">{meta.label}</span>}
    </button>
  );
  return compact ? (
    <Tooltip content={meta.label} side="top" className="w-full">
      {button}
    </Tooltip>
  ) : (
    button
  );
}

/** Who is signed in, and the way out. */
function UserChip({ compact }: { compact: boolean }) {
  const { user, signOut } = useAuth();
  const [confirm, setConfirm] = useState(false);
  const flow = useWorkflow();
  if (!user) return null;
  const leave = () => (flow.running ? setConfirm(true) : void signOut());
  const avatar = (
    <span
      className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-white/[0.1] font-display text-[12px] font-semibold text-white"
      aria-hidden
    >
      {initials(user.user_name) || "?"}
    </span>
  );
  return (
    <>
      {compact ? (
        <Tooltip content={`${user.user_name} · Sign out`} side="top" className="w-full">
          <button
            type="button"
            onClick={leave}
            aria-label={`Sign out ${user.user_name}`}
            className="mx-auto flex rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400"
          >
            {avatar}
          </button>
        </Tooltip>
      ) : (
        <div className="flex min-w-0 flex-1 items-center gap-2.5">
          {avatar}
          <div className="min-w-0 flex-1 leading-tight">
            <p className="truncate text-[13px] font-medium text-white" title={user.user_email_id}>
              {user.user_name}
            </p>
            <p className="text-caption text-white/45">{user.is_admin ? "Admin" : "Member"}</p>
          </div>
          <button
            type="button"
            onClick={leave}
            aria-label="Sign out"
            title="Sign out"
            className="rounded-md p-1.5 text-white/50 transition-colors hover:bg-white/10 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400"
          >
            <LogOut className="h-4 w-4" />
          </button>
        </div>
      )}
      <Modal
        open={confirm}
        onClose={() => setConfirm(false)}
        title="Sign out while cleaning?"
        description="The job keeps running on the server and stays in History. This browser forgets the files."
        footer={
          <>
            <Button onClick={() => setConfirm(false)}>Stay signed in</Button>
            <Button variant="primary" icon={<LogOut />} onClick={() => void signOut()}>
              Sign out
            </Button>
          </>
        }
      />
    </>
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
        ? "bg-danger-500"
        : job?.status === "running"
          ? "bg-sky-400 animate-pulse"
          : "bg-white/30";
  };
  return (
    <div className="rounded-xl border border-white/[0.07] bg-white/[0.04] p-3.5">
      <div className="flex items-start gap-2.5">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-white/[0.08] text-white/70" aria-hidden>
          <FileSpreadsheet className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] font-medium text-white">
            {flow.files.length === 1 ? flow.files[0].workbook.filename : plural(flow.files.length, "file")}
          </p>
          <p className="num text-caption text-white/50">
            {formatBytes(size)} · {plural(flow.totalSheets, "sheet")}
          </p>
        </div>
        <button
          type="button"
          aria-label="Remove all files"
          title="Remove all files"
          disabled={flow.running}
          onClick={() => setConfirm(true)}
          className="rounded-md p-1 text-white/50 hover:bg-white/10 hover:text-white disabled:opacity-40"
        >
          <Trash2 className="h-4 w-4" />
        </button>
      </div>
      {flow.files.length > 1 && (
        <ul className="mt-2.5 space-y-1">
          {shown.map((entry) => (
            <li key={entry.workbook.id} className="flex items-center gap-2 text-caption text-white/65">
              <span className={clsx("h-1.5 w-1.5 shrink-0 rounded-full", dot(entry.workbook.id))} aria-hidden />
              <span className="truncate" title={entry.workbook.filename}>{entry.workbook.filename}</span>
            </li>
          ))}
          {hidden > 0 && <li className="pl-3.5 text-caption text-white/40">+{hidden} more</li>}
        </ul>
      )}
      <div className="mt-2.5">{status}</div>
      <Modal
        open={confirm}
        onClose={() => setConfirm(false)}
        title={flow.files.length === 1 ? "Remove this workbook?" : `Remove all ${flow.files.length} files?`}
        description="Files and their results leave this workspace. Downloads are unaffected."
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
  const { user } = useAuth();
  const workspace = (Object.keys(WORKSPACE_META) as Exclude<Page, StepPage>[]).filter(
    (key) => !WORKSPACE_META[key].admin || user?.is_admin,
  );
  return (
    <nav aria-label="Workflow" className="flex h-full flex-col border-r border-nav-line bg-nav py-6 text-white/70 [@media(max-height:820px)]:py-4">
      <div className={clsx("mb-8 flex shrink-0 items-center px-4 [@media(max-height:820px)]:mb-5", compact ? "justify-center" : "justify-between px-6")}>
        <Logo compact={compact} />
      </div>
      {/* Scrolls on its own when the screen is short, so the account footer stays in view. */}
      <div className="min-h-0 flex-1 overflow-y-auto px-4 scroll-thin [scrollbar-color:rgba(255,255,255,0.15)_transparent]">
      {!compact && <p className="mb-3 px-3 text-caption font-medium text-white/40 [@media(max-height:820px)]:mb-2">Workflow</p>}
      <ol className="space-y-1.5">
        {(Object.keys(PAGE_META) as StepPage[]).map((key) => (
          <li key={key}>
            <NavItem page={key} active={key === page} compact={compact} onNavigate={onNavigate} />
          </li>
        ))}
      </ol>
      {!compact && <p className="mb-2 mt-7 px-3 text-caption font-medium text-white/40 [@media(max-height:820px)]:mt-5">Workspace</p>}
      <ul className={clsx("space-y-1", compact && "mt-6")}>
        {workspace.map((key) => (
          <li key={key}>
            <WorkspaceItem page={key} active={key === page} compact={compact} onNavigate={onNavigate} />
          </li>
        ))}
      </ul>
      <div className="my-6 border-t border-white/[0.07] [@media(max-height:820px)]:my-4" />
      {!compact && flow.files.length > 0 && (
        <p className="mb-3 px-3 text-caption font-medium text-white/40">{flow.files.length > 1 ? "Workbooks" : "Workbook"}</p>
      )}
      <WorkbookCard compact={compact} />
      </div>
      <div
        className={clsx(
          "mx-4 mt-4 flex shrink-0 items-center gap-2 border-t border-white/[0.07] px-1 pt-4 text-caption text-white/40",
          compact ? "flex-col" : "justify-between",
        )}
      >
        <UserChip compact={compact} />
        {onToggle && (
          <button
            type="button"
            onClick={onToggle}
            aria-label={compact ? "Expand sidebar" : "Collapse sidebar"}
            title={compact ? "Expand sidebar" : "Collapse sidebar"}
            className={clsx("hidden shrink-0 rounded-md p-1.5 hover:bg-white/10 hover:text-white lg:inline-flex", compact && "mx-auto")}
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
          collapsed ? "w-[76px]" : "w-[76px] lg:w-[272px]",
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
        <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-ink-200/80 bg-white/85 px-4 backdrop-blur-md md:px-10">
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
              {pageLabel(page)}
            </span>
          </nav>
          <div className="ml-auto flex items-center gap-3">
            {flow.running && flow.batch && (
              <button
                type="button"
                onClick={() => onNavigate("run")}
                className="flex items-center gap-2 rounded-full border border-brand-200 bg-brand-50 px-3 py-1 text-caption font-medium text-brand-700 hover:bg-brand-100"
              >
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                <span className="hidden sm:inline">
                  Cleaning{flow.batch.files_total > 1 ? ` · ${flow.batch.files_done}/${flow.batch.files_total}` : ""} ·
                </span>
                <span className="num">{Math.round(flow.batch.progress * 100)}%</span>
              </button>
            )}
          </div>
        </header>
        <main id="main" tabIndex={-1} className="flex-1 px-4 py-6 focus:outline-none md:px-8 md:py-8 xl:px-10 [@media(max-height:820px)]:md:py-6">
          <div key={page} className="mx-auto w-full max-w-[1320px] animate-fade-in 3xl:max-w-[1560px] 4xl:max-w-[1880px]">
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
  description?: string;
  actions?: ReactNode;
}) {
  const step = isStep(page) ? PAGE_META[page] : null;
  const meta = step ?? WORKSPACE_META[page as Exclude<Page, StepPage>];
  return (
    <div className="mb-6 flex flex-col gap-4 border-b border-ink-200/80 pb-6 lg:flex-row lg:items-end lg:justify-between [@media(max-height:820px)]:mb-5 [@media(max-height:820px)]:pb-4">
      <div className="min-w-0">
        <p className="eyebrow flex items-center gap-2">
          <span className="flex [&>svg]:h-3.5 [&>svg]:w-3.5" aria-hidden>{meta.icon}</span>
          {step ? `Step ${String(step.step).padStart(2, "0")}` : "Workspace"}
        </p>
        <h1 className="mt-1.5 text-[24px] font-semibold leading-8 text-ink-900 sm:text-page">{title}</h1>
        {description && <p className="mt-1 max-w-xl text-body text-ink-500">{description}</p>}
      </div>
      <div className="flex items-center gap-4">
        {actions}
        {step && <WorkflowStepper current={page as StepPage} />}
      </div>
    </div>
  );
}

function WorkflowStepper({ current }: { current: StepPage }) {
  const pages = Object.keys(PAGE_META) as StepPage[];
  return (
    <ol className="hidden shrink-0 items-center xl:flex" aria-label="Workflow progress">
      {pages.map((page, i) => (
        <StepperItem key={page} page={page} index={i} current={page === current} last={i === pages.length - 1} />
      ))}
    </ol>
  );
}

/** One step: ticked only when its work is actually done, not because it comes earlier. */
function StepperItem({ page, index, current, last }: { page: StepPage; index: number; current: boolean; last: boolean }) {
  const { done } = useStepState(page);
  const state = current ? "current" : done ? "done" : "upcoming";
  return (
    <li className="flex items-center">
      <div className="flex flex-col items-center gap-1.5">
        <span
          aria-current={current ? "step" : undefined}
          className={clsx(
            "num flex h-8 w-8 items-center justify-center rounded-full font-display text-[13px] font-semibold transition-colors",
            state === "current" && "bg-brand-600 text-white ring-4 ring-brand-100",
            state === "done" && "bg-ink-800 text-white",
            state === "upcoming" && "border border-ink-200 bg-white text-ink-500",
          )}
        >
          {state === "done" ? <Check className="h-3.5 w-3.5" strokeWidth={3} aria-label="Complete" /> : index + 1}
        </span>
        <span className={clsx("whitespace-nowrap text-caption", current ? "font-semibold text-ink-900" : "text-ink-500")}>
          {PAGE_META[page].label}
        </span>
      </div>
      {!last && <span className={clsx("mx-2 mb-5 h-0.5 w-10 rounded-full", done ? "bg-ink-500" : "bg-ink-200")} aria-hidden />}
    </li>
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
      <header className="flex flex-col gap-3 border-b border-ink-100 px-5 py-4 sm:flex-row sm:items-center sm:justify-between md:px-6">
        <div className="flex items-center gap-3">
          {icon && (
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-ink-200 bg-ink-50 text-ink-600 [&>svg]:h-4 [&>svg]:w-4" aria-hidden>
              {icon}
            </span>
          )}
          <div>
            <h2 className="flex items-center gap-2 text-card text-ink-900">
              {title}
              {step !== undefined && <span className="num font-mono text-caption font-medium text-ink-400">{String(step).padStart(2, "0")}</span>}
            </h2>
            {description && <p className="text-caption text-ink-500">{description}</p>}
          </div>
        </div>
        {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
      </header>
      <div className="px-5 py-5 md:px-6">{children}</div>
    </section>
  );
}
