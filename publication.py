"""Context-aware proactive publications shared by scheduled and manual commands."""

import hashlib
import json
import uuid
from dataclasses import dataclass, field

from nonebot.adapters.onebot.v11 import Bot, Message

from .context import (
    ContextContent,
    ContextError,
    ConversationRef,
    DeliveryResult,
    EntryRef,
    Scope,
    SourceInfo,
    get_manager,
)
from .messaging import send_in_session


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


@dataclass
class Publication:
    source_key: str
    title: str
    sources: list[tuple[SourceInfo, ContextContent]]
    batch_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    async def prepare(
        self, bot: Bot, group_id: int
    ) -> tuple[ConversationRef, list[EntryRef]]:
        manager = get_manager()
        scope = Scope(str(bot.self_id), "group", str(group_id))
        async with manager.operation(scope):
            conversation = await manager.create_session(
                scope,
                activate=False,
                title=self.title,
                source_key=self.source_key,
                idempotency_key=f"publication:{self.source_key}:{content_hash(self.title)}",
            )
            entries = []
            for source, content in self.sources:
                entries.append(
                    await manager.append_context(
                        conversation,
                        content=content,
                        source=source,
                        idempotency_key=f"source:{source.kind}:{source.source_id}:{content_hash(str(content.blocks))}",
                    )
                )
            return conversation, entries

    async def saved_commentary(
        self, bot: Bot, group_id: int, part_key: str
    ) -> str | None:
        conversation, _ = await self.prepare(bot, group_id)
        for entry in await get_manager().call("entries", conversation):
            if entry["idempotency_key"] == f"commentary:{self.batch_id}:{part_key}":
                return ContextContent(entry["content"]).plain_text()
        return None

    async def send(
        self,
        bot: Bot,
        group_id: int,
        message: Message | str,
        visible: ContextContent,
        *,
        part_key: str,
        forward: bool = False,
        commentary: bool = False,
    ) -> DeliveryResult:
        manager = get_manager()
        scope = Scope(str(bot.self_id), "group", str(group_id))
        # All fallback accounts serialize one logical group publication. Message
        # bindings still belong to the account that actually sent the message.
        async with manager.operation(Scope("publication", "group", str(group_id))):
            existing = await manager.call(
                "publication_delivery",
                scope,
                self.source_key,
                f"publish:{self.batch_id}:{part_key}",
            )
            if existing:
                if (
                    json.loads(existing["visible"]) != visible.blocks
                    or json.loads(existing["payload"])["forward"] != forward
                ):
                    raise ContextError("发布幂等键已用于不同消息。")
                return DeliveryResult(
                    existing["id"],
                    existing["status"],
                    existing["receipt"],
                    existing["error"],
                )
            return await self._send(
                bot,
                group_id,
                message,
                visible,
                part_key=part_key,
                forward=forward,
                commentary=commentary,
            )

    async def _send(
        self,
        bot: Bot,
        group_id: int,
        message: Message | str,
        visible: ContextContent,
        *,
        part_key: str,
        forward: bool,
        commentary: bool,
    ) -> DeliveryResult:
        manager = get_manager()
        scope = Scope(str(bot.self_id), "group", str(group_id))
        async with manager.operation(scope):
            conversation, entries = await self.prepare(bot, group_id)
            if commentary:
                entry = await manager.append_context(
                    conversation,
                    content=visible,
                    kind="assistant_publication",
                    source=SourceInfo("commentary", self.source_key),
                    idempotency_key=f"commentary:{self.batch_id}:{part_key}",
                )
                entries = [entry]
            return await send_in_session(
                bot,
                conversation,
                message,
                entries=entries,
                visible_content=visible,
                forward=forward,
                idempotency_key=f"publish:{self.batch_id}:{part_key}",
            )
