"""契约一致性守护（CI 门禁）：openapi.yaml 与实际实现零漂移。

复用 scripts/contract_audit.py 的三项检查（路径×方法、Envelope schema 引用、
错误码镜像）。contract_audit 以 __main__ 退出码表达结果，此处直接调用 main()
断言为 0；任何新增端点/错误码/视图未同步契约即红。
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