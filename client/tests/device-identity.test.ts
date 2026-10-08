/**
 * 设备指纹测试（注册赠分防刷标识）：生成/落盘/跨重启稳定/非法内容重建/受限环境降级。
 */
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, it } from "node:test";

import { ensureDeviceFingerprint, isValidFingerprint, newFingerprint } from "../main/device-identity.ts";

/** 独立临时目录（每个用例互不干扰）。 */
function tempFile(name = "device-fingerprint"): string {
  return path.join(mkdtempSync(path.join(tmpdir(), "erdos-fp-")), name);
}

describe("设备指纹", () => {
  it("首次运行生成并落盘（含目录创建），二次读取稳定一致", () => {
    const file = path.join(path.dirname(tempFile()), "nested", "device-fingerprint");
    const first = ensureDeviceFingerprint(file);
    assert.match(first, /^[0-9a-f]{32}$/);
    assert.equal(readFileSync(file, "utf-8").trim(), first);
    assert.equal(ensureDeviceFingerprint(file), first); // 跨调用（模拟重启）稳定
  });

  it("文件内容非法（过短）→ 重新生成合法指纹并覆盖落盘", () => {
    const file = tempFile();
    writeFileSync(file, "short", "utf-8");
    const value = ensureDeviceFingerprint(file);
    assert.ok(isValidFingerprint(value));
    assert.match(value, /^[0-9a-f]{32}$/);
    assert.equal(readFileSync(file, "utf-8").trim(), value);
  });

  it("不可写路径 → 降级返回会话内指纹（不抛错）", () => {
    const parent = tempFile("plain-file");
    writeFileSync(parent, "x", "utf-8");
    const value = ensureDeviceFingerprint(path.join(parent, "device-fingerprint")); // 父路径是文件：落盘必失败
    assert.ok(isValidFingerprint(value));
  });

  it("契约长度边界（8~64）判定", () => {
    assert.ok(isValidFingerprint(newFingerprint()));
    assert.equal(isValidFingerprint("1234567"), false);
    assert.equal(isValidFingerprint("12345678"), true);
    assert.equal(isValidFingerprint("a".repeat(64)), true);
    assert.equal(isValidFingerprint("a".repeat(65)), false);
    assert.equal(isValidFingerprint("   "), false);
  });
});