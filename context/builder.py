"""Bounded model views; the complete stored transcript remains available."""

import json
from typing import Any

from .models import ContextError


def input_cost(value: Any) -> int:
    """Conservative text budget, with an explicit allowance for image blocks.

    This is an estimate, not a model-specific tokenizer. Base64 bytes aren't tokens.
    """
    if isinstance(value, dict):
        if value.get("type") == "input_image":
            return 4096
        return sum(input_cost(key) + input_cost(item) for key, item in value.items())
    if isinstance(value, list):
        return sum(input_cost(item) for item in value)
    return len(str(value).encode("utf-8"))


def build_history(
    entries: list[dict[str, Any]],
    input_items: list[dict[str, Any]],
    *,
    max_input_tokens: int,
    recent_turns: int,
    entry_max_chars: int,
) -> list[dict[str, Any]]:
    remaining = max_input_tokens - input_cost(input_items)
    if remaining < 0:
        raise ContextError("本次消息或引用内容过长，请缩小范围后再试。")
    selected: list[list[dict[str, Any]]] = []
    # Keep source IDs discoverable even when full source bodies exceed the window.
    sources = [
        entry
        for entry in entries
        if entry["kind"] == "external" and not entry.get("skip_projection")
    ][-20:]
    catalog = (
        [
            {
                "role": "user",
                "content": "可用资料（read_context 可分段读取）：\n"
                + "\n".join(
                    f"{entry['id']}: {str(entry['source'].get('title') or entry['source'].get('source_id', '资料'))[:120]}"
                    for entry in sources
                ),
            }
        ]
        if sources
        else []
    )
    catalog_cost = input_cost(catalog)
    if catalog_cost <= remaining:
        remaining -= catalog_cost
    else:
        catalog = []
    turns = 0
    for entry in reversed(entries):
        if entry.get("skip_projection"):
            continue
        if entry["kind"] == "turn":
            if turns >= recent_turns:
                continue
            turns += 1
            items = entry["content"]
        else:
            content = entry["content"]
            text = "\n".join(str(b.get("text", "[图片或附件]")) for b in content)
            if len(text) > entry_max_chars:
                text = text[:entry_max_chars] + "\n[已截取；可用 read_context 补读]"
            source = json.dumps(entry["source"], ensure_ascii=False)
            label = f"context_data(entry_id={entry['id']}, source={source})"
            role = "assistant" if entry["kind"] == "assistant_publication" else "user"
            items = [{"role": role, "content": f"{label}:\n{text}"}]
            media = [
                block
                for block in content
                if block.get("type") in {"input_image", "input_file"}
            ]
            if role == "user" and media:
                items = [
                    {
                        "role": role,
                        "content": [
                            {"type": "input_text", "text": f"{label}:\n{text}"},
                            *media,
                        ],
                    }
                ]
        if entry.get("delivery_status") not in (None, "confirmed"):
            items = [
                *items,
                {
                    "role": "user",
                    "content": (
                        "delivery_status: 上述机器人内容未确认送达，不能假定用户已经看到。"
                    ),
                },
            ]
        cost = input_cost(items)
        if cost <= remaining:
            selected.append(items)
            remaining -= cost
    return catalog + [item for batch in reversed(selected) for item in batch]
