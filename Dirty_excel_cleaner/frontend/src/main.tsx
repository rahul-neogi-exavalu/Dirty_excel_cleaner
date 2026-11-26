import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ToastProvider } from "./components/ui/Feedback";
import { LoginPage } from "./pages/LoginPage";
import { AuthGate, AuthProvider } from "./state/auth";
import { WorkflowProvider } from "./state/workflow";
import "./index.css";

// The workflow mounts only once someone is signed in: it calls the API as it starts.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ToastProvider>
      <AuthProvider>
        <AuthGate signIn={<LoginPage />}>
          <WorkflowProvider>
            <App />
          </WorkflowProvider>
        </AuthGate>
      </AuthProvider>
    </ToastProvider>
  </StrictMode>,
);
