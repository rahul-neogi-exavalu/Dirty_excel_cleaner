import { useEffect } from "react";
import { AppShell } from "./components/layout/Layout";
import { ConfigurationPage } from "./pages/ConfigurationPage";
import { HistoryPage } from "./pages/HistoryPage";
import { IngestPage } from "./pages/IngestPage";
import { ResultsPage } from "./pages/ResultsPage";
import { RunPage } from "./pages/RunPage";
import { SilverPage } from "./pages/SilverPage";
import { UsersPage } from "./pages/UsersPage";
import { useAuth } from "./state/auth";
import { NavContext, usePage } from "./state/workflow";

export default function App() {
  const [page, navigate] = usePage();
  const { user } = useAuth();
  const forbidden = page === "users" && !user?.is_admin;

  // Users is for admins: anyone else who lands there goes to Configure.
  useEffect(() => {
    if (forbidden) navigate("configuration");
  }, [forbidden, navigate]);

  return (
    <NavContext.Provider value={navigate}>
      <AppShell page={forbidden ? "configuration" : page} onNavigate={navigate}>
        {(page === "configuration" || forbidden) && <ConfigurationPage />}
        {page === "run" && <RunPage />}
        {page === "results" && <ResultsPage />}
        {page === "ingest" && <IngestPage />}
        {page === "silver" && <SilverPage />}
        {page === "history" && <HistoryPage />}
        {page === "users" && !forbidden && <UsersPage />}
      </AppShell>
    </NavContext.Provider>
  );
}
