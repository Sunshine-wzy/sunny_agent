import asyncio
import os
from dataclasses import dataclass
from typing import Annotated, Any

from agents import RunContextWrapper, function_tool
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent


@dataclass(slots=True)
class ChatContext:
    bot: Bot
    event: GroupMessageEvent | PrivateMessageEvent


@function_tool
async def web_search(query: Annotated[str, "The web search query."]) -> str:
    """Searches the web and returns a short list of relevant results."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return "Web search is unavailable because TAVILY_API_KEY is not configured."

    try:
        from tavily import TavilyClient
    except ImportError:
        return "Web search is unavailable because tavily-python is not installed."

    def search() -> dict[str, Any]:
        client = TavilyClient(api_key=api_key)
        return client.search(query=query, max_results=2)

    try:
        response = await asyncio.to_thread(search)
    except Exception as exc:
        return f"Web search failed: {exc}"

    results = response.get("results") or []
    if not results:
        return "No web search results were found."

    lines = []
    for item in results[:2]:
        title = item.get("title", "Untitled")
        url = item.get("url", "")
        content = item.get("content", "")
        lines.append(f"- {title}\n  {url}\n  {content}")
    return "\n".join(lines)


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


@function_tool
async def send_private_message(
    ctx: RunContextWrapper[ChatContext],
    user_id: Annotated[int, "The QQ number of the user."],
    message: Annotated[str, "The message to send. CQ code is allowed."],
) -> str:
    """Sends a private chat message to the user."""
    await ctx.context.bot.send_private_msg(user_id=user_id, message=message)
    return "The private chat message was sent successfully."
