import re

from nonebot import on_message
from nonebot.rule import to_me
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent

from . import chat
from .graph import clear_group_history, clear_private_history


llm = on_message(rule=to_me(), priority=10, block=False)
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
