import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` serves the canvas; /api is proxied to the FastAPI backend
// (run: uvicorn api.main:app --reload --port 8000).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
