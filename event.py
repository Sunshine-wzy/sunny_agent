from nonebot import on_message
from nonebot.rule import to_me, startswith
from nonebot.adapters.onebot.v11 import Message, Bot, GroupMessageEvent, PrivateMessageEvent
from nonebot.params import CommandArg
from . import chat


llm = on_message(rule=to_me(), priority=10, block=True)

@llm.handle()
async def handle_llm_group(event: GroupMessageEvent):
    msg = event.message.__str__()
    print(msg)
    response = await chat.group_chat(event.group_id, msg, event.user_id, event.sender.card if event.sender.card else event.sender.nickname)
    await llm.finish(response)

@llm.handle()
async def handle_llm_user(event: PrivateMessageEvent):
    first_msg = event.message[0]
    if first_msg.is_text() and first_msg.data.get("text", "").startswith("/"):
        await llm.finish()
    msg = event.message.__str__()
    print(msg)
    response = await chat.user_chat(msg, event.user_id, event.sender.nickname)
    await llm.finish(response)
