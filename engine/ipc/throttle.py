"""token 增量节流器（EN-STREAM W15）：50ms 或 256 字符任一满足即发。

设计（开发文档 §6.2：token 流只作临时展示，允许丢帧；配合渲染层 16ms 帧合并）：
- push(delta)：入缓冲；达到字符阈值或距上次发射 ≥ 间隔 → 立即发射合并文本；
- flush()：收尾强制发射（LLM 单次调用结束时必须调用，保证尾部不丢）；
- 节流器实例绑定单次 LLM 调用（SolveLoop 每次 run 创建，无跨任务共享状态）。
"""

import time
from collections.abc import Callable

INTERVAL_SECONDS = 0.05
CHAR_THRESHOLD = 256


class DeltaThrottler:
    """增量合并发射器（同步 push，适配异步回调路径）。"""

    def __init__(
        self,
        emit: Callable[[str], None],
        interval: float = INTERVAL_SECONDS,
        char_threshold: int = CHAR_THRESHOLD,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._emit = emit
        self._interval = interval
        self._char_threshold = char_threshold
        self._clock = clock
        self._buf: list[str] = []
        self._len = 0
        self._last_emit = clock()

    def push(self, delta: str) -> None:
        """推入一个 token 增量；满足阈值即发射。"""
        self._buf.append(delta)
        self._len += len(delta)
        now = self._clock()
        if self._len >= self._char_threshold or now - self._last_emit >= self._interval:
            self.flush(now)

    def flush(self, now: float | None = None) -> None:
        """发射缓冲中的合并文本（空缓冲为 no-op）。"""
        if not self._buf:
            return
        text = "".join(self._buf)
        self._buf.clear()
        self._len = 0
        self._last_emit = now if now is not None else self._clock()
        self._emit(text)
