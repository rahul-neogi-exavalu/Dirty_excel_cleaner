import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the API runs separately under uvicorn on :8000.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8080", changeOrigin: true } },
  },
});
