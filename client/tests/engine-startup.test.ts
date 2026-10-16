/** G42：真实子进程验证拒启、正常退出与崩溃的宿主处置，无需模型或引擎环境。 */
import assert from "node:assert/strict";
import { it } from "node:test";
import { EngineHost, type EngineHostState } from "../main/engine-host/host.ts";

const credential = "sk-fixture-refusal12345678";
const fixture = String.raw`
const { createInterface } = require("node:readline");
createInterface({ input: process.stdin }).on("line", line => {
  if (!line.trim()) return;
  const request = JSON.parse(line);
  const mode = process.env.ERDOS_FIXTURE_MODE;
  if (mode === "refuse" || mode === "crash" ||
      (mode === "refuse-after-ready" && request.method === "get_status")) {
    process.stderr.write("ERDOS_TOOL_MODE 配置错误 sk-fixture-refusal12345678\n", () => process.exit(mode === "crash" ? 1 : 2));
    return;
  }
  if (mode === "exit" && request.method === "get_status") { process.exit(0); return; }
  const result = request.method === "initialize"
    ? { compatible: true, engine_version: "fixture", protocol_version: 2 }
    : { engine: { status: "idle" }, task: null };
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id: request.id, result }) + "\n");
});`;

function makeHost(mode: () => string) {
  const states: EngineHostState[] = [];
  const logs: string[] = [];
  const host = new EngineHost({
    command: process.execPath, args: ["-e", fixture], home: "fixture-home",
    environment: () => ({ ERDOS_FIXTURE_MODE: mode() }), key: () => null,
    onEvent: () => {}, onStateChange: state => states.push(state),
    onLog: line => logs.push(line), onProtocolError: () => {},
  });
  return { host, states, logs };
}

async function waitFor(condition: () => boolean) {
  const start = Date.now();
  while (!condition()) {
    assert.ok(Date.now() - start < 8000, "宿主状态未按时收敛");
    await new Promise(resolve => setTimeout(resolve, 20));
  }
}

it("退出码2立即给出脱敏诊断，状态轮询不重启，修正配置后可显式重载", async () => {
  let mode = "refuse";
  const { host, states, logs } = makeHost(() => mode);
  try {
    await assert.rejects(host.invoke("get_status", {}), error => {
      assert.match(String(error), /ENGINE_STARTUP_REFUSED.*ERDOS_TOOL_MODE/);
      assert.ok(!String(error).includes(credential));
      return true;
    });
    assert.equal(host.currentState, "failed");
    for (let index = 0; index < 3; index += 1) {
      await assert.rejects(host.invoke("get_status", {}), /ENGINE_STARTUP_REFUSED/);
    }
    await new Promise(resolve => setTimeout(resolve, 1150));
    assert.equal(states.filter(state => state === "spawning").length, 1);
    assert.ok(!logs.some(line => line.includes("次重启")));
    assert.ok(!logs.join("\n").includes(credential));
    mode = "ready";
    await host.reloadKey();
    assert.equal(host.currentState, "ready");
    assert.equal(states.filter(state => state === "spawning").length, 2);
    await host.invoke("get_status", {});
  } finally { host.stop(); await waitFor(() => host.childPid === null); }
});

it("握手后退出码2同样停止自动重启，当前请求收到拒启原因", async () => {
  const { host, states } = makeHost(() => "refuse-after-ready");
  try {
    await host.reloadKey();
    await assert.rejects(host.invoke("get_status", {}), /ENGINE_STARTUP_REFUSED.*ERDOS_TOOL_MODE/);
    assert.equal(host.currentState, "failed");
    assert.ok(!states.includes("crashed"));
  } finally { host.stop(); await waitFor(() => host.childPid === null); }
});

it("退出码0收敛为stopped，无崩溃重启", async () => {
  const { host, states, logs } = makeHost(() => "exit");
  try {
    await host.reloadKey();
    await assert.rejects(host.invoke("get_status", {}), /ENGINE_RESTART/);
    assert.equal(host.currentState, "stopped");
    assert.ok(!states.includes("crashed"));
    assert.ok(!logs.some(line => line.includes("次重启")));
  } finally { host.stop(); await waitFor(() => host.childPid === null); }
});

it("退出码1仍由看门狗重启，下一次启动成功后可继续调用", async () => {
  let attempts = 0;
  const { host, states } = makeHost(() => attempts++ === 0 ? "crash" : "ready");
  try {
    await assert.rejects(host.reloadKey(), /ENGINE_RESTART/);
    await waitFor(() => host.currentState === "ready");
    assert.ok(states.includes("crashed"));
    assert.equal(attempts, 2);
    await host.invoke("get_status", {});
  } finally { host.stop(); await waitFor(() => host.childPid === null); }
});
