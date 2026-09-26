"""Per-run Agents SDK Session: durable history is never modified by the SDK."""

from copy import deepcopy
from typing import Any


class ConversationSession:
    session_settings = None

    def __init__(self, session_id: str, history: list[dict[str, Any]]) -> None:
        self.session_id = session_id
        self._items = [(False, item) for item in deepcopy(history)]

    async def get_items(self, limit: int | None = None) -> list[dict[str, Any]]:
        items = [item for _, item in self._items]
        if limit is not None:
            items = items[-limit:] if limit > 0 else []
        return deepcopy(items)

    async def add_items(self, items: list[dict[str, Any]]) -> None:
        self._items.extend((True, item) for item in deepcopy(items))

    async def pop_item(self) -> dict[str, Any] | None:
        return deepcopy(self._items.pop()[1]) if self._items else None

    async def clear_session(self) -> None:
        self._items.clear()

    @property
    def new_items(self) -> list[dict[str, Any]]:
        return deepcopy([item for added, item in self._items if added])
