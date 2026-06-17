import asyncio
import json
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Literal

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
DEFAULT_WEB_SEARCH_URL = "https://open.bigmodel.cn/api/paas/v4/web_search"


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


def _get_bigmodel_api_key() -> str:
    return (
        os.getenv("SUNNY_AGENT_WEB_SEARCH_API_KEY")
        or os.getenv("SUNNY_AGENT_OPENAI_API_KEY")
        or os.getenv("ZHIPUAI_API_KEY")
        or os.getenv("BIGMODEL_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()


def _get_web_search_url() -> str:
    return os.getenv("SUNNY_AGENT_WEB_SEARCH_URL", DEFAULT_WEB_SEARCH_URL).strip()


def _post_bigmodel_web_search(
    url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: float = 20,
) -> tuple[int, str]:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "sunny-agent/1.0",
    }
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


@function_tool
async def web_search(
    ctx: RunContextWrapper[ChatContext],
    query: Annotated[
        str,
        "The web search query. Keep it concise; BigModel recommends no more than 70 characters.",
    ],
    count: Annotated[int, "Number of search results to return, from 1 to 50."] = 5,
    search_engine: Annotated[
        Literal["search_std", "search_pro", "search_pro_sogou", "search_pro_quark"],
        "BigModel search engine code.",
    ] = "search_pro",
    search_recency_filter: Annotated[
        Literal["oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit"],
        "Restrict results to a time range.",
    ] = "noLimit",
    content_size: Annotated[
        Literal["medium", "high"],
        "How much webpage summary content to return.",
    ] = "high",
    search_domain_filter: Annotated[
        str,
        "Optional domain whitelist, such as www.example.com. Leave empty for all domains.",
    ] = "",
    search_intent: Annotated[
        bool,
        "Whether BigModel should first identify search intent before searching.",
    ] = False,
) -> dict[str, Any]:
    """Searches the web using BigModel Web Search API and returns cited results."""
    clean_query = query.strip()
    if not clean_query:
        return {"ok": False, "error": "Search query cannot be empty."}

    if len(clean_query) > 70:
        return {
            "ok": False,
            "error": "Search query is too long. BigModel recommends no more than 70 characters.",
        }

    if count < 1 or count > 50:
        return {"ok": False, "error": "count must be between 1 and 50."}

    api_key = _get_bigmodel_api_key()
    if not api_key:
        return {
            "ok": False,
            "error": (
                "BigModel API key is not configured. Set SUNNY_AGENT_WEB_SEARCH_API_KEY, "
                "SUNNY_AGENT_OPENAI_API_KEY, ZHIPUAI_API_KEY, BIGMODEL_API_KEY, or OPENAI_API_KEY."
            ),
        }

    event = ctx.context.event
    payload: dict[str, Any] = {
        "search_query": clean_query,
        "search_engine": search_engine,
        "search_intent": search_intent,
        "count": count,
        "search_recency_filter": search_recency_filter,
        "content_size": content_size,
        "request_id": uuid.uuid4().hex,
        "user_id": f"sunny_{event.user_id}",
    }
    clean_domain = search_domain_filter.strip()
    if clean_domain:
        payload["search_domain_filter"] = clean_domain

    url = _get_web_search_url()
    try:
        status, body = await asyncio.to_thread(
            _post_bigmodel_web_search,
            url,
            api_key,
            payload,
        )
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "url": url,
            "error": f"Could not reach BigModel web search API: {exc.reason}",
        }
    except OSError as exc:
        return {
            "ok": False,
            "url": url,
            "error": f"Could not send BigModel web search request: {exc}",
        }
    except ValueError as exc:
        return {
            "ok": False,
            "url": url,
            "error": f"Invalid BigModel web search URL {url!r}: {exc}",
        }

    decoded = _decode_sunny_flayer_response(status, body)
    return {
        "ok": 200 <= status < 300,
        "url": url,
        **decoded,
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
