from langchain.globals import set_verbose, set_debug

from langchain_community.chat_models import ChatZhipuAI

from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate, ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableConfig

from .graph import graph


set_verbose(True)
# set_debug(True)

model = ChatZhipuAI(
    model="glm-4-plus"
)

parser = StrOutputParser()
chain = model | parser

thread_chat_user_prompt = PromptTemplate.from_template("user(name={name},id={id}):{text}")


async def thread_chat(thread_id: str, text: str, user_id: int, user_name: str | None) -> str:
    config = RunnableConfig({"configurable": {"thread_id": thread_id}})
    message = HumanMessage(content=thread_chat_user_prompt.invoke(
        {"name": user_name, "id": user_id, "text": text}
    ).to_string())
    output = await graph.ainvoke({"messages": [message]}, config)
    print("Output messages: " + output.__str__())
    return parser.invoke(output["messages"][-1])

async def group_chat(group_id: int, text: str, user_id: int, user_name: str | None) -> str:
    return await thread_chat(f"g{group_id}", text, user_id, user_name)

async def user_chat(text: str, user_id: int, user_name: str | None) -> str:
    return await thread_chat(f"u{user_id}", text, user_id, user_name)


translate_prompt_template = ChatPromptTemplate.from_messages(
    [("system", "Translate the following from Chinese into English"), ("user", "{text}")]
)
translate_chain = translate_prompt_template | chain

def translate(text: str) -> str:
    return translate_chain.invoke({"text": text})
