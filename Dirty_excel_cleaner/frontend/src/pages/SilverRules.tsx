import clsx from "clsx";
import { Check, FileCode2, Pencil, Plus, Trash2, Wand2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api/client";
import type { SilverQuality, SilverRule, SilverRuleKind, SilverRuleTarget } from "../api/types";
import { SectionCard } from "../components/layout/Layout";
import { Badge } from "../components/ui/Badge";
import { Button, IconButton } from "../components/ui/Button";
import { SearchInput, Switch } from "../components/ui/Controls";
import { Alert, EmptyState, Skeleton, useToast } from "../components/ui/Feedback";
import { Select, type SelectOption } from "../components/ui/Select";
import { plural } from "../lib/format";

const toError = (error: unknown) => (error instanceof ApiError ? error : new ApiError(0, { code: "unknown", message: String(error) }));

const KINDS: SelectOption<SilverRuleKind>[] = [
  { value: "filter", label: "Filter", description: "Keep or drop bronze rows" },
  { value: "date_format", label: "Date format", description: "Read a date column the file's way" },
  { value: "lookup", label: "Lookup", description: "Fill columns from a table keyed on a value" },
  { value: "translate", label: "Translate", description: "A source code to a business value" },
  { value: "derive", label: "Derive", description: "An expression over bronze and Silver values" },
  { value: "constant", label: "Constant", description: "A value the file never states" },
];
const TARGETS: SelectOption<SilverRuleTarget>[] = [
  { value: "transaction", label: "Transaction", description: "silver_transaction" },
  { value: "aggregate", label: "Aggregate", description: "silver_aggregate" },
  { value: "both", label: "Both" },
];
/** A starting spec for each kind, shown when a new rule's kind is chosen. */
const EXAMPLES: Record<SilverRuleKind, Record<string, unknown>> = {
  filter: { keep: { op: "eq", args: ["src:Status", "ISSUED"] } },
  date_format: { formats: ["dd/MM/yyyy"] },
  lookup: { source: "inline", key: "src:Agency", columns: { producer_billing_city: "city" }, entries: [{ key: "Agency name", city: "City" }] },
  translate: { source: "src:Tran", cases: [{ when: "RWL", then: true }], default: false },
  derive: { expr: { op: "div_safe", args: ["silver:gross_commission_amount", "silver:premium"] } },
  constant: { value: "" },
};
const COLUMN_KINDS: SilverRuleKind[] = ["date_format", "translate", "derive", "constant"];

const blank = (): SilverRule => ({
  profit_center: "", key: "", kind: "constant", target: "transaction", column: "", scope_bronze_table: null,
  scope_file_like: null, scope_sheet: null, seq: 100, spec: EXAMPLES.constant, enabled: true, note: null, source_ref: null,
});

const scopeOf = (rule: SilverRule) =>
  [rule.scope_file_like && `file ${rule.scope_file_like}`, rule.scope_sheet && `sheet ${rule.scope_sheet}`,
    rule.scope_bronze_table && `table ${rule.scope_bronze_table}`].filter(Boolean).join(" · ") || "Every file";

/** The Rules tab: every profit center's rules, to review, switch off, edit or add. */
export function RulesView() {
  const toast = useToast();
  const [rules, setRules] = useState<SilverRule[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<SilverRule | null>(null);

  const load = useCallback(() => {
    setError(null);
    api.silverRules().then(setRules).catch((err) => setError(toError(err)));
  }, []);
  useEffect(load, [load]);

  const needle = query.trim().toLowerCase();
  const visible = useMemo(() => (rules ?? []).filter((rule) => !needle || [
    rule.profit_center, rule.key, rule.kind, rule.column ?? "", rule.note ?? "", rule.scope_file_like ?? "", rule.scope_sheet ?? "",
  ].some((value) => value.toLowerCase().includes(needle))), [rules, needle]);
  const byPc = useMemo(() => {
    const groups = new Map<string, SilverRule[]>();
    for (const rule of visible) groups.set(rule.profit_center, [...(groups.get(rule.profit_center) ?? []), rule]);
    return [...groups.entries()];
  }, [visible]);

  const toggle = async (rule: SilverRule, enabled: boolean) => {
    try {
      const saved = await api.saveSilverRule({ ...rule, enabled });
      setRules((current) => current?.map((item) => (item.id === rule.id ? { ...item, ...saved } : item)) ?? null);
      toast({ severity: "success", title: enabled ? "Rule on" : "Rule off", description: "Applies from the next Silver run." });
    } catch (err) {
      toast({ severity: "error", title: "Not changed", description: toError(err).body.message });
    }
  };
  const remove = async (rule: SilverRule) => {
    if (rule.id == null || !window.confirm(`Delete ${rule.profit_center} ${rule.key}? Switching it off keeps it for later.`)) return;
    try {
      await api.deleteSilverRule(rule.id);
      setRules((current) => current?.filter((item) => item.id !== rule.id) ?? null);
      toast({ severity: "success", title: "Rule deleted", description: "Applies from the next Silver run." });
    } catch (err) {
      toast({ severity: "error", title: "Not deleted", description: toError(err).body.message });
    }
  };

  if (error) return <Alert tone="error" title="Rules not loaded" action={<Button size="sm" onClick={load}>Retry</Button>}>{error.body.message}</Alert>;
  if (!rules) return <div className="card space-y-2 p-6"><Skeleton className="h-10" /><Skeleton className="h-10" /></div>;

  return (
    <div className="space-y-4">
      {editing && (
        <RuleEditor
          rule={editing}
          onCancel={() => setEditing(null)}
          onSaved={(saved) => {
            setRules((current) => {
              const rest = (current ?? []).filter((item) => item.id !== saved.id);
              return [...rest, saved].sort((a, b) => a.profit_center.localeCompare(b.profit_center) || a.kind.localeCompare(b.kind) || a.seq - b.seq);
            });
            setEditing(null);
          }}
        />
      )}
      <SectionCard
        icon={<Wand2 />}
        title="Profit center rules"
        description="Logic one profit center's files need on the way to Silver: filters, date formats, lookups, translations, derived columns and constants."
        actions={
          <div className="flex items-center gap-2">
            <SearchInput value={query} onChange={setQuery} placeholder="Search" label="Search rules" className="sm:w-56" />
            <Button size="sm" icon={<Plus />} onClick={() => setEditing(blank())}>Add rule</Button>
          </div>
        }
      >
        <p className="mb-3 text-caption text-ink-500">
          {plural(visible.length, "rule")} in {plural(byPc.length, "profit center")}. A rule applies only to its profit center's loads, and only
          to the files and sheets its scope names. Changes apply from the next Silver run; a review started before a change must be started again.
        </p>
        {byPc.length ? (
          <div className="space-y-4">
            {byPc.map(([pc, items]) => (
              <div key={pc} className="rounded-lg border border-ink-200">
                <p className="border-b border-ink-200 bg-ink-50 px-3 py-2 text-caption font-semibold text-ink-700">{pc}</p>
                <ul className="divide-y divide-ink-100">
                  {items.map((rule) => (
                    <li key={rule.id ?? rule.key} className={clsx("flex flex-wrap items-start gap-3 px-3 py-2", !rule.enabled && "opacity-60")}>
                      <div className="min-w-0 flex-1">
                        <p className="flex flex-wrap items-center gap-1.5">
                          <span className="font-mono text-[12.5px] font-semibold text-ink-900">{rule.key}</span>
                          <Badge tone="brand">{rule.kind.replace("_", " ")}</Badge>
                          {rule.target !== "transaction" && <Badge tone="info">{rule.target}</Badge>}
                          {rule.column && <Badge tone="neutral">{rule.column}</Badge>}
                          {rule.approved_by && rule.approved_by !== "seed" && <Badge tone="success">edited by {rule.approved_by}</Badge>}
                        </p>
                        <p className="mt-1 text-caption text-ink-500">{scopeOf(rule)}{rule.source_ref ? ` · from ${rule.source_ref}` : ""}</p>
                        {rule.note && <p className="mt-1 text-caption text-ink-600">{rule.note}</p>}
                      </div>
                      <Switch id={`rule-${rule.id}`} checked={rule.enabled} label={rule.enabled ? "On" : "Off"} onChange={(value) => toggle(rule, value)} />
                      <IconButton label={`Edit ${rule.key}`} className="!h-7 !w-7" onClick={() => setEditing(rule)}><Pencil /></IconButton>
                      <IconButton label={`Delete ${rule.key}`} className="!h-7 !w-7" onClick={() => remove(rule)}><Trash2 /></IconButton>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState compact icon={<Wand2 />} title={rules.length ? "No matching rules" : "No rules yet"}
            description={rules.length ? "Try another search." : "Load the seed files with backend/tools/load_pc_rules.py, or add a rule."} />
        )}
      </SectionCard>
    </div>
  );
}

const field = "h-9 w-full rounded-lg border border-ink-200 px-3 text-body";

function TextField({ label, value, onChange, placeholder, mono }: {
  label: string; value: string | null; onChange: (value: string) => void; placeholder?: string; mono?: boolean;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-caption font-medium text-ink-600">{label}</span>
      <input value={value ?? ""} placeholder={placeholder} onChange={(event) => onChange(event.target.value)} className={clsx(field, mono && "font-mono text-[12.5px]")} />
    </label>
  );
}

/** Add or change one rule: checked by the server before it is saved. */
function RuleEditor({ rule, onCancel, onSaved }: { rule: SilverRule; onCancel: () => void; onSaved: (rule: SilverRule) => void }) {
  const toast = useToast();
  const [draft, setDraft] = useState<SilverRule>(rule);
  const [specText, setSpecText] = useState(JSON.stringify(rule.spec, null, 2));
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const set = <K extends keyof SilverRule>(key: K, value: SilverRule[K]) => setDraft((current) => ({ ...current, [key]: value }));
  const columnKind = COLUMN_KINDS.includes(draft.kind);

  const parsed = (): SilverRule | null => {
    try {
      const spec = JSON.parse(specText) as Record<string, unknown>;
      const empty = (value: string | null) => (value && value.trim() ? value.trim() : null);
      return {
        ...draft, spec, column: columnKind ? empty(draft.column) : null, scope_bronze_table: empty(draft.scope_bronze_table),
        scope_file_like: empty(draft.scope_file_like), scope_sheet: empty(draft.scope_sheet), note: empty(draft.note),
        source_ref: empty(draft.source_ref),
      };
    } catch (err) {
      setErrors([`The spec is not valid JSON: ${String(err)}`]);
      return null;
    }
  };
  const check = async () => {
    const next = parsed();
    if (!next) return false;
    const result = await api.validateSilverRule(next);
    setErrors(result.errors);
    return result.valid;
  };
  const save = async () => {
    const next = parsed();
    if (!next) return;
    setBusy(true);
    try {
      if (!(await check())) return;
      const saved = await api.saveSilverRule(next);
      toast({ severity: "success", title: rule.id == null ? "Rule added" : "Rule saved", description: "Applies from the next Silver run." });
      onSaved(saved);
    } catch (err) {
      const body = toError(err).body;
      setErrors([body.message, ...(body.advice ? [body.advice] : [])]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <SectionCard icon={<FileCode2 />} title={rule.id == null ? "New rule" : `Edit ${rule.key}`}
      actions={<IconButton label="Close the editor" onClick={onCancel}><X /></IconButton>}>
      <div className="grid gap-3 md:grid-cols-3">
        <TextField label="Profit center" value={draft.profit_center} onChange={(value) => set("profit_center", value)} placeholder="PC2041" />
        <TextField label="Key" value={draft.key} onChange={(value) => set("key", value)} placeholder="pc2041.retention.issued_only" mono />
        <Select<SilverRuleKind> label="Kind" value={draft.kind} options={KINDS} onChange={(value) => {
          set("kind", value);
          if (rule.id == null) setSpecText(JSON.stringify(EXAMPLES[value], null, 2));
        }} />
        <Select<SilverRuleTarget> label="Target" value={draft.target} options={TARGETS} onChange={(value) => set("target", value)} />
        {columnKind ? (
          <TextField label="Silver column it writes" value={draft.column} onChange={(value) => set("column", value)} placeholder="renewal_flag" mono />
        ) : (
          <p className="self-end pb-2 text-caption text-ink-500">{draft.kind === "lookup" ? "A lookup names its columns in the spec." : "A filter writes no column."}</p>
        )}
        <label className="block">
          <span className="mb-1 block text-caption font-medium text-ink-600">Order within its kind</span>
          <input type="number" value={draft.seq} onChange={(event) => set("seq", Number(event.target.value) || 0)} className={field} />
        </label>
        <TextField label="Files (glob, optional)" value={draft.scope_file_like} onChange={(value) => set("scope_file_like", value)} placeholder="*Billing Register*" />
        <TextField label="Sheet (glob, optional)" value={draft.scope_sheet} onChange={(value) => set("scope_sheet", value)} placeholder="Renewal" />
        <TextField label="Bronze table (glob, optional)" value={draft.scope_bronze_table} onChange={(value) => set("scope_bronze_table", value)} />
      </div>
      <label className="mt-3 block">
        <span className="mb-1 block text-caption font-medium text-ink-600">
          Spec (JSON). Operands: "src:Header as written", "silver:column", a value, or {"{"}"op": …, "args": […]{"}"}.
        </span>
        <textarea value={specText} onChange={(event) => setSpecText(event.target.value)} rows={10} spellCheck={false}
          className="w-full rounded-lg border border-ink-200 p-3 font-mono text-[12.5px]" />
      </label>
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <TextField label="Note" value={draft.note} onChange={(value) => set("note", value)} />
        <TextField label="Where it came from" value={draft.source_ref} onChange={(value) => set("source_ref", value)} placeholder="notebook.py:123" />
      </div>
      {errors.length > 0 && (
        <Alert tone="error" title="The rule cannot be saved as written" className="mt-3">
          <ul className="list-disc pl-5">{errors.map((error) => <li key={error}>{error}</li>)}</ul>
        </Alert>
      )}
      <div className="mt-4 flex items-center justify-between gap-3">
        <Switch id="rule-enabled" checked={draft.enabled} label="On" onChange={(value) => set("enabled", value)} />
        <div className="flex gap-2">
          <Button variant="secondary" disabled={busy} onClick={() => { void check(); }} icon={<Check />}>Check</Button>
          <Button disabled={busy} onClick={save}>{rule.id == null ? "Add rule" : "Save"}</Button>
        </div>
      </div>
    </SectionCard>
  );
}

/** What the profit center's rules did in this table's dry run, for the Data quality tab. */
export function RuleQuality({ quality }: { quality: SilverQuality }) {
  const stats = quality.rules ?? [];
  const notes = quality.rule_notes ?? [];
  if (!stats.length && !notes.length) return null;
  return (
    <div className="mt-3 rounded-lg border border-ink-200 p-3">
      <p className="mb-2 flex items-center gap-1.5 text-caption font-semibold text-ink-600 [&>svg]:h-3.5 [&>svg]:w-3.5">
        <Wand2 />Profit center rules{quality.rows_filtered ? ` · ${plural(quality.rows_filtered, "row")} dropped` : ""}
      </p>
      <div className="flex flex-wrap gap-1.5">
        {stats.map((stat) => {
          const warn = stat.unreadable > 0 || stat.missing_sources.length > 0;
          const what = stat.kind === "filter"
            ? `${stat.rows_dropped} dropped`
            : `${stat.rows_changed} of ${stat.rows_in_scope} changed`;
          const title = [
            stat.columns.length ? `Writes ${stat.columns.join(", ")}` : null,
            stat.unreadable ? `${stat.unreadable} values the column could not hold (NULL)` : null,
            stat.missing_sources.length ? `Not in the file: ${stat.missing_sources.join(", ")}` : null,
          ].filter(Boolean).join(". ");
          return <span key={stat.key} title={title}><Badge tone={warn ? "warning" : "neutral"}>{stat.key}: {what}</Badge></span>;
        })}
      </div>
      {notes.map((note) => <p key={note} className="mt-2 text-caption text-amber-800">{note}</p>)}
    </div>
  );
}
