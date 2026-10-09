"""BYOK 模型适配器（SP1-5）：OpenAI 兼容多厂商调用（W8 起含工具调用协议）。"""

from engine.adapters.capabilities import (
    CapabilityCache,
    ProviderCapabilities,
    capability_key,
    fixture_capabilities,
    probe_capabilities,
    probe_models_endpoint,
)
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
    ToolCall,
    Usage,
    UsageAccumulator,
)

__all__ = [
    "AdapterError",
    "CapabilityCache",
    "ChatMessage",
    "ChatResult",
    "ErrorKind",
    "KeyStore",
    "ModelConfig",
    "OpenAIChatAdapter",
    "ProviderCapabilities",
    "ToolCall",
    "Usage",
    "UsageAccumulator",
    "capability_key",
    "classify_status",
    "fixture_capabilities",
    "probe_capabilities",
    "probe_models_endpoint",
    "readable_message",
]
