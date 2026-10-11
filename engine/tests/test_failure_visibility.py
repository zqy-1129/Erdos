"""三处"兜底失败必须可见"的分支自身要有用例覆盖。

本批把三个静默 `except` 改成了"计数 + 首次点名"（普查见 `test_silent_exception_guard.py`）。
改动本身若没有用例，就等于新增了一堆**从没跑过的错误处理分支**——那正是本项目反复清理的
"字段已存但从不生效"形状（G9 内存限额、G40 gc_orphans 都在这一族）。

三条用例各断两件事：**计数器变了**（机器可查，不靠人读日志）与**点名只发一次**（不淹 stderr）。
全部用注入替身，不依赖 Docker、不拉起真实引擎子进程。
"""

import io
import sys
from pathlib import Path

import pytest

from engine.adapters.capabilities import CapabilityCache, ProviderCapabilities
from engine.regression.rpc_flow import RpcTaskFlow
from engine.sandbox.docker_sandbox import DockerSandbox


def _caps() -> ProviderCapabilities:
    return ProviderCapabilities(tools=True, probe_source="probe")


async def test_capability_cache_write_failure_is_counted_and_named_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """缓存写不进去必须点名：静默 = 每任务重探厂商，BYOK 下花的是用户额度。"""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("我是文件，不能当目录用", encoding="utf-8")
    cache = CapabilityCache(root=blocker / "sub")

    cache.store("k1", _caps())

    assert cache.write_failures == 1, "写失败没被计数（分支可能压根没走到）"
    assert "能力缓存写入失败" in capsys.readouterr().err

    cache.store("k2", _caps())
    second = capsys.readouterr().err
    assert cache.write_failures == 2, "计数要持续累加，否则看不出劣化仍在发生"
    assert "能力缓存写入失败" not in second, "点名只发一次，不淹 stderr"


async def test_container_removal_failure_is_counted_and_named_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """回收失败必须可查：否则容器一直泄漏而主流程看着完全正常。"""

    async def failing_runner(argv: list[str]) -> tuple[int, bytes, bytes]:
        raise OSError("docker daemon 未运行")

    sandbox = DockerSandbox(runner=failing_runner)
    await sandbox._force_remove("erdos-c1")  # noqa: SLF001 - 被测目标就是兜底分支
    await sandbox._force_remove("erdos-c2")  # noqa: SLF001

    err = capsys.readouterr().err
    assert sandbox.removal_failures == 2
    assert "容器兜底回收失败" in err, err
    assert err.count("容器兜底回收失败") == 1, "点名只发一次"


def test_event_callback_failure_in_driver_is_counted() -> None:
    """回归驱动里展示/证据回调炸了不能静默：跑通 ≠ 事件都送出去了。

    绕开 `__init__`（它会拉起真实引擎子进程）——本用例只测 `_pump_events` 的容错分支。
    """

    def broken_printer(_event: dict) -> None:
        raise RuntimeError("打印机坏了")

    class _Transport:
        def take_events(self) -> list[dict]:
            return [{"event": "gate.failed", "reason": "硬检查驳回：缺章节"}]

    flow = object.__new__(RpcTaskFlow)
    # _transport 的声明类型是具体的 _EngineTransport（构造即拉起子进程），故用 setattr 注入
    # 替身：mypy 不做赋值检查，其余字段仍走正常赋值以保留类型面。
    setattr(flow, "_transport", _Transport())  # noqa: B010 - 有意绕过具体类型赋值
    flow._on_event = broken_printer  # noqa: SLF001
    flow._hard_reject = None  # noqa: SLF001
    flow.event_callback_errors = 0
    flow._callback_note_shown = False  # noqa: SLF001

    captured = io.StringIO()
    saved = sys.stderr
    sys.stderr = captured
    try:
        flow._pump_events()  # noqa: SLF001
    finally:
        sys.stderr = saved

    assert flow.event_callback_errors == 1
    assert "事件回调失败" in captured.getvalue()
    # 归因优先于展示：打印机坏了也不能丢 gate.failed 的驳回原因
    assert flow.hard_reject_reason() == "硬检查驳回：缺章节"
