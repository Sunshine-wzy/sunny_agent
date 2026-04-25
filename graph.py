import asyncio

from langchain_core.prompts import PromptTemplate, ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, SystemMessage, trim_messages

from langchain_ollama import ChatOllama

from langchain_community.chat_models import QianfanChatEndpoint
from langchain_community.tools.tavily_search import TavilySearchResults

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver

from .state import GroupState, PrivateState
from .token_counter import trimmer
from . import tool


model = ChatOllama(
    base_url="http://127.0.0.1:21434",
    model="gemma4:e4b",
    reasoning=True
)

search_tool = TavilySearchResults(max_results=2)
common_tools = [search_tool]

group_tools = common_tools + [
    tool.group_name,
    tool.group_member_list,
    tool.send_private_message,
]
private_tools = common_tools

model_with_group_tools = model.bind_tools(group_tools)
model_with_private_tools = model.bind_tools(private_tools)

group_graph_builder = StateGraph(GroupState)
private_graph_builder = StateGraph(PrivateState)

chat_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "user(name,qq)是与你聊天的用户的名字和QQ号,通常叫用户的名字即可,无需主动说出QQ号."
        ),
        MessagesPlaceholder(variable_name="messages")
    ]
)
group_chat_chain = chat_prompt | trimmer | model_with_group_tools
private_chat_chain = chat_prompt | trimmer | model_with_private_tools

def group_chatbot(state: GroupState):
    return {"messages": [group_chat_chain.invoke(state["messages"])]}

def private_chatbot(state: PrivateState):
    return {"messages": [private_chat_chain.invoke(state["messages"])]}


group_graph_builder.add_node("chatbot", group_chatbot)
group_tool_node = ToolNode(tools=group_tools)
group_graph_builder.add_node("tools", group_tool_node)
group_graph_builder.add_conditional_edges("chatbot", tools_condition)
group_graph_builder.add_edge("tools", "chatbot")
group_graph_builder.set_entry_point("chatbot")

private_graph_builder.add_node("chatbot", private_chatbot)
private_tool_node = ToolNode(tools=private_tools)
private_graph_builder.add_node("tools", private_tool_node)
private_graph_builder.add_conditional_edges("chatbot", tools_condition)
private_graph_builder.add_edge("tools", "chatbot")
private_graph_builder.set_entry_point("chatbot")

group_memory_saver = MemorySaver()
group_graph = group_graph_builder.compile(
    checkpointer=group_memory_saver
)

private_memory_saver = MemorySaver()
private_graph = private_graph_builder.compile(
    checkpointer=private_memory_saver
)


async def _delete_thread(memory_saver: MemorySaver, thread_id: str | int) -> None:
    if hasattr(memory_saver, "adelete_thread"):
        await memory_saver.adelete_thread(thread_id)
        return

    await asyncio.to_thread(memory_saver.delete_thread, thread_id)


async def _clear_thread_history(memory_saver: MemorySaver, thread_id: str | int) -> None:
    candidates = [thread_id]
    thread_id_str = str(thread_id)
    if thread_id_str != thread_id:
        candidates.append(thread_id_str)

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            await _delete_thread(memory_saver, candidate)
        except Exception as exc:
            last_error = exc

    if last_error is not None:
        raise last_error


async def clear_group_history(group_id: int) -> None:
    await _clear_thread_history(group_memory_saver, group_id)


async def clear_private_history(user_id: int) -> None:
    await _clear_thread_history(private_memory_saver, user_id)
