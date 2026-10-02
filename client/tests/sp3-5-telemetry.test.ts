/**
 * SP3-5 遥测 SDK 测试：事件白名单 + 违禁字段本地剥离（隐私红线）、
 * 本地 outbox 缓冲、批量分批上报、断网重试/退避、失败保留（不丢数据）、
 * 服务端拒绝汇总、自动 flush 阈值，以及 createCloudTelemetry 对接真实桩服全链路。
 */

import assert from "node:assert/strict";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { describe, it } from "node:test";

import { CloudHttpClient } from "../main/cloud/http.ts";
import {
  InMemoryOutboxStore,
  TelemetrySdk,
  createCloudTelemetry,
  type QueuedEvent,
  type TelemetryUploader,
  type UploadResult,
} from "../main/telemetry/sdk.ts";

function envelope(data: unknown): string {
  return JSON.stringify({ code: 0, message: "ok", detail: null, data });
}

/** 可编程上传器：记录调用、可注入失败。 */
class FakeUploader implements TelemetryUploader {
  readonly calls: QueuedEvent[][] = [];
  /** 剩余失败次数（>0 时抛网络错误）。 */
  failures = 0;

  async upload(events: QueuedEvent[]): Promise<UploadResult> {
    this.calls.push(events);
    if (this.failures > 0) {
      this.failures -= 1;
      throw new Error("network down");
    }
    return { accepted: events.length, rejected: 0, reasons: [] };
  }
}

/** 部分拒绝上传器（模拟服务端 schema 拦截）。 */
class PartialRejectUploader implements TelemetryUploader {
  async upload(events: QueuedEvent[]): Promise<UploadResult> {
    return {
      accepted: 1,
      rejected: events.length - 1,
      reasons: ["非法事件名：hack"],
    };
  }
}

const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

describe("TelemetrySdk 采集与过滤", () => {
  it("事件名白名单校验：非法事件本地丢弃不上报", () => {
    const uploader = new FakeUploader();
    const sdk = new TelemetrySdk({ uploader, autoFlushAt: 100 });
    assert.ok(sdk.capture({ event_name: "page_view", distinct_id: "u1" }));
    assert.equal(sdk.capture({ event_name: "hack_event", distinct_id: "u1" }), null);
    assert.equal(sdk.capture({ event_name: "page_view", distinct_id: "" }), null);
    assert.equal(sdk.capture({ event_name: "page_view", distinct_id: "   " }), null);
    assert.equal(sdk.queued(), 1);
  });

  it("隐私红线：违禁字段（题面/Key/路径）采集时即剥离", () => {
    const outbox = new InMemoryOutboxStore();
    const sdk = new TelemetrySdk({ uploader: new FakeUploader(), store: outbox, autoFlushAt: 100 });
    sdk.capture({
      event_name: "app_error",
      distinct_id: "u1",
      props: {
        error_code: "E1",
        message: "boom",
        api_key: "sk-secret",
        file_path: "C:\\answers\\p1.txt",
        prompt_zh: "请证明歌德巴赫猜想",
      },
    });
    const rows = outbox.list(10);
    assert.equal(rows.length, 1);
    const stored = JSON.parse(rows[0].props) as Record<string, unknown>;
    assert.deepEqual(Object.keys(stored).sort(), ["error_code", "message"]);
    // 出盒行补齐上报字段
    assert.equal(rows[0].channel, "stable");
    assert.equal(rows[0].app_version, "");
  });
});

