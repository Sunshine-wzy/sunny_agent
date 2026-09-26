"""OneBot chat controller: commands, reference routing, one model turn and delivery."""

import asyncio
import re

from nonebot import get_plugin_config
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.log import logger

from . import chat
from .config import Config
from .context import (
    ContextContent,
    ContextError,
    ConversationRef,
    MessageBinding,
    Scope,
    get_manager,
)
from .messaging import (
    deliver,
    message_content,
    outgoing_payload,
    scope_for_event,
    send_in_session,
)

MessageEvent = GroupMessageEvent | PrivateMessageEvent
AT_SEGMENT_PATTERN = re.compile(r"\[CQ:at,qq=(?P<cq>all|\d+)(?:,[^\]]*)?\]")


def group_response(response: str) -> Message:
    message, cursor = Message(), 0
    for match in AT_SEGMENT_PATTERN.finditer(response):
        if match.start() > cursor:
            message.append(MessageSegment.text(response[cursor : match.start()]))
        qq = match.group("cq")
        message.append(MessageSegment.at(qq if qq == "all" else int(qq)))
        cursor = match.end()
    if cursor < len(response):
        message.append(MessageSegment.text(response[cursor:]))
    return message


def reply_message_id(event: MessageEvent) -> str | int | None:
    reply = getattr(event, "reply", None)
    return chat._get_field(reply, "message_id") or chat._reply_segment_message_id(
        event.message
    )


def clean_message(event: MessageEvent) -> Message:
    result = Message()
    for segment in event.message:
        if segment.type == "reply":
            continue
        if segment.type == "at" and str(segment.data.get("qq")) == str(event.self_id):
            continue
        result.append(segment.copy())
    return result


def first_command(message: Message) -> str | None:
    for segment in message:
        if not segment.is_text():
            return None
        text = segment.data.get("text", "").lstrip()
        if text:
            return text.split(maxsplit=1)[0].lower() if text.startswith("/") else None
    return None


def without_command(message: Message) -> Message:
    result = Message()
    stripped = False
    for segment in message:
        if not stripped and segment.is_text():
            text = segment.data.get("text", "").lstrip()
            if not text:
                continue
            parts = text.split(maxsplit=1)
            result.append(MessageSegment.text(parts[1] if len(parts) > 1 else ""))
            stripped = True
        else:
            result.append(segment)
    return result


def binding_reference(
    binding: MessageBinding, max_chars: int
) -> chat.ReferencedMessage:
    message = Message()
    remaining = max_chars
    truncated = False
    for block in binding.content.blocks:
        if block.get("type") == "input_text":
            text = block["text"]
            message.append(MessageSegment.text(text[:remaining]))
            truncated |= len(text) > remaining
            remaining = max(0, remaining - len(text))
        elif block.get("type") == "input_image":
            message.append(MessageSegment.image(block["image_url"]))
    if truncated:
        message.append(
            MessageSegment.text(
                f"\n[引用已截取，可用 read_context(entry_id='quote:{binding.message_id}') 补读所选消息]",
            )
        )
    return chat.ReferencedMessage(
        binding.message_id, binding.sender_name, binding.sender_id, message
    )


async def _control(
    bot: Bot, conversation: ConversationRef, turn_id: str, text: str
) -> None:
    await send_in_session(
        bot,
        conversation,
        Message(MessageSegment.text(text)),
        idempotency_key=f"control:{turn_id}",
    )


async def new_session_from_poke(scope: Scope, bot: Bot, event_key: str) -> None:
    manager = get_manager()
    async with manager.operation(scope):
        if await manager.call("request", scope, event_key):
            return
        conversation = await manager.create_session(scope, idempotency_key=event_key)
        turn_id = await manager.begin_turn(
            conversation,
            event_key,
            ContextContent.text("[戳一戳]"),
            "用户",
            "",
            control=True,
        )
        await _control(
            bot,
            conversation,
            turn_id,
            f"已新建会话 #{conversation.short_id}。引用旧消息可以继续之前的会话。",
        )


