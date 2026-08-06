import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by the Python server (or nginx) under /app — never at the domain
// root, so every asset URL must be relative to that base.
export default defineConfig({
  plugins: [react()],
  base: "/app/",
  server: {
    port: 5173,
    // Dev only: the Python API keeps running on its own port; the browser
    // talks to Vite, Vite forwards anything API-shaped.
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/v1": "http://127.0.0.1:8765",
      "/login": "http://127.0.0.1:8765",
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
