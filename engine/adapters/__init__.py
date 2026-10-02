"""BYOK 模型适配器（SP1-5）：OpenAI 兼容多厂商调用。"""

from engine.adapters.errors import (
    AdapterError,
    ErrorKind,
    classify_status,
    readable_message,
)
from engine.adapters.key_store import KeyStore
from engine.adapters.openai_compat import (
    ChatMessage,
    ChatResult,
    ModelConfig,
    OpenAIChatAdapter,
    Usage,
    UsageAccumulator,
)

__all__ = [
    "AdapterError",
    "ChatMessage",
    "ChatResult",
    "ErrorKind",
    "KeyStore",
    "ModelConfig",
    "OpenAIChatAdapter",
    "Usage",
    "UsageAccumulator",
    "classify_status",
    "readable_message",
]
