import asyncio
import random
from nonebot import on_message
from nonebot.rule import to_me
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent

from . import chat
from .graph import clear_group_history, clear_private_history
from .mem import add_memory
from .mem.group_mem import is_group_mem_enabled
from .sora.group_sora import is_group_sora_enabled, set_group_sora_enabled
from .sora.sora_task import request_sora


llm = on_message(rule=to_me(), priority=10, block=False)
mem = on_message(priority=15, block=False)
sora = on_message(rule=to_me(), priority=15, block=False)
CLEAR_CONTEXT_COMMANDS = {"/clear"}


def _get_first_command(event: GroupMessageEvent | PrivateMessageEvent) -> str | None:
    if not event.message:
        return None

    first_msg = event.message[0]
    if not first_msg.is_text():
        return None

    text = first_msg.data.get("text", "").strip()
    if not text.startswith("/"):
        return None

    return text.split(maxsplit=1)[0].lower()


def _is_command_message(event: GroupMessageEvent | PrivateMessageEvent) -> bool:
    return _get_first_command(event) is not None


@llm.handle()
async def handle_llm_group(event: GroupMessageEvent, bot: Bot):
    command = _get_first_command(event)
    if command in CLEAR_CONTEXT_COMMANDS:
        try:
            await clear_group_history(event.group_id)
        except Exception as exc:
            print(f"Failed to clear group context {event.group_id}: {exc}")
            await llm.finish("Failed to clear context.")
        await llm.finish("Context cleared.")

    if _is_command_message(event):
        await llm.finish()
    
    response = await chat.group_chat(event, bot, True)
    await llm.finish(response)

@llm.handle()
async def handle_llm_user(event: PrivateMessageEvent, bot: Bot):
    command = _get_first_command(event)
    if command in CLEAR_CONTEXT_COMMANDS:
        try:
            await clear_private_history(event.user_id)
        except Exception as exc:
            print(f"Failed to clear private context {event.user_id}: {exc}")
            await llm.finish("Failed to clear context.")
        await llm.finish("Context cleared.")

    if _is_command_message(event):
        await llm.finish()

    response = await chat.private_chat(event, bot, True)
    await llm.finish(response)


# @mem.handle()
# async def handle_mem_group(event: GroupMessageEvent, bot: Bot):
#     if is_group_mem_enabled(event.group_id):
#         user_id = event.sender.user_id
#         if user_id:
#             mem_user_id = f"u{user_id}"
#             asyncio.create_task(add_memory(event.message.__str__(), user_id=mem_user_id))
            
#             # 概率回复消息
#             if random.random() < 0.1:
#                 response = await chat.group_chat(event, bot, True)
#                 await mem.finish(response)
    
#     await mem.finish()


# @sora.handle()
# async def handle_sora_group(event: GroupMessageEvent):
#     first_msg = event.message[0]
#     if not first_msg.is_text():
#         await sora.finish()

#     text = first_msg.data.get("text", "").strip()
#     if not text.startswith("/sora"):
#         await sora.finish()

#     parts = text.split(maxsplit=1)
#     cmd = parts[0].lower()
#     arg = parts[1].strip() if len(parts) > 1 else ""
    
#     if event.sender.user_id == 1123574549:
#         arg_lower = arg.lower()
        
#         # /sora open
#         if arg_lower == "open":
#             set_group_sora_enabled(event.group_id, True)
#             await sora.finish("✅ Sora 已开启")

#         # /sora close
#         elif arg_lower == "close":
#             set_group_sora_enabled(event.group_id, False)
#             await sora.finish("🈚 Sora 已关闭")

#     # /sora {prompt}
#     if not is_group_sora_enabled(event.group_id):
#         await sora.finish("⚠️ 本群未开启 Sora 功能")
#     else:
#         prompt = arg
#         if prompt:
#             print(f"[Sora] 来自群 {event.group_id}: {prompt}")
#             # await sora.send(f"🎨 收到 Sora Prompt: {prompt}")
#             await request_sora(prompt, lambda msg: sora.send(msg))
#         else:
#             await sora.finish("请输入要生成的内容，如 /sora 一只小狗在天空中玩耍")
