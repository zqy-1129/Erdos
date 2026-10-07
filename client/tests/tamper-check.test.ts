/**
 * 引擎篡改校验单测（FE-PKG W18 / SP5-3）：sha256 计算、完整性校验、清单解析。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { parseManifest, sha256, verifyIntegrity } from "../main/tamper-check.ts";

const HASH_HELLO = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824";

describe("sha256 计算（W18）", () => {
  it("已知明文哈希一致", () => {
    assert.equal(sha256(Buffer.from("hello")), HASH_HELLO);
  });
});

describe("verifyIntegrity 篡改校验（W18）", () => {
  it("全部一致 → ok", () => {
    const result = verifyIntegrity(
      { "engine/engine.exe": HASH_HELLO },
      { version: 1, engineVersion: "0.1.0", files: { "engine/engine.exe": HASH_HELLO } },
    );
    assert.equal(result.ok, true);
    assert.deepEqual(result.mismatches, []);
  });

  it("文件缺失 → mismatch「缺失」", () => {
    const result = verifyIntegrity(
      {},
      { version: 1, engineVersion: "0.1.0", files: { "engine/engine.exe": HASH_HELLO } },
    );
    assert.equal(result.ok, false);
    assert.deepEqual(result.mismatches, ["engine/engine.exe: 缺失"]);
  });

  it("哈希不一致 → mismatch「哈希不一致」", () => {
    const result = verifyIntegrity(
      { "engine/engine.exe": "deadbeef" },
      { version: 1, engineVersion: "0.1.0", files: { "engine/engine.exe": HASH_HELLO } },
    );
    assert.equal(result.ok, false);
    assert.deepEqual(result.mismatches, ["engine/engine.exe: 哈希不一致"]);
  });

  it("空清单 → ok（无可校验项）", () => {
    const result = verifyIntegrity({}, { version: 1, engineVersion: "0.1.0", files: {} });
    assert.equal(result.ok, true);
  });

  it("清单外多余文件不判违规（仅校验清单内项）", () => {
    const result = verifyIntegrity(
      { "engine/engine.exe": HASH_HELLO, "engine/extra.dll": "whatever" },
      { version: 1, engineVersion: "0.1.0", files: { "engine/engine.exe": HASH_HELLO } },
    );
    assert.equal(result.ok, true);
  });
});

describe("parseManifest 清单解析（W18）", () => {
  it("正常解析 files 与 engineVersion", () => {
    const manifest = parseManifest(JSON.stringify({ engineVersion: "0.1.0", files: { "a": HASH_HELLO } }));
    assert.equal(manifest.engineVersion, "0.1.0");
    assert.deepEqual(manifest.files, { a: HASH_HELLO });
  });

  it("非法 JSON → 抛错", () => {
    assert.throws(() => parseManifest("{not json"), /非法 JSON/);
  });

  it("缺 files 字段 → 抛错", () => {
    assert.throws(() => parseManifest(JSON.stringify({ engineVersion: "0.1.0" })), /缺少 files/);
  });
});
