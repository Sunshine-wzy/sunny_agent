"""Text-only, bounded naming input and display-safe conversation titles."""

import re
import unicodedata
from typing import Any

TITLE_INPUT_MAX_CHARS = 6000
TITLE_MAX_CHARS = 24


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"][:1000]
        for block in content
        if isinstance(block, dict)
        and block.get("type") in {"input_text", "output_text", "text"}
        and isinstance(block.get("text"), str)
    )


def build_title_input(entries: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    remaining = TITLE_INPUT_MAX_CHARS
    for entry in entries:
        items = (
            entry["content"]
            if entry["kind"] == "turn"
            else [{"role": "资料", "content": entry["content"]}]
        )
        for item in items:
            role = item.get("role")
            if role not in {"user", "assistant", "资料"}:
                continue
            value = _text(item.get("content"))
            # Sender IDs and reference headers are routing metadata, not topics.
            value = re.sub(r"(?m)^(?:user|referenced_message)\([^\n]*\):\s*", "", value)
            value = value.strip()[:1000]
            if not value:
                continue
            part = f"{role}: {value}\n"[:remaining]
            parts.append(part)
            remaining -= len(part)
            if remaining <= 0:
                return "".join(parts)
    return "".join(parts)


def normalize_title(value: str) -> str:
    value = re.sub(r"\[CQ:[^\]]*\]", "", value, flags=re.IGNORECASE)
    value = "".join(
        char
        for char in value
        if not unicodedata.category(char).startswith("C") or char.isspace()
    )
    value = " ".join(value.split()).strip(" `\"'“”‘’#*《》")
    value = re.sub(
        r"^(?:会话标题|会话名称|标题|title)\s*[:：]\s*", "", value, flags=re.IGNORECASE
    )
    value = value.strip(" `\"'“”‘’#*《》")[:TITLE_MAX_CHARS].strip()
    if not value or value == "新会话":
        raise ValueError("模型没有返回有效的会话标题")
    return value
