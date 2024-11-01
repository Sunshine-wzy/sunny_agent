from nonebot import on_message
from nonebot.rule import to_me
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent
from . import chat


llm = on_message(rule=to_me(), priority=10, block=True)

@llm.handle()
async def handle_llm_group(event: GroupMessageEvent, bot: Bot):
    response = await chat.group_chat(event, bot)
    await llm.finish(response)

@llm.handle()
async def handle_llm_user(event: PrivateMessageEvent, bot: Bot):
    first_msg = event.message[0]
    if first_msg.is_text() and first_msg.data.get("text", "").startswith("/"):
        await llm.finish()
    response = await chat.private_chat(event, bot)
    await llm.finish(response)
