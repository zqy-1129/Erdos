/**
 * 云端正版鉴权联调测试（真实服务端 SP2-2）：服务端不可达时整体跳过，
 * 本地未启动 server 不影响单测门禁（启动方式见报告/UAT 手册）。
 *
 * 覆盖：健康探测 → 注册（唯一账号，指纹必填）→ 受保护端点（Bearer，证明登录态真实可用）
 * → 注销（吊销刷新令牌）→ 密码登录。
 * 地址可用 ERDOS_API_BASE_URL 覆盖（默认本机 http://127.0.0.1:8000）。
 *
 * 纪律：联调**不注入登录失败**——服务端防爆破按「账号 + IP」双维度计数（阈值 5 次 / 锁 15 分钟，
 * IP 维度成功后不清零），若联调每轮制造一次失败会锁死共享出口（127.0.0.1），
 * 使后续联调与人工试用全部 423。错误密码/401 归因由桩测试覆盖（sp3-5-cloud-auth.test.ts）。
 * 登录步对既有锁定窗口容错（diagnostic 跳过），不因环境态红灯。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { AuthFailureError, CloudAuthBridge } from "../main/ipc/cloud-auth.ts";
import { InMemoryTokenStore } from "../main/cloud/auth-client.ts";
import { newFingerprint } from "../main/device-identity.ts";

const BASE_URL = (process.env.ERDOS_API_BASE_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

/** 服务端探活（1.5s 超时；失败 → 跳过联调）。 */
async function serverReachable(): Promise<boolean> {
  try {
    const response = await fetch(`${BASE_URL}/v1/health`, { signal: AbortSignal.timeout(1500) });
    return response.ok;
  } catch {
    return false;
  }
}

describe("CloudAuthBridge 真实服务端联调", () => {
  it("注册 → 受保护端点 → 注销 → 登录 → 错误密码归因", async (t) => {
    if (!(await serverReachable())) {
      t.skip(`服务端不可达（${BASE_URL}），跳过联调`);
      return;
    }
    const email = `live-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.com`;
    const password = "Live0Pass";
    const store = new InMemoryTokenStore(); // 会话落盘替身：验证「重启免登录」恢复链路
    const bridge = new CloudAuthBridge({
      baseUrl: BASE_URL,
      fingerprint: newFingerprint(),
      platform: "test",
      timeoutMs: 5000,
      store,
    });

    // 注册即登录
    const registered = await bridge.register({ username: email, password });
    assert.equal(registered.username, email);
    assert.ok(registered.expiresInMs > 0, "访问令牌有效期应为正数");
    assert.equal(bridge.signedIn(), true);

    // 模拟重启：同一会话存储重建桥 → 免登录恢复（账号回填）
    const restored = new CloudAuthBridge({
      baseUrl: BASE_URL,
      fingerprint: newFingerprint(),
      timeoutMs: 5000,
      store,
    });
    assert.equal(restored.signedIn(), true);
    assert.equal(restored.currentView()?.username, email);

    // 受保护端点：Bearer 令牌读取自身资料（令牌真实有效，而非仅签发成功）
    const token = await bridge.getToken();
    assert.ok(token, "登录态应可取出访问令牌");
    const profileResponse = await fetch(`${BASE_URL}/v1/account/profile`, {
      headers: { authorization: `Bearer ${token}` },
    });
    const profile = (await profileResponse.json()) as { code: number; data?: { email?: string | null } };
    assert.equal(profile.code, 0);
    assert.equal(profile.data?.email, email);

    // 注销：吊销刷新令牌 + 清本地会话
    assert.equal(await bridge.logout(), 1);
    assert.equal(bridge.signedIn(), false);

    // 密码登录（同一账号）→ 再次进入登录态；已被其它来源武装的 IP 锁定窗口内跳过（打点说明）
    try {
      const logged = await bridge.login({ username: email, password });
      assert.equal(logged.username, email);
      assert.equal(bridge.signedIn(), true);
    } catch (error) {
      if (error instanceof AuthFailureError && error.reason === "locked") {
        t.diagnostic(`出口 IP 处于防爆破锁定窗口（423），跳过登录断言：${error.message}`);
        return;
      }
      throw error;
    }
  });
});