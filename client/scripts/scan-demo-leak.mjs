/**
 * 生产构建产物扫描（W4 验收 + F1 打包基线）：
 * ① demo 泄漏：production 构建零 WebDemoBridge / DemoTaskSimulator 命中
 *    （生产渠道 tree-shaking 应已移除 import.meta.env.DEV 分支引用的演示桥；
 *     若命中，说明生产构建把 demo 代码打进去了，必须排查渠道隔离——DEC-013）；
 * ② 相对资源路径：dist/index.html 不得出现绝对 `/assets/...` 引用
 *    （Electron file:// 加载下绝对路径会 404 白屏；vite base 必须为 "./"）；
 * ③ 生产 CSP：dist/index.html 必须携带 Content-Security-Policy meta
 *    （渲染层零外部连接；缺失说明 cspPlugin 未生效）。
 *
 * 在 `npm run build:renderer`（vite build → dist/）之后执行。
 * 用法：node scripts/scan-demo-leak.mjs
 * 退出码：0 = 通过；1 = 命中（泄漏/路径/加固缺失）；2 = 未找到构建产物（需先 build）。
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const rendererDist = join(__dirname, "..", "dist");

/** 生产构建中绝对不应出现的 demo 标识（类名最干净，无 UI 文案误报）。 */
const DEMO_MARKERS = ["DemoTaskSimulator", "WebDemoBridge"];

/** 递归收集 .js 产物（跳过主进程 dist/main）。 */
function collectJs(dir, skipMain = true) {
  const out = [];
  let entries;
  try {
    entries = readdirSync(dir);
  } catch {
    return out;
  }
  for (const name of entries) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      if (skipMain && name === "main") continue; // 主进程产物不含 renderer demo
      out.push(...collectJs(p, false));
    } else if (name.endsWith(".js")) {
      out.push(p);
    }
  }
  return out;
}

const files = collectJs(rendererDist);
if (files.length === 0) {
  console.error("未找到渲染层构建产物，请先执行 `npm run build:renderer`。");
  process.exit(2);
}

const hits = [];
for (const file of files) {
  const text = readFileSync(file, "utf-8");
  for (const marker of DEMO_MARKERS) {
    if (text.includes(marker)) hits.push({ file, marker });
  }
}

if (hits.length > 0) {
  console.error("生产构建泄漏 demo 标识（违反 DEC-013 渠道隔离）：");
  for (const hit of hits) console.error(`  ${hit.marker}  @  ${hit.file}`);
  process.exit(1);
}
console.log(`① demo 泄漏扫描通过：${files.length} 个渲染层 js 产物零 demo 命中。`);

// ---------------------------------------------------------------------------
// ② 相对资源路径（Electron file:// 加载基线）
// ---------------------------------------------------------------------------
const htmlPath = join(rendererDist, "index.html");
let html;
try {
  html = readFileSync(htmlPath, "utf-8");
} catch {
  console.error("未找到 dist/index.html，请先执行 `npm run build:renderer`。");
  process.exit(2);
}

const absoluteRefs = html.match(/(?:src|href)="\/[^"]*"/g) ?? [];
if (absoluteRefs.length > 0) {
  console.error("index.html 含绝对资源路径（file:// 下 404 白屏，vite base 必须为 \"./\"）：");
  for (const ref of absoluteRefs) console.error(`  ${ref}`);
  process.exit(1);
}
console.log("② 资源路径检查通过：index.html 无绝对 / 引用（相对路径，file:// 可用）。");

// ---------------------------------------------------------------------------
// ③ 生产 CSP meta（渲染层零外部连接）
// ---------------------------------------------------------------------------
if (!/http-equiv="Content-Security-Policy"/.test(html) || !/default-src 'self'/.test(html)) {
  console.error("index.html 缺少生产 CSP meta（cspPlugin 未生效，渲染层外部连接未受约束）。");
  process.exit(1);
}
console.log("③ 生产 CSP 检查通过：default-src 'self' 已注入。");
