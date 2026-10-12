"""异常吞噬守护：`except` 处理器不得只有 pass / continue / return None。

为什么要有这条机器判定：本项目已经四次栽在同一类问题上——代码"看起来有兜底"，实际把
故障吞掉，值班只看到"一切正常"：

- `scheduler.claim` 撞键回滚整个调用方事务（10-08 修）；
- `monitoring.upsert_minute` 同款（10-09 修）；
- `notification.append` 同款（10-09 修）；
- **月赠单用户发放失败 `except Exception: continue`**——没有计数、没有日志、没有告警，
  于是"某个人订了订阅却没拿到月赠"这件事在全链路里不留痕迹（10-09 修）。

判定口径（AST，不看注释）：处理器体若**全部**由 pass / continue / break / `...` /
`return None` 组成即视为静默；有日志、计数、重新抛出或返回有意义值的都放行。
确属正常控制流的站点进 ALLOWED 白名单，**每条必须写理由**，且白名单条目失效即红
（防腐：站点没了要删条目，不让白名单变成永不清理的杂物间）。
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

# (相对路径, 所在函数, 异常类型) -> 为什么这里可以静默
ALLOWED: dict[tuple[str, str, str], str] = {
    ("infra/events.py", "_offer", "asyncio.QueueEmpty"): (
        "队列满时先丢最旧再入队；get_nowait 抢不到说明别人已取走，属正常竞态，无副作用可做"
    ),
    ("infra/db.py", "after_cursor_execute", "RuntimeError"): (
        "监测采集器是可选装配（脚本/迁移进程没有 collector），取不到就跳过计时，不影响查询本身"
    ),
    ("repository/notification.py", "append", "IntegrityError"): (
        "幂等协议：撞 message_id 唯一键交回 None，由上层按'已处理'复用既有记录；"
        "插入在保存点内，不会回滚调用方事务"
    ),
    ("infra/auth.py", "verify", "(InvalidSignature, ValueError)"): (
        "多密钥轮试：签名对不上是预期结果，试完返回 False 就是契约；"
        "已把宽 except 收窄到这两类，其它异常照旧往上冒"
    ),
}


def _trivial(node: ast.stmt) -> bool:
    """pass / continue / break / 裸 ... / return None。"""
    if isinstance(node, (ast.Pass, ast.Continue, ast.Break)):
        return True
    if isinstance(node, ast.Expr):
        value = node.value
        return isinstance(value, ast.Constant) and value.value is Ellipsis
    if isinstance(node, ast.Return):
        return node.value is None or (
            isinstance(node.value, ast.Constant) and node.value.value is None
        )
    return False


def _scan_file(path: Path) -> list[tuple[str, str, str, int]]:
    """单个文件里的静默处理器：(相对路径, 函数名, 异常类型, 行号)。"""
    found: list[tuple[str, str, str, int]] = []
    relative = str(path.relative_to(APP.parent)).replace("\\", "/").removeprefix("app/")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    stack: list[str] = ["<module>"]

    class _Walk(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Try(self, node: ast.Try) -> None:
            for handler in node.handlers:
                if handler.body and all(_trivial(stmt) for stmt in handler.body):
                    exc = ast.unparse(handler.type) if handler.type else "<bare>"
                    found.append((relative, stack[-1], exc, handler.lineno))
            self.generic_visit(node)

    _Walk().visit(tree)
    return found


def _sites() -> list[tuple[str, str, str, int]]:
    """扫描 app/ 下所有静默处理器。"""
    return [site for path in sorted(APP.rglob("*.py")) for site in _scan_file(path)]


def test_no_silent_exception_handlers_outside_allowlist() -> None:
    """新增静默 except 必须要么加日志/计数，要么进白名单并写清理由。"""
    offenders = [site for site in _sites() if site[:3] not in ALLOWED]
    assert not offenders, (
        "以下 except 处理器只有 pass/continue/return None——故障会被吞掉：\n"
        + "\n".join(f"  app/{f}:{line} ({fn} / except {exc})" for f, fn, exc, line in offenders)
        + "\n处置：补 logger.exception 与计数；确属正常控制流则加进 ALLOWED 并写明理由。"
    )


def test_allowlist_entries_are_still_live() -> None:
    """白名单防腐：条目对应的站点已不存在时必须删掉条目。"""
    live = {site[:3] for site in _sites()}
    stale = sorted(set(ALLOWED) - live)
    assert not stale, f"白名单里的站点已消失，删掉条目：{stale}"


def test_allowlist_reasons_are_substantive() -> None:
    for key, reason in ALLOWED.items():
        assert len(reason) >= 20, f"{key} 的理由太短，看不出为什么可以静默"
