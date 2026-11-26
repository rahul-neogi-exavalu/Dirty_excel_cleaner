import { Loader2 } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, ApiError, AUTH_EXPIRED } from "../api/client";
import type { AuthUser } from "../api/types";
import { STORAGE_KEYS } from "./workflow";

type Status = "checking" | "signed-out" | "signed-in";

interface Auth {
  status: Status;
  user: AuthUser | null;
  /** Why the sign-in page is showing, when it is not a fresh visit. */
  notice: string | null;
  /** Check a password; the caller decides when to enter (after its success state). */
  login: (email: string, password: string) => Promise<AuthUser>;
  /** Start the app as this user. Every sign-in lands on Configure. */
  enter: (user: AuthUser) => void;
  changePassword: (current: string, next: string) => Promise<AuthUser>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<Auth | null>(null);

export function useAuth(): Auth {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}

/** Whose workbooks this tab's stored workflow holds. */
const OWNER_KEY = "exavalu.session.owner";

/** A session's files belong to the person who uploaded them. */
function forgetSession() {
  try {
    STORAGE_KEYS.forEach((key) => sessionStorage.removeItem(key));
    sessionStorage.removeItem(OWNER_KEY);
    localStorage.removeItem("exavalu.reviewer"); // the free-text reviewer name, now the signed-in user
  } catch {
    /* storage unavailable: nothing kept */
  }
}

/**
 * Called before the workflow mounts for `user`. A session can end without a sign-out
 * (expiry, deactivation); if someone else then signs in on this tab, they must not
 * inherit the previous person's files.
 */
function claimSession(user: AuthUser) {
  try {
    const owner = sessionStorage.getItem(OWNER_KEY);
    if (owner && owner !== user.user_id) forgetSession();
    sessionStorage.setItem(OWNER_KEY, user.user_id);
  } catch {
    /* storage unavailable: nothing kept to leak */
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>("checking");
  const [user, setUser] = useState<AuthUser | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Read by the expiry listener: a 401 before anyone signed in is not an expiry.
  const signedIn = useRef(false);
  signedIn.current = status === "signed-in";

  useEffect(() => {
    let live = true;
    api
      .me()
      .then(({ user }) => {
        if (!live) return;
        claimSession(user);
        setUser(user);
        setStatus("signed-in");
      })
      .catch((error: unknown) => {
        if (!live) return;
        const body = error instanceof ApiError ? error.body : null;
        if (body && body.code !== "not_authenticated") setNotice(body.message);
        setStatus("signed-out");
      });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    const onExpired = (event: Event) => {
      if (!signedIn.current) return;
      setUser(null);
      setStatus("signed-out");
      setNotice((event as CustomEvent<string>).detail ?? "Session expired. Sign in again.");
    };
    window.addEventListener(AUTH_EXPIRED, onExpired);
    return () => window.removeEventListener(AUTH_EXPIRED, onExpired);
  }, []);

  const login = useCallback(async (email: string, password: string) => (await api.login(email, password)).user, []);

  const enter = useCallback((next: AuthUser) => {
    claimSession(next);
    if (!next.must_change_password) window.location.hash = "/configuration";
    setNotice(null);
    setUser(next);
    setStatus("signed-in");
  }, []);

  const changePassword = useCallback(
    async (current: string, next: string) => (await api.changePassword(current, next)).user,
    [],
  );

  const signOut = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      /* the cookie is cleared server-side when reachable; the UI signs out regardless */
    }
    forgetSession();
    setUser(null);
    setNotice(null);
    setStatus("signed-out");
  }, []);

  const value = useMemo(
    () => ({ status, user, notice, login, enter, changePassword, signOut }),
    [status, user, notice, login, enter, changePassword, signOut],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** Shows sign-in until there is a user who may use the app, then the app itself. */
export function AuthGate({ signIn, children }: { signIn: ReactNode; children: ReactNode }) {
  const { status, user } = useAuth();
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setSlow(true), 400);
    return () => window.clearTimeout(timer);
  }, []);
  if (status === "checking") {
    // Blank for the first moments, so a quick check never flashes a spinner.
    return (
      <div className="flex min-h-[100dvh] items-center justify-center bg-ink-50" aria-busy="true">
        {slow && <Loader2 className="h-5 w-5 animate-spin text-ink-400" aria-label="Checking your session" />}
      </div>
    );
  }
  return <>{user && !user.must_change_password ? children : signIn}</>;
}
