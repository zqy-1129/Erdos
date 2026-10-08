"""遥测契约守护：事件名与违禁字段三方对齐（contracts ↔ 服务端 ↔ 客户端 SDK）。

为什么补这一条：其余三条边界都有机器守护（openapi 有 contract_audit + 鉴权矩阵、engine-rpc 有
test_contract_alignment 三边一致性），唯独遥测契约没有——事件名与隐私违禁名单靠人肉同步，
而它守的是"题面/Key/路径/论文内容永不上报"这条隐私红线。冻结评审前必须让它可机器判定。

客户端侧的同一份对齐由 client/tests/sp3-5-telemetry-contract.test.ts 承担（跨仓不互相 import，
两侧各自读 contracts/telemetry.schema.json 这一份真源）。
"""

import json
from pathlib import Path

from app.domain.telemetry.ports import FORBIDDEN_PROPS, VALID_EVENT_NAMES

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "telemetry.schema.json"


def _spec() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_event_names_match_contract_exactly() -> None:
    """事件名集合必须与契约逐字相等：多一个=接受未登记事件，少一个=丢合法事件。"""
    assert VALID_EVENT_NAMES == frozenset(_spec()["events"]), (
        f"只在服务端：{sorted(VALID_EVENT_NAMES - set(_spec()['events']))}；"
        f"只在契约：{sorted(set(_spec()['events']) - VALID_EVENT_NAMES)}"
    )


def test_forbidden_props_match_contract_exactly() -> None:
    """违禁字段名单与契约一致（隐私红线：题面/Key/文件路径/论文内容/留痕明细不上报）。"""
    contract_forbidden = frozenset(_spec()["propsWhitelist"]["forbidden"])
    assert FORBIDDEN_PROPS == contract_forbidden, (
        f"只在服务端：{sorted(FORBIDDEN_PROPS - contract_forbidden)}；"
        f"只在契约：{sorted(contract_forbidden - FORBIDDEN_PROPS)}"
    )


def test_contract_still_declares_the_four_privacy_categories() -> None:
    """契约本身不得被改坏：四类必禁数据（题面/Key/路径/论文）必须都在名单里。

    这条看似冗余，防的是"精简白名单"时把某类删掉——删掉就等于放开一条隐私红线。
    """
    joined = " ".join(sorted(FORBIDDEN_PROPS)).lower()
    for category in ("prompt", "api_key", "file_path", "paper_content"):
        assert category in joined, f"违禁名单缺少 {category} 类字段"
    assert "trail_detail" in joined and "model_output" in joined, "留痕与模型输出不得上报"


def test_envelope_fields_are_typed_in_contract() -> None:
    """信封字段口径：os/channel 是枚举，distinctId 有长度上限（防 PII 直填）。"""
    definitions = _spec()["definitions"]
    assert set(definitions["os"]["enum"]) == {"windows", "macos", "linux"}
    assert set(definitions["channel"]["enum"]) == {"stable", "beta", "dev"}
    assert definitions["distinctId"]["maxLength"] <= 128
