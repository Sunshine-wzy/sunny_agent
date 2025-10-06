from nonebot.rule import to_me
from arclet.alconna import Alconna, Args
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot_plugin_alconna import Match, Option, Subcommand, on_alconna

from ..mem.group_mem import is_group_mem_enabled, set_group_mem_enabled
from ..mem.knowledge_base import (
    add_knowledge_to_group,
    remove_knowledge_from_group,
    get_group_knowledge_list
)


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
        Subcommand(
            "knowledgebase",
            Subcommand(
                "add",
                Option("-n|--name", Args["name", str]),
                Args["text", str],
            ),
            Subcommand(
                "remove",
                Option("-n|--name", Args["name", str]),
                Args["index?", int],
            ),
            Subcommand(
                "list",
            ),
            alias=["kb"],
        ),
    ),
    rule=to_me(),
    use_cmd_start=True,
)


@mem.assign("$main")
async def handle_mem_main(event: GroupMessageEvent):
    await mem.finish(f"记忆是否开启: {is_group_mem_enabled(event.group_id)}")

@mem.assign("open")
async def handle_mem_open(event: GroupMessageEvent):
    set_group_mem_enabled(event.group_id, True)
    await mem.finish("记忆已开启")

@mem.assign("close")
async def handle_mem_close(event: GroupMessageEvent):
    set_group_mem_enabled(event.group_id, False)
    await mem.finish("记忆已关闭")


@mem.assign("knowledgebase.add")
async def handle_kb_add(
    event: GroupMessageEvent,
    name: Match[str],
    text: Match[str]
):
    if not text.available:
        await mem.finish("请提供要添加的文本内容")
        return
    
    # 如果没有提供name参数，使用文本的前20个字符作为名称
    kb_name = name.result if name.available else text.result[:20]
    
    success = await add_knowledge_to_group(event.group_id, kb_name, text.result)
    if success:
        await mem.finish(f"知识库条目已添加: {kb_name}")
    else:
        await mem.finish("添加失败，可能是名称重复")

@mem.assign("knowledgebase.list")
async def handle_kb_list(event: GroupMessageEvent):
    kb_list = get_group_knowledge_list(event.group_id)
    if not kb_list:
        await mem.finish("本群暂无知识库条目")
        return
    
    # 格式化列表显示
    formatted_list = []
    for i, name in enumerate(kb_list, 1):
        formatted_list.append(f"{i}: {name}")
    
    result = "本群知识库列表:\n" + "\n".join(formatted_list)
    await mem.finish(result)

@mem.assign("knowledgebase.remove")
async def handle_kb_remove(
    event: GroupMessageEvent,
    name: Match[str],
    index: Match[int]
):
    if not name.available and not index.available:
        await mem.finish("请提供要删除的知识库名称(-n)或序号")
        return
    
    # 优先使用序号删除
    if index.available:
        success = await remove_knowledge_from_group(event.group_id, index=index.result)
        if success:
            await mem.finish(f"已删除序号为 {index.result} 的知识库条目")
        else:
            await mem.finish("删除失败，请检查序号是否正确")
    # 使用名称删除
    elif name.available:
        success = await remove_knowledge_from_group(event.group_id, name=name.result)
        if success:
            await mem.finish(f"已删除知识库条目: {name.result}")
        else:
            await mem.finish("删除失败，请检查名称是否存在")