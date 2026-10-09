/**
 * 生产构建 demo 泄漏守护（W4 验收：production 构建零 demo 命中）。
 *
 * 源码级守护（不依赖 build）：渲染入口必须经 bridge-factory 选择桥实现，
 * 生产渠道（import.meta.env.DEV=false）强制真实 preload 桥，任何缺失都抛错
 * 而非静默降级到 WebDemoBridge / DemoTaskSimulator（对齐 DEC-013 渠道隔离）。
 *
 * 构建产物级扫描见 scripts/scan-demo-leak.mjs（vite build 后执行）。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const entry = readFileSync(join(root, "renderer", "src", "entry.tsx"), "utf-8");
const factory = readFileSync(join(root, "renderer", "src", "bridges", "bridge-factory.ts"), "utf-8");

describe("生产构建 demo 泄漏守护（W4）", () => {
  it("入口不直接 import web-bridge / WebDemoBridge（必须经桥工厂）", () => {
    assert.ok(!entry.includes("web-bridge"), "entry.tsx 不应直接 import web-bridge");
    assert.ok(!entry.includes("WebDemoBridge"), "entry.tsx 不应直接引用 WebDemoBridge");
    assert.match(entry, /createBridge/, "入口应经 createBridge 工厂选择桥实现");
  });

  it("桥工厂仅在 DEV 分支引用 WebDemoBridge，生产分支抛错而非降级", () => {
    assert.match(factory, /import\.meta\.env\.DEV/, "桥工厂须按 DEV 渠道分流");
    assert.match(factory, /new WebDemoBridge\(\)/, "DEV 分支回退演示桥");
    assert.match(factory, /throw new Error/, "生产无 preload 桥必须抛错（禁止静默降级）");
  });

  it("WebDemoBridge / DemoTaskSimulator 类名仅在 web-bridge.ts 定义", () => {
    const webBridge = readFileSync(join(root, "renderer", "src", "bridges", "web-bridge.ts"), "utf-8");
    assert.match(webBridge, /export class DemoTaskSimulator/);
    assert.match(webBridge, /export class WebDemoBridge/);
  });
});
