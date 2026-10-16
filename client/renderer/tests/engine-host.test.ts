// @vitest-environment node
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { afterEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ spawn: vi.fn(), execFile: vi.fn() }));
vi.mock("node:child_process", () => mocks);
import { EngineHost } from "../../main/engine-host/host.ts";

afterEach(() => { vi.useRealTimers(); vi.clearAllMocks(); });

it("握手超时后引擎协作退出码0仍继续故障重启，不误判为正常停止", async () => {
  vi.useFakeTimers();
  mocks.spawn.mockImplementation(() => {
    const child = Object.assign(new EventEmitter(), {
      pid: 12345, stdin: new PassThrough(), stdout: new PassThrough(), stderr: new PassThrough(),
      kill: vi.fn(() => {
        setTimeout(() => {
          child.emit("exit", 0, null);
          child.stdout.end(); child.stderr.end();
          child.emit("close", 0, null);
        }, 1);
        return true;
      }),
    });
    return child;
  });
  const host = new EngineHost({ command: "fixture", args: [], home: "fixture-home", key: () => null,
    onEvent: () => {}, onStateChange: () => {}, onLog: () => {}, onProtocolError: () => {} });
  try {
    host.ensureStarted();
    await vi.advanceTimersByTimeAsync(10_000);
    await vi.advanceTimersByTimeAsync(1);
    expect(host.currentState).toBe("crashed");
    await vi.advanceTimersByTimeAsync(1000);
    expect(mocks.spawn).toHaveBeenCalledTimes(2);
    expect(host.currentState).toBe("spawning");
  } finally { host.stop(); }
});
