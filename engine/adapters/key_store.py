"""Key 注入通道（SP1-5）：主进程经 stdin 一次性注入，引擎内存持有。

红线（SP1-5 提示词）：
- Key 只经 stdin 一次性注入并常驻内存；
- 禁止任何路径打印或持久化 Key 明文（日志/事件/检查点均脱敏）。
"""

import sys


class KeyStore:
    """API Key 内存存储：stdin 注入，脱敏访问，禁止明文外泄。"""

    def __init__(self) -> None:
        self._key: str | None = None

    def inject_from_stdin(self) -> None:
        """从 stdin 读取一行 Key（主进程一次性注入）。"""
        line = sys.stdin.readline().strip()
        if line:
            self._key = line

    def inject(self, key: str) -> None:
        """注入 Key（测试/内部用）。"""
        self._key = key

    @property
    def has_key(self) -> bool:
        return self._key is not None

    def masked(self) -> str:
        """脱敏显示（仅保留前后几位，中间打码），安全用于日志。"""
        if self._key is None:
            return "<未注入>"
        if len(self._key) <= 8:
            return "****"
        return f"{self._key[:4]}...{self._key[-4:]}"

    def auth_header(self) -> str:
        """构造 Authorization 头（仅在发请求时使用，不外泄）。"""
        if self._key is None:
            raise ValueError("API Key 未注入")
        return f"Bearer {self._key}"
