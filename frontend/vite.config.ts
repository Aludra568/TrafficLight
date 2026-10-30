/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// В разработке запросы /api идут на бэкенд (uvicorn :8000); в Docker это делает nginx.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: process.env.API_URL ?? "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 1500,
  },
  test: {
    environment: "jsdom",
    globals: true, // testing-library сам убирает отрисованное между тестами
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
