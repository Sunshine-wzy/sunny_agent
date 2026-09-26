"""Persist outgoing messages before sending, then bind actual OneBot receipts."""

import asyncio
import json
import sqlite3
from typing import Any

from nonebot import get_plugin_config
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed, ApiNotAvailable
from nonebot.log import logger

from .config import Config
from .context import (
    ContextContent,
    ContextError,
    ConversationRef,
    DeliveryResult,
    EntryRef,
    Scope,
    get_manager,
)


def scope_for_event(event: Any, bot: Bot) -> Scope:
    group_id = getattr(event, "group_id", None)
    return Scope(
        str(bot.self_id),
        "group" if group_id is not None else "private",
        str(group_id if group_id is not None else event.user_id),
    )


def serialize_message(value: Any) -> Any:
    if isinstance(value, MessageSegment):
        return {"type": value.type, "data": serialize_message(value.data)}
    if isinstance(value, (Message, list, tuple)):
        return [serialize_message(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize_message(item) for key, item in value.items()}
    return value


def message_content(message: Message | str) -> ContextContent:
    blocks = []
    for segment in Message(message):
        if segment.type == "reply":
            continue
        if segment.type == "text":
            blocks.append({"type": "input_text", "text": segment.data["text"]})
        elif segment.type == "image":
            url = segment.data.get("url") or segment.data.get("file", "")
            if url.startswith("base64://"):
                url = "data:image/png;base64," + url[len("base64://") :]
            if url.startswith(("http://", "https://", "data:")):
                blocks.append({"type": "input_image", "image_url": url})
            else:
                blocks.append({"type": "input_text", "text": "[图片]"})
        else:
            blocks.append({"type": "input_text", "text": str(segment)})
    return ContextContent(blocks or [{"type": "input_text", "text": ""}])


def outgoing_payload(
    message: Message | str, *, forward: bool = False
) -> dict[str, Any]:
    return {"forward": forward, "message": serialize_message(Message(message))}


def deserialize_message(items: list[dict[str, Any]]) -> Message:
    message = Message()
    for item in items:
        data = dict(item["data"])
        if item["type"] == "node" and isinstance(data.get("content"), list):
            data["content"] = deserialize_message(data["content"])
        message.append(MessageSegment(item["type"], data))
    return message


async def deliver(
    bot: Bot, conversation: ConversationRef, delivery: dict[str, Any]
) -> DeliveryResult:
    manager = get_manager()
    if str(bot.self_id) != conversation.scope.bot_id:
        raise ContextError("发送 Bot 与会话不一致。")
    async with manager.operation(conversation.scope):
        row = await manager.call("delivery", delivery["id"])
        if row["status"] in {"confirmed", "unknown", "sending"}:
            return DeliveryResult(
                row["id"], row["status"], row["receipt"], row["error"]
            )
        payload = json.loads(row["payload"])
        message = deserialize_message(payload["message"])
        peer_id = int(conversation.scope.peer_id)
        await manager.call("delivery_status", row["id"], "sending")
        try:
            if payload["forward"]:
                request = bot.call_api(
                    "send_group_forward_msg", group_id=peer_id, messages=message
                )
            elif conversation.scope.chat_type == "group":
                request = bot.send_group_msg(group_id=peer_id, message=message)
            else:
                request = bot.send_private_msg(user_id=peer_id, message=message)
            receipt = await asyncio.wait_for(
                request,
                timeout=get_plugin_config(
                    Config
                ).sunny_agent_context_send_timeout_seconds,
            )
        except (ActionFailed, ApiNotAvailable) as exc:
            await manager.call("delivery_status", row["id"], "failed", str(exc))
            return DeliveryResult(row["id"], "failed", error=str(exc))
        except asyncio.CancelledError:
            await asyncio.shield(
                manager.call("delivery_status", row["id"], "unknown", "发送被中断")
            )
            raise
        except Exception as exc:
            # A lost receipt does not establish that the platform didn't send the message.
            await manager.call("delivery_status", row["id"], "unknown", str(exc))
            logger.warning(f"Message delivery uncertain ({row['id']}): {exc}")
            return DeliveryResult(row["id"], "unknown", error=str(exc))
        message_id = receipt.get("message_id") if isinstance(receipt, dict) else None
        message_id = str(message_id) if isinstance(message_id, (str, int)) else None
        # Retrying local registration must never retransmit the message.
        for attempt in range(3):
            try:
                await manager.call("confirm", conversation, row["id"], message_id)
                break
            except sqlite3.OperationalError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.1)
        return DeliveryResult(row["id"], "confirmed", message_id)


async def send_in_session(
    bot: Bot,
    conversation: ConversationRef,
    message: Message | str,
    *,
    idempotency_key: str,
    entries: list[EntryRef] | None = None,
    visible_content: ContextContent | None = None,
    forward: bool = False,
    turn_id: str | None = None,
) -> DeliveryResult:
    manager = get_manager()
    async with manager.operation(conversation.scope):
        if any(entry.conversation != conversation for entry in entries or []):
            raise ContextError("不能关联其他会话的内容。")
        row = await manager.call(
            "prepare_delivery",
            conversation,
            idempotency_key,
            outgoing_payload(message, forward=forward),
            (visible_content or message_content(message)).blocks,
            [entry.entry_id for entry in entries or []],
            turn_id,
        )
        return await deliver(bot, conversation, row)


async def recover_pending(bot: Bot) -> None:
    manager = get_manager()
    for conversation, row in await manager.call("pending_deliveries", str(bot.self_id)):
        try:
            await deliver(bot, conversation, row)
        except Exception:
            logger.exception(f"Failed to recover pending delivery {row['id']}")
