"""服务端架构红线守护：分层依赖、BYOK 与金额整数四条铁律的可执行版本。

为什么存在：这些约束写在《服务端研发手册》与《服务端架构与模块设计》里，此前只靠人 review 兜着，
CI 也只 grep 了 langchain 一条；越界写法在落地当下没人发现，等到联调或对账时才暴露。
违规怎么修：把能力挪回该在的层（领域只依赖本域 ports、持久化一律经 Repository），或改掉自造的
字段名（BYOK 铁律：服务端永不接收模型 Key；金额一律整数分、积分 integer）。

扫描器用 tmp_path 负向用例证明"真的会报错"——只有正向用例的守护等于没守护。
"""

import ast
import re
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = SERVER_ROOT.parent
CONTRACT = REPO_ROOT / "contracts" / "openapi.yaml"
SKIP_DIR_NAMES = {".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", "build"}

# 规则1：编排框架顶层包（含 langchain_core / langgraph_prebuilt 等衍生包）
LLM_FRAMEWORKS = ("langchain", "langgraph")
# 规则2：领域层禁止触碰的持久化依赖（业务层不写 SQL、不跨服务直读表）
DOMAIN_FORBIDDEN_PREFIXES = ("sqlalchemy", "app.repository")
# 规则3：接入层禁止的 ORM 查询构造与行模型
API_FORBIDDEN_MODULES = ("app.repository.models",)
API_FORBIDDEN_NAMES = ("select", "delete", "update", "insert")
# 规则4：BYOK 红线——接口面不得出现"模型侧 Key"语义字段（对象存储 AK/SK 等平台自有凭据不在此列）
KEY_FIELD_RE = re.compile(r"^(api_?key|apikey|model_key|vendor_key|provider_key|llm_key)$", re.I)
# 规则5：金额/积分整数的对立面（行模型里的浮点与定点列类型）
FLOAT_COLUMN_TYPES = ("Float", "DECIMAL", "Numeric")


def _python_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if not any(part in SKIP_DIR_NAMES for part in path.parts)
    )


def _imports(tree: ast.AST) -> list[tuple[str, tuple[str, ...], int]]:
    """(模块点分名, 子模块名, 行号)；相对 import（level>0）不跨顶层包，跳过。"""
    found: list[tuple[str, tuple[str, ...], int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, (), node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.module, tuple(alias.name for alias in node.names), node.lineno))
    return found


def _is_llm_framework(top: str) -> bool:
    return any(top == name or top.startswith(f"{name}_") for name in LLM_FRAMEWORKS)


def _class_field_names(tree: ast.AST) -> list[tuple[str, int]]:
    """类体内带注解的字段名（Pydantic 模型即此形态）——字符串名单不算字段声明。"""
    fields: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                fields.append((stmt.target.id, stmt.lineno))
    return fields


def _column_types(tree: ast.AST) -> list[tuple[str, int]]:
    """行模型里出现的列类型名（sa.Float() 与裸 Float() 两种写法都抓）。"""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in FLOAT_COLUMN_TYPES:
            found.append((node.attr, node.lineno))
        elif isinstance(node, ast.Name) and node.id in FLOAT_COLUMN_TYPES:
            found.append((node.id, node.lineno))
    return found


def _contract_key_fields(contract: Path) -> list[tuple[str, int]]:
    """契约里按行匹配的属性名——遍历已解析结构拿不到行号，报错就只能让人全文找。"""
    hits: list[tuple[str, int]] = []
    for lineno, line in enumerate(
        contract.read_text(encoding="utf-8").splitlines(), start=1
    ):
        match = re.match(r"^\s*([A-Za-z][\w-]*):", line)
        if match and KEY_FIELD_RE.match(match.group(1)):
            hits.append((match.group(1), lineno))
    return hits


