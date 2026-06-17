import re

from nonebot import on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageSegment,
    PokeNotifyEvent,
    PrivateMessageEvent,
)

from . import chat, tool
from .graph import clear_group_history, clear_private_history


async def _is_to_me_or_active_group(event) -> bool:
    if isinstance(event, GroupMessageEvent) and tool.is_group_active_receiving_enabled(event.group_id):
        return True

    return event.is_tome()


async def _is_group_poke_to_bot(event, bot: Bot) -> bool:
    return (
        isinstance(event, PokeNotifyEvent)
        and event.group_id is not None
        and event.target_id == int(bot.self_id)
    )


llm = on_message(rule=_is_to_me_or_active_group, priority=10, block=False)
poke_clear = on_notice(rule=_is_group_poke_to_bot, priority=10, block=False)
CLEAR_CONTEXT_COMMANDS = {"/clear"}
AT_SEGMENT_PATTERN = re.compile(
    r"\[CQ:at,qq=(?P<cq>all|\d+)(?:,[^\]]*)?\]"
)


def _get_first_command(event: GroupMessageEvent | PrivateMessageEvent) -> str | None:
    if not event.message:
        return None

    first_msg = event.message[0]
    if not first_msg.is_text():
        return None

    text = first_msg.data.get("text", "").strip()
    if not text.startswith("/"):
        return None

    return text.split(maxsplit=1)[0].lower()


def _is_command_message(event: GroupMessageEvent | PrivateMessageEvent) -> bool:
    return _get_first_command(event) is not None


def _build_group_response(response: str) -> Message:
    message = Message()
    cursor = 0

    for match in AT_SEGMENT_PATTERN.finditer(response):
        if match.start() > cursor:
            message.append(MessageSegment.text(response[cursor : match.start()]))

        qq = match.group("cq")
        message.append(MessageSegment.at(qq if qq == "all" else int(qq)))
        cursor = match.end()

    if cursor < len(response):
        message.append(MessageSegment.text(response[cursor:]))

    return message


@poke_clear.handle()
async def handle_poke_clear_group(event: PokeNotifyEvent, bot: Bot):
    if event.group_id is None:
        return
    try:
        await clear_group_history(event.group_id)
    except Exception as exc:
        print(f"Failed to clear group context {event.group_id}: {exc}")
        await bot.send_group_msg(group_id=event.group_id, message="Failed to clear context.")
        await poke_clear.finish()
    await bot.send_group_msg(group_id=event.group_id, message="Context cleared.")
    await poke_clear.finish()


@llm.handle()
async def handle_llm_group(event: GroupMessageEvent, bot: Bot):
    command = _get_first_command(event)
    if command in CLEAR_CONTEXT_COMMANDS:
        try:
            await clear_group_history(event.group_id)
        except Exception as exc:
            print(f"Failed to clear group context {event.group_id}: {exc}")
            await llm.finish("Failed to clear context.")
        await llm.finish("Context cleared.")

    if _is_command_message(event):
        await llm.finish()

    response = await chat.group_chat(event, bot, True)
    await llm.finish(_build_group_response(response))


@llm.handle()
async def handle_llm_user(event: PrivateMessageEvent, bot: Bot):
    command = _get_first_command(event)
    if command in CLEAR_CONTEXT_COMMANDS:
        try:
            await clear_private_history(event.user_id)
        except Exception as exc:
            print(f"Failed to clear private context {event.user_id}: {exc}")
            await llm.finish("Failed to clear context.")
        await llm.finish("Context cleared.")

    if _is_command_message(event):
        await llm.finish()

    response = await chat.private_chat(event, bot, True)
    await llm.finish(response)
