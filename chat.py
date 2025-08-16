from langchain.globals import set_verbose, set_debug

from langchain_community.chat_models import QianfanChatEndpoint

from langchain_core.messages import HumanMessage, messages_to_dict
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate, ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableConfig

from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent, Bot

from .graph import group_graph, private_graph
from .mem import memory


set_verbose(True)
# set_debug(True)

model = QianfanChatEndpoint(model="qwen3-235b-a22b", timeout=3000)

parser = StrOutputParser()
chain = model | parser

thread_chat_user_prompt = PromptTemplate.from_template("user(name={name},id={id}):{text}")
thread_chat_with_mem_user_prompt = PromptTemplate.from_template("""# User Memories
{memories}
# user(name={name},id={id}):
{text}
""")

role_mapping = {
    'human': 'user',
    'ai': 'assistant',
    'system': 'system'
}


def convert_messages_to_dict(messages):
    # 转换为字典格式
    dict_messages = messages_to_dict(messages)
    
    # 转换为标准聊天格式
    chat_format = []
    for msg in dict_messages:
        chat_format.append({
            "role": role_mapping.get(msg['type'], 'user'),
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
    
    if mem_enabled:
        mem_user_id = f"u{event.sender.user_id}"
        relevant_memories = memory.search(query=msg, user_id=mem_user_id, limit=3)
        memories_str = "\n".join(f"- {entry['memory']}" for entry in relevant_memories["results"])
        message = HumanMessage(content=thread_chat_with_mem_user_prompt.invoke(
            {"name": user_name, "id": event.user_id, "text": msg, "memories": memories_str}
        ).to_string())
    else:
        message = HumanMessage(content=thread_chat_user_prompt.invoke(
            {"name": user_name, "id": event.user_id, "text": msg}
        ).to_string())
    
    output = await group_graph.ainvoke({
        "messages": [message]
    }, config)
    output_messages = output["messages"]
    dict_messages = convert_messages_to_dict(output_messages)
    print(f"Output messages: {dict_messages}")
    
    if mem_enabled:
        memory.add(dict_messages, user_id=mem_user_id)
    
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
    
    if mem_enabled:
        mem_user_id = f"u{event.user_id}"
        relevant_memories = memory.search(query=msg, user_id=mem_user_id, limit=3)
        memories_str = "\n".join(f"- {entry['memory']}" for entry in relevant_memories["results"])
        message = HumanMessage(content=thread_chat_with_mem_user_prompt.invoke(
            {"name": event.sender.nickname, "id": event.user_id, "text": msg, "memories": memories_str}
        ).to_string())
    else:
        message = HumanMessage(content=thread_chat_user_prompt.invoke(
            {"name": event.sender.nickname, "id": event.user_id, "text": msg}
        ).to_string())
    
    output = await private_graph.ainvoke({
        "messages": [message]
    }, config)
    output_messages = output["messages"]
    dict_messages = convert_messages_to_dict(output_messages)
    print(f"Output messages: {dict_messages}")
    
    if mem_enabled:
        memory.add(dict_messages, user_id=mem_user_id)
    
    return parser.invoke(output_messages[-1])


translate_prompt_template = ChatPromptTemplate.from_messages(
    [("system", "Translate the following from Chinese into English"), ("user", "{text}")]
)
translate_chain = translate_prompt_template | chain

def translate(text: str) -> str:
    return translate_chain.invoke({"text": text})
