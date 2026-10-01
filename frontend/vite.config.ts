import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Node's process, without pulling in @types/node for one variable.
declare const process: { env: Record<string, string | undefined> };

// In development the API runs separately under uvicorn on :8000.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Point at another API with VITE_API_TARGET, e.g. http://127.0.0.1:8000.
    proxy: { "/api": { target: process.env.VITE_API_TARGET ?? "http://127.0.0.1:8080", changeOrigin: true } },
  },
});
