"""事件主题覆盖面守护：发出去的主题必须有人能订到，看板能订到的主题必须在契约里点名。

发现形状（2026-10-13）：`/v1/auth/register` 向 EventBroker 发布 `points.registration_grant`，
而看板 SSE 只订 `{presence.online_count, monitoring.snapshot, dashboard.events}`——这条发布
永远无人接收，注释却写着"供遥测/看板订阅"。注册赠分真正的可见路径是 `DashboardEvent`
投影（`points.grant`，由 `/v1/admin/dashboard/events` 读出）。

两层判据：
① 生产代码里每一次 `publish` 的主题都必须在 `STREAM_TOPICS` 内；
② `STREAM_TOPICS` 必须与契约 `dashboardStream` 描述里点名的主题集**双向**相等——
   多发一个主题而契约不动，客户端与值班看到的主题表就成了假口径。
"""

import ast
import re
from pathlib import Path

from app.api.v1.dashboard import STREAM_TOPICS
from app.core import topics

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "openapi.yaml"


def _topic_constants() -> dict[str, str]:
    """`app.core.topics` 里的 名称 -> 主题字符串。"""
    return {
        name: value
        for name, value in vars(topics).items()
        if not name.startswith("_") and isinstance(value, str)
    }


def _published_topics() -> set[str]:
    """扫描生产代码中所有 broker.publish(...) 的第一个实参。"""
    constants = _topic_constants()
    published: set[str] = set()
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "publish"
                and node.args
            ):
                continue
            arg = node.args[0]
            if not isinstance(arg, ast.Name):
                raise AssertionError(
                    f"{path.relative_to(APP_ROOT.parents[0])} 的 publish 主题不是 "
                    f"app.core.topics 常量：守护需要显式扩充，不接受静默漏判"
                )
            value = constants.get(arg.id)
            if value is not None:
                published.add(value)
    return published


def _contract_stream_topics() -> set[str]:
    """契约 `dashboardStream` 描述里点名的主题（形如 `a.b（说明）`）。"""
    text = CONTRACT.read_text(encoding="utf-8")
    block = text[text.index("dashboardStream") :]
    block = block[: block.index("security:")]
    return set(re.findall(r"([a-z_]+(?:\.[a-z_]+)+)（", block))


def test_every_published_topic_is_streamed() -> None:
    published = _published_topics()
    assert published, "扫描没抓到任何发布点——判据本身失效了"
    orphan = published - STREAM_TOPICS
    assert not orphan, f"这些主题被发布但无人可订：{sorted(orphan)}"


def test_stream_topics_match_the_contract() -> None:
    declared = _contract_stream_topics()
    assert declared == STREAM_TOPICS, (
        f"看板可订主题与契约点名不一致：契约多 {sorted(declared - STREAM_TOPICS)}、"
        f"实现多 {sorted(STREAM_TOPICS - declared)}"
    )