describe("TelemetrySdk 批量上报与重试", () => {
  it("flush 分批（≤batchSize）上报并清空 outbox", async () => {
    const uploader = new FakeUploader();
    const sdk = new TelemetrySdk({ uploader, batchSize: 2, autoFlushAt: 100 });
    for (let i = 0; i < 5; i++) {
      sdk.capture({ event_name: "page_view", distinct_id: `u${i}`, props: { page: "home" } });
    }
    const result = await sdk.flush();
    assert.equal(uploader.calls.length, 3);
    assert.deepEqual(
      uploader.calls.map((batch) => batch.length),
      [2, 2, 1],
    );
    assert.equal(result.accepted, 5);
    assert.equal(sdk.queued(), 0);
  });

  it("断网重试：指数退避后成功，数据不丢", async () => {
    const uploader = new FakeUploader();
    uploader.failures = 2;
    const slept: number[] = [];
    const sdk = new TelemetrySdk({
      uploader,
      maxAttempts: 3,
      backoffMs: 500,
      sleep: async (ms: number) => {
        slept.push(ms);
      },
      autoFlushAt: 100,
    });
    sdk.capture({ event_name: "stage_start", distinct_id: "u1", props: { stage: "analysis" } });
    const result = await sdk.flush();
    assert.equal(result.accepted, 1);
    assert.deepEqual(slept, [500, 1000]); // 第 2、3 次尝试前各退避一次
    assert.equal(sdk.queued(), 0);
  });

  it("重试耗尽仍失败：抛出错误且保留 outbox（断网不丢数据）", async () => {
    const uploader = new FakeUploader();
    uploader.failures = 99;
    const sdk = new TelemetrySdk({
      uploader,
      maxAttempts: 2,
      backoffMs: 1,
      sleep: async () => {},
      autoFlushAt: 100,
    });
    sdk.capture({ event_name: "feature_click", distinct_id: "u1", props: { feature: "solve" } });
    await assert.rejects(() => sdk.flush(), /遥测上报失败/);
    assert.equal(sdk.queued(), 1); // 事件仍在本地缓冲
  });

  it("服务端部分拒绝：已受理批次移除并汇总 reasons", async () => {
    const sdk = new TelemetrySdk({
      uploader: new PartialRejectUploader(),
      autoFlushAt: 100,
      batchSize: 10,
    });
    sdk.capture({ event_name: "page_view", distinct_id: "a" });
    sdk.capture({ event_name: "stage_fail", distinct_id: "b", props: { error_kind: "timeout" } });
    const result = await sdk.flush();
    assert.equal(result.accepted, 1);
    assert.equal(result.rejected, 1);
    assert.deepEqual(result.reasons, ["非法事件名：hack"]);
    assert.equal(sdk.queued(), 0);
  });

  it("达到 autoFlushAt 自动后台 flush", async () => {
    const uploader = new FakeUploader();
    const sdk = new TelemetrySdk({ uploader, autoFlushAt: 2, batchSize: 10 });
    sdk.capture({ event_name: "page_view", distinct_id: "u1" });
    sdk.capture({ event_name: "page_view", distinct_id: "u2" });
    await tick();
    await tick();
    assert.equal(uploader.calls.length, 1);
    assert.equal(uploader.calls[0].length, 2);
  });
});

describe("createCloudTelemetry 云端全链路（桩服）", () => {
  it("批量上报：过滤后的事件经 HTTP 抵达，附带 Bearer 头", async () => {
    const received: QueuedEvent[] = [];
    let authHeader: string | null = null;
    const server = createServer((req: IncomingMessage, res: ServerResponse) => {
      const url = req.url ?? "";
      res.setHeader("content-type", "application/json");
      authHeader = req.headers["authorization"] ?? null;
      if (url === "/v1/telemetry/events" && req.method === "POST") {
        let raw = "";
        req.on("data", (chunk: Buffer) => (raw += chunk.toString()));
        req.on("end", () => {
          const body = JSON.parse(raw) as { events: QueuedEvent[] };
          received.push(...body.events);
          res.end(envelope({ accepted: body.events.length, rejected: 0, reasons: [] }));
        });
        return;
      }
      res.statusCode = 404;
      res.end(JSON.stringify({ code: 40401, message: "not found", detail: null }));
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const port = (server.address() as AddressInfo).port;
    try {
      const sdk = createCloudTelemetry({
        baseUrl: `http://127.0.0.1:${port}`,
        getToken: async () => "tok-1",
        batchSize: 10,
      });
      sdk.capture({
        event_name: "page_view",
        distinct_id: "u1",
        props: { page: "home", api_key: "sk-should-be-stripped" },
        app_version: "0.1.0",
        os: "windows",
        channel: "dev",
      });
      sdk.capture({ event_name: "stage_success", distinct_id: "u2", props: { duration_ms: 1200 } });
      const result = await sdk.flush();
      assert.equal(result.accepted, 2);
      assert.equal(authHeader, "Bearer tok-1");
      assert.equal(received.length, 2);
      assert.deepEqual(received[0].props, { page: "home" }); // 违禁字段未出网
      assert.equal(received[0].app_version, "0.1.0");
      assert.equal(received[0].os, "windows");
      assert.equal(received[0].channel, "dev");
    } finally {
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });

  it("云端信封非 0 业务错误经 CloudHttpClient 抛出", async () => {
    const server = createServer((_req: IncomingMessage, res: ServerResponse) => {
      res.setHeader("content-type", "application/json");
      res.statusCode = 400;
      res.end(JSON.stringify({ code: 40001, message: "载荷校验失败", detail: null }));
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const port = (server.address() as AddressInfo).port;
    try {
      const http = new CloudHttpClient({ baseUrl: `http://127.0.0.1:${port}`, maxRetries: 0 });
      await assert.rejects(
        () => http.get("/v1/telemetry/events"),
        /载荷校验失败/,
      );
    } finally {
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });
});