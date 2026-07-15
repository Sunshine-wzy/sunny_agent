from arclet.alconna import Alconna
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.log import logger
from nonebot.rule import to_me
from nonebot_plugin_alconna import Subcommand, on_alconna

from ..tibo_monitor import (
    fetch_tibo_posts,
    format_post_message,
    is_group_tibo_monitor_enabled,
    latest_included_post,
    send_group_message,
    set_group_tibo_monitor_enabled,
    translate_post,
)


tibo = on_alconna(
    Alconna(
        "tibo",
        Subcommand("open", alias=["on", "enable", "开启"]),
        Subcommand("close", alias=["off", "disable", "关闭"]),
        Subcommand("latest", alias=["now", "fetch", "最新"]),
    ),
    rule=to_me(),
    use_cmd_start=True,
)


@tibo.assign("$main")
async def handle_tibo_main(event: GroupMessageEvent) -> None:
    status = "开启" if is_group_tibo_monitor_enabled(event.group_id) else "关闭"
    await tibo.finish(
        f"本群 Tibo 推文监控：{status}\n"
        "可用命令：/tibo open、/tibo close、/tibo latest",
    )


@tibo.assign("open")
async def handle_tibo_open(event: GroupMessageEvent) -> None:
    set_group_tibo_monitor_enabled(event.group_id, True)
    await tibo.finish(
        "本群 Tibo 推文监控已开启，之后的新推文会自动发送英文原文和中文翻译",
    )


@tibo.assign("close")
async def handle_tibo_close(event: GroupMessageEvent) -> None:
    set_group_tibo_monitor_enabled(event.group_id, False)
    await tibo.finish("本群 Tibo 推文监控已关闭")


@tibo.assign("latest")
async def handle_tibo_latest(event: GroupMessageEvent, bot: Bot) -> None:
    await tibo.send("正在获取 Tibo 的最新推文并翻译...")

    try:
        posts = await fetch_tibo_posts()
        post = latest_included_post(posts)
    except Exception as exc:
        logger.exception(f"Failed to fetch latest Tibo post: {exc}")
        await tibo.finish("获取 Tibo 最新推文失败，请稍后再试")
        return

    if post is None:
        await tibo.finish("暂时没有获取到 Tibo 的原创推文")
        return

    try:
        translation = await translate_post(post)
    except Exception as exc:
        logger.exception(f"Failed to translate latest Tibo post {post.tweet_id}: {exc}")
        await tibo.finish("最新推文已获取，但翻译失败，请稍后再试")
        return

    if await send_group_message(
        event.group_id,
        format_post_message(post, translation),
        preferred_bot=bot,
    ):
        return

    await tibo.finish("Tibo 最新推文发送失败，请稍后再试")
