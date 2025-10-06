from typing import List
from langchain_core.messages import BaseMessage, ToolMessage, HumanMessage, AIMessage, SystemMessage, trim_messages

import tiktoken


def str_token_counter(text: str) -> int:
    enc = tiktoken.get_encoding("o200k_base")
    return len(enc.encode(text))


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
            + str_token_counter(msg.content) # type: ignore
        )
        if msg.name:
            num_tokens += tokens_per_name + str_token_counter(msg.name)
    print(num_tokens)
    return num_tokens


trimmer = trim_messages(
    max_tokens=3000,
    strategy="last",
    token_counter=tiktoken_counter,
    include_system=True,
    allow_partial=False,
    start_on="human"
)
