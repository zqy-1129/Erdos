"""契约一致性守护（CI 门禁）：openapi.yaml 与实际实现零漂移。

复用 scripts/contract_audit.py 的四项检查（路径×方法、Envelope schema 引用、错误码镜像、
契约 security ⇔ 实现公开白名单）。contract_audit 以 __main__ 退出码表达结果，此处直接调用
main() 断言为 0；任何新增端点/错误码/视图未同步契约即红。
"""

import importlib.util
from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parent.parent.parent  # server/


@pytest.fixture(scope="module")
def contract_audit():
    spec = importlib.util.spec_from_file_location(
        "contract_audit", SERVER_ROOT / "scripts" / "contract_audit.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_contract_has_no_drift(contract_audit) -> None:
    """契约与实际实现必须零漂移（路径面、schema 引用、错误码）。"""
    assert contract_audit.main() == 0


def test_security_check_catches_drift_both_directions(contract_audit) -> None:
    """第 4 项必须真能抓到漂移，而不是"恰好通过"：两个方向各造一次假。

    - 契约要凭证、实现当公开 → 生产里承诺的鉴权不存在；
    - 契约匿名、实现要凭证 → enforce 一开就静默 401。
    """
    spec = {
        "paths": {
            "/v1/health": {"get": {"security": [{"BearerAuth": []}]}},
            "/v1/points/balance": {"get": {}},
        }
    }
    problems = contract_audit.check_security_alignment(spec)
    assert len(problems) == 2, problems
    assert any("/v1/health" in p for p in problems)
    assert any("/v1/points/balance" in p for p in problems)

    aligned = {
        "paths": {
            "/v1/health": {"get": {}},
            "/v1/points/balance": {"get": {"security": [{"BearerAuth": []}]}},
        }
    }
    assert contract_audit.check_security_alignment(aligned) == []