from nonebot import get_driver, on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    PokeNotifyEvent,
    PrivateMessageEvent,
)

from . import tool
from .context import get_manager, stop_title_tasks
from .conversation_chat import handle_message, new_session_from_poke, reply_message_id
from .messaging import recover_pending, scope_for_event


async def _is_to_me_or_active_group(
    event: GroupMessageEvent | PrivateMessageEvent, bot: Bot
) -> bool:
    if event.is_tome():
        return True
    if isinstance(event, GroupMessageEvent) and tool.is_group_active_receiving_enabled(
        event.group_id
    ):
        return True
    return (
        await get_manager().lookup_message(
            scope_for_event(event, bot), reply_message_id(event)
        )
        is not None
    )


async def _is_group_poke_to_bot(event, bot: Bot) -> bool:
    return (
        isinstance(event, PokeNotifyEvent)
        and event.group_id is not None
        and event.target_id == int(bot.self_id)
    )


llm = on_message(rule=_is_to_me_or_active_group, priority=10, block=False)
poke_new = on_notice(rule=_is_group_poke_to_bot, priority=10, block=False)


@llm.handle()
async def handle_llm(event: GroupMessageEvent | PrivateMessageEvent, bot: Bot) -> None:
    await handle_message(event, bot)


@poke_new.handle()
async def handle_poke_new_group(event: PokeNotifyEvent, bot: Bot) -> None:
    if event.group_id is not None:
        await new_session_from_poke(
            scope_for_event(event, bot),
            bot,
            f"poke:{event.time}:{event.user_id}:{event.target_id}",
        )


@get_driver().on_bot_connect
async def recover_context_deliveries(bot: Bot) -> None:
    if isinstance(bot, Bot):
        await recover_pending(bot)


@get_driver().on_shutdown
async def stop_context_title_tasks() -> None:
    await stop_title_tasks()
