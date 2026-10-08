/**
 * 云端正版鉴权联调测试（真实服务端 SP2-2）：服务端不可达时整体跳过，
 * 本地未启动 server 不影响单测门禁（启动方式见报告/UAT 手册）。
 *
 * 覆盖：健康探测 → 注册（唯一账号，指纹必填）→ 受保护端点（Bearer，证明登录态真实可用）
 * → 注销（吊销刷新令牌）→ 密码登录 → 错误密码归因（credentials）。
 * 地址可用 ERDOS_API_BASE_URL 覆盖（默认本机 http://127.0.0.1:8000）。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { AuthFailureError, CloudAuthBridge } from "../main/ipc/cloud-auth.ts";
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
    const bridge = new CloudAuthBridge({
      baseUrl: BASE_URL,
      fingerprint: newFingerprint(),
      platform: "test",
      timeoutMs: 5000,
    });

    // 注册即登录
    const registered = await bridge.register({ username: email, password });
    assert.equal(registered.username, email);
    assert.ok(registered.expiresInMs > 0, "访问令牌有效期应为正数");
    assert.equal(bridge.signedIn(), true);

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

    // 密码登录（同一账号）→ 再次进入登录态
    const logged = await bridge.login({ username: email, password });
    assert.equal(logged.username, email);
    assert.equal(bridge.signedIn(), true);

    // 错误密码 → 可读归因（credentials）
    await assert.rejects(
      () => bridge.login({ username: email, password: "Wrong0Pass" }),
      (error: unknown) => error instanceof AuthFailureError && error.reason === "credentials",
    );
  });
});