def scan(server_root: Path, contract: Path = CONTRACT) -> list[str]:
    """扫描服务端树，返回违规清单（``文件:行号 规则N 说明``）；空列表即红线干净。"""
    violations: list[str] = []

    for path in _python_files(server_root):
        rel = path.relative_to(server_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        site = f"server/{rel}"
        in_domain = rel.startswith("app/domain/")
        in_api = rel.startswith("app/api/")

        for module, subnames, lineno in _imports(tree):
            top = module.split(".", 1)[0]
            if _is_llm_framework(top):
                violations.append(
                    f"{site}:{lineno} 规则1：LangChain/LangGraph（{module}）只允许出现在 engine/，"
                    "服务端引入即违背解耦边界（跨端只走 contracts 契约）"
                )
            if in_domain and module.startswith(DOMAIN_FORBIDDEN_PREFIXES):
                violations.append(
                    f"{site}:{lineno} 规则2：领域层禁止依赖持久化实现（{module}）——"
                    "不写 SQL、不跨服务直读表，只依赖本域 ports"
                )
            if in_api and module in API_FORBIDDEN_MODULES:
                violations.append(
                    f"{site}:{lineno} 规则3：接入层禁止直连 ORM 行模型（{module}）——"
                    "持久化一律经 Repository"
                )
            if in_api and module == "sqlalchemy" and set(subnames) & set(API_FORBIDDEN_NAMES):
                hit = ", ".join(sorted(set(subnames) & set(API_FORBIDDEN_NAMES)))
                violations.append(
                    f"{site}:{lineno} 规则3：接入层禁止直连 ORM 查询构造"
                    f"（from sqlalchemy import {hit}）——持久化一律经 Repository"
                )

        for name, lineno in _class_field_names(tree):
            if KEY_FIELD_RE.match(name):
                violations.append(
                    f"{site}:{lineno} 规则4：BYOK 铁律——服务端永不接收模型 Key，"
                    f"字段 {name} 不得出现在接口面"
                )

        if rel == "app/repository/models.py":
            for name, lineno in _column_types(tree):
                violations.append(
                    f"{site}:{lineno} 规则5：金额一律整数分、积分 integer，"
                    f"行模型不得使用浮点/定点列 {name}"
                )

    for name, lineno in _contract_key_fields(contract):
        violations.append(
            f"contracts/openapi.yaml:{lineno} 规则4：BYOK 铁律——契约不得声明模型 Key 字段 {name}"
        )

    return violations


def test_server_arch_redlines_are_clean() -> None:
    violations = scan(SERVER_ROOT)
    assert not violations, "服务端架构红线越界：\n" + "\n".join(violations)


def _empty_contract(tmp_path: Path) -> Path:
    contract = tmp_path / "openapi.yaml"
    contract.write_text("paths: {}\n", encoding="utf-8")
    return contract


def test_scan_catches_llm_framework_in_api_and_sql_in_domain(tmp_path: Path) -> None:
    domain = tmp_path / "app" / "domain" / "billing"
    domain.mkdir(parents=True)
    (domain / "service.py").write_text("from sqlalchemy import select\n", encoding="utf-8")
    api = tmp_path / "app" / "api" / "v1"
    api.mkdir(parents=True)
    (api / "agent.py").write_text("from langgraph.graph import StateGraph\n", encoding="utf-8")

    violations = scan(tmp_path, contract=_empty_contract(tmp_path))

    joined = "\n".join(violations)
    assert len(violations) == 2, violations
    assert "规则1" in joined and "app/api/v1/agent.py:1" in joined, joined
    assert "规则2" in joined and "app/domain/billing/service.py:1" in joined, joined


def test_scan_catches_api_orm_leak(tmp_path: Path) -> None:
    api = tmp_path / "app" / "api" / "v1"
    api.mkdir(parents=True)
    (api / "dashboard.py").write_text(
        "from sqlalchemy import select\n"
        "from app.repository.models import UsersDailyStats\n"
        "\n"
        "stmt = select(UsersDailyStats)\n",
        encoding="utf-8",
    )

    violations = scan(tmp_path, contract=_empty_contract(tmp_path))

    joined = "\n".join(violations)
    assert "规则3" in joined, violations
    assert "app/api/v1/dashboard.py:1" in joined, joined
    assert "app/api/v1/dashboard.py:2" in joined, joined
    assert "规则4" not in joined and "规则5" not in joined, joined


def test_scan_catches_byok_field_and_float_column(tmp_path: Path) -> None:
    api = tmp_path / "app" / "api" / "v1"
    api.mkdir(parents=True)
    (api / "byok.py").write_text(
        "class BindRequest(BaseModel):\n    api_key: str\n", encoding="utf-8"
    )
    repo = tmp_path / "app" / "repository"
    repo.mkdir()
    (repo / "models.py").write_text(
        "class OrderRow:\n    price = sa.Float()\n    score: Numeric = 0\n",
        encoding="utf-8",
    )
    contract = tmp_path / "openapi.yaml"
    contract.write_text("components:\n  schemas:\n    Bind:\n    api_key: { type: string }\n")

    violations = scan(tmp_path, contract=contract)
    joined = "\n".join(violations)
    assert "规则4" in joined and "app/api/v1/byok.py:2" in joined, joined
    assert "openapi.yaml:4" in joined, joined
    assert "规则5" in joined and "app/repository/models.py:2" in joined, joined
    assert "app/repository/models.py:3" in joined, joined


def test_key_field_rule_ignores_forbidden_field_allowlist(tmp_path: Path) -> None:
    """遥测违禁字段名单里出现 "api_key" 字符串正是合规要求，不得被规则4 误报。"""
    api = tmp_path / "app" / "api" / "v1"
    api.mkdir(parents=True)
    (api / "telemetry.py").write_text(
        'FORBIDDEN_FIELDS = frozenset({"api_key", "prompt_zh"})\n', encoding="utf-8"
    )

    assert scan(tmp_path, contract=_empty_contract(tmp_path)) == []
