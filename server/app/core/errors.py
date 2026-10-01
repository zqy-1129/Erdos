"""错误码契约镜像（契约冻结项）。

本模块条目必须与 contracts/openapi.yaml 的 x-error-codes 保持 100% 一致，
由 tests/unit/test_errors.py 的契约一致性测试守护。新增错误码须同步修改契约
文件并走契约评审；既有错误码禁止修改语义与 numeric code。
"""

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """单个错误码的完整定义。"""

    code: int
    http_status: int
    message: str


# 成功（唯一 code=0 条目）
OK: Final = ErrorSpec(code=0, http_status=200, message="成功")

# 客户端错误
BAD_REQUEST: Final = ErrorSpec(code=40001, http_status=400, message="请求参数错误")
RESET_TOKEN_INVALID: Final = ErrorSpec(code=40002, http_status=400, message="重置令牌无效或已过期")
UNAUTHENTICATED: Final = ErrorSpec(code=40101, http_status=401, message="未认证或凭证无效")
TOKEN_EXPIRED: Final = ErrorSpec(code=40102, http_status=401, message="令牌已过期")
INVALID_CREDENTIALS: Final = ErrorSpec(code=40103, http_status=401, message="用户名或密码错误")
TOKEN_REVOKED: Final = ErrorSpec(code=40104, http_status=401, message="刷新令牌已失效，请重新登录")
PERMISSION_DENIED: Final = ErrorSpec(code=40301, http_status=403, message="无操作权限")
ACCOUNT_FROZEN: Final = ErrorSpec(code=40302, http_status=403, message="账号已冻结或已注销")
NOT_FOUND: Final = ErrorSpec(code=40401, http_status=404, message="资源不存在")
METHOD_NOT_ALLOWED: Final = ErrorSpec(code=40501, http_status=405, message="请求方法不允许")
CONFLICT: Final = ErrorSpec(code=40901, http_status=409, message="资源状态冲突或请求重复")
ACCOUNT_LOCKED: Final = ErrorSpec(code=42301, http_status=423, message="登录尝试过多，账号已临时锁定")
RATE_LIMITED: Final = ErrorSpec(code=42901, http_status=429, message="请求过于频繁，请稍后重试")

# 服务端错误
INTERNAL_ERROR: Final = ErrorSpec(code=50001, http_status=500, message="服务内部错误")
DB_UNAVAILABLE: Final = ErrorSpec(code=50301, http_status=503, message="依赖服务不可用")

# 注册表：名称 -> 定义（供契约一致性测试与按名查找使用）
ERROR_SPECS: Final[dict[str, ErrorSpec]] = {
    "OK": OK,
    "BAD_REQUEST": BAD_REQUEST,
    "RESET_TOKEN_INVALID": RESET_TOKEN_INVALID,
    "UNAUTHENTICATED": UNAUTHENTICATED,
    "TOKEN_EXPIRED": TOKEN_EXPIRED,
    "INVALID_CREDENTIALS": INVALID_CREDENTIALS,
    "TOKEN_REVOKED": TOKEN_REVOKED,
    "PERMISSION_DENIED": PERMISSION_DENIED,
    "ACCOUNT_FROZEN": ACCOUNT_FROZEN,
    "NOT_FOUND": NOT_FOUND,
    "METHOD_NOT_ALLOWED": METHOD_NOT_ALLOWED,
    "CONFLICT": CONFLICT,
    "ACCOUNT_LOCKED": ACCOUNT_LOCKED,
    "RATE_LIMITED": RATE_LIMITED,
    "INTERNAL_ERROR": INTERNAL_ERROR,
    "DB_UNAVAILABLE": DB_UNAVAILABLE,
}


class AppError(Exception):
    """领域/网关层抛出的业务异常，由全局异常处理器转换为统一信封。

    `spec` 必须取自本模块的契约错误码（禁止自造错误码）。
    """

    def __init__(self, spec: ErrorSpec, detail: str | None = None) -> None:
        super().__init__(detail or spec.message)
        self.spec = spec
        self.detail = detail