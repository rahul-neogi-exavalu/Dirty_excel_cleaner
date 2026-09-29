import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ToastProvider } from "./components/ui/Feedback";
import { WorkflowProvider } from "./state/workflow";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ToastProvider>
      <WorkflowProvider>
        <App />
      </WorkflowProvider>
    </ToastProvider>
  </StrictMode>,
);
