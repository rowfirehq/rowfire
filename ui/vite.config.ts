import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In dev the UI runs on 5173 and proxies /api to the local FastAPI server, so
// the browser only ever talks to one origin and no CORS is involved.
//
// In the Docker image there is no dev server: the built assets are copied into
// the Python package and FastAPI serves them from the same origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
  },
});
