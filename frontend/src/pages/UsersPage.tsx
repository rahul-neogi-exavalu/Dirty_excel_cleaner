import clsx from "clsx";
import { Copy, KeyRound, Pencil, ShieldCheck, UserPlus, UsersRound } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { api, ApiError } from "../api/client";
import type { AuthUser, UserDraft, UserState } from "../api/types";
import { PageHeader } from "../components/layout/Layout";
import { Badge, type Tone } from "../components/ui/Badge";
import { Button, type ButtonState } from "../components/ui/Button";
import { SearchInput, Switch, TextInput } from "../components/ui/Controls";
import { Alert, EmptyState, Skeleton, useToast } from "../components/ui/Feedback";
import { SidePanel } from "../components/ui/Overlay";
import { Pagination, useFitPageSize, usePaged } from "../components/ui/Pagination";
import { formatIso, initials } from "../lib/format";
import { useAuth } from "../state/auth";

const STATE: Record<UserState, { label: string; tone: Tone }> = {
  active: { label: "Active", tone: "success" },
  inactive: { label: "Inactive", tone: "neutral" },
  expired: { label: "Expired", tone: "warning" },
};

type Editing = { mode: "new" } | { mode: "edit"; user: AuthUser };
type Issued = { user: AuthUser; password: string; reason: "created" | "reset" };

