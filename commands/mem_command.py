from nonebot.rule import to_me
from arclet.alconna import Alconna, Args
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot_plugin_alconna import Match, Option, Subcommand, on_alconna
from ..mem.group_mem import get_group_mem_enabled, set_group_mem_enabled


mem = on_alconna(
    Alconna(
        "mem",
        Subcommand("open"),
        Subcommand("close"),
        Subcommand(
            "search",
            Option("-u|--user", Args["user", str]),
            Args["text?", str],
        ),
    ),
    rule=to_me(),
    use_cmd_start=True,
)


@mem.assign("$main")
async def handle_mem_main(event: GroupMessageEvent):
    await mem.finish(f"记忆是否开启: {get_group_mem_enabled(event.group_id)}")

@mem.assign("open")
async def handle_mem_open(event: GroupMessageEvent):
    set_group_mem_enabled(event.group_id, True)
    await mem.finish("记忆已开启")

@mem.assign("close")
async def handle_mem_close(event: GroupMessageEvent):
    set_group_mem_enabled(event.group_id, False)
    await mem.finish("记忆已关闭")