"""Conversation routing and public APIs, independent of NoneBot events."""

import asyncio
import json
import logging
import uuid
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import Context, ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .builder import build_history
from .models import (
    ContextContent,
    ContextError,
    ConversationRef,
    EntryRef,
    MessageBinding,
    Scope,
    SourceInfo,
    TurnContext,
)
from .naming import build_title_input, normalize_title
from .session import ConversationSession
from .store import ContextStore
from .transcript import transcript_page

logger = logging.getLogger(__name__)


@dataclass
class _Operation:
    scope: Scope
    active: bool = True


class ConversationManager:
    def __init__(
        self,
        path: str | Path,
        *,
        max_input_tokens: int = 24000,
        recent_turns: int = 20,
        entry_max_chars: int = 12000,
        title_generator: Callable[[str], Awaitable[str]] | None = None,
        title_timeout_seconds: float = 30.0,
    ) -> None:
        self.store = ContextStore(path)
        self.max_input_tokens = max_input_tokens
        self.recent_turns = recent_turns
        self.entry_max_chars = entry_max_chars
        self.title_generator = title_generator
        self.title_timeout_seconds = title_timeout_seconds
        self._title_tasks: dict[str, asyncio.Task[None]] = {}
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._operation: ContextVar[_Operation | None] = ContextVar(
            f"conversation_operation_{id(self)}",
            default=None,
        )

    async def call(self, method: str, *args: Any) -> Any:
        return await asyncio.to_thread(getattr(self.store, method), *args)

    @asynccontextmanager
    async def operation(self, scope: Scope) -> AsyncIterator[None]:
        """FIFO lock. Awaited SDK tools inherit the current operation context."""
        current = self._operation.get()
        if current is not None and current.active and current.scope == scope:
            yield
            return
        lock = self._locks.setdefault(scope.key, asyncio.Lock())
        async with lock:
            operation = _Operation(scope)
            token = self._operation.set(operation)
            try:
                yield
            finally:
                operation.active = False
                self._operation.reset(token)

    async def create_session(
        self,
        scope: Scope,
        *,
        activate: bool = True,
        title: str | None = None,
        source_key: str | None = None,
        idempotency_key: str | None = None,
    ) -> ConversationRef:
        async with self.operation(scope):
            return await self.call(
                "create",
                scope,
                title or "新会话",
                source_key,
                idempotency_key or uuid.uuid4().hex,
                activate,
                not title,
            )

    def schedule_title(self, conversation: ConversationRef) -> None:
        """Start at most one naming task per conversation without holding its lock."""
        key = conversation.conversation_id
        if self.title_generator is None or key in self._title_tasks:
            return
        task = asyncio.create_task(
            self._generate_title(conversation), context=Context()
        )
        self._title_tasks[key] = task
        task.add_done_callback(lambda _: self._title_tasks.pop(key, None))

    async def _generate_title(self, conversation: ConversationRef) -> None:
        try:
            entries = await self.call("title_entries", conversation)
            text = build_title_input(entries)
            if not text or self.title_generator is None:
                return
            title = normalize_title(
                await asyncio.wait_for(
                    self.title_generator(text), timeout=self.title_timeout_seconds
                )
            )
            await self.call("set_generated_title", conversation, title)
        except Exception:
            # Keep pending so a later successful chat turn can retry.
            logger.warning(
                "Conversation title generation failed: %s",
                conversation.conversation_id,
                exc_info=True,
            )

    async def cancel_title_tasks(self) -> None:
        tasks = list(self._title_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def get_active_session(self, scope: Scope) -> ConversationRef:
        async with self.operation(scope):
            return await self.call("active", scope) or await self.create_session(scope)

    async def switch_session(self, conversation: ConversationRef) -> None:
        async with self.operation(conversation.scope):
            await self.call("switch", conversation)

    async def list_sessions(
        self, scope: Scope, limit: int = 10
    ) -> list[ConversationRef]:
        return await self.call("list_sessions", scope, max(1, min(limit, 50)))

    async def find_session(self, scope: Scope, short_id: str) -> ConversationRef | None:
        return await self.call("find", scope, short_id)

    async def list_session_catalog(
        self, scope: Scope, offset: int = 0, limit: int = 10
    ) -> dict[str, Any]:
        return await self.call(
            "session_catalog",
            scope,
            max(0, min(offset, 2**63 - 1)),
            max(1, min(limit, 50)),
        )

    async def read_session(
        self,
        scope: Scope,
        session_id: str,
        offset: int = 0,
        limit: int = 4000,
        *,
        exclude_turn: str = "",
    ) -> dict[str, Any]:
        conversation = await self.find_session(scope, session_id)
        if conversation is None:
            return {
                "error": "找不到本聊天中的会话。请先调用 list_sessions 查询会话编号。"
            }
        entries = await self.call("entries", conversation, exclude_turn)
        return {
            "session_id": conversation.short_id,
            "title": conversation.title[:200],
            **await asyncio.to_thread(
                transcript_page, entries, max(0, offset), max(1, min(limit, 8000))
            ),
        }

    async def append_context(
        self,
        conversation: ConversationRef,
        *,
        content: ContextContent,
        source: SourceInfo,
        idempotency_key: str,
        kind: str = "external",
    ) -> EntryRef:
        if kind not in {"external", "assistant_publication"}:
            raise ContextError("不支持的上下文类型。")
        if not content.blocks or any(
            block.get("type") not in {"input_text", "input_image", "input_file"}
            for block in content.blocks
        ):
            raise ContextError("上下文内容必须包含文本、图片或文件内容块。")
        async with self.operation(conversation.scope):
            entry_id = await self.call(
                "append",
                conversation,
                kind,
                content.blocks,
                asdict(source),
                idempotency_key,
            )
            return EntryRef(conversation, entry_id)

    async def bind_message(
        self,
        conversation: ConversationRef,
        *,
        message_id: str | int,
        visible_content: ContextContent,
        entries: list[EntryRef] | None = None,
        sender_name: str = "Sunny",
        sender_id: str | None = None,
    ) -> None:
        if any(entry.conversation != conversation for entry in entries or []):
            raise ContextError("消息不能关联其他会话的内容。")
        async with self.operation(conversation.scope):
            await self.call(
                "bind",
                conversation,
                str(message_id),
                visible_content.blocks,
                sender_name,
                sender_id or conversation.scope.bot_id,
                [entry.entry_id for entry in entries or []],
            )

    async def lookup_message(
        self, scope: Scope, message_id: str | int | None
    ) -> MessageBinding | None:
        if message_id is None:
            return None
        row = await self.call("binding", scope, str(message_id))
        if row is None:
            return None
        return MessageBinding(
            row["conversation"],
            row["message_id"],
            ContextContent(json.loads(row["content"])),
            row["sender_name"],
            row["sender_id"],
            json.loads(row["entry_ids"]),
        )

    async def resolve(
        self,
        scope: Scope,
        reply_id: str | int | None,
        *,
        quote: bool = False,
    ) -> tuple[ConversationRef, MessageBinding | None, str]:
        current = await self.get_active_session(scope)
        reference = await self.lookup_message(scope, reply_id)
        if reference and not quote:
            target = reference.conversation
            notice = f"已切换到会话 #{target.short_id}。" if target != current else ""
            return target, reference, notice
        return (
            current,
            reference,
            (
                "未找到引用消息的历史会话，继续当前会话。"
                if reply_id is not None and not reference
                else ""
            ),
        )

    async def begin_turn(
        self,
        conversation: ConversationRef,
        message_id: str | int,
        visible: ContextContent,
        sender_name: str,
        sender_id: str,
        *,
        activate: bool = True,
        control: bool = False,
    ) -> str:
        return await self.call(
            "begin",
            conversation,
            str(message_id),
            visible.blocks,
            sender_name,
            sender_id,
            activate,
            control,
        )

    async def prepare_model_turn(
        self,
        conversation: ConversationRef,
        turn_id: str,
        input_items: str | list[dict[str, Any]],
    ) -> TurnContext:
        items = (
            [{"role": "user", "content": input_items}]
            if isinstance(input_items, str)
            else input_items
        )
        entries = await self.call("entries", conversation, turn_id)
        history = build_history(
            entries,
            items,
            max_input_tokens=self.max_input_tokens,
            recent_turns=self.recent_turns,
            entry_max_chars=self.entry_max_chars,
        )
        entry_id = await self.call("set_input", conversation, turn_id, items)
        return TurnContext(
            conversation,
            turn_id,
            ConversationSession(conversation.conversation_id, history),
            items,
            entry_ids=[entry_id],
        )

    async def read_context(
        self, conversation: ConversationRef, entry_id: str, offset: int, limit: int
    ) -> str:
        entries = await self.call("entries", conversation)
        if entry_id.startswith("quote:"):
            reference = next(
                (
                    entry["source"].get("reference")
                    for entry in reversed(entries)
                    if entry["source"].get("reference", {}).get("message_id")
                    == entry_id[6:]
                ),
                None,
            )
            if reference:
                text = ContextContent(reference["content"]).plain_text()
                start, size = max(0, offset), min(max(1, limit), 8000)
                return json.dumps(
                    {
                        "content": text[start : start + size],
                        "total_chars": len(text),
                        "offset": start,
                        "has_more": start + size < len(text),
                    },
                    ensure_ascii=False,
                )
        entry = next((e for e in entries if e["id"] == entry_id), None)
        if entry is None:
            return "找不到本会话中的条目；不能读取其他会话的内容。"
        text = json.dumps(entry["content"], ensure_ascii=False)
        start, size = max(0, offset), min(max(1, limit), 8000)
        return json.dumps(
            {
                "entry_id": entry_id,
                "offset": start,
                "total_chars": len(text),
                "content": text[start : start + size],
                "has_more": start + size < len(text),
            },
            ensure_ascii=False,
        )
