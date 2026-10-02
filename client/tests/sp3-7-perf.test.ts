/**
 * SP3-7 非功能测量前置测试（node:test）：冷启动计时与预算判定（PRD 6.1：<3s）。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { BootTimer, isColdStartWithin } from "../renderer/src/engine/boot-mark.ts";

function makeTimer(): BootTimer {
  let now = 0;
  return new BootTimer(() => now++); // 每次调用 +1ms 模拟推进
}

describe("冷启动计时（BootTimer）", () => {
  it("未打点返回 null；同名只记首次", () => {
    const timer = makeTimer();
    assert.equal(timer.appReadyMs(), null);
    timer.mark("boot");
    const first = timer.snapshot()["boot"];
    timer.mark("boot");
    assert.equal(timer.snapshot()["boot"], first);
  });

  it("boot → first-render 耗时与预算判定", () => {
    const timer = makeTimer();
    timer.mark("boot");
    timer.mark("first-render");
    assert.equal(timer.appReadyMs(), 1);
    assert.equal(isColdStartWithin(timer.appReadyMs(), 3000), true);
  });

  it("超预算（≥3000ms）判定失败；未测量返回 null", () => {
    let now = 0;
    const slow = new BootTimer(() => (now += 4000));
    slow.mark("boot"); // 4000
    slow.mark("first-render"); // 8000
    assert.equal(slow.appReadyMs(), 4000);
    assert.equal(isColdStartWithin(slow.appReadyMs(), 3000), false);
    assert.equal(isColdStartWithin(null, 3000), null);
  });
});