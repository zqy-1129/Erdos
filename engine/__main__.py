"""引擎入口：stdio JSON-RPC server 启动 + SIGTERM 优雅退出。

用法（主进程拉起）：
    python -m engine
主进程经 stdin 发送 NDJSON 请求，读 stdout 响应与事件。
"""

import asyncio
import signal

from engine.ipc.events import EventEmitter
from engine.ipc.methods import register_all
from engine.ipc.server import JsonRpcServer
from engine.ipc.state import EngineState


def main() -> None:
    state = EngineState()
    events = EventEmitter()
    server = JsonRpcServer(state, events)
    register_all(server, state)

    def handle_sigterm(_signum, _frame):
        # 优雅退出：SIGTERM 300ms 内落盘检查点标记（SP1-1 骨架仅记录状态）
        server.stop()

    signal.signal(signal.SIGTERM, handle_sigterm)

    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
