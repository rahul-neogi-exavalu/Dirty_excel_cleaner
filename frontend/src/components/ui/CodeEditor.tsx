import { json, jsonParseLinter } from "@codemirror/lang-json";
import { PostgreSQL, sql } from "@codemirror/lang-sql";
import { linter, lintGutter, setDiagnostics, type Diagnostic } from "@codemirror/lint";
import CodeMirror, { EditorView } from "@uiw/react-codemirror";
import clsx from "clsx";
import { Check, CheckCircle2, CircleAlert, Copy, Loader2, Maximize2, Minimize2 } from "lucide-react";
import { forwardRef, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

export type CodeLanguage = "json" | "sql";

/** A problem in the text: what, and where (character offsets) when known. */
export interface CodeError {
  message: string;
  start?: number | null;
  end?: number | null;
}

/** Where JSON.parse stopped, as the reviewer reads it: "Line 5, column 3: Unexpected token". */
function jsonError(text: string): string | null {
  if (!text.trim()) return "Empty: the spec must be a JSON object.";
  try {
    JSON.parse(text);
    return null;
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    const at = /position (\d+)/.exec(message);
    const clean = message.replace(/^JSON\.parse: /, "").replace(/ in JSON at position \d+.*$/, "").replace(/ at position \d+.*$/, "");
    return at ? `${lineColumn(text, Number(at[1]))}: ${clean}` : clean;
  }
}

function lineColumn(text: string, offset: number): string {
  const before = text.slice(0, offset);
  return `Line ${before.split("\n").length}, column ${before.length - before.lastIndexOf("\n")}`;
}

/** Copy text, falling back to a hidden textarea where the clipboard API is not allowed. */
async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const done = document.execCommand("copy");
    area.remove();
    return done;
  }
}

const editorTheme = EditorView.theme({
  "&": { fontSize: "12.5px", backgroundColor: "#fff" },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", lineHeight: "1.6" },
  ".cm-gutters": { backgroundColor: "#F7F8FA", borderRight: "1px solid #E4E7EC", color: "#98A2B3" },
  ".cm-activeLineGutter": { backgroundColor: "#EFF4FE" },
  ".cm-activeLine": { backgroundColor: "#F8FAFF" },
});

/**
 * A code editor for a rule's JSON or SQL: coloured as in a code editor, checked as you
 * type (a red mark at the place, and the problem above), with copy and an expanded view
 * that fills the window. JSON is checked here; SQL is checked by the caller (``error``,
 * ``checking``), which knows what the rules accept.
 */
