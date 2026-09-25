import { AppShell } from "./components/layout/Layout";
import { ConfigurationPage } from "./pages/ConfigurationPage";
import { ResultsPage } from "./pages/ResultsPage";
import { RunPage } from "./pages/RunPage";
import { NavContext, usePage } from "./state/workflow";

export default function App() {
  const [page, navigate] = usePage();
  return (
    <NavContext.Provider value={navigate}>
      <AppShell page={page} onNavigate={navigate}>
        {page === "configuration" && <ConfigurationPage />}
        {page === "run" && <RunPage />}
        {page === "results" && <ResultsPage />}
      </AppShell>
    </NavContext.Provider>
  );
}
