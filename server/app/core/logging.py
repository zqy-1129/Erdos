"""结构化日志：request_id 全链路可追踪（研发手册 SP2-1 验收项）。"""

import logging
from contextvars import ContextVar

# 请求追踪 ID：由 RequestIdMiddleware 写入，日志过滤器自动附加到每条日志
request_id_var: ContextVar[str] = ContextVar("erdos_request_id", default="-")

_FORMAT = "%(asctime)s %(levelname)s %(name)s [request_id=%(request_id)s] %(message)s"


class _RequestIdFilter(logging.Filter):
    """把上下文中的 request_id 注入日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def setup_logging(level: str) -> None:
    """初始化根日志器（幂等：重复调用仅更新级别）。"""
    logger = logging.getLogger("erdos")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.addFilter(_RequestIdFilter())
        logger.addHandler(handler)
    else:
        logger.handlers[0].setLevel(level)


def get_logger(name: str = "erdos.server") -> logging.Logger:
    """获取 erdos 命名空间下的日志器。"""
    return logging.getLogger(name)