import clsx from "clsx";
import { AlertTriangle, ArrowRight, Check, Eye, EyeOff, KeyRound, Lock, Mail } from "lucide-react";
import {
  forwardRef,
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type InputHTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { ApiError } from "../api/client";
import type { AuthUser } from "../api/types";
import { SheetBackdrop } from "../components/auth/SheetBackdrop";
import { Logo } from "../components/layout/Layout";
import { Button, type ButtonState } from "../components/ui/Button";
import { Alert } from "../components/ui/Feedback";
import { useAuth } from "../state/auth";

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const PASSWORD_MIN = 8;
/** Long enough to read the success state, short enough not to feel like waiting. */
const ENTER_DELAY = 550;

/**
 * Sign-in, on a spreadsheet: the card is the selected range and the active cell follows
 * the focused field (see SheetBackdrop). A password an admin issued is replaced here,
 * in the same card, before the app opens.
 */
export function LoginPage() {
  const auth = useAuth();
  // Signed in with a password still to replace (e.g. the page was reloaded mid-change).
  const pending = auth.user?.must_change_password ? auth.user : null;
  const [step, setStep] = useState<{ user: AuthUser; password: string | null } | null>(
    pending ? { user: pending, password: null } : null,
  );

  return (
    <SheetBackdrop>
      <main className="px-5 pb-7 pt-7 sm:px-8 sm:pt-8">
        <Logo light />
        {step ? (
          <NewPassword user={step.user} current={step.password} />
        ) : (
          <SignIn onNeedsNewPassword={(user, password) => setStep({ user, password })} />
        )}
      </main>
    </SheetBackdrop>
  );
}

/* -------------------------------------------------------------------------- */

function SignIn({ onNeedsNewPassword }: { onNeedsNewPassword: (user: AuthUser, password: string) => void }) {
  const auth = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<{ email?: string; password?: string }>({});
  const [failure, setFailure] = useState<string | null>(null);
  const [state, setState] = useState<ButtonState>("idle");
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  useEffect(() => emailRef.current?.focus(), []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const found: typeof errors = {};
    if (!email.trim()) found.email = "Enter your work email.";
    else if (!EMAIL.test(email.trim())) found.email = "Enter a valid email address.";
    if (!password) found.password = "Enter your password.";
    setErrors(found);
    setFailure(null);
    if (found.email) return emailRef.current?.focus();
    if (found.password) return passwordRef.current?.focus();

    setState("loading");
    try {
      const user = await auth.login(email.trim(), password);
      if (user.must_change_password) {
        setState("idle");
        onNeedsNewPassword(user, password);
        return;
      }
      setState("success");
      window.setTimeout(() => auth.enter(user), ENTER_DELAY);
    } catch (error) {
      setState("idle");
      setFailure(error instanceof ApiError ? error.body.message : "Sign-in unavailable. Try again shortly.");
      setPassword("");
      passwordRef.current?.focus();
    }
  };

  return (
    <form onSubmit={submit} noValidate className="mt-8">
      <h1 className="text-[22px] font-semibold leading-7 text-ink-900">Sign in</h1>
      <p className="mt-1 text-body text-ink-500">Use the account your admin created.</p>

      {(failure || auth.notice) && (
        <Alert tone={failure ? "error" : "info"} className="mt-5">
          {failure ?? auth.notice}
        </Alert>
      )}

      <div className="mt-6 space-y-5">
        <Field
          ref={emailRef}
          label="Email"
          icon={<Mail />}
          type="email"
          autoComplete="username"
          inputMode="email"
          spellCheck={false}
          value={email}
          error={errors.email}
          onChange={(event) => {
            setEmail(event.target.value);
            setErrors((current) => ({ ...current, email: undefined }));
          }}
        />
        <PasswordField
          ref={passwordRef}
          label="Password"
          autoComplete="current-password"
          value={password}
          error={errors.password}
          onChange={(event) => {
            setPassword(event.target.value);
            setErrors((current) => ({ ...current, password: undefined }));
          }}
        />
      </div>

      <Button
        type="submit"
        variant="primary"
        size="lg"
        state={state}
        loadingText="Signing in"
        successText="Signed in"
        iconRight={state === "idle" ? <ArrowRight /> : undefined}
        className="mt-7 w-full"
      >
        Sign in
      </Button>
      <p className="mt-5 text-caption text-ink-500">Access is managed by your admin.</p>
    </form>
  );
}

/* -------------------------------------------------------------------------- */

function NewPassword({ user, current }: { user: AuthUser; current: string | null }) {
  const auth = useAuth();
  const [old, setOld] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<{ old?: string; next?: string; confirm?: string }>({});
  const [failure, setFailure] = useState<string | null>(null);
  const [state, setState] = useState<ButtonState>("idle");
  const oldRef = useRef<HTMLInputElement>(null);
  const nextRef = useRef<HTMLInputElement>(null);
  const confirmRef = useRef<HTMLInputElement>(null);
  const long = next.length >= PASSWORD_MIN;

  useEffect(() => (current ? nextRef : oldRef).current?.focus(), [current]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const found: typeof errors = {};
    if (!current && !old) found.old = "Enter the password you signed in with.";
    if (!long) found.next = `Use at least ${PASSWORD_MIN} characters.`;
    else if (next.trim().toLowerCase() === user.user_email_id.toLowerCase()) found.next = "The password can't be your email address.";
    if (!found.next && confirm !== next) found.confirm = "The passwords don't match.";
    setErrors(found);
    setFailure(null);
    const first = found.old ? oldRef : found.next ? nextRef : found.confirm ? confirmRef : null;
    if (first) return first.current?.focus();

    setState("loading");
    try {
      const updated = await auth.changePassword(current ?? old, next);
      setState("success");
      window.setTimeout(() => auth.enter(updated), ENTER_DELAY);
    } catch (error) {
      setState("idle");
      const body = error instanceof ApiError ? error.body : null;
      if (body?.field === "current_password") setErrors({ old: body.message });
      else if (body?.field === "new_password") setErrors({ next: body.message });
      else setFailure(body?.message ?? "Sign-in unavailable. Try again shortly.");
    }
  };

  return (
    <form onSubmit={submit} noValidate className="mt-8">
      <h1 className="text-[22px] font-semibold leading-7 text-ink-900">Set a new password</h1>
      <p className="mt-1 text-body text-ink-500">
        Replace the temporary one for <span className="font-medium text-ink-700">{user.user_email_id}</span>.
      </p>

      {failure && (
        <Alert tone="error" className="mt-5">
          {failure}
        </Alert>
      )}

      <div className="mt-6 space-y-5">
        {!current && (
          <PasswordField
            ref={oldRef}
            label="Current password"
            autoComplete="current-password"
            value={old}
            error={errors.old}
            onChange={(event) => setOld(event.target.value)}
          />
        )}
        <PasswordField
          ref={nextRef}
          label="New password"
          icon={<KeyRound />}
          autoComplete="new-password"
          value={next}
          error={errors.next}
          hint={
            <span className={clsx("inline-flex items-center gap-1.5 transition-colors", long ? "text-emerald-700" : "text-ink-500")}>
              <Check className={clsx("h-3.5 w-3.5 transition-opacity", long ? "opacity-100" : "opacity-30")} aria-hidden />
              At least {PASSWORD_MIN} characters
            </span>
          }
          onChange={(event) => {
            setNext(event.target.value);
            setErrors((current) => ({ ...current, next: undefined }));
          }}
        />
        <PasswordField
          ref={confirmRef}
          label="Repeat new password"
          icon={<KeyRound />}
          autoComplete="new-password"
          value={confirm}
          error={errors.confirm}
          onChange={(event) => {
            setConfirm(event.target.value);
            setErrors((current) => ({ ...current, confirm: undefined }));
          }}
        />
      </div>

      <Button
        type="submit"
        variant="primary"
        size="lg"
        state={state}
        loadingText="Saving"
        successText="Saved"
        className="mt-7 w-full"
      >
        Save and continue
      </Button>
      <button
        type="button"
        onClick={() => void auth.signOut()}
        className="mt-4 rounded text-caption font-medium text-ink-600 underline-offset-4 hover:text-ink-900 hover:underline"
      >
        Use a different account
      </button>
    </form>
  );
}

/* -------------------------------------------------------------------------- */

type FieldProps = InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  icon?: ReactNode;
  error?: string;
  hint?: ReactNode;
  trailing?: ReactNode;
};

