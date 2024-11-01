from langchain_core.prompts import PromptTemplate, ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, SystemMessage, trim_messages

from langchain_community.chat_models import ChatZhipuAI
from langchain_community.tools.tavily_search import TavilySearchResults

from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver

from .state import State
from .token_counter import trimmer


model = ChatZhipuAI(
    model="glm-4-plus"
)

search_tool = TavilySearchResults(max_results=2)
tools = [search_tool]
model_with_tools = model.bind_tools(tools)

graph_builder = StateGraph(State)

group_chat_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "你的名字是Sunny,尽你所能可爱、俏皮地回答所有问题. user(name,id)是与你聊天的用户的名字和QQ号,通常叫用户的名字即可,无需主动说出QQ号."
        ),
        MessagesPlaceholder(variable_name="messages")
    ]
)
group_chat_chain = group_chat_prompt | trimmer | model_with_tools

def chatbot(state: State):
    return {"messages": [group_chat_chain.invoke(state["messages"])]}


graph_builder.add_node("chatbot", chatbot)

tool_node = ToolNode(tools=tools)
graph_builder.add_node("tools", tool_node)

graph_builder.add_conditional_edges("chatbot", tools_condition)
graph_builder.add_edge("tools", "chatbot")
graph_builder.set_entry_point("chatbot")

memory_saver = MemorySaver()
graph = graph_builder.compile(checkpointer=memory_saver)
