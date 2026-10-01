"""统一时间源：全部业务时间一律 UTC，禁止使用本地时区。"""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(UTC)