import asyncio
import base64
import mimetypes
import urllib.request
from typing import Any
from langchain_core.globals import set_verbose, set_debug

from langchain_ollama import ChatOllama

from langchain_core.messages import HumanMessage, messages_to_dict
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate, ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableConfig

from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent, Bot, MessageSegment

from .graph import group_graph, private_graph
from .mem import get_memory, add_memory


set_verbose(True)
# set_debug(True)

model = ChatOllama(
    base_url="http://127.0.0.1:21434",
    model="gemma4:e4b",
    reasoning=True
)

parser = StrOutputParser()
chain = model | parser

thread_chat_user_prompt = PromptTemplate.from_template("user(name={name},qq={id}):{text}")
IMAGE_TOKEN_HINT = "[user sent an image]"
MessageEvent = GroupMessageEvent | PrivateMessageEvent


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


def _build_user_prompt(name: str, user_id: int, text: str) -> str:
    return thread_chat_user_prompt.invoke({"name": name, "id": user_id, "text": text}).to_string()


def _download_image_as_data_url(url: str, file_hint: str = "") -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "sunny-agent/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        image_bytes = response.read()
        content_type = response.headers.get_content_type()

    if not content_type or content_type == "application/octet-stream":
        content_type = mimetypes.guess_type(file_hint or url)[0] or "image/jpeg"

    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{content_type};base64,{encoded}"


async def _build_image_block(segment: MessageSegment, bot: Bot) -> dict[str, Any] | None:
    image_url = segment.data.get("url")
    image_file = segment.data.get("file", "")

    if not image_url and image_file:
        try:
            image_info = await bot.get_image(file=image_file)
        except Exception as exc:
            print(f"Failed to fetch image info for {image_file}: {exc}")
            return None
        image_url = image_info.get("url")

    if not image_url:
        print(f"Image segment missing url: {segment}")
        return None

    try:
        data_url = await asyncio.to_thread(_download_image_as_data_url, image_url, image_file)
    except Exception as exc:
        print(f"Failed to download image {image_url}: {exc}")
        return None

    return {"type": "image_url", "image_url": {"url": data_url}}


async def _build_human_message(event: MessageEvent, bot: Bot, user_name: str) -> HumanMessage:
    raw_text = event.message.__str__()
    has_image = any(segment.type == "image" for segment in event.message)

    if not has_image:
        return HumanMessage(content=_build_user_prompt(user_name, event.user_id, raw_text))

    content: list[dict[str, Any]] = [
        {"type": "text", "text": _build_user_prompt(user_name, event.user_id, IMAGE_TOKEN_HINT)}
    ]

    for segment in event.message:
        if segment.is_text():
            text = segment.data.get("text", "")
            if text:
                content.append({"type": "text", "text": text})
            continue

        if segment.type == "image":
            image_block = await _build_image_block(segment, bot)
            if image_block:
                content.append(image_block)
                continue

            content.append({"type": "text", "text": IMAGE_TOKEN_HINT})
            continue

        fallback_text = str(segment)
        if fallback_text:
            content.append({"type": "text", "text": fallback_text})

    return HumanMessage(content=content) # type: ignore

async def group_chat(event: GroupMessageEvent, bot: Bot, mem_enabled: bool) -> str:
    msg = event.message.__str__()
    print(msg)
    user_name = event.sender.card if event.sender.card else event.sender.nickname or "Unknown"
    config = RunnableConfig({
        "configurable": {
            "thread_id": str(event.group_id),
            "event": event,
            "bot": bot
        }
    })
    
    message = await _build_human_message(event, bot, user_name)
    input_data: dict[str, Any] = {"messages": [message], "memories": None}
    
    # if mem_enabled:
    #     memory = await get_memory()
    #     mem_user_id = f"u{event.sender.user_id}"
    #     mem_group_id = f"g{event.group_id}"
        
    #     relevant_user_memories = await memory.search(query=msg, user_id=mem_user_id, limit=5)
    #     relevant_group_memories = await memory.search(query=msg, user_id=mem_group_id, limit=5)
    #     all_memories = relevant_user_memories["results"] + relevant_group_memories["results"]
        
    #     memories_str = "\n".join(f"- {entry['memory']}" for entry in all_memories)
    #     input_data["memories"] = memories_str if memories_str.strip() else "暂无相关记忆信息"
    #     print(f"Group Chat Memories ({mem_user_id}): {memories_str}")
    
    output = await group_graph.ainvoke(input_data, config) # type: ignore
    output_messages = output["messages"]
    dict_messages = convert_messages_to_dict(output_messages)
    print(f"Output messages: {dict_messages}")
    
    # if mem_enabled:
    #     current_conversation = [
    #         {
    #             "role": "user",
    #             "content": msg
    #         },
    #         {
    #             "role": "assistant",
    #             "content": dict_messages[-1]["content"]
    #         }
    #     ]
    #     asyncio.create_task(add_memory(current_conversation, user_id=mem_user_id))
    #     print(f"Current conversation: {current_conversation}")
    
    return parser.invoke(output_messages[-1])

async def private_chat(event: PrivateMessageEvent, bot: Bot, mem_enabled: bool) -> str:
    msg = event.message.__str__()
    print(msg)
    config = RunnableConfig({
        "configurable": {
            "thread_id": str(event.user_id),
            "event": event,
            "bot": bot
        }
    })
    
    message = await _build_human_message(event, bot, event.sender.nickname or "Unknown")
    input_data: dict[str, Any] = {"messages": [message], "memories": None}
    
    # if mem_enabled:
    #     memory = await get_memory()
    #     mem_user_id = f"u{event.user_id}"
    #     relevant_memories = await memory.search(query=msg, user_id=mem_user_id, limit=5)
    #     memories_str = "\n".join(f"- {entry['memory']}" for entry in relevant_memories["results"])
    #     input_data["memories"] = memories_str if memories_str.strip() else "暂无相关记忆信息"
    #     print(f"Private Chat Memories ({mem_user_id}): {memories_str}")
    
    output = await private_graph.ainvoke(input_data, config) # type: ignore
    output_messages = output["messages"]
    dict_messages = convert_messages_to_dict(output_messages)
    print(f"Output messages: {dict_messages}")
    
    # if mem_enabled:
    #     current_conversation = [
    #         {
    #             "role": "user",
    #             "content": msg
    #         },
    #         {
    #             "role": "assistant",
    #             "content": dict_messages[-1]["content"]
    #         }
    #     ]
    #     asyncio.create_task(add_memory(current_conversation, user_id=mem_user_id))
    #     print(f"Current conversation: {current_conversation}")
    
    return parser.invoke(output_messages[-1])


translate_prompt_template = ChatPromptTemplate.from_messages(
    [("system", "Translate the following from Chinese into English"), ("user", "{text}")]
)
translate_chain = translate_prompt_template | chain

def translate(text: str) -> str:
    return translate_chain.invoke({"text": text})
