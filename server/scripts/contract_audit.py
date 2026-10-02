"""契约一致性审计（contracts/openapi.yaml vs 实际实现）。

三项检查：
1. 路径×方法比对：openapi 声明与 FastAPI 实际路由互差集；
2. envelope schema 引用完整性：每个 *Envelope* 的 data 引用可解析；
3. 错误码镜像：x-error-codes 与 app.core.errors.ERROR_SPECS 100% 一致。

用于契约 v1 冻结评审前自检，可重复执行：
    python scripts/contract_audit.py
退出码非 0 表示存在漂移；tests/unit/test_contract_audit.py 将其接入 CI 门禁。
"""

import sys
from pathlib import Path

import yaml

from app.core.config import Settings
from app.core.errors import ERROR_SPECS
from app.main import create_app

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT.parent / "contracts" / "openapi.yaml"

_METHODS = {"get", "post", "put", "patch", "delete"}


def load_contract() -> dict:
    with CONTRACT.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def extract_contract_paths(spec: dict) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path, item in spec["paths"].items():
        out[path] = {m for m in item if m in _METHODS}
    return out


def extract_app_routes(app) -> dict[str, set[str]]:
    """取应用实际生成的 OpenAPI schema 路径面（客户端真实视角）。

    新版 FastAPI 子路由为惰性 _IncludedRouter 私有结构，直接遍历 routes
    不稳健；官方 schema 生成即最终的路径集合。
    """
    schema = app.openapi()
    result: dict[str, set[str]] = {}
    for path, item in schema.get("paths", {}).items():
        result[path] = {m for m in item if m in _METHODS}
    return result


def check_schema_refs(spec: dict) -> list[str]:
    schemas: dict = spec.get("components", {}).get("schemas", {})
    missing: list[str] = []
    for name, schema in schemas.items():
        if not name.startswith("Envelope") or "allOf" not in schema:
            continue
        for part in schema["allOf"]:
            if "properties" not in part:
                continue
            for prop in part["properties"].values():
                ref = prop.get("$ref")
                if ref:
                    target = ref.rsplit("/", 1)[-1]
                    if target not in schemas:
                        missing.append(f"{name} -> {ref}")
    return missing


def check_error_codes(spec: dict) -> list[str]:
    problems: list[str] = []
    codes = spec.get("x-error-codes", {})
    contract_codes = {int(v.get("code")): (k, int(v.get("http_status"))) for k, v in codes.items()}
    impl_codes = {s.code: (name, s.http_status) for name, s in ERROR_SPECS.items()}
    for c in sorted(set(contract_codes) | set(impl_codes)):
        if c == 0:  # OK 为成功码，不参与错误码镜像比对
            continue
        if c not in contract_codes:
            problems.append(f"实现有而契约缺: {c} ({impl_codes[c][0]})")
        elif c not in impl_codes:
            problems.append(f"契约有而实现缺: {c} ({contract_codes[c][0]})")
        elif contract_codes[c][1] != impl_codes[c][1]:
            problems.append(
                f"HTTP 状态不一致: {c} 契约 {contract_codes[c][1]} vs 实现 {impl_codes[c][1]}"
            )
    return problems


def main() -> int:
    spec = load_contract()
    contract_paths = extract_contract_paths(spec)
    application = create_app(Settings(env="test", database_url="sqlite+aiosqlite:///:memory:"))
    app_paths = extract_app_routes(application)

    c_keys = {(p, m) for p, ms in contract_paths.items() for m in ms}
    a_keys = {(p, m) for p, ms in app_paths.items() for m in ms}
    only_contract = sorted(c_keys - a_keys)
    only_app = sorted(a_keys - c_keys)

    print(f"契约路径数: {sum(len(v) for v in contract_paths.values())}（{len(contract_paths)} 个 URL）")
    print(f"实际路由数: {sum(len(v) for v in app_paths.values())}（{len(app_paths)} 个 URL）")
    print()
    if only_contract:
        print("[漂移] 契约声明但未实现:")
        for p, m in only_contract:
            print(f"  {m.upper():6s} {p}")
    if only_app:
        print("[漂移] 已实现但契约未声明:")
        for p, m in only_app:
            print(f"  {m.upper():6s} {p}")
    if not only_contract and not only_app:
        print("[OK] 路径×方法无漂移")

    ref_missing = check_schema_refs(spec)
    print()
    if ref_missing:
        print("[漂移] 未解析的 Envelope schema 引用:")
        for r in ref_missing:
            print(f"  {r}")
    else:
        print("[OK] Envelope schema 引用完整")

    err_problems = check_error_codes(spec)
    print()
    if err_problems:
        print("[漂移] 错误码镜像不一致:")
        for r in err_problems:
            print(f"  {r}")
    else:
        print("[OK] x-error-codes 与 ERROR_SPECS 一致（{} 个）".format(len(ERROR_SPECS) - 1))

    return 1 if (only_contract or only_app or ref_missing or err_problems) else 0


if __name__ == "__main__":
    sys.exit(main())