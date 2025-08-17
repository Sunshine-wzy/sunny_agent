from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

from nonebot.adapters.onebot.v11 import Bot

from typing import Annotated

from .mem import get_memory


@tool
async def group_name(config: RunnableConfig) -> str:
    """Gets the name of the current group."""
    conf = config["configurable"] # type: ignore
    bot = conf["bot"]
    event = conf["event"]
    
    group_info = await bot.get_group_info(group_id=event.group_id)
    return group_info["group_name"]

@tool
async def group_member_list(config: RunnableConfig) -> list:
    """Gets all members of the current group."""
    conf = config["configurable"] # type: ignore
    bot = conf["bot"]
    event = conf["event"]
    
    members = await bot.get_group_member_list(group_id=event.group_id)
    return members[:10]

@tool
async def send_private_message(
    user_id: Annotated[int, "the qq of the user"],
    message: Annotated[str, "the message to send(can use CQ code)"],
    config: RunnableConfig
) -> str:
    """Sends private chat message to the user."""
    conf = config["configurable"] # type: ignore
    bot: Bot = conf["bot"]
    await bot.send_private_msg(user_id=user_id, message=message)
    return "The private chat message was sent successfully"

@tool
async def search_user_memories(
    user_id: Annotated[int, "the qq of the user"],
    query: Annotated[str, "the query to use for searching memories"]
) -> str:
    """Searches the user's memory store for information and experiences that are highly relevant to the provided natural language query."""
    memory = await get_memory()
    mem_user_id = f"u{user_id}"
    relevant_memories = await memory.search(query=query, user_id=mem_user_id, limit=10)
    memories_str = "\n".join(f"- {entry['memory']}" for entry in relevant_memories["results"])
    print(f"Memories ({mem_user_id}): {memories_str}")
    return memories_str if memories_str.strip() else "暂无相关记忆信息"

@tool
async def list_user_memories(
    user_id: Annotated[int, "the qq of the user"]
) -> str:
    """Lists the user's all memories."""
    memory = await get_memory()
    mem_user_id = f"u{user_id}"
    relevant_memories = await memory.get_all(user_id=mem_user_id, limit=20)
    memories_str = "\n".join(f"- {entry['memory']}" for entry in relevant_memories["results"])
    print(f"Memories ({mem_user_id}): {memories_str}")
    return memories_str if memories_str.strip() else "暂无相关记忆信息"