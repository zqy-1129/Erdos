/**
 * 引擎篡改校验单测（FE-PKG W18 / SP5-3）：sha256 计算、完整性校验、清单解析、
 * 目录级校验（verifyEngineDir：清单缺失/损坏/篡改一律 fail-closed）。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  collectDirHashes,
  ENGINE_MANIFEST_FILE,
  parseManifest,
  sha256,
  verifyEngineDir,
  verifyIntegrity,
} from "../main/tamper-check.ts";

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

describe("verifyEngineDir 引擎目录级校验（W18，fail-closed）", () => {
  /** 造一个带嵌套目录的引擎产物目录（含合法的 manifest.json）。 */
  function makeEngineDir(): string {
    const dir = mkdtempSync(join(tmpdir(), "erdos-engine-"));
    mkdirSync(join(dir, "internal"), { recursive: true });
    writeFileSync(join(dir, "engine.exe"), "bin");
    writeFileSync(join(dir, "internal", "python.dll"), "dll");
    const files = {
      "engine.exe": sha256(Buffer.from("bin")),
      "internal/python.dll": sha256(Buffer.from("dll")),
    };
    writeFileSync(join(dir, ENGINE_MANIFEST_FILE), JSON.stringify({ engineVersion: "0.1.0", files }));
    return dir;
  }

  it("collectDirHashes：嵌套相对路径以 / 分隔（跨平台可比对）", () => {
    const dir = makeEngineDir();
    try {
      const hashes = collectDirHashes(dir);
      assert.equal(hashes["engine.exe"], sha256(Buffer.from("bin")));
      assert.equal(hashes["internal/python.dll"], sha256(Buffer.from("dll")));
      assert.ok(ENGINE_MANIFEST_FILE in hashes, "清单文件自身也在收集中");
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("全部一致 → ok（清单外的多余文件不判违规）", () => {
    const dir = makeEngineDir();
    try {
      writeFileSync(join(dir, "extra.txt"), "unlisted");
      const result = verifyEngineDir(dir);
      assert.deepEqual(result, { ok: true, mismatches: [] });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("文件被篡改 → 哈希不一致（拒绝启动）", () => {
    const dir = makeEngineDir();
    try {
      writeFileSync(join(dir, "engine.exe"), "tampered");
      const result = verifyEngineDir(dir);
      assert.equal(result.ok, false);
      assert.deepEqual(result.mismatches, ["engine.exe: 哈希不一致"]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("文件缺失 → 缺失（拒绝启动）", () => {
    const dir = makeEngineDir();
    try {
      rmSync(join(dir, "internal", "python.dll"));
      const result = verifyEngineDir(dir);
      assert.equal(result.ok, false);
      assert.deepEqual(result.mismatches, ["internal/python.dll: 缺失"]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("清单缺失 → 拒绝（无法证明完整性）", () => {
    const dir = makeEngineDir();
    try {
      rmSync(join(dir, ENGINE_MANIFEST_FILE));
      const result = verifyEngineDir(dir);
      assert.equal(result.ok, false);
      assert.match(result.mismatches[0] ?? "", /清单缺失/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("清单损坏（非法 JSON）→ 拒绝且不抛错", () => {
    const dir = makeEngineDir();
    try {
      writeFileSync(join(dir, ENGINE_MANIFEST_FILE), "{not json");
      const result = verifyEngineDir(dir);
      assert.equal(result.ok, false);
      assert.match(result.mismatches[0] ?? "", /非法 JSON/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
