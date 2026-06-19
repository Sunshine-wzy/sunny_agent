import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Annotated, Any

from agents import RunContextWrapper, function_tool
from nonebot import get_plugin_config
import nonebot_plugin_localstore as store
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment, PrivateMessageEvent

from .config import Config


@dataclass(slots=True)
class ChatContext:
    bot: Bot
    event: GroupMessageEvent | PrivateMessageEvent


plugin_config = get_plugin_config(Config)
ACTIVE_GROUP_RECEIVE_FILE = store.get_plugin_data_file("active_group_receive.json")
active_group_receiving_group_ids: set[int] = set()


def _load_active_group_receiving_group_ids() -> set[int]:
    try:
        raw_state = ACTIVE_GROUP_RECEIVE_FILE.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return set()

    if not raw_state.strip():
        return set()

    try:
        state = json.loads(raw_state)
    except json.JSONDecodeError as exc:
        print(f"Failed to parse active_group_receive.json: {exc}")
        return set()

    if isinstance(state, list):
        raw_group_ids = state
    elif isinstance(state, dict):
        raw_group_ids = state.get("enabled_group_ids", [])
        if not isinstance(raw_group_ids, list):
            raw_group_ids = [
                group_id
                for group_id, enabled in state.items()
                if enabled and group_id != "enabled_group_ids"
            ]
    else:
        return set()

    group_ids: set[int] = set()
    for raw_group_id in raw_group_ids:
        try:
            group_ids.add(int(raw_group_id))
        except (TypeError, ValueError):
            print(f"Skipping invalid active group receive id: {raw_group_id!r}")

    return group_ids


