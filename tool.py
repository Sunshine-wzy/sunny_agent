import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Annotated, Any

from agents import RunContextWrapper, function_tool
from nonebot import get_plugin_config
import nonebot_plugin_localstore as store
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent

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
