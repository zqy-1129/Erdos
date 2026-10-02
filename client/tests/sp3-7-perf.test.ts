/**
 * SP3-7 非功能测量前置测试（node:test）：冷启动计时与预算判定（PRD 6.1：<3s）。
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { BootTimer, isColdStartWithin } from "../renderer/src/engine/boot-mark.ts";
import { CrashGuard, type EventSourceLike, type ErrorLikeEvent } from "../renderer/src/engine/crash-guard.ts";

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

describe("崩溃率守卫（CrashGuard）", () => {
  /** 可编程事件源：捕获监听器并手动触发。 */
  class FakeSource implements EventSourceLike {
    handlers = new Map<string, (event: ErrorLikeEvent) => void>();
    addEventListener(type: string, handler: (event: ErrorLikeEvent) => void): void {
      this.handlers.set(type, handler);
    }
    removeEventListener(type: string): void {
      this.handlers.delete(type);
    }
    fire(type: string, event: ErrorLikeEvent): void {
      this.handlers.get(type)?.(event);
    }
  }

  it("捕获 error 与 unhandledrejection 并累计统计", () => {
    const source = new FakeSource();
    const crashes: Array<[string, string]> = [];
    const guard = new CrashGuard((kind, detail) => crashes.push([kind, detail]), source, () => 1_000);
    guard.attach();
    source.fire("error", { message: "boom" });
    source.fire("unhandledrejection", { reason: "promise fail" });
    assert.deepEqual(guard.stats(), {
      errors: 1,
      rejections: 1,
      lastAt: new Date(1_000).toISOString(),
    });
    assert.deepEqual(crashes, [
      ["error", "boom"],
      ["unhandledrejection", "promise fail"],
    ]);
  });

  it("卸除监听后不再统计；无事件源时 attach 为空操作", () => {
    const source = new FakeSource();
    const guard = new CrashGuard(undefined, source);
    const detach = guard.attach();
    detach();
    source.fire("error", { message: "late" });
    assert.equal(guard.stats().errors, 0);

    const orphan = new CrashGuard(undefined, null);
    orphan.attach();
    assert.deepEqual(orphan.stats(), { errors: 0, rejections: 0, lastAt: null });
  });
});