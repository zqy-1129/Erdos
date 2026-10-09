/**
 * preload 暴露面快照测试（FE-HOST W3 / FE-PRELOAD W4 安全基线验收）。
 *
 * 红线（客户端开发详细方案 §4.1）：preload 只暴露 invoke（请求-响应）与
 * subscribe（事件订阅）两个受限能力；不暴露 Node/Electron 危险 API；
 * 渲染层不直连 Node/文件/SQLite。
 *
 * preload.ts 在运行时 import electron，无法在 node --test 直接加载，
 * 故对源码做静态断言（只读，守护暴露面不扩张）。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

const preloadPath = join(dirname(fileURLToPath(import.meta.url)), "..", "main", "preload.ts");
const src = readFileSync(preloadPath, "utf-8");

/** 提取 erdosBridge 对象字面体的顶层 key（2 空格缩进的标识符后跟 `<` 或 `(`）。 */
function extractTopLevelKeys(source: string): string[] {
  const start = source.indexOf("const erdosBridge = {");
  assert.notEqual(start, -1, "应存在 `const erdosBridge = {` 对象字面量");
  const open = source.indexOf("{", start);
  assert.notEqual(open, -1, "应存在对象体左花括号");
  let depth = 0;
  let i = open;
  for (; i < source.length; i++) {
    const ch = source[i];
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) break;
    }
  }
  const body = source.slice(open + 1, i);
  const keys: string[] = [];
  const re = /^ {2}([A-Za-z_$][\w$]*)\s*[<(]/gm;
  let m: RegExpExecArray | null;
  while ((m = re.exec(body)) !== null) keys.push(m[1]);
  return keys;
}

describe("preload 暴露面快照（安全基线 §4.1）", () => {
  it("只调用一次 exposeInMainWorld，且全局键名为 erdos", () => {
    const matches = src.match(/exposeInMainWorld\s*\(\s*["']([^"']+)["']/g) ?? [];
    assert.equal(matches.length, 1, "exposeInMainWorld 只允许调用一次");
    assert.match(src, /exposeInMainWorld\s*\(\s*["']erdos["']/, "全局键名必须是 erdos");
  });

  it("erdos 桥只暴露 invoke 与 subscribe 两个能力", () => {
    assert.deepEqual(
      extractTopLevelKeys(src).sort(),
      ["invoke", "subscribe"].sort(),
      "preload 暴露面扩张：只允许 invoke / subscribe",
    );
  });

  it("不暴露 Node/Electron 危险 API（零直连文件/进程/系统）", () => {
    const forbidden = [
      "ipcRenderer.send", // 事件订阅只用 on/removeListener，禁止 send（渲染层不得主动发事件）
      "require(",
      "process.env",
      'exposeInMainWorld("node"',
      'exposeInMainWorld("require"',
      "shell",
      "child_process",
      "node:fs",
      "node:path",
    ];
    for (const frag of forbidden) {
      assert.ok(!src.includes(frag), `preload 不应出现：${frag}`);
    }
  });

  it("事件订阅经 ipcRenderer.on/removeListener 成对管理（可退订）", () => {
    assert.match(src, /ipcRenderer\.on\(/);
    assert.match(src, /ipcRenderer\.removeListener\(/);
  });
});
