import os
from collections.abc import MutableMapping
from typing import Any

from agents import (
    Agent,
    CodeInterpreterTool,
    ImageGenerationTool,
    ModelSettings,
    OpenAIProvider,
    RunConfig,
    Runner,
    SQLiteSession,
    WebSearchTool,
    set_tracing_disabled,
)
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent

from . import tool


MODEL_NAME = os.getenv("SUNNY_AGENT_MODEL", "gpt-5.5")
MODEL_BASE_URL = os.getenv("SUNNY_AGENT_OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL")
MODEL_API_KEY = os.getenv("SUNNY_AGENT_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
MAX_TURNS = int(os.getenv("SUNNY_AGENT_MAX_TURNS", "8"))

set_tracing_disabled(disabled=os.getenv("SUNNY_AGENT_ENABLE_TRACING", "").lower() not in {"1", "true", "yes"})

model_provider = OpenAIProvider(api_key=MODEL_API_KEY, base_url=MODEL_BASE_URL, use_responses=True)

chat_instructions = (
    "你是 Sunny，输入里的 user(name,qq) 表示正在和你聊天的用户姓名和 QQ 号。"
    "通常称呼用户姓名即可，不需要主动说出 QQ 号。"
)

model_settings = ModelSettings()
hosted_tools = [
    WebSearchTool(),
    CodeInterpreterTool(
        tool_config={"type": "code_interpreter", "container": {"type": "auto"}}
    ),
    ImageGenerationTool(tool_config={"type": "image_generation"}),
]

group_agent = Agent[tool.ChatContext](
    name="Sunny Group Agent",
    instructions=chat_instructions,
    model=MODEL_NAME,
    model_settings=model_settings,
    tools=[
        *hosted_tools,
        tool.group_name,
        tool.group_member_list,
        tool.send_private_message,
    ],
)

private_agent = Agent[tool.ChatContext](
    name="Sunny Private Agent",
    instructions=chat_instructions,
    model=MODEL_NAME,
    model_settings=model_settings,
    tools=hosted_tools,
)

translator_agent = Agent(
    name="Sunny Translator",
    instructions="Translate the user's Chinese text into English. Return only the translation.",
    model=MODEL_NAME,
    model_settings=model_settings,
)

group_sessions: dict[str, SQLiteSession] = {}
private_sessions: dict[str, SQLiteSession] = {}


def _get_session(sessions: MutableMapping[str, SQLiteSession], session_id: str) -> SQLiteSession:
    session = sessions.get(session_id)
    if session is None:
        session = SQLiteSession(session_id)
        sessions[session_id] = session
    return session


async def _clear_session(sessions: MutableMapping[str, SQLiteSession], session_id: str) -> None:
    session = sessions.pop(session_id, None)
    if session is not None:
        await session.clear_session()


async def _run_agent(
    agent: Agent[tool.ChatContext],
    session: SQLiteSession,
    session_id: str,
    event: GroupMessageEvent | PrivateMessageEvent,
    bot: Bot,
    input_items: str | list[dict[str, Any]],
    workflow_name: str,
) -> str:
    result = await Runner.run(
        agent,
        input_items,  # type: ignore[arg-type]
        context=tool.ChatContext(bot=bot, event=event),
        session=session,
        max_turns=MAX_TURNS,
        run_config=RunConfig(
            model_provider=model_provider,
            workflow_name=workflow_name,
            group_id=session_id,
        ),
    )
    return str(result.final_output or "")


async def run_group_chat(
    event: GroupMessageEvent,
    bot: Bot,
    input_items: str | list[dict[str, Any]],
) -> str:
    session_id = f"group:{event.group_id}"
    return await _run_agent(
        group_agent,
        _get_session(group_sessions, session_id),
        session_id,
        event,
        bot,
        input_items,
        "Sunny group chat",
    )


async def run_private_chat(
    event: PrivateMessageEvent,
    bot: Bot,
    input_items: str | list[dict[str, Any]],
) -> str:
    session_id = f"private:{event.user_id}"
    return await _run_agent(
        private_agent,
        _get_session(private_sessions, session_id),
        session_id,
        event,
        bot,
        input_items,
        "Sunny private chat",
    )


async def clear_group_history(group_id: int) -> None:
    await _clear_session(group_sessions, f"group:{group_id}")


async def clear_private_history(user_id: int) -> None:
    await _clear_session(private_sessions, f"private:{user_id}")
