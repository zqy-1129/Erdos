/**
 * IPC 来源校验单测（FE-HOST W3 安全基线）：生产仅 file://，开发追加 dev server；
 * 非法来源（第三方 http(s)/扩展/空值）一律拒绝。
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { isAllowedSenderUrl, isAllowedExternalUrl } from "../main/ipc/guard.ts";

const PROD = { dev: false, devServerUrl: "http://localhost:5173", rendererUrl: "file:///D:/Erdos/client/dist/index.html" };
const DEV = { ...PROD, dev: true };

describe("IPC 来源校验（W3 安全基线）", () => {
  it("生产渠道：file:// 产物页放行", () => {
    assert.equal(isAllowedSenderUrl("file:///D:/Erdos/client/dist/index.html", PROD), true);
  });

  it("生产渠道：dev server 来源拒绝（渠道隔离）", () => {
    assert.equal(isAllowedSenderUrl("http://localhost:5173/", PROD), false);
  });

  it("开发渠道：dev server 来源放行", () => {
    assert.equal(isAllowedSenderUrl("http://localhost:5173/index.html", DEV), true);
  });

  it("第三方站点一律拒绝（即使开发渠道）", () => {
    assert.equal(isAllowedSenderUrl("https://evil.example.com/", DEV), false);
    assert.equal(isAllowedSenderUrl("http://localhost:5174/", DEV), false);
  });

  it("非 http/file 协议拒绝（扩展页/数据页/about:blank）", () => {
    for (const url of ["chrome-extension://abc/page.html", "data:text/html,x", "about:blank", "devtools://x"]) {
      assert.equal(isAllowedSenderUrl(url, DEV), false, `应拒绝：${url}`);
    }
  });

  it("空值/未定义（帧已销毁）拒绝（宁严勿松）", () => {
    assert.equal(isAllowedSenderUrl("", DEV), false);
    assert.equal(isAllowedSenderUrl(null, DEV), false);
    assert.equal(isAllowedSenderUrl(undefined, DEV), false);
  });
  it("拒绝其他本地文件与相似域名，只允许自身页面的查询/哈希路由", () => {
    assert.equal(isAllowedSenderUrl(PROD.rendererUrl + "?mode=unit#/keys", PROD), true);
    for (const url of ["file:///D:/evil.html", "file://other-host/D:/Erdos/client/dist/index.html", "http://localhost:5173.evil/", "http://localhost:51730/", "http://user:pass@localhost:5173/"]) {
      assert.equal(isAllowedSenderUrl(url, DEV), false);
    }
    assert.equal(isAllowedSenderUrl(PROD.rendererUrl, { ...PROD, rendererUrl: undefined }), false);
    assert.equal(isAllowedSenderUrl("bad url", PROD), false);
    assert.equal(isAllowedSenderUrl("http://localhost:5173/", { ...DEV, devServerUrl: "invalid" }), false);
  });
  it("只向系统浏览器打开普通网页，不打开本地文件或执行协议", () => {
    for (const url of ["https://docs.example/path", "http://127.0.0.1:8000/"]) assert.equal(isAllowedExternalUrl(url), true);
    for (const url of ["file:///D:/evil.exe", "javascript:alert(1)", "data:text/html,test", "erdos://run", "https://user:pass@docs.example", "bad url"]) assert.equal(isAllowedExternalUrl(url), false);
  });
});
