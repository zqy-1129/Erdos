"""BYOK 适配层错误分类（SP1-5）：401/429/网络/余额 → 可读文案。"""

from enum import StrEnum


class ErrorKind(StrEnum):
    AUTH = "auth"  # 401 认证失败（Key 无效/过期）
    RATE_LIMIT = "rate_limit"  # 429 限流
    NETWORK = "network"  # 连接超时/断流/网络错误
    INSUFFICIENT_BALANCE = "insufficient_balance"  # 余额不足
    SERVER = "server"  # 5xx 服务端错误
    UNKNOWN = "unknown"


class AdapterError(Exception):
    """适配层业务异常：带分类与可读文案。"""

    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


# HTTP 状态码 → 错误分类
def classify_status(status: int) -> ErrorKind:
    if status == 401:
        return ErrorKind.AUTH
    if status == 429:
        return ErrorKind.RATE_LIMIT
    if status == 402:
        return ErrorKind.INSUFFICIENT_BALANCE
    if 500 <= status < 600:
        return ErrorKind.SERVER
    return ErrorKind.UNKNOWN


# 各分类可读文案（模型可读）
MESSAGES = {
    ErrorKind.AUTH: "API Key 无效或已过期，请检查密钥配置",
    ErrorKind.RATE_LIMIT: "请求过于频繁，触发厂商限流，请稍后重试",
    ErrorKind.NETWORK: "网络连接失败或超时，请检查网络后重试",
    ErrorKind.INSUFFICIENT_BALANCE: "账户余额不足，请充值后重试",
    ErrorKind.SERVER: "模型服务暂时不可用，请稍后重试",
    ErrorKind.UNKNOWN: "模型调用失败，未知错误",
}


def readable_message(kind: ErrorKind) -> str:
    """返回可读文案。"""
    return MESSAGES[kind]
