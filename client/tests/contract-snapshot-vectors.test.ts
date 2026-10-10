/**
 * 快照/许可签名金样例守护（客户端侧）：与 server/tests/unit/test_snapshot_vectors.py
 * 读同一份 contracts/snapshot-vectors.json，各自断言。
 *
 * 为什么不能只靠本仓手抄示例：sp3-3 里的断言把期望值写死在 TS 里，服务端改了字节序
 * 客户端不会红——只有两边共用同一份文件，"字节序漂移"才变成可机器判定的故障。
 * 金样例由一次性测试密钥签发（私钥未落盘），只发公钥 SPKI DER。
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { canonical, canonicalBytes, verifySignature } from "../main/entitlement/verify.ts";

const vectorsPath = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "contracts",
  "snapshot-vectors.json",
);

interface Vector {
  id: string;
  kind: string;
  payload: Record<string, unknown>;
  canonical: string;
  signature: string;
  expect_valid?: boolean;
}

const spec = JSON.parse(readFileSync(vectorsPath, "utf-8")) as {
  key: { public_key_spki_der_hex: string };
  vectors: Vector[];
};
const publicKeyDer = Buffer.from(spec.key.public_key_spki_der_hex, "hex");

describe("快照签名金样例（客户端侧）", () => {
  it("规范化字节逐字等于金样例（键排序 / 紧凑 / ensure_ascii / 整数写法）", () => {
    for (const vector of spec.vectors) {
      assert.equal(canonical(vector.payload), vector.canonical, vector.id);
      assert.deepEqual(
        canonicalBytes(vector.payload),
        Buffer.from(vector.canonical, "utf8"),
        vector.id,
      );
    }
  });

  it("正向量验签通过、篡改向量验签失败", () => {
    for (const vector of spec.vectors) {
      const expectValid = vector.expect_valid ?? true;
      assert.equal(
        verifySignature(canonicalBytes(vector.payload), vector.signature, publicKeyDer),
        expectValid,
        vector.id,
      );
    }
  });

  it("金样例覆盖了非 ASCII、null 与篡改三类边界", () => {
    const ids = spec.vectors.map((v) => v.id);
    assert.ok(ids.includes("canonical-non-ascii-and-null"), "缺非 ASCII 向量：ensure_ascii 失去守护");
    assert.ok(ids.includes("snapshot-frozen-no-subscription"), "缺 null 向量");
    assert.ok(ids.includes("tamper-purchased-boost"), "缺篡改向量：验签失败路径未被测到");
  });

  it("文档第 2 节示例串就是金样例（人读的与机器判定的同源）", () => {
    const docPath = join(dirname(vectorsPath), "snapshot.md");
    const block = readFileSync(docPath, "utf-8").split("```json")[1].split("```")[0].trim();
    const docExample = spec.vectors.find((v) => v.id === "snapshot-doc-example");
    assert.ok(docExample);
    assert.equal(block, docExample!.canonical);
    assert.equal(canonical(JSON.parse(block)), docExample!.canonical);
  });
});
