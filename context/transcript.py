"""Readable history for retrieval, without SDK tool payloads or reasoning items."""

import json
from collections.abc import Iterator
from typing import Any


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in {"input_text", "output_text", "text"}:
            parts.append(str(block.get("text", "")))
        elif kind == "input_image":
            parts.append("[图片]")
        elif kind == "input_file":
            parts.append("[附件]")
    return "\n".join(parts)


def transcript_parts(entries: list[dict[str, Any]]) -> Iterator[str]:
    for entry in entries:
        parts = []
        if entry["kind"] == "turn":
            for item in entry["content"]:
                role = item.get("role")
                if (
                    role not in {"user", "assistant"}
                    or item.get("type", "message") != "message"
                ):
                    continue
                text = _content_text(item.get("content"))
                if text:
                    parts.append(f"[{role}]\n{text}")
            if entry.get("turn_status") == "running":
                parts.append("[该轮回复尚未完成]")
        else:
            label = "资料" if entry["kind"] == "external" else "Sunny 主动发送的内容"
            source = {
                key: entry["source"][key]
                for key in ("kind", "source_id", "title", "url", "published")
                if entry["source"].get(key)
            }
            parts.append(
                f"[{label}]\n来源：{json.dumps(source, ensure_ascii=False)}\n{_content_text(entry['content'])}"
            )
        if entry.get("delivery_status") not in (None, "confirmed"):
            parts.append(f"[机器人内容未确认送达：{entry['delivery_status']}]")
        elif entry["kind"] == "assistant_publication" and not entry.get(
            "delivery_status"
        ):
            parts.append("[机器人内容尚未确认送达]")
        if parts:
            yield f"[entry_id={entry['id']}]\n" + "\n\n".join(parts) + "\n\n"


def transcript_page(
    entries: list[dict[str, Any]], offset: int, limit: int
) -> dict[str, Any]:
    total = 0
    selected = []
    for part in transcript_parts(entries):
        start, end = max(0, offset - total), min(len(part), offset + limit - total)
        if start < end:
            selected.append(part[start:end])
        total += len(part)
    text = "".join(selected)
    next_offset = offset + len(text)
    return {
        "content": text,
        "offset": offset,
        "total_chars": total,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
    }
