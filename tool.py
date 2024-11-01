from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig


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