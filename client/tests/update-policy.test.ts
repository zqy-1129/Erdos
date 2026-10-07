/**
 * 自动更新策略单测（FE-PKG W18 / F-102）：渠道解析、渠道门禁、版本比较、更新决策。
 * 覆盖红线：preview 渠道禁用自动更新；低于最低支持版本 → 强制升级。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  compareVersions,
  decideUpdate,
  isAutoUpdateEnabled,
  resolveChannel,
} from "../main/update/policy.ts";

describe("resolveChannel 渠道解析", () => {
  it("ERDOS_CHANNEL 显式指定优先", () => {
    assert.equal(resolveChannel({ ERDOS_CHANNEL: "preview" }, false), "preview");
    assert.equal(resolveChannel({ ERDOS_CHANNEL: "ALPHA" }, true), "alpha");
  });

  it("未指定：dev 运行 → dev；正式运行 → production", () => {
    assert.equal(resolveChannel({}, true), "dev");
    assert.equal(resolveChannel({}, false), "production");
  });

  it("非法值回落（不静默放行到正式渠道）", () => {
    assert.equal(resolveChannel({ ERDOS_CHANNEL: "beta" }, true), "dev");
    assert.equal(resolveChannel({ ERDOS_CHANNEL: "beta" }, false), "production");
  });
});

describe("渠道门禁（F-102）", () => {
  it("preview 禁用自动更新（产品约束）", () => {
    assert.equal(isAutoUpdateEnabled("preview"), false);
  });

  it("dev 禁用（本地调试不发起更新）", () => {
    assert.equal(isAutoUpdateEnabled("dev"), false);
  });

  it("alpha / production 允许", () => {
    assert.equal(isAutoUpdateEnabled("alpha"), true);
    assert.equal(isAutoUpdateEnabled("production"), true);
  });
});

describe("compareVersions 版本比较", () => {
  it("数字段逐位比较", () => {
    assert.equal(compareVersions("1.2.3", "1.2.3"), 0);
    assert.equal(compareVersions("1.2.10", "1.2.9"), 1);
    assert.equal(compareVersions("1.3.0", "1.2.9"), 1);
    assert.equal(compareVersions("2.0.0", "10.0.0"), -1);
  });

  it("容忍 v 前缀与预发布后缀（后缀不参与数值比较）", () => {
    assert.equal(compareVersions("v1.2.3", "1.2.3"), 0);
    assert.equal(compareVersions("1.2.3-beta.1", "1.2.3"), 0);
  });

  it("位数不足按 0 补（1.2 == 1.2.0）", () => {
    assert.equal(compareVersions("1.2", "1.2.0"), 0);
  });
});

describe("decideUpdate 更新决策", () => {
  it("preview 渠道 → none（即使有新版本）", () => {
    const decision = decideUpdate("1.0.0", { version: "2.0.0" }, "preview");
    assert.equal(decision.action, "none");
    assert.match(decision.reason, /preview/);
  });

  it("已是最新（或本地更新）→ none", () => {
    assert.equal(decideUpdate("1.2.0", { version: "1.2.0" }, "production").action, "none");
    assert.equal(decideUpdate("1.3.0", { version: "1.2.0" }, "production").action, "none");
  });

  it("有新版本且高于最低支持 → optional", () => {
    const decision = decideUpdate("1.2.0", { version: "1.3.0", minimumSupported: "1.0.0" }, "production");
    assert.equal(decision.action, "optional");
    assert.match(decision.reason, /1\.3\.0/);
  });

  it("当前版本低于最低支持 → forced（强制升级）", () => {
    const decision = decideUpdate("1.0.0", { version: "1.3.0", minimumSupported: "1.2.0" }, "alpha");
    assert.equal(decision.action, "forced");
    assert.match(decision.reason, /1\.2\.0/);
  });

  it("无 minimumSupported → 不强制（optional）", () => {
    assert.equal(decideUpdate("1.0.0", { version: "1.1.0" }, "production").action, "optional");
  });
});