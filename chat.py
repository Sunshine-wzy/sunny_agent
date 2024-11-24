from langchain.globals import set_verbose, set_debug

from langchain_community.chat_models import ChatZhipuAI

from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate, ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableConfig

from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent, Bot

from .graph import group_graph, private_graph


set_verbose(True)
# set_debug(True)

model = ChatZhipuAI(
    model="glm-4-air"
)

parser = StrOutputParser()
chain = model | parser

thread_chat_user_prompt = PromptTemplate.from_template("user(name={name},id={id}):{text}")


async def group_chat(event: GroupMessageEvent, bot: Bot) -> str:
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
    output = await group_graph.ainvoke({
        "messages": [message]
    }, config)
    output_messages = output["messages"]
    print("Output messages: " + output_messages.__str__())
    return parser.invoke(output_messages[-1])

async def private_chat(event: PrivateMessageEvent, bot: Bot) -> str:
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
    output = await private_graph.ainvoke({
        "messages": [message]
    }, config)
    output_messages = output["messages"]
    print("Output messages: " + output_messages.__str__())
    return parser.invoke(output_messages[-1])


translate_prompt_template = ChatPromptTemplate.from_messages(
    [("system", "Translate the following from Chinese into English"), ("user", "{text}")]
)
translate_chain = translate_prompt_template | chain

def translate(text: str) -> str:
    return translate_chain.invoke({"text": text})
