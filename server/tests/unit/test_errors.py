"""错误码契约一致性测试（契约 CI 校验：研发手册 SP2-1 验收标准）。"""

from pathlib import Path

import pytest
import yaml

from app.core import errors

CONTRACT_PATH = Path(__file__).resolve().parents[3] / "contracts" / "openapi.yaml"


@pytest.fixture(scope="module")
def contract() -> dict:
    with CONTRACT_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_contract_file_exists() -> None:
    assert CONTRACT_PATH.exists(), "contracts/openapi.yaml 契约文件缺失"


def test_error_specs_match_contract(contract: dict) -> None:
    """代码错误码与契约 x-error-codes 100% 一致（名称、code、http_status、message）。"""
    contract_codes: dict = contract.get("x-error-codes", {})
    assert set(contract_codes) == set(errors.ERROR_SPECS), "错误码名称集合不一致"

    for name, spec in errors.ERROR_SPECS.items():
        entry = contract_codes[name]
        assert entry["code"] == spec.code, f"{name} code 不一致"
        assert entry["http_status"] == spec.http_status, f"{name} http_status 不一致"
        assert entry["message"] == spec.message, f"{name} message 不一致"


def test_error_codes_are_unique(contract: dict) -> None:
    codes = [entry["code"] for entry in contract["x-error-codes"].values()]
    assert len(codes) == len(set(codes)), "错误码重复定义"


def test_only_ok_uses_code_zero(contract: dict) -> None:
    for name, entry in contract["x-error-codes"].items():
        if entry["code"] == 0:
            assert name == "OK", "code=0 仅允许 OK 使用"


def test_app_error_carries_spec_and_detail() -> None:
    exc = errors.AppError(errors.CONFLICT)
    assert exc.spec is errors.CONFLICT
    assert exc.detail is None
    assert str(exc) == errors.CONFLICT.message

    exc2 = errors.AppError(errors.RATE_LIMITED, detail="触发限流")
    assert exc2.detail == "触发限流"