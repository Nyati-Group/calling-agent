import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, forward API calls to the FastAPI server on port 8000.
const api = "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/health": api,
      "/make-call": api,
      "/calls": api
    }
  }
});
