import tailwindcss from "@tailwindcss/vite"
import { defineConfig } from "vitest/config"
import solid from "vite-plugin-solid"

const backend = process.env.TRAINHUB_BACKEND ?? `http://127.0.0.1:${process.env.TRAINHUB_PORT ?? "8420"}`

export default defineConfig({
  plugins: [solid(), tailwindcss()],
  resolve: { alias: { "~": "/src" } },
  server: {
    port: 5173,
    proxy: {
      "/api": backend,
      "/mcp": backend,
      "/login": backend,
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
    },
  },
  build: {
    target: "es2022",
    sourcemap: false,
    chunkSizeWarningLimit: 600,
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
})
