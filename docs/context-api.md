# 会话管理：使用与接入

## 聊天命令

群聊继续沿用原有的机器人触发条件（@、昵称、回复机器人或主动接收开启），并新增「引用本群已登记的会话消息」触发。引用先前参与会话的用户消息也不需要额外 @。

| 操作 | 结果 |
| --- | --- |
| `/new`、`/clear`、群内戳机器人 | 新建并切换会话；旧历史保留 |
| 引用已登记消息并提问 | 切换到消息所在会话的最新进度 |
| 只引用已登记消息、不填写正文 | 切换并确认，不调用模型 |
| 引用消息并发送 `/quote 问题` | 在当前会话讨论引用内容，不切换、不合并源会话 |
| `/session current` | 查看当前会话编号 |
| `/session list` | 查看本聊天最近 10 个会话 |
| `/session use ABC123` | 切换到指定编号，可带 `#` |
| 引用消息并发送 `/session use` | 仅切换到其会话 |

群成员共享当前会话。不同 Bot、群和私聊之间隔离。没有登记的历史消息不能恢复所属会话；仅在底层提供的信息能验证本聊天来源时，才作为引用资料使用。跨会话引用只引入选中消息的内容快照，原消息归属保持不变。

## 添加非消息资料

以下示例适合从 sunny_agent 内的其他模块调用。`Scope` 的 ID 使用字符串；构造器也会将数值 ID 规范化。数据库在首次使用时由 localstore 创建。

```python
from .context import ContextContent, Scope, SourceInfo, get_manager

manager = get_manager()
scope = Scope(bot_id=str(bot.self_id), chat_type="group", peer_id=str(group_id))
conversation = await manager.get_active_session(scope)

entry = await manager.append_context(
    conversation,
    content=ContextContent.text("项目进度：原型完成，正在进行集成测试。"),
    source=SourceInfo(kind="project", source_id="project-7", title="项目进度"),
    idempotency_key="project-7:revision-12",
)
```

这不会发消息、调用模型或修改当前会话指针。会话里下一轮聊天会读取这份资料。没有当前会话时，`get_active_session` 会创建一个；要向特定会话写入，保留其 `ConversationRef` 并直接传入。

`ContextContent.blocks` 接受 SDK 输入形式的 `input_text`、`input_image`、`input_file` 内容块。结构化数据可以先 JSON 序列化，再使用 `ContextContent.text()`。外部资料以带来源的数据投影到模型输入，不作为系统指令。`kind="assistant_publication"` 只用于机器人主动撰写的正文或点评。

每个会话内幂等键唯一：相同内容重试返回原条目，不重复入库；相同键对应不同内容会报 `ContextError`。资料更新使用新版本键。

## 创建独立会话并发送入口消息

```python
from .context import ContextContent, SourceInfo
from .messaging import send_in_session

conversation = await manager.create_session(
    scope,
    activate=False,
    title="项目周报",
    source_key="project-7:weekly:2026-W39",
    idempotency_key="weekly-session:project-7:2026-W39",
)
entry = await manager.append_context(
    conversation,
    content=ContextContent.text(full_report),
    source=SourceInfo("weekly-report", "project-7:2026-W39"),
    idempotency_key="weekly-content:project-7:2026-W39:v1",
)
delivery = await send_in_session(
    bot,
    conversation,
    "本周项目周报已更新，可以引用这条消息继续讨论。",
    entries=[entry],
    idempotency_key="weekly-send:project-7:2026-W39:v1",
)
```

`activate=False` 保持正在聊的会话。用户引用这条消息后才切换到周报会话，能够读取已保存的完整正文。普通 `/quote` 只引用该入口消息实际展示的内容，不授权读取源会话全文。

`send_in_session` 保存待发送内容，调用 OneBot，并将真实的 `message_id` 绑定到明确的会话。最终模型回复已经由 Runner 写入历史，发送时只关联该轮条目，避免重复追加 assistant 内容。

已有其他代码负责发送时，可只补登记：

