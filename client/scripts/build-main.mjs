/**
 * 主进程 / preload 构建（FE-HOST W3）：esbuild 打包为
 * - dist/main/index.js（ESM，Electron ≥28 支持；入口见 package.json "main"）
 * - dist/main/preload.cjs（CJS；sandbox: true 下 preload 必须为 CJS）
 *
 * 关键点：ESM 产物内嵌 CJS 依赖（electron-updater → fs-extra → graceful-fs）
 * 会触发 "Dynamic require of \"fs\" is not supported"（esbuild 的 __require 垫片在
 * ESM 下无 require 可委托）。解法：注入 banner 用 createRequire 提供真实 require，
 * 使垫片直接委托——无需改为 CJS 产物（保留 import.meta.url 等 ESM 语义）。
 *
 * 用法（client/ 目录）：node scripts/build-main.mjs（= npm run build:main）
 */
import { build } from "esbuild";
import { readFileSync } from "node:fs";
const engineVersion = readFileSync(new URL("../../engine/pyproject.toml", import.meta.url), "utf8")
  .match(/^version\s*=\s*"([^"]+)"/m)?.[1];
if (!engineVersion) throw new Error("engine/pyproject.toml 缺少 version 字段");

const shared = {
  bundle: true,
  platform: "node",
  target: "node22",
  external: ["electron", "pdfjs-dist", "tesseract.js"], // 解析器保留运行时 worker/资源路径
  logLevel: "info",
};

/** ESM 主进程包：banner 提供 createRequire 托底 __require 垫片（见文件头说明）。 */
await build({
  ...shared,
  entryPoints: ["main/index.ts"],
  define: { __ERDOS_ENGINE_VERSION__: JSON.stringify(engineVersion) },
  format: "esm",
  banner: {
    js: 'import { createRequire } from "node:module"; const require = createRequire(import.meta.url);',
  },
  outfile: "dist/main/index.js",
});

/** preload：CJS（sandbox 模式下 Electron 要求）。 */
await build({
  ...shared,
  entryPoints: ["main/preload.ts"],
  format: "cjs",
  outfile: "dist/main/preload.cjs",
});

console.log("[build:main] dist/main/index.js（ESM）+ dist/main/preload.cjs（CJS）完成");