export function CodeEditor({
  value,
  onChange,
  label,
  language = "json",
  error,
  checking = false,
  toolbarStart,
  height = 260,
}: {
  value: string;
  onChange: (value: string) => void;
  /** Names the editor for screen readers and the expanded view's heading. */
  label: string;
  language?: CodeLanguage;
  /** SQL only: the caller's verdict on the text (null: it reads). */
  error?: CodeError | null;
  /** SQL only: a verdict is on its way. */
  checking?: boolean;
  /** Shown at the start of the toolbar, before the status (e.g. a JSON | SQL switch). */
  toolbarStart?: ReactNode;
  height?: number;
}) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState<"yes" | "failed" | null>(null);
  const view = useRef<EditorView | null>(null);
  const problem: CodeError | null = useMemo(() => {
    if (language === "json") {
      const message = jsonError(value);
      return message ? { message } : null;
    }
    return error ?? null;
  }, [language, value, error]);
  const shown = problem
    ? problem.start != null && language === "sql" ? `${lineColumn(value, problem.start)}: ${problem.message}` : problem.message
    : null;
  const extensions = useMemo(
    () => [
      language === "json" ? json() : sql({ dialect: PostgreSQL, upperCaseKeywords: true }),
      ...(language === "json" ? [linter(jsonParseLinter(), { delay: 250 })] : []),
      lintGutter(), EditorView.lineWrapping, editorTheme,
    ],
    [language],
  );
  const titleId = useId();
  const minimise = useRef<HTMLButtonElement>(null);

  // SQL: mark the caller's problem in the text, where it is.
  useEffect(() => {
    const current = view.current;
    if (!current || language !== "sql") return;
    const length = current.state.doc.length;
    const diagnostics: Diagnostic[] = problem
      ? [{
          from: Math.min(problem.start ?? 0, length),
          to: Math.min(Math.max(problem.end ?? problem.start ?? length, (problem.start ?? 0) + 1), length),
          severity: "error",
          message: problem.message,
        }]
      : [];
    current.dispatch(setDiagnostics(current.state, diagnostics));
  }, [problem, language, expanded, value]);

  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(null), 1800);
    return () => clearTimeout(timer);
  }, [copied]);

  useEffect(() => {
    if (!expanded) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setExpanded(false);
    };
    document.addEventListener("keydown", onKey);
    minimise.current?.focus();
    return () => {
      document.body.style.overflow = previous;
      document.removeEventListener("keydown", onKey);
    };
  }, [expanded]);

  const okText = language === "json" ? "Valid JSON" : "Valid SQL";
  const toolbar = (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-ink-200 bg-ink-50 px-2.5 py-1.5">
      <div className="flex min-w-0 flex-1 items-center gap-3">
        {toolbarStart}
        <span role="status" className={clsx("flex min-w-0 items-center gap-1.5 text-caption",
          checking ? "text-ink-500" : shown ? "text-danger-700" : "text-emerald-700")}>
          {checking ? <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" aria-hidden />
            : shown ? <CircleAlert className="h-3.5 w-3.5 shrink-0" aria-hidden />
            : <CheckCircle2 className="h-3.5 w-3.5 shrink-0" aria-hidden />}
          <span className="truncate" title={shown ?? undefined}>{checking ? "Checking…" : shown ?? okText}</span>
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <ToolButton
          label={copied === "yes" ? "Copied" : copied === "failed" ? "Copy failed" : "Copy"}
          icon={copied === "yes" ? <Check /> : <Copy />}
          onClick={async () => setCopied((await copyText(value)) ? "yes" : "failed")}
        />
        {expanded ? (
          <ToolButton ref={minimise} label="Minimise" icon={<Minimize2 />} onClick={() => setExpanded(false)} />
        ) : (
          <ToolButton label="Expand" icon={<Maximize2 />} onClick={() => setExpanded(true)} />
        )}
      </div>
    </div>
  );

  const editor = (fill: boolean) => (
    <CodeMirror
      value={value}
      onChange={onChange}
      extensions={extensions}
      theme="light"
      height={fill ? "100%" : `${height}px`}
      className={clsx(fill && "min-h-0 flex-1 [&_.cm-editor]:h-full")}
      basicSetup={{ foldGutter: true, bracketMatching: true, closeBrackets: true, highlightActiveLine: true, autocompletion: false }}
      onCreateEditor={(created) => { view.current = created; }}
      aria-label={label}
    />
  );

  return (
    <>
      <div className={clsx("overflow-hidden rounded-lg border", shown && !checking ? "border-danger-300" : "border-ink-200")}>
        {toolbar}
        {expanded ? (
          <p className="px-3 py-6 text-center text-caption text-ink-500">Editing in the expanded view.</p>
        ) : (
          editor(false)
        )}
      </div>
      {expanded &&
        createPortal(
          <div className="fixed inset-0 z-50 flex p-4 sm:p-8">
            <div className="absolute inset-0 animate-fade-in bg-ink-900/40" onClick={() => setExpanded(false)} aria-hidden />
            <div role="dialog" aria-modal="true" aria-labelledby={titleId}
              className="relative flex min-h-0 w-full animate-scale-in flex-col overflow-hidden rounded-lg bg-white shadow-pop">
              <h2 id={titleId} className="shrink-0 border-b border-ink-200 px-4 py-2.5 text-card text-ink-900">{label}</h2>
              {toolbar}
              {editor(true)}
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}

const ToolButton = forwardRef<HTMLButtonElement, { label: string; icon: ReactNode; onClick: () => void }>(
  function ToolButton({ label, icon, onClick }, ref) {
    return (
      <button ref={ref} type="button" onClick={onClick} title={label}
        className="inline-flex h-7 items-center gap-1 rounded px-2 text-caption font-medium text-ink-600 hover:bg-ink-100 hover:text-ink-900 focus-visible:outline-none focus-visible:shadow-focus [&>svg]:h-3.5 [&>svg]:w-3.5">
        {icon}
        {label}
      </button>
    );
  },
);
