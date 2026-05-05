import asyncio
import base64
import mimetypes
import urllib.request
from dataclasses import dataclass
from typing import Any

from agents import RunConfig, Runner
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent

from .graph import model_provider, run_group_chat, run_private_chat, translator_agent


IMAGE_TOKEN_HINT = "[user sent an image]"
MessageEvent = GroupMessageEvent | PrivateMessageEvent


@dataclass(slots=True)
class ReferencedMessage:
    message_id: int | str | None
    user_name: str
    user_id: int | str
    message: Message


def _build_user_prompt(name: str, user_id: int, text: str) -> str:
    return f"user(name={name},qq={user_id}):{text}"


def _build_reference_prompt(reference: ReferencedMessage, text: str) -> str:
    return (
        f"referenced_message(name={reference.user_name},qq={reference.user_id},"
        f"message_id={reference.message_id}):{text}"
    )


def _get_field(data: Any, field: str, default: Any = None) -> Any:
    if isinstance(data, dict):
        return data.get(field, default)
    return getattr(data, field, default)


def _get_sender_name(sender: Any) -> str:
    return str(_get_field(sender, "card") or _get_field(sender, "nickname") or "Unknown")


def _get_sender_user_id(sender: Any) -> int | str:
    return _get_field(sender, "user_id") or _get_field(sender, "qq") or "unknown"


def _coerce_message(message: Any) -> Message:
    if isinstance(message, Message):
        return message

    if isinstance(message, list):
        coerced = Message()
        for item in message:
            if isinstance(item, MessageSegment):
                coerced.append(item)
                continue

            if isinstance(item, dict):
                segment_type = item.get("type")
                if segment_type:
                    coerced.append(MessageSegment(segment_type, item.get("data") or {}))
        return coerced

    return Message(str(message or ""))


def _message_has_image(message: Message) -> bool:
    return any(segment.type == "image" for segment in message)


def _message_text(message: Message, *, skip_reply: bool = False) -> str:
    return "".join(str(segment) for segment in message if not (skip_reply and segment.type == "reply"))


def _reply_segment_message_id(message: Message) -> int | str | None:
    for segment in message:
        if segment.type != "reply":
            continue

        message_id = segment.data.get("id")
        if message_id is not None:
            return message_id

    return None


async def _get_referenced_message(event: MessageEvent, bot: Bot) -> ReferencedMessage | None:
    reply = getattr(event, "reply", None)
    if reply is not None:
        message = _get_field(reply, "message")
        sender = _get_field(reply, "sender")
        if message is not None:
            return ReferencedMessage(
                message_id=_get_field(reply, "message_id"),
                user_name=_get_sender_name(sender),
                user_id=_get_sender_user_id(sender),
                message=_coerce_message(message),
            )

    message_id = _reply_segment_message_id(event.message)
    if message_id is None:
        return None

    try:
        message_info = await bot.get_msg(message_id=int(message_id))
    except Exception as exc:
        print(f"Failed to fetch referenced message {message_id}: {exc}")
        return None

    sender = message_info.get("sender") or {}
    return ReferencedMessage(
        message_id=message_info.get("message_id", message_id),
        user_name=_get_sender_name(sender),
        user_id=_get_sender_user_id(sender),
        message=_coerce_message(message_info.get("message") or message_info.get("raw_message")),
    )


def _download_image_as_data_url(url: str, file_hint: str = "") -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "sunny-agent/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        image_bytes = response.read()
        content_type = response.headers.get_content_type()

    if not content_type or content_type == "application/octet-stream":
        content_type = mimetypes.guess_type(file_hint or url)[0] or "image/jpeg"

    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{content_type};base64,{encoded}"


async def _build_image_block(segment: MessageSegment, bot: Bot) -> dict[str, Any] | None:
    image_url = segment.data.get("url")
    image_file = segment.data.get("file", "")

    if not image_url and image_file:
        try:
            image_info = await bot.get_image(file=image_file)
        except Exception as exc:
            print(f"Failed to fetch image info for {image_file}: {exc}")
            return None
        image_url = image_info.get("url")

    if not image_url:
        print(f"Image segment missing url: {segment}")
        return None

    try:
        data_url = await asyncio.to_thread(_download_image_as_data_url, image_url, image_file)
    except Exception as exc:
        print(f"Failed to download image {image_url}: {exc}")
        return None

    return {"type": "input_image", "image_url": data_url}


async def _append_message_content(
    content: list[dict[str, Any]],
    message: Message,
    bot: Bot,
    leading_text: str,
    *,
    skip_reply: bool = False,
) -> None:
    content.append({"type": "input_text", "text": leading_text})

    if not _message_has_image(message):
        return

    for segment in message:
        if skip_reply and segment.type == "reply":
            continue

        if segment.is_text():
            text = segment.data.get("text", "")
            if text:
                content.append({"type": "input_text", "text": text})
            continue

        if segment.type == "image":
            image_block = await _build_image_block(segment, bot)
            if image_block:
                content.append(image_block)
                continue

            content.append({"type": "input_text", "text": IMAGE_TOKEN_HINT})
            continue

        fallback_text = str(segment)
        if fallback_text:
            content.append({"type": "input_text", "text": fallback_text})


async def _build_agent_input(
    event: MessageEvent,
    bot: Bot,
    user_name: str,
) -> str | list[dict[str, Any]]:
    referenced_message = await _get_referenced_message(event, bot)
    raw_text = _message_text(event.message, skip_reply=True)
    has_image = _message_has_image(event.message)
    referenced_has_image = bool(referenced_message and _message_has_image(referenced_message.message))

    if not has_image and not referenced_has_image:
        prompts: list[str] = []
        if referenced_message:
            prompts.append(_build_reference_prompt(referenced_message, _message_text(referenced_message.message)))
        prompts.append(_build_user_prompt(user_name, event.user_id, raw_text))
        return "\n".join(prompts)

    content: list[dict[str, Any]] = []
    if referenced_message:
        reference_text = IMAGE_TOKEN_HINT if referenced_has_image else _message_text(referenced_message.message)
        await _append_message_content(
            content,
            referenced_message.message,
            bot,
            _build_reference_prompt(referenced_message, reference_text),
        )

    user_text = IMAGE_TOKEN_HINT if has_image else raw_text
    await _append_message_content(
        content,
        event.message,
        bot,
        _build_user_prompt(user_name, event.user_id, user_text),
        skip_reply=True,
    )

    return [{"role": "user", "content": content}]


async def group_chat(event: GroupMessageEvent, bot: Bot, mem_enabled: bool) -> str:
    msg = event.message.__str__()
    print(msg)
    user_name = event.sender.card if event.sender.card else event.sender.nickname or "Unknown"
    agent_input = await _build_agent_input(event, bot, user_name)
    print(agent_input)
    response = await run_group_chat(event, bot, agent_input)
    print(f"Agent output: {response}")
    return response


async def private_chat(event: PrivateMessageEvent, bot: Bot, mem_enabled: bool) -> str:
    msg = event.message.__str__()
    print(msg)
    user_name = event.sender.nickname or "Unknown"
    agent_input = await _build_agent_input(event, bot, user_name)
    response = await run_private_chat(event, bot, agent_input)
    print(f"Agent output: {response}")
    return response


async def atranslate(text: str) -> str:
    result = await Runner.run(
        translator_agent,
        text,
        run_config=RunConfig(model_provider=model_provider),
    )
    return str(result.final_output or "")


def translate(text: str) -> str:
    result = Runner.run_sync(
        translator_agent,
        text,
        run_config=RunConfig(model_provider=model_provider),
    )
    return str(result.final_output or "")
