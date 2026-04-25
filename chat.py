import asyncio
import base64
import mimetypes
import urllib.request
from typing import Any

from agents import RunConfig, Runner
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment, PrivateMessageEvent

from .graph import model_provider, run_group_chat, run_private_chat, translator_agent


IMAGE_TOKEN_HINT = "[user sent an image]"
MessageEvent = GroupMessageEvent | PrivateMessageEvent


def _build_user_prompt(name: str, user_id: int, text: str) -> str:
    return f"user(name={name},qq={user_id}):{text}"


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


async def _build_agent_input(
    event: MessageEvent,
    bot: Bot,
    user_name: str,
) -> str | list[dict[str, Any]]:
    raw_text = event.message.__str__()
    has_image = any(segment.type == "image" for segment in event.message)

    if not has_image:
        return _build_user_prompt(user_name, event.user_id, raw_text)

    content: list[dict[str, Any]] = [
        {"type": "input_text", "text": _build_user_prompt(user_name, event.user_id, IMAGE_TOKEN_HINT)}
    ]

    for segment in event.message:
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

    return [{"role": "user", "content": content}]


async def group_chat(event: GroupMessageEvent, bot: Bot, mem_enabled: bool) -> str:
    msg = event.message.__str__()
    print(msg)
    user_name = event.sender.card if event.sender.card else event.sender.nickname or "Unknown"
    agent_input = await _build_agent_input(event, bot, user_name)
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