export function UsersPage() {
  const { user: me } = useAuth();
  const [users, setUsers] = useState<AuthUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<Editing | null>(null);
  const [issued, setIssued] = useState<Issued | null>(null);

  const load = useCallback(async () => {
    try {
      setUsers((await api.listUsers()).users);
      setError(null);
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.body.message : "Users could not be loaded.");
      setUsers([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const needle = query.trim().toLowerCase();
  const shown = (users ?? []).filter(
    (user) => !needle || user.user_name.toLowerCase().includes(needle) || user.user_email_id.includes(needle),
  );
  const table = useRef<HTMLDivElement>(null);
  const paged = usePaged(shown, useFitPageSize(table, { min: 5, fallbackRow: 57, reserve: 110 }), needle);

  const saved = (user: AuthUser) => {
    setUsers((current) => {
      const list = current ?? [];
      const found = list.some((item) => item.user_id === user.user_id);
      const next = found ? list.map((item) => (item.user_id === user.user_id ? user : item)) : [...list, user];
      return next.sort((a, b) => a.user_name.localeCompare(b.user_name));
    });
  };

  return (
    <>
      <PageHeader
        page="users"
        title="Users"
        description="Who can sign in, and as what."
        actions={
          <Button variant="primary" icon={<UserPlus />} onClick={() => setEditing({ mode: "new" })}>
            Add user
          </Button>
        }
      />

      <SearchInput value={query} onChange={setQuery} label="Search users" placeholder="Name or email" className="mb-4 max-w-sm" />

      {error && <Alert tone="error" title={error} className="mb-4" />}

      <div ref={table} className="card overflow-hidden">
        <div className="relative overflow-x-auto scroll-thin">
          <table className="w-full min-w-[760px] text-table">
            <thead>
              <tr className="border-b border-ink-200 bg-ink-50 text-left text-caption font-semibold text-ink-600">
                <th scope="col" className="px-4 py-2.5">Name</th>
                <th scope="col" className="px-3 py-2.5">Phone</th>
                <th scope="col" className="px-3 py-2.5">Role</th>
                <th scope="col" className="px-3 py-2.5">Status</th>
                <th scope="col" className="px-3 py-2.5">Last sign-in</th>
                <th scope="col" className="px-3 py-2.5">Expires</th>
                <th scope="col" className="w-20 px-3 py-2.5"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {users === null &&
                Array.from({ length: 4 }, (_, index) => (
                  <tr key={index} className="border-b border-ink-100 last:border-0">
                    <td colSpan={7} className="px-4 py-3">
                      <Skeleton className="h-6 w-full" />
                    </td>
                  </tr>
                ))}
              {paged.slice.map((user) => (
                <tr key={user.user_id} data-row className="border-b border-ink-100 transition-colors last:border-0 hover:bg-ink-50/70">
                  <td className="px-4 py-2.5">
                    <div className="flex items-center gap-3">
                      <Initials name={user.user_name} />
                      <div className="min-w-0">
                        <p className="truncate font-medium text-ink-900">
                          {user.user_name}
                          {user.user_id === me?.user_id && <span className="ml-1.5 font-normal text-ink-500">(you)</span>}
                        </p>
                        <p className="truncate text-caption text-ink-500">{user.user_email_id}</p>
                      </div>
                    </div>
                  </td>
                  <td className="num px-3 py-2.5 text-ink-700">{user.user_phone_number ?? "—"}</td>
                  <td className="px-3 py-2.5">
                    {user.is_admin ? (
                      <Badge icon={<ShieldCheck />}>Admin</Badge>
                    ) : (
                      <span className="text-ink-600">Member</span>
                    )}
                  </td>
                  <td className="px-3 py-2.5">
                    <span className="inline-flex items-center gap-2">
                      <Badge tone={STATE[user.state].tone}>{STATE[user.state].label}</Badge>
                      {user.must_change_password && user.state === "active" && (
                        <span className="text-caption text-ink-500" title="Signs in with a temporary password">
                          New password due
                        </span>
                      )}
                    </span>
                  </td>
                  <td className="num whitespace-nowrap px-3 py-2.5 text-ink-700">
                    {user.last_login_at ? formatIso(user.last_login_at) : <span className="text-ink-400">Never</span>}
                  </td>
                  <td className="num whitespace-nowrap px-3 py-2.5 text-ink-700">{user.expiry_date ? formatIso(user.expiry_date) : "—"}</td>
                  <td className="px-3 py-2.5 text-right">
                    <Button size="sm" variant="ghost" icon={<Pencil />} onClick={() => setEditing({ mode: "edit", user })}>
                      Edit
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {users !== null && shown.length === 0 && !error && (
          <EmptyState
            icon={<UsersRound />}
            title={needle ? "No matching users" : "No users yet"}
            description={needle ? "Try another name or email." : "Add the people who clean and ingest files."}
            compact
          />
        )}
        <Pagination {...paged} onPage={paged.setPage} noun="user" />
      </div>

      <UserPanel
        editing={editing}
        me={me}
        onClose={() => setEditing(null)}
        onSaved={(user, password, reason) => {
          saved(user);
          if (password) {
            setEditing(null);
            setIssued({ user, password, reason: reason ?? "created" });
          }
        }}
      />
      <IssuedPanel issued={issued} onClose={() => setIssued(null)} />
    </>
  );
}

function Initials({ name }: { name: string }) {
  const letters = initials(name);
  return (
    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-ink-100 font-display text-[12px] font-semibold text-ink-700" aria-hidden>
      {letters || "?"}
    </span>
  );
}

/* -------------------------------------------------------------------------- */

/** datetime-local value <-> ISO, in the reader's time zone. */
const toLocalInput = (iso: string | null) => {
  if (!iso) return "";
  const date = new Date(iso);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};
const fromLocalInput = (value: string) => (value ? new Date(value).toISOString() : null);

function UserPanel({
  editing,
  me,
  onClose,
  onSaved,
}: {
  editing: Editing | null;
  me: AuthUser | null;
  onClose: () => void;
  onSaved: (user: AuthUser, password?: string, reason?: Issued["reason"]) => void;
}) {
  const toast = useToast();
  const id = useId();
  const existing = editing?.mode === "edit" ? editing.user : null;
  const self = existing?.user_id === me?.user_id;
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [admin, setAdmin] = useState(false);
  const [active, setActive] = useState(true);
  const [expiry, setExpiry] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<string | null>(null);
  const [state, setState] = useState<ButtonState>("idle");
  const [resetting, setResetting] = useState<ButtonState>("idle");

  useEffect(() => {
    setName(existing?.user_name ?? "");
    setEmail(existing?.user_email_id ?? "");
    setPhone(existing?.user_phone_number ?? "");
    setAdmin(existing?.is_admin ?? false);
    setActive(existing?.is_active ?? true);
    setExpiry(toLocalInput(existing?.expiry_date ?? null));
    setErrors({});
    setFailure(null);
    setState("idle");
    setResetting("idle");
  }, [editing, existing]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const found: Record<string, string> = {};
    if (name.trim().length < 2) found.user_name = "Enter a name of at least 2 characters.";
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim())) found.user_email_id = "Enter a valid email address.";
    setErrors(found);
    setFailure(null);
    if (Object.keys(found).length) return;

    const draft: UserDraft = {
      user_name: name.trim(),
      user_email_id: email.trim().toLowerCase(),
      user_phone_number: phone.trim() || null,
      is_admin: admin,
      expiry_date: fromLocalInput(expiry),
    };
    if (existing) draft.is_active = active;
    setState("loading");
    try {
      if (existing) {
        const { user } = await api.updateUser(existing.user_id, draft);
        onSaved(user);
        toast({ severity: "success", title: `${user.user_name} updated` });
        onClose();
      } else {
        const { user, temporary_password } = await api.createUser(draft);
        onSaved(user, temporary_password, "created");
      }
    } catch (error) {
      setState("idle");
      const body = error instanceof ApiError ? error.body : null;
      if (body?.field) setErrors({ [body.field]: body.message });
      else setFailure(body?.message ?? "The user could not be saved.");
    }
  };

  const reset = async () => {
    if (!existing) return;
    setResetting("loading");
    try {
      const { user, temporary_password } = await api.resetPassword(existing.user_id);
      onSaved(user, temporary_password, "reset");
    } catch (error) {
      setResetting("idle");
      setFailure(error instanceof ApiError ? error.body.message : "The password could not be reset.");
    }
  };

  const formId = `${id}-form`;
  return (
    <SidePanel
      open={Boolean(editing)}
      onClose={onClose}
      title={existing ? "Edit user" : "Add user"}
      description={existing ? existing.user_email_id : "They get a temporary password to replace at first sign-in."}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" form={formId} variant="primary" state={state} loadingText="Saving">
            {existing ? "Save" : "Add user"}
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} noValidate className="space-y-5">
        {failure && <Alert tone="error">{failure}</Alert>}
        <FormRow id={`${id}-name`} label="Name" error={errors.user_name}>
          <TextInput id={`${id}-name`} value={name} onChange={(e) => setName(e.target.value)} invalid={Boolean(errors.user_name)} maxLength={100} autoComplete="off" />
        </FormRow>
        <FormRow id={`${id}-email`} label="Email" error={errors.user_email_id}>
          <TextInput id={`${id}-email`} type="email" value={email} onChange={(e) => setEmail(e.target.value)} invalid={Boolean(errors.user_email_id)} maxLength={255} autoComplete="off" spellCheck={false} />
        </FormRow>
        <FormRow id={`${id}-phone`} label="Phone" hint="Optional." error={errors.user_phone_number}>
          <TextInput id={`${id}-phone`} type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} maxLength={20} autoComplete="off" />
        </FormRow>
        <FormRow id={`${id}-expiry`} label="Access ends" hint="Optional. No sign-in from this moment on." error={errors.expiry_date}>
          <TextInput id={`${id}-expiry`} type="datetime-local" value={expiry} onChange={(e) => setExpiry(e.target.value)} />
        </FormRow>
        <ToggleRow
          id={`${id}-admin`}
          label="Admin"
          description="Manages users and sees everyone's jobs."
          checked={admin}
          onChange={setAdmin}
          disabled={self}
          note={self ? "You can't change your own role." : undefined}
        />
        {existing && (
          <ToggleRow
            id={`${id}-active`}
            label="Can sign in"
            description="Off ends their sessions within 30 seconds. History is kept."
            checked={active}
            onChange={setActive}
            disabled={self}
            note={self ? "You can't deactivate yourself." : undefined}
          />
        )}
      </form>

      {existing && (
        <div className="mt-8 border-t border-ink-200 pt-5">
          <p className="text-body font-medium text-ink-900">Password</p>
          <p className="mt-0.5 text-caption text-ink-500">Issues a temporary password and signs them out everywhere.</p>
          <Button className="mt-3" icon={<KeyRound />} state={resetting} loadingText="Resetting" onClick={() => void reset()}>
            Reset password
          </Button>
        </div>
      )}
    </SidePanel>
  );
}

function FormRow({ id, label, hint, error, children }: { id: string; label: string; hint?: string; error?: string; children: ReactNode }) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block text-body font-medium text-ink-800">
        {label}
      </label>
      {children}
      {error ? (
        <p className="text-caption font-medium text-danger-700">{error}</p>
      ) : hint ? (
        <p className="text-caption text-ink-500">{hint}</p>
      ) : null}
    </div>
  );
}

function ToggleRow({
  id,
  label,
  description,
  checked,
  onChange,
  disabled,
  note,
}: {
  id: string;
  label: string;
  description: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
  note?: string;
}) {
  return (
    <div className={clsx("flex items-start justify-between gap-4 rounded-lg border border-ink-200 px-4 py-3", disabled && "bg-ink-50")}>
      <div className="min-w-0">
        <p className="text-body font-medium text-ink-800">{label}</p>
        <p className="text-caption text-ink-500">{note ?? description}</p>
      </div>
      <Switch id={id} label={label} checked={checked} onChange={onChange} disabled={disabled} description={description} />
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function IssuedPanel({ issued, onClose }: { issued: Issued | null; onClose: () => void }) {
  const toast = useToast();
  const copy = async () => {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.password);
      toast({ severity: "success", title: "Password copied" });
    } catch {
      toast({ severity: "error", title: "Copy is blocked in this browser" });
    }
  };
  return (
    <SidePanel
      open={Boolean(issued)}
      onClose={onClose}
      title={issued?.reason === "reset" ? "Password reset" : "User added"}
      description={issued && `${issued.user.user_name} · ${issued.user.user_email_id}`}
      footer={
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      {issued && (
        <>
          <p className="text-body text-ink-700">Temporary password</p>
          <div className="mt-2 flex items-center gap-2 rounded-md border border-ink-200 bg-ink-50 px-3 py-2.5">
            <code className="min-w-0 flex-1 select-all break-all font-mono text-[15px] text-ink-900">{issued.password}</code>
            <Button size="sm" icon={<Copy />} onClick={() => void copy()}>
              Copy
            </Button>
          </div>
          <Alert tone="warning" className="mt-4" title="Shown once">
            Only its hash is stored. Share it privately; they set their own at first sign-in.
          </Alert>
        </>
      )}
    </SidePanel>
  );
}
