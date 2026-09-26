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


async def _generate_title(text: str) -> str:
    from ..graph import generate_conversation_title

    return await generate_conversation_title(text)


async def stop_title_tasks() -> None:
    if _manager is not None:
        await _manager.cancel_title_tasks()


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
            title_generator=(
                _generate_title
                if config.sunny_agent_context_auto_title_enabled
                else None
            ),
            title_timeout_seconds=config.sunny_agent_context_title_timeout_seconds,
        )
    return _manager