/** Label above, input, then the error (or a hint) below: never a placeholder as label. */
const Field = forwardRef<HTMLInputElement, FieldProps>(function Field(
  { label, icon, error, hint, trailing, className, ...rest },
  ref,
) {
  const id = useId();
  const note = `${id}-note`;
  return (
    <div className="space-y-2">
      <label htmlFor={id} className="block text-body font-medium text-ink-800">
        {label}
      </label>
      <div className="relative">
        {icon && (
          <span className="pointer-events-none absolute left-3 top-1/2 flex -translate-y-1/2 text-ink-400 [&>svg]:h-4 [&>svg]:w-4" aria-hidden>
            {icon}
          </span>
        )}
        <input
          ref={ref}
          id={id}
          aria-invalid={error ? true : undefined}
          aria-describedby={error || hint ? note : undefined}
          className={clsx(
            "h-11 w-full rounded border bg-white text-[15px] text-ink-900 transition-shadow",
            "focus:outline-none focus:shadow-focus",
            icon ? "pl-10" : "pl-3",
            trailing ? "pr-11" : "pr-3",
            error ? "border-danger-500 focus:border-danger-600" : "border-ink-300 hover:border-ink-400 focus:border-brand-500",
            className,
          )}
          {...rest}
        />
        {trailing && <span className="absolute right-1 top-1/2 flex -translate-y-1/2">{trailing}</span>}
      </div>
      {error ? (
        <p id={note} className="text-caption font-medium text-danger-700">
          {error}
        </p>
      ) : hint ? (
        <p id={note} className="text-caption">
          {hint}
        </p>
      ) : null}
    </div>
  );
});

const PasswordField = forwardRef<HTMLInputElement, Omit<FieldProps, "type" | "trailing">>(function PasswordField(
  { icon, hint, ...rest },
  ref,
) {
  const [shown, setShown] = useState(false);
  const [caps, setCaps] = useState(false);
  const capsCheck = (event: KeyboardEvent<HTMLInputElement>) => setCaps(event.getModifierState?.("CapsLock") ?? false);
  return (
    <Field
      ref={ref}
      {...rest}
      type={shown ? "text" : "password"}
      icon={icon ?? <Lock />}
      onKeyDown={capsCheck}
      onKeyUp={capsCheck}
      onBlur={() => setCaps(false)}
      hint={
        caps ? (
          <span className="inline-flex items-center gap-1.5 font-medium text-amber-800">
            <AlertTriangle className="h-3.5 w-3.5" aria-hidden />
            Caps Lock is on
          </span>
        ) : (
          hint
        )
      }
      trailing={
        <button
          type="button"
          onClick={() => setShown(!shown)}
          aria-label={shown ? "Hide password" : "Show password"}
          aria-pressed={shown}
          className="inline-flex h-9 w-9 items-center justify-center rounded text-ink-500 transition-colors hover:bg-ink-100 hover:text-ink-800 focus-visible:outline-none focus-visible:shadow-focus"
        >
          {shown ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
        </button>
      }
    />
  );
});
