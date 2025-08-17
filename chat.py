import asyncio
from typing import Any
from langchain.globals import set_verbose, set_debug

from langchain_community.chat_models import QianfanChatEndpoint

from langchain_core.messages import HumanMessage, messages_to_dict
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate, ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableConfig

from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent, Bot

from .graph import group_graph, private_graph
from .mem import get_memory, add_memory


set_verbose(True)
# set_debug(True)

model = QianfanChatEndpoint(model="qwen3-235b-a22b", timeout=3000)

parser = StrOutputParser()
chain = model | parser

thread_chat_user_prompt = PromptTemplate.from_template("user(name={name},qq={id}):{text}")


def convert_messages_to_dict(messages):
    # 转换为字典格式
    dict_messages = messages_to_dict(messages)
    
    # 转换为标准聊天格式
    chat_format = []
    for msg in dict_messages:
        chat_format.append({
            "role": msg['type'],
            "content": str(msg['data']['content'])
        })
    
    return chat_format

async def group_chat(event: GroupMessageEvent, bot: Bot, mem_enabled: bool) -> str:
    msg = event.message.__str__()
    print(msg)
    user_name = event.sender.card if event.sender.card else event.sender.nickname
    config = RunnableConfig({
        "configurable": {
            "thread_id": event.group_id,
            "event": event,
            "bot": bot
        }
    })
    
    message = HumanMessage(content=thread_chat_user_prompt.invoke(
        {"name": user_name, "id": event.user_id, "text": msg}
    ).to_string())
    input_data: dict[str, Any] = {"messages": [message], "memories": None}
    
    if mem_enabled:
        memory = await get_memory()
        mem_user_id = f"u{event.sender.user_id}"
        mem_group_id = f"g{event.group_id}"
        
        relevant_user_memories = await memory.search(query=msg, user_id=mem_user_id, limit=5)
        relevant_group_memories = await memory.search(query=msg, user_id=mem_group_id, limit=5)
        all_memories = relevant_user_memories["results"] + relevant_group_memories["results"]
        
        memories_str = "\n".join(f"- {entry['memory']}" for entry in all_memories)
        input_data["memories"] = memories_str if memories_str.strip() else "暂无相关记忆信息"
        print(f"Group Chat Memories ({mem_user_id}): {memories_str}")
    
    output = await group_graph.ainvoke(input_data, config)
    output_messages = output["messages"]
    dict_messages = convert_messages_to_dict(output_messages)
    print(f"Output messages: {dict_messages}")
    
    if mem_enabled:
        current_conversation = [
            {
                "role": "user",
                "content": msg
            },
            {
                "role": "assistant",
                "content": dict_messages[-1]["content"]
            }
        ]
        asyncio.create_task(add_memory(current_conversation, user_id=mem_user_id))
        print(f"Current conversation: {current_conversation}")
    
    return parser.invoke(output_messages[-1])

async def private_chat(event: PrivateMessageEvent, bot: Bot, mem_enabled: bool) -> str:
    msg = event.message.__str__()
    print(msg)
    config = RunnableConfig({
        "configurable": {
            "thread_id": event.user_id,
            "event": event,
            "bot": bot
        }
    })
    
    message = HumanMessage(content=thread_chat_user_prompt.invoke(
        {"name": event.sender.nickname, "id": event.user_id, "text": msg}
    ).to_string())
    input_data: dict[str, Any] = {"messages": [message], "memories": None}
    
    if mem_enabled:
        memory = await get_memory()
        mem_user_id = f"u{event.user_id}"
        relevant_memories = await memory.search(query=msg, user_id=mem_user_id, limit=5)
        memories_str = "\n".join(f"- {entry['memory']}" for entry in relevant_memories["results"])
        input_data["memories"] = memories_str if memories_str.strip() else "暂无相关记忆信息"
        print(f"Private Chat Memories ({mem_user_id}): {memories_str}")
    
    output = await private_graph.ainvoke(input_data, config)
    output_messages = output["messages"]
    dict_messages = convert_messages_to_dict(output_messages)
    print(f"Output messages: {dict_messages}")
    
    if mem_enabled:
        current_conversation = [
            {
                "role": "user",
                "content": msg
            },
            {
                "role": "assistant",
                "content": dict_messages[-1]["content"]
            }
        ]
        asyncio.create_task(add_memory(current_conversation, user_id=mem_user_id))
        print(f"Current conversation: {current_conversation}")
    
    return parser.invoke(output_messages[-1])


translate_prompt_template = ChatPromptTemplate.from_messages(
    [("system", "Translate the following from Chinese into English"), ("user", "{text}")]
)
translate_chain = translate_prompt_template | chain

def translate(text: str) -> str:
    return translate_chain.invoke({"text": text})
