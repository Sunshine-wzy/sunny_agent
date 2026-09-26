"""Public context API. Storage is initialized lazily after NoneBot starts."""

from .manager import ConversationManager
from .models import (
    ContextContent,
    ContextError,
    ConversationRef,
    DeliveryResult,
    EntryRef,
    MessageBinding,
    Scope,
    SourceInfo,
    TurnContext,
)

__all__ = [
    "ContextContent",
    "ContextError",
    "ConversationManager",
    "ConversationRef",
    "DeliveryResult",
    "EntryRef",
    "MessageBinding",
    "Scope",
    "SourceInfo",
    "TurnContext",
    "get_manager",
]

_manager: ConversationManager | None = None


def get_manager() -> ConversationManager:
    global _manager
    if _manager is None:
        import nonebot_plugin_localstore as store
        from nonebot import get_plugin_config

        from ..config import Config

        config = get_plugin_config(Config)
        _manager = ConversationManager(
            store.get_data_file("sunny_agent", "context.sqlite3"),
            max_input_tokens=config.sunny_agent_context_max_input_tokens,
            recent_turns=config.sunny_agent_context_recent_turns,
            entry_max_chars=config.sunny_agent_context_entry_max_chars,
        )
    return _manager
