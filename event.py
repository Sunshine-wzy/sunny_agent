import asyncio
from nonebot import on_message
from nonebot.rule import to_me
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent

from . import chat
from .mem import add_memory
from .mem.group_mem import get_group_mem_enabled


llm = on_message(rule=to_me(), priority=10, block=True)
mem = on_message(priority=15, block=True)


@llm.handle()
async def handle_llm_group(event: GroupMessageEvent, bot: Bot):
    first_msg = event.message[0]
    if first_msg.is_text() and first_msg.data.get("text", "").startswith("/"):
        await llm.finish()
    
    mem_enabled = get_group_mem_enabled(event.group_id)
    response = await chat.group_chat(event, bot, mem_enabled)
    await llm.finish(response)

@llm.handle()
async def handle_llm_user(event: PrivateMessageEvent, bot: Bot):
    first_msg = event.message[0]
    if first_msg.is_text() and first_msg.data.get("text", "").startswith("/"):
        await llm.finish()
    response = await chat.private_chat(event, bot, True)
    await llm.finish(response)


@mem.handle()
async def handle_mem_group(event: GroupMessageEvent):
    if get_group_mem_enabled(event.group_id):
        user_id = event.sender.user_id
        if user_id:
            mem_user_id = f"u{user_id}"
            asyncio.create_task(add_memory(event.message.__str__(), user_id=mem_user_id))
    await mem.finish()