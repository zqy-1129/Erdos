/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * SP3-4 渲染层工程（Vite + React 19）。
 * - build/dev 入口 client/index.html → renderer/src/entry.tsx；
 * - 组件测试 vitest + jsdom（renderer/tests/**），纯逻辑测试沿用 node:test（client/tests）。
 */
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  build: { outDir: "dist", emptyOutDir: true },
  test: {
    environment: "jsdom",
    include: ["renderer/tests/**/*.test.{ts,tsx}"],
  },
});