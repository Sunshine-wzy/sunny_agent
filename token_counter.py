from typing import Any, List
from langchain_core.messages import BaseMessage, ToolMessage, HumanMessage, AIMessage, SystemMessage, trim_messages

import tiktoken


def str_token_counter(text: str) -> int:
    enc = tiktoken.get_encoding("o200k_base")
    return len(enc.encode(text))


def message_content_token_counter(content: str | list[Any]) -> int:
    if isinstance(content, str):
        return str_token_counter(content)

    num_tokens = 0
    for block in content:
        if isinstance(block, str):
            num_tokens += str_token_counter(block)
            continue

        if not isinstance(block, dict):
            num_tokens += str_token_counter(str(block))
            continue

        if block.get("type") == "text":
            num_tokens += str_token_counter(block.get("text", ""))
            continue

        if block.get("type") == "image_url":
            # Image token cost is model-specific; use a small fixed estimate so
            # multimodal messages can still participate in history trimming.
            num_tokens += 256
            continue

        num_tokens += str_token_counter(str(block))

    return num_tokens


def tiktoken_counter(messages: List[BaseMessage]) -> int:
    """Approximately reproduce https://github.com/openai/openai-cookbook/blob/main/examples/How_to_count_tokens_with_tiktoken.ipynb

    For simplicity only supports str Message.contents.
    """
    num_tokens = 3  # every reply is primed with <|start|>assistant<|message|>
    tokens_per_message = 3
    tokens_per_name = 1
    for msg in messages:
        if isinstance(msg, HumanMessage):
            role = "human"
        elif isinstance(msg, AIMessage):
            role = "assistant"
        elif isinstance(msg, ToolMessage):
            role = "tool"
        elif isinstance(msg, SystemMessage):
            role = "system"
        else:
            raise ValueError(f"Unsupported messages type {msg.__class__}")
        num_tokens += (
            tokens_per_message
            + str_token_counter(role)
            + message_content_token_counter(msg.content) # type: ignore
        )
        if msg.name:
            num_tokens += tokens_per_name + str_token_counter(msg.name)
    print(num_tokens)
    return num_tokens


trimmer = trim_messages(
    max_tokens=100000,
    strategy="last",
    token_counter=tiktoken_counter,
    include_system=True,
    allow_partial=False,
    start_on="human"
)
