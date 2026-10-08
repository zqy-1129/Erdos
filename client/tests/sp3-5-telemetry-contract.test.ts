/**
 * 遥测契约守护（客户端侧）：SDK 的事件名白名单与隐私违禁字段必须与 contracts 逐字一致。
 *
 * 与 server/tests/unit/test_telemetry_contract.py 成对：跨仓不互相 import，
 * 两侧各自读同一份 contracts/telemetry.schema.json。这条边界此前没有任何机器检查，
 * 而它守的是"题面/API Key/文件路径/论文内容永不上报"的隐私红线。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import { FORBIDDEN_PROPS, VALID_EVENT_NAMES } from "../main/telemetry/sdk.ts";

const contract = JSON.parse(
  readFileSync(new URL("../../contracts/telemetry.schema.json", import.meta.url), "utf-8"),
) as { events: Record<string, unknown>; propsWhitelist: { forbidden: string[] } };

const asSorted = (items: Iterable<string>): string[] => [...items].sort();

describe("遥测契约对齐（SDK ↔ contracts/telemetry.schema.json）", () => {
  it("事件名白名单与契约逐字相等", () => {
    assert.deepEqual(
      asSorted(VALID_EVENT_NAMES),
      asSorted(Object.keys(contract.events)),
      "事件名漂移：SDK 与契约必须同时改（改契约要走契约评审）",
    );
  });

  it("违禁字段名单与契约逐字相等", () => {
    assert.deepEqual(
      asSorted(FORBIDDEN_PROPS),
      asSorted(contract.propsWhitelist.forbidden),
      "违禁名单漂移：任一侧单独删字段都会放开一条隐私红线",
    );
  });

  it("四类必禁数据仍在名单内（防止白名单被精简掉）", () => {
    const joined = asSorted(FORBIDDEN_PROPS).join(" ").toLowerCase();
    for (const category of ["prompt", "api_key", "file_path", "paper_content"]) {
      assert.ok(joined.includes(category), `违禁名单缺少 ${category} 类字段`);
    }
    assert.ok(joined.includes("trail_detail") && joined.includes("model_output"));
  });
});
