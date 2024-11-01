from langchain_core.prompts import PromptTemplate, ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, SystemMessage, trim_messages

from langchain_community.chat_models import ChatZhipuAI
from langchain_community.tools.tavily_search import TavilySearchResults

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver

from .state import GroupState, PrivateState
from .token_counter import trimmer
from . import tool


model = ChatZhipuAI(
    model="glm-4-plus"
)

search_tool = TavilySearchResults(max_results=2)
common_tools = [search_tool]

group_tools = common_tools + [
    tool.group_name,
    tool.group_member_list,
    tool.send_private_message
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
            "你的名字是Sunny,尽你所能可爱、俏皮地回答所有问题. user(name,id)是与你聊天的用户的名字和QQ号,通常叫用户的名字即可,无需主动说出QQ号."
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
