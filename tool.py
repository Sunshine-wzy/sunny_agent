from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

from nonebot.adapters.onebot.v11 import Bot

from typing import Annotated

from .sora.sora_task import request_sora


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

# @tool
# async def generate_video_sora(
#     prompt: Annotated[str, "the prompt to generate the video"],
#     config: RunnableConfig
# ):
#     """Generates a video by sora-2"""
#     conf = config["configurable"] # type: ignore
#     bot: Bot = conf["bot"]
#     event = conf["event"]
#     group_id = event.group_id
#     await request_sora(
#         prompt, lambda msg: bot.send_group_msg(group_id=group_id, message=msg)
#     )
#     return f"The video was generated successfully (prompt: {prompt})"
