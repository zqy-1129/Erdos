"""引擎侧静默异常守护：`except` 处理器不得只有 pass / continue / break / ... / return None。

同一把尺在服务端已经用过（`server/tests/unit/test_silent_exception_guard.py`，10-10 那批查出
四处真缺陷：claim 回滚全会话、upsert 撞键、notify 重复、**月赠失败 `except: continue` 全链路
不留痕**）。引擎侧此前**既没有普查也没有守护**——本文件补上，判据完全对齐服务端那份：

- 处理器体**全部**由 pass / continue / break / 裸 `...` / `return None` 组成即视为静默；
- 有日志、计数、重新抛出、写 stderr、或返回有意义值的都放行（不看注释，只看 AST）；
- 确属正常控制流的进 ALLOWED，**每条必须写清理由**；白名单条目失效（站点没了或已修）即红，
  防止白名单变成永不清理的杂物间。

本轮普查的产出不是"八点都合理"，而是当场把三处**静默失败**改成可见：
能力缓存写失败（静默 = 缓存永久失效、每任务重探，BYOK 下花的是用户额度）、容器兜底回收失败
（静默 = 资源持续泄漏而主流程照常返回成功）、回归驱动的事件回调异常（静默 = "跑通了"但事件
一条都没送出去）。三处都改了计数 + 首次点名，因此它们自动离开白名单。
"""

import ast
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]

# (引擎内相对路径, 所在函数, 异常类型原文) -> 为什么这里可以静默
# 键必须是 `ast.unparse` 的原文（多异常带圆括号），且以实测扫描结果为准——
# 本文件第一版的六处键名全被自己的"白名单失效即红"用例抓到了（凭印象写键名=又一次假引用）。
ALLOWED: dict[tuple[str, str, str], str] = {
    ("adapters/capabilities.py", "load", "(OSError, json.JSONDecodeError)"): (
        "EC-D5：缓存文件损坏/不可读一律按未命中处理，交回 None 由调用方重探厂商；"
        "缓存是优化而非事实源，读它不该让任务失败"
    ),
    ("adapters/capabilities.py", "load", "(TypeError, ValueError)"): (
        "stored_at 被写坏（非数值）时 TTL 无从计算，按过期/未命中处理是既定口径"
    ),
    (
        "adapters/capabilities.py",
        "load",
        "(KeyError, TypeError, ValueError, json.JSONDecodeError)",
    ): (
        "capabilities 字段缺失或格式漂移 → 按未命中重探，不让旧缓存污染本次判定"
    ),
    ("adapters/openai_compat.py", "_chat_stream", "json.JSONDecodeError"): (
        "SSE 单行不是 JSON（心跳/代理注入的杂行）跳过该块，属流式解析的正常控制流；"
        "整条流的完整性另由 finish_reason 与内容长度判定"
    ),
    ("adapters/openai_compat.py", "_retry_after_seconds", "ValueError"): (
        "Retry-After 缺失或为 HTTP-date 形式 → 返回 None 走指数退避，docstring 已写明"
    ),
    ("orchestrator/kernel/spec.py", "_interrupt_sites", "(OSError, TypeError)"): (
        "测试替身等动态构造的节点取不到 inspect.getsource，跳过静态计数；"
        "每节点 1 个挂起点的约束另有运行期判定（interrupt() 内）"
    ),
    ("regression/rpc_flow.py", "_parse", "json.JSONDecodeError"): (
        "stdout 上非 NDJSON 的杂行按噪声丢弃；协议层另有行长上限与计数守护（W36）"
    ),
    ("regression/rpc_flow.py", "_recover_gate_failed_reason", "RpcFlowError"): (
        "驳回原因的 events_replay 只是兜底取回；取不到就保持 None，**不编造归因**"
        "（主路径的 gate.failed 事件才是原因来源）"
    ),
    ("regression/rpc_flow.py", "close", "OSError"): (
        "进程收尾关闭管道：已关闭/关闭失败都没有可挽回的副作用，硬抛会淹掉真正的失败归因"
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


def _enclosing_function(tree: ast.AST) -> dict[ast.AST, str]:
    owner: dict[ast.AST, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                owner.setdefault(child, node.name)
    return owner


def _scan_source(source: str, relative: str) -> list[tuple[str, str, str, int]]:
    """源码里的静默处理器：(相对路径, 函数名, 异常类型, 行号)。"""
    tree = ast.parse(source)
    owner = _enclosing_function(tree)
    found: list[tuple[str, str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.body and all(_trivial(stmt) for stmt in node.body):
            exc = ast.unparse(node.type) if node.type is not None else "BARE"
            found.append((relative, owner.get(node, "<module>"), exc, node.lineno))
    return found


def _engine_files() -> list[Path]:
    return [
        p
        for p in sorted(ENGINE.rglob("*.py"))
        if "tests" not in p.parts and "__pycache__" not in p.parts
    ]


def _scan_all() -> list[tuple[str, str, str, int]]:
    hits: list[tuple[str, str, str, int]] = []
    for path in _engine_files():
        rel = str(path.relative_to(ENGINE)).replace("\\", "/")
        hits += _scan_source(path.read_text(encoding="utf-8"), rel)
    return hits


def test_no_unwhitelisted_silent_handlers() -> None:
    assert _engine_files(), f"没扫到任何引擎源文件：{ENGINE}"
    unexplained = [h for h in _scan_all() if (h[0], h[1], h[2]) not in ALLOWED]
    assert not unexplained, "出现未说明理由的静默异常处理器（要么可见化，要么写明理由进 ALLOWED）：\n" + "\n".join(
        f"  {rel}:{line} {func}() except {exc}: 体只有 pass/continue/return None"
        for rel, func, exc, line in unexplained
    )


def test_whitelist_entries_are_all_still_needed() -> None:
    """白名单失效即红：站点被删/改成可见后必须删条目，否则它会变成杂物间。"""
    present = {(rel, func, exc) for rel, func, exc, _ in _scan_all()}
    stale = sorted(set(ALLOWED) - present)
    assert not stale, f"白名单里有已不存在的静默站点，请删掉条目：{stale}"


def test_whitelist_reasons_are_written() -> None:
    """放行必须给理由——空话（"没关系"/"正常"）不算，长度下限逼写清"为什么可以静默"。"""
    thin = {key: reason for key, reason in ALLOWED.items() if len(reason) < 20}
    assert not thin, f"以下白名单条目理由过短：{list(thin)}"
    assert all(key[0].endswith(".py") for key in ALLOWED), "白名单键必须是引擎内相对路径"


def test_guard_actually_detects_a_silent_handler() -> None:
    """正向对照：守护必须能被测坏（三种静默形态各一例），且放行两种非静默形态。"""
    hits = _scan_source(
        "\n".join([
            "def a():",
            "    try:",
            "        x = 1",
            "    except ValueError:",
            "        pass",
            "def b():",
            "    try:",
            "        x = 1",
            "    except OSError:",
            "        continue",
            "def c():",
            "    try:",
            "        x = 1",
            "    except KeyError:",
            "        return None",
            "def d():",
            "    try:",
            "        x = 1",
            "    except TypeError:",
            "        log.warning('boom')",
            "def e():",
            "    try:",
            "        x = 1",
            "    except Exception as exc:",
            "        errors.append(exc)",
        ]),
        "synthetic.py",
    )
    assert [(rel, func, exc) for rel, func, exc, _ in hits] == [
        ("synthetic.py", "a", "ValueError"),
        ("synthetic.py", "b", "OSError"),
        ("synthetic.py", "c", "KeyError"),
    ], hits