async def handle_message(event: MessageEvent, bot: Bot) -> None:
    manager, scope = get_manager(), scope_for_event(event, bot)
    message = clean_message(event)
    command = first_command(message)
    if command and command not in {"/new", "/clear", "/quote", "/session"}:
        return
    async with manager.operation(scope):
        if await manager.call("request", scope, str(event.message_id)):
            return
        reference_id = reply_message_id(event)
        if command in {"/new", "/clear"}:
            conversation = await manager.create_session(
                scope, idempotency_key=f"new:{event.message_id}"
            )
            binding, notice = None, ""
        else:
            conversation, binding, notice = await manager.resolve(
                scope, reference_id, quote=command == "/quote"
            )
        sender_name = chat._get_sender_name(event.sender)
        control_text = None
        activate = command not in {"/quote", "/session"}
        if command in {"/new", "/clear"}:
            control_text = (
                f"已新建会话 #{conversation.short_id}。引用旧消息可以继续之前的会话。"
            )
        elif command == "/session":
            args = without_command(message).extract_plain_text().strip().split()
            subcommand = args[0].lower() if args else "current"
            conversation = await manager.get_active_session(scope)
            if subcommand == "current" and len(args) <= 1:
                control_text = (
                    f"当前会话 #{conversation.short_id}：{conversation.title}"
                )
            elif subcommand == "list" and len(args) == 1:
                sessions = await manager.list_sessions(scope)
                control_text = "本聊天最近的会话：\n" + "\n".join(
                    f"{'→ ' if item == conversation else ''}#{item.short_id} {item.title}"
                    for item in sessions
                )
            elif subcommand == "use" and len(args) <= 2:
                target = (
                    await manager.find_session(scope, args[1])
                    if len(args) == 2
                    else (binding.conversation if binding else None)
                )
                if target:
                    conversation, activate = target, True
                    control_text = f"已切换到会话 #{conversation.short_id}。"
                else:
                    control_text = "找不到本聊天中的会话。请引用已登记的消息，或使用 /session use <编号>。"
            else:
                control_text = "用法：/session current、/session list、/session use <编号>；也可引用消息发送 /session use。"
        elif command == "/quote":
            message = without_command(message)
            if reference_id is None:
                control_text = "请引用一条消息，再发送 /quote 问题；当前会话保持不变。"
            elif (
                not message.extract_plain_text().strip()
                and not chat._message_has_image(message)
            ):
                control_text = "请在 /quote 后填写问题。"
        elif binding and not str(message).strip():
            control_text = f"当前会话已切换到 #{conversation.short_id}。"

        turn_id = await manager.begin_turn(
            conversation,
            event.message_id,
            message_content(message),
            sender_name,
            str(event.user_id),
            activate=activate,
            control=control_text is not None,
        )
        if control_text is not None:
            await _control(bot, conversation, turn_id, control_text)
            return

        async def run_model():
            reference = (
                binding_reference(binding, manager.entry_max_chars)
                if binding
                else (
                    await chat._get_referenced_message(event, bot)
                    if reference_id is not None
                    else None
                )
            )
            if command == "/quote" and reference is None:
                raise ContextError("无法验证或读取被引用消息；当前会话保持不变。")
            agent_input = await chat._build_agent_input(
                event,
                bot,
                sender_name,
                referenced_message=reference,
                message=message,
            )
            turn = await manager.prepare_model_turn(conversation, turn_id, agent_input)
            turn.reference = binding
            if binding:
                await manager.call(
                    "set_reference",
                    conversation,
                    turn_id,
                    {
                        "message_id": binding.message_id,
                        "content": binding.content.blocks,
                        "conversation_id": binding.conversation.conversation_id,
                        "sender_name": binding.sender_name,
                        "sender_id": binding.sender_id,
                    },
                )
            if isinstance(event, GroupMessageEvent):
                response = await chat.run_group_chat(event, bot, turn.input_items, turn)
            else:
                response = await chat.run_private_chat(
                    event, bot, turn.input_items, turn
                )
            if not turn.session.new_items:
                raise RuntimeError("Runner did not persist the completed turn")
            return turn, response

        try:
            turn, response = await asyncio.wait_for(
                run_model(),
                timeout=get_plugin_config(
                    Config
                ).sunny_agent_context_turn_timeout_seconds,
            )
        except asyncio.CancelledError:
            await asyncio.shield(manager.call("fail", turn_id, "处理被中断"))
            raise
        except Exception as exc:
            await manager.call("fail", turn_id, str(exc))
            logger.exception(f"Conversation turn failed: {turn_id}")
            reason = (
                str(exc)
                if isinstance(exc, ContextError)
                else "本次回复失败，请稍后再试。"
            )
            await _control(
                bot,
                conversation,
                turn_id,
                f"{reason}\n当前会话 #{conversation.short_id}。",
            )
            return
        text = "\n\n".join(
            part for part in (notice, response or "（暂无回复）") if part
        )
        outgoing = (
            group_response(text)
            if isinstance(event, GroupMessageEvent)
            else Message(text)
        )
        delivery = await manager.call(
            "complete",
            conversation,
            turn_id,
            turn.session.new_items,
            outgoing_payload(outgoing),
            message_content(outgoing).blocks,
            turn.entry_ids[0],
        )
        await deliver(bot, conversation, delivery)
