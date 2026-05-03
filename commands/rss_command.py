import xml.etree.ElementTree as ET

from arclet.alconna import Alconna
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.log import logger
from nonebot.rule import to_me
from nonebot_plugin_alconna import Subcommand, on_alconna

from ..rss_daily import (
    fetch_ai_daily_items,
    is_group_ai_daily_enabled,
    load_state,
    save_state,
    send_items_to_group,
    set_group_ai_daily_enabled,
)


rss = on_alconna(
    Alconna(
        "rss",
        Subcommand("open", alias=["on", "enable", "开启"]),
        Subcommand("close", alias=["off", "disable", "关闭"]),
        Subcommand("today", alias=["now", "fetch", "立即", "今日"]),
    ),
    rule=to_me(),
    use_cmd_start=True,
)


@rss.assign("$main")
async def handle_rss_main(event: GroupMessageEvent) -> None:
    status = "开启" if is_group_ai_daily_enabled(event.group_id) else "关闭"
    await rss.finish(
        f"本群每日 AI 早报推送：{status}\n"
        "可用命令：/rss open、/rss close、/rss today",
    )


@rss.assign("open")
async def handle_rss_open(event: GroupMessageEvent) -> None:
    set_group_ai_daily_enabled(event.group_id, True)
    await rss.finish("本群每日 AI 早报推送已开启")


@rss.assign("close")
async def handle_rss_close(event: GroupMessageEvent) -> None:
    set_group_ai_daily_enabled(event.group_id, False)
    await rss.finish("本群每日 AI 早报推送已关闭")


@rss.assign("today")
async def handle_rss_today(event: GroupMessageEvent, bot: Bot) -> None:
    await rss.send("正在获取今日 AI 早报...")

    try:
        items = await fetch_ai_daily_items()
    except (OSError, TimeoutError, ET.ParseError, ValueError) as exc:
        logger.exception(f"Failed to fetch AI daily RSS: {exc}")
        await rss.finish("获取 AI 早报失败，请稍后再试")
        return

    if not items:
        await rss.finish("RSS 源暂时没有返回 AI 早报内容")
        return

    state = load_state()
    sent_count, state_changed = await send_items_to_group(
        event.group_id,
        items,
        state,
        preferred_bot=bot,
        only_unsent=False,
    )

    if state_changed:
        save_state(state)

    if sent_count:
        # await rss.finish("今日 AI 早报已发送")
        return

    await rss.finish("AI 早报发送失败，请稍后再试")