```python
await manager.bind_message(
    conversation,
    message_id=receipt["message_id"],
    visible_content=ContextContent.text(sent_text),
    entries=[entry],
)
```

这里要求回执来自同一 Bot 和同一聊天，`visible_content` 必须是实际显示内容。优先使用 `send_in_session`，由它校验 Bot、保存投递状态并处理登记。

## 早报和其他主动推送

`publication.py::Publication` 封装独立会话、来源资料、发布批次与投递分片。RSS 定时推送和 `/rss today` 共用这条路径：

- 完整报告保存为 external 条目；直发摘要、每批合并转发和点评都登记到报告集合的会话。
- 用户引用转发外层消息或点评即可切换；不为转发内部自定义节点伪造普通消息 ID。
- 定时批次使用稳定键，重启后重试跳过已确认的分片；手动重发采用新批次，正文保持幂等。
- 明确发送失败可尝试备用 Bot；回执绑定到实际 Bot 的 Scope。网络超时、丢失回执等不确定情况停止重发和 Bot 切换。
- Tibo 推送、图片生成和私信工具也使用会话登记。私信工具持久化目标私聊中的独立通知会话后异步发送，向模型如实返回 queued；不复制源群历史。

`DeliveryResult.status` 为 `confirmed`、`failed`、`unknown` 等状态；`bool(result)` 仅在 confirmed 时为真。confirmed 但 `message_id is None` 表示平台确认发送却未返回可引用的普通消息 ID。

相同发送键可以重试明确失败的投递；未知状态不会重发。重连时只恢复尚未开始发送的 pending 任务，不重放发送中的 unknown 任务。单个群的对话与会话切换按顺序执行，不同群可以并行。

## 存储和配置

数据文件：`nonebot_plugin_localstore.get_data_file("sunny_agent", "context.sqlite3")`。会话、当前指针、消息归属、历史正文和投递记录都在同一 SQLite 文件中，使用 WAL。备份运行中的数据库应使用 SQLite 备份功能；停机后可复制数据库及尚未合并的 WAL 文件。

| 配置字段 | 默认值 | 用途 |
| --- | --- | --- |
| `sunny_agent_context_max_input_tokens` | 24000 | 本轮输入与选入历史的保守估算预算 |
| `sunny_agent_context_recent_turns` | 20 | 最近完整对话轮数上限 |
| `sunny_agent_context_entry_max_chars` | 12000 | 外部资料/引用的默认展示长度 |
| `sunny_agent_context_turn_timeout_seconds` | 300 | 输入构建和模型运行超时 |
| `sunny_agent_context_send_timeout_seconds` | 60 | 单次发送超时 |

预算使用文本 UTF-8 长度和图片额度作保守估算，并非模型专用 tokenizer；不包含模型运行中后来产生的所有工具输出。历史按完整轮次选择，工具调用与结果不会被单独裁掉。长资料原文仍保存，可通过只读 `read_context` 工具分段读取。现有 `SUNNY_AGENT_MAX_TURNS` 仍用于控制单次 Runner 执行步数。

第一版支持单进程，不自动删除旧会话，也不修改 `/mem` 知识库。旧实现的历史只存在于进程内存中，首次重启上线无法自动迁移；上线前的消息没有归属索引，不能自动恢复原会话。

旧 RSS/Tibo 的网络错误重发设置不再用于未知投递状态，避免重复发送。明确失败后由后续推送重试或备用 Bot 接续。

## 验证

在已安装项目依赖的环境中运行：

```powershell
python -m unittest discover -s tests -v
python -m ruff check --select E9,F,I context messaging.py publication.py conversation_chat.py event.py graph.py chat.py tool.py rss_daily.py tibo_monitor.py tests
```

测试使用临时 SQLite、模拟 OneBot 回执和假模型；包含真实 Agents SDK Runner 与 Session/工具调用协议的验证，不访问模型服务，不向真实聊天发消息。仓库父目录的 Pyright 配置仍为 Python 3.9，检查此插件时需通过 `--pythonversion 3.13 --pythonpath <项目虚拟环境中的 python>` 选择本地实际运行环境。
