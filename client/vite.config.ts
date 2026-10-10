/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

/**
 * SP3-4 渲染层工程（Vite + React 19）。
 * - build/dev 入口 client/index.html → renderer/src/entry.tsx；
 * - 组件测试 vitest + jsdom（renderer/tests/**），纯逻辑测试沿用 node:test（client/tests）；
 * - Electron 打包基线（F1/FE-HOST）：
 *   ① base="./" —— 产物以相对路径引用静态资源，file:// 加载可用（绝对 /assets 会 404 白屏）；
 *   ② 生产构建注入 CSP —— 渲染层零外部连接（云端/厂商流量一律经主进程，见 bridges/bridge.ts 红线）。
 */

/** 生产 CSP（仅 build 注入；dev 由 vite dev server 服务，不受此约束）。 */
const PROD_CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'", // React 内联 style 属性需要
  "img-src 'self' data:",
  "font-src 'self' data:",
  "connect-src 'self'", // 渲染层不直连云端/厂商（流量经主进程 IPC）
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
].join("; ");

/** 生产构建注入 CSP meta（dist/index.html）。 */
function cspPlugin(): Plugin {
  return {
    name: "erdos-prod-csp",
    apply: "build",
    transformIndexHtml(html) {
      return html.replace(
        "</head>",
        `    <meta http-equiv="Content-Security-Policy" content="${PROD_CSP}" />\n  </head>`,
      );
    },
  };
}

export default defineConfig({
  plugins: [react(), cspPlugin()],
  base: "./",
  server: { port: 5173 },
  build: { outDir: "dist", emptyOutDir: true },
  test: {
    environment: "jsdom",
    include: ["renderer/tests/**/*.test.{ts,tsx}"],
    coverage: {
      provider: "v8",
      include: ["renderer/src/**/*.{ts,tsx}", "main/ipc/bridge.ts", "main/safe-storage-encryptor.ts", "main/update/updater.ts", "main/preload.ts", "main/index.ts", "main/window.ts"],
      exclude: ["renderer/src/bridges/web-bridge.ts"], // 开发演示代码在生产产物中被树摇除去，scan-demo-leak 单独守护
      reporter: ["json", "text"],
      reportsDirectory: "coverage/renderer",
    },
  },
});
