from langchain.globals import set_verbose, set_debug
from langchain_community.chat_models import ChatZhipuAI
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate, ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.prebuilt import create_react_agent

from .token_counter import trimmer
from .graph import graph


set_verbose(True)
# set_debug(True)

model = ChatZhipuAI(
    model="glm-4-plus"
)

parser = StrOutputParser()
chain = model | parser

# group_chat
thread_chat_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "你的名字是Sunny,尽你所能可爱、俏皮地回答所有问题. user(name,id)是与你聊天的用户的名字和QQ号,通常叫用户的名字即可,无需主动说出QQ号."
        ),
        MessagesPlaceholder(variable_name="messages")
    ]
)
thread_chat_prompt_timmer_chain = thread_chat_prompt | trimmer
thread_chat_chain = thread_chat_prompt_timmer_chain | model
thread_chat_user_prompt = PromptTemplate.from_template("user(name={name},id={id}):{text}")

search = TavilySearchResults(max_results=2)
tools = [search]

agent_executor = create_react_agent(model, tools)

workflow = StateGraph(state_schema=MessagesState)

async def call_model(state: MessagesState):
    response = await thread_chat_chain.ainvoke(state)
    return {"messages": response}
    
    # messages = group_chat_prompt_timmer_chain.invoke(state)
    # print("State messages: " + messages.__str__())
    # agent_response = await agent_executor.ainvoke({"messages": messages})
    # print("Agent response: " + agent_response.__str__())
    # return agent_response
    
workflow.add_edge(START, "model")
workflow.add_node("model", call_model)

memory = MemorySaver()
app = workflow.compile(checkpointer=memory)
# app = workflow.compile()


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
