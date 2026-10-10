import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
const require = createRequire(import.meta.url);
const { createCoverageMap } = require("istanbul-lib-coverage");
const libReport = require("istanbul-lib-report");
const reports = require("istanbul-reports");
function run(args) {
  const result = spawnSync(process.execPath, args, { stdio: "inherit", env: process.env });
  if (result.status !== 0) process.exit(result.status ?? 1);
}
run(["node_modules/c8/bin/c8.js", "--all", "--include=main/**/*.ts", "--include=shared/**/*.ts",
  "--include=declaration/**/*.ts", "--include=renderer/src/**/*.ts", "--exclude=renderer/src/bridges/web-bridge.ts",
  "--reporter=json", "--reports-dir=coverage/native", "node", "scripts/test-unit.mjs"]);
run(["node_modules/vitest/vitest.mjs", "run", "--coverage"]);
const native = JSON.parse(readFileSync("coverage/native/coverage-final.json", "utf8"));
const renderer = JSON.parse(readFileSync("coverage/renderer/coverage-final.json", "utf8"));
const coverage = createCoverageMap(native);
for (const [path, data] of Object.entries(renderer)) {
  // Electron 模块在 Node 的 --all 中只有零计数占位，使用执行过的 Vitest 映射。
  const current = coverage.files().includes(path) ? coverage.fileCoverageFor(path) : null;
  if (current && current.toSummary().lines.covered === 0) {
    coverage.data[path] = createCoverageMap({ [path]: data }).fileCoverageFor(path);
  } else coverage.merge({ [path]: data });
}
mkdirSync("coverage/combined", { recursive: true });
const context = libReport.createContext({ dir: "coverage/combined", coverageMap: coverage });
for (const format of ["text", "html", "json", "json-summary"]) reports.create(format).execute(context);
const summary = coverage.getCoverageSummary();
writeFileSync("coverage/combined/summary.json", JSON.stringify(summary.toJSON(), null, 2));
let failed = false;
for (const metric of ["lines", "branches", "functions", "statements"]) {
  const value = summary[metric].pct;
  console.log("覆盖率 " + metric + ": " + value + "% / 门禁 80%");
  if (value < 80) failed = true;
}
for (const path of coverage.files().filter(p => /(?:engine-launch|task-controller|stage-accounting|checkout-session|credential-registry|problem-import|artifact-browser|commerce|content|preferences-store|update[\\/]controller)\.(?:ts)$/.test(p))) {
  if (coverage.fileCoverageFor(path).toSummary().lines.pct < 80) {
    console.error("模块行覆盖率未达标: " + path); failed = true;
  }
}
if (failed) process.exitCode = 1;
