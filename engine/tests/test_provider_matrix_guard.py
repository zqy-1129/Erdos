"""provider 键集三方互证 + tool_mode 取值校验（G30/G31，工作单 W39）。

缺陷形状：客户端按 hostname 派生 provider，非通义域一律发 `openai-compat`；引擎默认值也是
`openai-compat`；而离线能力矩阵只有 `openai`。三个名字看起来是一回事，实际查不到条目 →
默认装配永远保守降级 `stage_level`，工具循环与 tool.call 留痕在用户机主路径整条不可达。
这类"名字差一个后缀"的错误不会报错，只会让一条已实现的能力链静默失效。

这里锁四件事：默认值命中矩阵、客户端可产出的每个值都命中矩阵、`ERDOS_TOOL_MODE` 非法值拒启
（该值会上 `initialize.capabilities.tool_mode`，而契约是二选一枚举）、以及 fixture 来源必须
如实标 unprobed（tools 能力目前只有矩阵默认，运行时探测只覆盖 /models）。
"""

import ast
import json
import re
from pathlib import Path

import pytest

from engine.__main__ import KeyStore, ModelConfig, _resolve_tool_mode
from engine.adapters.capabilities import (
    DEFAULT_PROVIDER,
    FIXTURE_MATRIX,
    KNOWN_PROVIDERS,
    TOOL_MODES,
    fixture_capabilities,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "contracts" / "engine-rpc.schema.json"


def _contract_tool_mode_enums() -> list[list[str]]:
    """收集契约里所有 tool_mode 的 enum 声明（initialize.capabilities 与 provider_test 两处）。"""
    found: list[list[str]] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "tool_mode" and isinstance(value, dict) and "enum" in value:
                    found.append(list(value["enum"]))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    return found


# ----------------------------------------------------------------------
# G31：默认值与客户端可产出的值必须都在矩阵里
# ----------------------------------------------------------------------
def test_default_provider_is_in_matrix() -> None:
    assert DEFAULT_PROVIDER in FIXTURE_MATRIX
    assert fixture_capabilities(DEFAULT_PROVIDER) is not None


def test_matrix_covers_every_provider_the_client_can_send() -> None:
    """读客户端源码取"能产出的 provider 值"，与引擎矩阵键集互证（不是靠人记住两边同名）。"""
    source = (REPO_ROOT / "client" / "main" / "credential-registry.ts").read_text(encoding="utf-8")
    produced = set(re.findall(r'"([a-z0-9-]*compat)"', source))
    assert produced, "客户端派生逻辑已改写，本守护失去意义，必须同步更新而不是删除"
    unknown = produced - set(KNOWN_PROVIDERS)
    assert not unknown, f"客户端会发但矩阵没有的 provider：{sorted(unknown)}（会被静默降级）"


def test_openai_compat_row_does_not_overclaim_structured_output() -> None:
    """不加 alias 到 openai 行的理由：那会把 structured_output 当作已具备，属凭接口名假定兼容。"""
    row = FIXTURE_MATRIX[DEFAULT_PROVIDER]
    assert row.tools is True and row.tool_choice is True
    assert row.structured_output is False


def test_matrix_rows_report_tool_mode_and_provenance() -> None:
    for provider, caps in FIXTURE_MATRIX.items():
        assert caps.tool_mode in TOOL_MODES, provider
        assert caps.probe_source == "fixture", provider  # 未实测，不得伪装成 probe/cache


# ----------------------------------------------------------------------
# G30：ERDOS_TOOL_MODE 不校验 → 会上报契约枚举外的能力值
# ----------------------------------------------------------------------
def test_illegal_tool_mode_refuses_startup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ERDOS_TOOL_MODE", "TOOL_LOOP")  # 大小写与首尾空白无关
    keys = KeyStore()
    config = ModelConfig(provider="openai-compat", base_url="https://x/v1", model="m")
    assert _resolve_tool_mode(keys, config, tmp_path) == "tool_loop"

    for bad in ("three_level", "tool-loop", " tool_loop2 "):  # 连字符/多余字符同样算拼错
        monkeypatch.setenv("ERDOS_TOOL_MODE", bad)
        with pytest.raises(SystemExit) as caught:
            _resolve_tool_mode(keys, config, tmp_path)
        assert caught.value.code == 2, bad


def test_absent_tool_mode_falls_back_to_capability_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ERDOS_TOOL_MODE", raising=False)
    # 无 Key：不做网络探测，保守阶段级（冷启动红线）
    assert _resolve_tool_mode(KeyStore(), None, tmp_path) == "stage_level"
    # 有 Key + 有坐标：命中 fixture 矩阵 → tool_loop（这就是 G31 修掉的"整条链不可达"）
    keys = KeyStore()
    keys.inject("sk-fixture-only-not-real")
    config = ModelConfig(provider=DEFAULT_PROVIDER, base_url="https://x/v1", model="m")
    assert _resolve_tool_mode(keys, config, tmp_path) == "tool_loop"


def test_engine_env_defaults_reference_the_matrix_constants() -> None:
    """默认 provider 不允许再出现字面量：两处各写一遍正是本次缺陷的成因。"""
    entry = (REPO_ROOT / "engine" / "__main__.py").read_text(encoding="utf-8")
    assert '"openai-compat"' not in entry, "仍在使用字面量默认值，矩阵改名时不会同步"
    assert "DEFAULT_PROVIDER" in entry and "TOOL_MODES" in entry
    tree = ast.parse(entry)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "engine.adapters.capabilities"
        for alias in node.names
    }
    assert {"DEFAULT_PROVIDER", "TOOL_MODES"} <= imported


def test_tool_modes_match_contract_enum() -> None:
    """TOOL_MODES 注释自称"契约同源"，就必须真去读契约比对。

    本项目反复出现"注释里的口径"被当作依据（`test_billing.py` 的 `# 商品列表公开` 同形）；
    G30 的缺陷正是"引擎把一个字面值上报给二选一枚举的字段"，只测引擎侧测不出两侧漂移。
    """
    enums = _contract_tool_mode_enums()
    assert enums, "契约里已无 tool_mode 的 enum 声明，本守护须同步改写而不是删掉"
    for enum in enums:
        assert set(enum) == set(TOOL_MODES), f"契约枚举 {enum} 与引擎 TOOL_MODES {list(TOOL_MODES)} 不一致"