def _save_active_group_receiving_group_ids() -> None:
    ACTIVE_GROUP_RECEIVE_FILE.write_text(
        json.dumps(
            {"enabled_group_ids": sorted(active_group_receiving_group_ids)},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


active_group_receiving_group_ids = _load_active_group_receiving_group_ids()


def set_group_active_receiving_enabled(group_id: int, enabled: bool) -> None:
    if enabled:
        active_group_receiving_group_ids.add(group_id)
    else:
        active_group_receiving_group_ids.discard(group_id)

    _save_active_group_receiving_group_ids()


def is_group_active_receiving_enabled(group_id: int) -> bool:
    return group_id in active_group_receiving_group_ids


@function_tool
async def group_name(ctx: RunContextWrapper[ChatContext]) -> str:
    """Gets the name of the current group."""
    event = ctx.context.event
    if not isinstance(event, GroupMessageEvent):
        return "This chat is not a group chat."

    group_info = await ctx.context.bot.get_group_info(group_id=event.group_id)
    return group_info["group_name"]


@function_tool
async def group_member_list(ctx: RunContextWrapper[ChatContext]) -> list[dict[str, Any]]:
    """Gets a short list of members in the current group."""
    event = ctx.context.event
    if not isinstance(event, GroupMessageEvent):
        return []

    members = await ctx.context.bot.get_group_member_list(group_id=event.group_id)
    return members[:10]


def _clean_member_name(value: Any) -> str:
    return str(value or "").strip()


@function_tool
async def group_member_qq_by_nickname(
    ctx: RunContextWrapper[ChatContext],
    nickname: Annotated[
        str,
        "The QQ group member nickname or group card name to search for. Partial match is supported.",
    ],
) -> dict[str, Any]:
    """Finds QQ numbers for current group members by nickname or group card name."""
    event = ctx.context.event
    if not isinstance(event, GroupMessageEvent):
        return {
            "query": nickname,
            "matches": [],
            "total": 0,
            "truncated": False,
            "message": "This chat is not a group chat.",
        }

    query = nickname.strip()
    if not query:
        return {
            "query": nickname,
            "matches": [],
            "total": 0,
            "truncated": False,
            "message": "Nickname cannot be empty.",
        }

    query_folded = query.casefold()
    members = await ctx.context.bot.get_group_member_list(group_id=event.group_id)
    scored_matches: list[tuple[int, dict[str, Any]]] = []

    for member in members:
        card = _clean_member_name(member.get("card"))
        member_nickname = _clean_member_name(member.get("nickname"))
        display_name = card or member_nickname
        names = [name for name in (card, member_nickname) if name]

        if any(query_folded == name.casefold() for name in names):
            score = 0
        elif any(query_folded in name.casefold() for name in names):
            score = 1
        else:
            continue

        scored_matches.append(
            (
                score,
                {
                    "user_id": member.get("user_id"),
                    "nickname": member_nickname,
                    "card": card,
                    "display_name": display_name,
                },
            )
        )

    scored_matches.sort(
        key=lambda item: (
            item[0],
            len(item[1]["display_name"] or item[1]["nickname"]),
            item[1]["user_id"] or 0,
        )
    )
    matches = [match for _, match in scored_matches]
    limit = 20
    result = {
        "query": query,
        "matches": matches[:limit],
        "total": len(matches),
        "truncated": len(matches) > limit,
    }
    print(result)
    return result


@function_tool
async def send_private_message(
    ctx: RunContextWrapper[ChatContext],
    user_id: Annotated[int, "The QQ number of the user."],
    message: Annotated[str, "The message to send. CQ code is allowed."],
) -> str:
    """Sends a private chat message to the user."""
    await ctx.context.bot.send_private_msg(user_id=user_id, message=message)
    return "The private chat message was sent successfully."


def _sunny_flayer_source(ctx: RunContextWrapper[ChatContext]) -> dict[str, Any]:
    event = ctx.context.event
    source: dict[str, Any] = {
        "adapter": "onebot.v11",
        "user_id": event.user_id,
    }

    if isinstance(event, GroupMessageEvent):
        source["chat_type"] = "group"
        source["group_id"] = event.group_id
    else:
        source["chat_type"] = "private"

    return source


def _post_sunny_flayer_instruction(
    url: str,
    token: str,
    payload: dict[str, Any],
    timeout: float,
) -> tuple[int, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "sunny-agent/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body


def _decode_sunny_flayer_response(status: int, body: str) -> dict[str, Any]:
    body = body.strip()
    if not body:
        return {
            "ok": 200 <= status < 300,
            "http_status": status,
        }

    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        return {
            "ok": 200 <= status < 300,
            "http_status": status,
            "raw_body": body[:1000],
        }

    if isinstance(decoded, dict):
        return {
            "http_status": status,
            **decoded,
        }

    return {
        "ok": 200 <= status < 300,
        "http_status": status,
        "response": decoded,
    }


def _image_generation_endpoint(base_url: str) -> str:
    cleaned_base_url = base_url.strip().rstrip("/")
    if not cleaned_base_url:
        cleaned_base_url = "https://api.openai.com/v1"

    if cleaned_base_url.endswith("/images/generations"):
        return cleaned_base_url

    return f"{cleaned_base_url}/images/generations"


def _post_image_generation(
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: float,
) -> tuple[int, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "sunny-agent/1.0",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(
        _image_generation_endpoint(base_url),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return exc.code, body


def _truncate_debug_text(value: Any, limit: int = 4000) -> str:
    text = str(value)
    if len(text) <= limit:
        return text

    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"


def _extract_image_generation_error(decoded: Any) -> str:
    if isinstance(decoded, dict):
        error = decoded.get("error")
        if isinstance(error, dict):
            for key in ("message", "detail", "code", "type"):
                value = error.get(key)
                if value:
                    return str(value)
            return json.dumps(error, ensure_ascii=False)

        if error:
            return str(error)

        for key in ("message", "detail", "msg"):
            value = decoded.get(key)
            if value:
                return str(value)

    return "Image generation API request failed."


def _image_generation_request_summary(
    endpoint: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    summary = {
        "url": endpoint,
        "model": payload.get("model"),
        "n": payload.get("n"),
    }
    if payload.get("size"):
        summary["size"] = payload["size"]
    if payload.get("prompt"):
        summary["prompt_preview"] = _truncate_debug_text(payload["prompt"], 300)

    return summary


def _image_generation_failure_result(
    *,
    http_status: int | None,
    error_message: str,
    endpoint: str,
    payload: dict[str, Any],
    upstream_raw_body: str = "",
    upstream_response: Any = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "http_status": http_status,
        "error": error_message,
        "error_message": error_message,
        "request": _image_generation_request_summary(endpoint, payload),
        "message": (
            f"Image generation failed"
            f"{f' with HTTP {http_status}' if http_status is not None else ''}: "
            f"{error_message}"
        ),
    }
    if upstream_raw_body:
        result["upstream_raw_body"] = _truncate_debug_text(upstream_raw_body)
    if upstream_response is not None:
        result["upstream_response"] = upstream_response

    return result


def _log_image_generation_failure(result: dict[str, Any]) -> None:
    debug_result = {
        "http_status": result.get("http_status"),
        "error_message": result.get("error_message") or result.get("error"),
        "request": result.get("request"),
        "upstream_raw_body": result.get("upstream_raw_body"),
        "upstream_response": result.get("upstream_response"),
    }
    print(
        "Image generation failed: "
        + _truncate_debug_text(json.dumps(debug_result, ensure_ascii=False), 5000)
    )


def _decode_image_generation_response(status: int, body: str) -> dict[str, Any]:
    raw_body = body.strip()
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "http_status": status,
            "error": "Image generation API returned a non-JSON response.",
            "error_message": "Image generation API returned a non-JSON response.",
            "upstream_raw_body": _truncate_debug_text(raw_body),
        }

    if not isinstance(decoded, dict):
        return {
            "ok": False,
            "http_status": status,
            "error": "Image generation API returned an unexpected response.",
            "error_message": "Image generation API returned an unexpected response.",
            "upstream_raw_body": _truncate_debug_text(raw_body),
            "upstream_response": decoded,
        }

    if not 200 <= status < 300:
        error_message = _extract_image_generation_error(decoded)
        return {
            "ok": False,
            "http_status": status,
            "error": error_message,
            "error_message": error_message,
            "upstream_error": decoded.get("error"),
            "upstream_raw_body": _truncate_debug_text(raw_body),
            "upstream_response": decoded,
        }

    data = decoded.get("data")
    if not isinstance(data, list) or not data:
        return {
            "ok": False,
            "http_status": status,
            "error": "Image generation API did not return any images.",
            "response": decoded,
        }

    images: list[dict[str, Any]] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            continue

        image: dict[str, Any] = {"index": index}
        if item.get("url"):
            image["url"] = item["url"]
        if item.get("b64_json"):
            image["b64_json"] = item["b64_json"]
        if item.get("revised_prompt"):
            image["revised_prompt"] = item["revised_prompt"]
        if image.keys() - {"index"}:
            images.append(image)

    if not images:
        return {
            "ok": False,
            "http_status": status,
            "error": "Image generation API returned images in an unsupported format.",
            "response": decoded,
        }

    return {
        "ok": True,
        "http_status": status,
        "created": decoded.get("created"),
        "images": images,
    }


async def _send_generated_image(
    ctx: RunContextWrapper[ChatContext],
    image: dict[str, Any],
) -> dict[str, Any]:
    if image.get("url"):
        segment = MessageSegment.image(image["url"])
    elif image.get("b64_json"):
        segment = MessageSegment.image(f"base64://{image['b64_json']}")
    else:
        return {
            "sent": False,
            "error": "Image result has no URL or base64 data.",
        }

    event = ctx.context.event
    if isinstance(event, GroupMessageEvent):
        await ctx.context.bot.send_group_msg(group_id=event.group_id, message=segment)
    else:
        await ctx.context.bot.send_private_msg(user_id=event.user_id, message=segment)

    return {"sent": True}


@function_tool
async def image_generation(
    ctx: RunContextWrapper[ChatContext],
    prompt: Annotated[
        str,
        "The detailed image generation prompt.",
    ],
    size: Annotated[
        str,
        "Optional image size, such as 1024x1024, 1024x1536, or 1536x1024.",
    ] = "",
    n: Annotated[
        int,
        "Number of images to generate. Use 1 unless the user explicitly asks for more.",
    ] = 1,
) -> dict[str, Any]:
    """Generates image(s) and sends them to the current chat."""
    print(f"Generating image: prompt({prompt}), size({size}), n({n})")
    clean_prompt = prompt.strip()
    if not clean_prompt:
        return {
            "ok": False,
            "error": "Prompt cannot be empty.",
        }

    api_key = plugin_config.sunny_agent_image_generation_api_key.strip()
    if not api_key:
        return {
            "ok": False,
            "error": "sunny_agent_image_generation_api_key is not configured.",
        }

    if n < 1 or n > 4:
        return {
            "ok": False,
            "error": "n must be between 1 and 4.",
        }

    model = plugin_config.sunny_agent_image_generation_model.strip()
    if not model:
        return {
            "ok": False,
            "error": "sunny_agent_image_generation_model is not configured.",
        }

    clean_size = size.strip() or plugin_config.sunny_agent_image_generation_size.strip()
    payload = {
        "model": model,
        "prompt": clean_prompt,
        "n": n,
    }
    if clean_size:
        payload["size"] = clean_size

    endpoint = _image_generation_endpoint(plugin_config.sunny_agent_image_generation_base_url)
    try:
        status, body = await asyncio.to_thread(
            _post_image_generation,
            plugin_config.sunny_agent_image_generation_base_url,
            api_key,
            payload,
            plugin_config.sunny_agent_image_generation_timeout_seconds,
        )
    except urllib.error.URLError as exc:
        result = _image_generation_failure_result(
            http_status=None,
            error_message=f"Could not reach image generation API: {exc.reason}",
            endpoint=endpoint,
            payload=payload,
        )
        _log_image_generation_failure(result)
        return result
    except OSError as exc:
        result = _image_generation_failure_result(
            http_status=None,
            error_message=f"Could not call image generation API: {exc}",
            endpoint=endpoint,
            payload=payload,
        )
        _log_image_generation_failure(result)
        return result
    except ValueError as exc:
        result = _image_generation_failure_result(
            http_status=None,
            error_message=f"Invalid image generation API URL: {exc}",
            endpoint=endpoint,
            payload=payload,
        )
        _log_image_generation_failure(result)
        return result

    result = _decode_image_generation_response(status, body)
    if not result.get("ok"):
        result.setdefault("request", _image_generation_request_summary(endpoint, payload))
        result.setdefault(
            "message",
            (
                f"Image generation failed with HTTP {result.get('http_status')}: "
                f"{result.get('error_message') or result.get('error')}"
            ),
        )
        _log_image_generation_failure(result)
        return result

    sent_images: list[dict[str, Any]] = []
    for image in result["images"]:
        send_result: dict[str, Any]
        try:
            send_result = await _send_generated_image(ctx, image)
        except Exception as exc:
            send_result = {
                "sent": False,
                "error": f"Generated image could not be sent to chat: {exc}",
            }

        safe_image = {
            key: value
            for key, value in image.items()
            if key != "b64_json"
        }
        safe_image.update(send_result)
        sent_images.append(safe_image)

    return {
        "ok": True,
        "http_status": result["http_status"],
        "created": result.get("created"),
        "images": sent_images,
        "message": "Generated image(s) were sent to the current chat.",
    }


@function_tool
async def send_minecraft_instruction(
    ctx: RunContextWrapper[ChatContext],
    instruction: Annotated[
        str,
        "The natural-language instruction to send to Minecraft.",
    ],
) -> dict[str, Any]:
    """Sends a natural-language instruction to Minecraft."""
    clean_instruction = instruction.strip()
    if not clean_instruction:
        return {
            "ok": False,
            "error": "Instruction cannot be empty.",
        }

    username = plugin_config.sunny_agent_flayer_default_username.strip()
    if not username:
        return {
            "ok": False,
            "error": (
                "Minecraft username is required. Configure "
                "sunny_agent_flayer_default_username."
            ),
        }

    url = plugin_config.sunny_agent_flayer_instruction_url.strip()
    if not url:
        return {
            "ok": False,
            "error": "sunny_agent_flayer_instruction_url is not configured.",
        }

    payload = {
        "username": username,
        "instruction": clean_instruction,
        "source": _sunny_flayer_source(ctx),
    }

    try:
        status, body = await asyncio.to_thread(
            _post_sunny_flayer_instruction,
            url,
            plugin_config.sunny_agent_flayer_instruction_token.strip(),
            payload,
            plugin_config.sunny_agent_flayer_instruction_timeout_seconds,
        )
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "url": url,
            "error": f"Could not reach sunny-flayer instruction API: {exc.reason}",
        }
    except OSError as exc:
        return {
            "ok": False,
            "url": url,
            "error": f"Could not send instruction to sunny-flayer: {exc}",
        }
    except ValueError as exc:
        return {
            "ok": False,
            "url": url,
            "error": f"Invalid sunny-flayer instruction API URL {url!r}: {exc}",
        }

    return _decode_sunny_flayer_response(status, body)


@function_tool
async def enable_active_group_message_receiving(ctx: RunContextWrapper[ChatContext]) -> str:
    """Enables active receiving of group chat messages in the current group.

    When enabled, Sunny can receive messages from this group even when Sunny is not
    mentioned or replied to.
    """
    event = ctx.context.event
    if not isinstance(event, GroupMessageEvent):
        return "This chat is not a group chat."

    set_group_active_receiving_enabled(event.group_id, True)
    return "Active receiving of group chat messages is enabled for this group."


@function_tool
async def disable_active_group_message_receiving(ctx: RunContextWrapper[ChatContext]) -> str:
    """Disables active receiving of group chat messages in the current group.

    When disabled, Sunny only receives group messages when mentioned or replied to.
    """
    event = ctx.context.event
    if not isinstance(event, GroupMessageEvent):
        return "This chat is not a group chat."

    set_group_active_receiving_enabled(event.group_id, False)
    return "Active receiving of group chat messages is disabled for this group."
