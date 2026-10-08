/**
 * 登录页注册门禁测试（试用反馈修复）：
 * 密码不足 8 位时「注册并登录」禁用且原因可见；满足强度后可点击并经桥注册（注册即登录）。
 */

import { afterEach, describe, expect, it } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";

import { fakeBridge, type FakeBridgeCall } from "./helpers.ts";
import { App, createStores } from "../src/entry.tsx";
import { BRIDGE_CHANNELS } from "../src/bridges/bridge.ts";

afterEach(cleanup);

/** 注册成功后进入工作台：预置其数据通道（避免未登记通道返回 undefined 干扰渲染）。 */
const SIGNED_IN_RESULTS: Record<string, unknown> = {
  [BRIDGE_CHANNELS.keysList]: [],
  [BRIDGE_CHANNELS.contentList]: [],
  [BRIDGE_CHANNELS.historyList]: [],
  [BRIDGE_CHANNELS.billingOverview]: { planName: "免费版", subEndAt: null, pointsBalance: 100 },
  [BRIDGE_CHANNELS.billingLedger]: [],
  [BRIDGE_CHANNELS.entitlementStatus]: { status: "ready", balance: 100, graceDeadlineMs: null },
};

function fillForm(username: string, password: string): void {
  fireEvent.change(screen.getByLabelText("账号"), { target: { value: username } });
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: password } });
}

describe("登录页注册门禁", () => {
  it("密码不足 8 位：注册按钮禁用且显示原因提示（登录按钮不受影响）", () => {
    render(<App stores={createStores(fakeBridge())} />);
    fillForm("a@b.com", "short7");
    const register = screen.getByRole("button", { name: "注册并登录" }) as HTMLButtonElement;
    expect(register.disabled).toBe(true);
    expect(screen.getByText(/密码至少 8 位/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "登录" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("满足强度后：按钮可点击，注册经桥调用并进入登录态；提示隐藏", async () => {
    const calls: FakeBridgeCall[] = [];
    const bridge = fakeBridge({
      calls,
      results: {
        ...SIGNED_IN_RESULTS,
        [BRIDGE_CHANNELS.authRegister]: { username: "a@b.com", expiresInMs: 900_000 },
      },
    });
    const stores = createStores(bridge);
    render(<App stores={stores} />);
    fillForm("a@b.com", "Test1234");

    const register = screen.getByRole("button", { name: "注册并登录" }) as HTMLButtonElement;
    expect(register.disabled).toBe(false);
    expect(screen.queryByText(/密码至少 8 位/)).toBeNull();

    await act(async () => {
      register.click();
    });
    // 注册为首次桥调用（其后 AppShell 登录态会触发 entitlement:status，非本用例关注点）
    expect(calls[0]).toEqual({
      channel: BRIDGE_CHANNELS.authRegister,
      payload: { username: "a@b.com", password: "Test1234" },
    });
    expect(stores.session.getState().status).toBe("signed-in");
  });

  it("注册失败（服务端 409 等）→ 页面展示可读错误", async () => {
    const bridge = fakeBridge({
      failChannels: new Set([BRIDGE_CHANNELS.authRegister]),
    });
    render(<App stores={createStores(bridge)} />);
    fillForm("a@b.com", "Test1234");
    await act(async () => {
      (screen.getByRole("button", { name: "注册并登录" }) as HTMLButtonElement).click();
    });
    expect(screen.getByText(/模拟网络错误/)).toBeTruthy();
  });
});