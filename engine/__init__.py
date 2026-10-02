"""Erdos Agent 引擎（P1，SP1-1 运行时与 IPC 骨架）。

职责边界（SP1-1 仅通信骨架，禁止阶段业务逻辑）：
- 可被主进程拉起/重启/优雅退出的 Python 子进程；
- stdio JSON-RPC 2.0 server，6 个方法全部注册；
- NDJSON 事件发射器（traceID + 时序 + schema 校验）。
"""

__version__ = "0.1.0"
