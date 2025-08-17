from typing import Annotated, Optional
from typing_extensions import TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent


class GroupState(TypedDict):
    # Messages have the type "list". The `add_messages` function
    # in the annotation defines how this state key should be updated
    # (in this case, it appends messages to the list, rather than overwriting them)
    messages: Annotated[list[AnyMessage], add_messages]
    memories: Optional[str]


class PrivateState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    memories: Optional[str]
