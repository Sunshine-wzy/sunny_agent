"""Transport-independent conversation and context values."""

import json
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class Scope:
    bot_id: str
    chat_type: Literal["group", "private"]
    peer_id: str
    adapter: str = "onebot.v11"

    def __post_init__(self) -> None:
        if self.chat_type not in {"group", "private"}:
            raise ValueError("chat_type must be group or private")
        object.__setattr__(self, "bot_id", str(self.bot_id))
        object.__setattr__(self, "peer_id", str(self.peer_id))

    @property
    def key(self) -> str:
        return json.dumps(
            [self.adapter, str(self.bot_id), self.chat_type, str(self.peer_id)],
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class ConversationRef:
    scope: Scope
    conversation_id: str
    short_id: str
    title: str


@dataclass(frozen=True, slots=True)
class ContextContent:
    blocks: list[dict[str, Any]]

    @classmethod
    def text(cls, value: str) -> "ContextContent":
        return cls([{"type": "input_text", "text": value}])

    def plain_text(self) -> str:
        return "\n".join(
            str(block.get("text", "[图片或附件]")) for block in self.blocks
        )


@dataclass(frozen=True, slots=True)
class SourceInfo:
    kind: str
    source_id: str
    title: str = ""
    url: str = ""
    published: str = ""
    turn_id: str = ""


@dataclass(frozen=True, slots=True)
class EntryRef:
    conversation: ConversationRef
    entry_id: str


@dataclass(frozen=True, slots=True)
class MessageBinding:
    conversation: ConversationRef
    message_id: str
    content: ContextContent
    sender_name: str
    sender_id: str
    entry_ids: list[str]


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    delivery_id: str
    status: str
    message_id: str | None = None
    error: str = ""

    def __bool__(self) -> bool:
        return self.status == "confirmed"


@dataclass(slots=True)
class TurnContext:
    conversation: ConversationRef
    turn_id: str
    session: Any
    input_items: list[dict[str, Any]]
    reference: MessageBinding | None = None
    notice: str = ""
    entry_ids: list[str] = field(default_factory=list)


class ContextError(ValueError):
    """An invalid context operation that can be explained to the user."""
