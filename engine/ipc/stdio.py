"""进程 stdio 编码基线（NDJSON 协议红线：UTF-8 + LF）。

引擎与主进程之间的 stdin/stdout 是 UTF-8 字节流（每行一个 JSON）。Windows 控制台默认
码页常为 cp936/GBK，中文负载（题面、门禁意见、工具摘要）会被编码成非 UTF-8 字节，主进程
按 utf8 解码即得乱码并静默丢弃事件——Linux CI 上不会暴露，Windows 是 P0 目标平台。
因此进程入口强制重配三条管道，使协议字节序与宿主码页无关。
"""

import sys
from typing import TextIO


def configure_stdio() -> None:
    """强制 stdin/stdout/stderr 为 UTF-8；stdout 行结束符固定 LF（不做平台翻译）。"""
    # stdin 用 strict：坏字节若按 replace 变成 U+FFFD，task_id/题面会被静默改写后照常执行，
    # 比直接失败更危险（如实呈现原则）。
    _reconfigure(sys.stdin)
    _reconfigure(sys.stdout, newline="\n")
    _reconfigure(sys.stderr, errors="replace")


def _reconfigure(stream: TextIO, *, newline: str | None = None, errors: str = "strict") -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:  # 测试替身/非 TextIOWrapper：保持现状，不阻断启动
        return
    reconfigure(encoding="utf-8", errors=errors, newline=newline)
