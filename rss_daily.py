import asyncio
import hashlib
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar

import nonebot_plugin_localstore as store
from nonebot import get_bots, get_plugin_config
from nonebot.adapters.onebot.v11 import Bot
from nonebot.adapters.onebot.v11.exception import (
    ActionFailed,
    ApiNotAvailable,
    NetworkError,
)
from nonebot.log import logger
from nonebot_plugin_apscheduler import scheduler

from .config import Config

JOB_ID = "sunny_agent_ai_daily_rss"
STATE_FILE = store.get_data_file("sunny_agent", "ai_daily_rss_state.json")
USER_AGENT = "sunny-agent/1.0"

plugin_config = get_plugin_config(Config)


@dataclass(slots=True)
class FeedItem:
    item_id: str
    title: str
    link: str
    published: str
    content: str


class HtmlToTextParser(HTMLParser):
    block_tags: ClassVar[set[str]] = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        _attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag == "li":
            self._append("\n- ")
        elif tag in self.block_tags:
            self._append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.block_tags:
            self._append("\n")

    def handle_data(self, data: str) -> None:
        self._append(data)

    def text(self) -> str:
        return normalize_text("".join(self.parts))

    def _append(self, text: str) -> None:
        if text:
            self.parts.append(text)


def normalize_text(text: str) -> str:
    text = unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_text(text: str) -> str:
    if not text:
        return ""

    parser = HtmlToTextParser()
    parser.feed(text)
    parser.close()
    parsed_text = parser.text()
    return parsed_text or normalize_text(re.sub(r"<[^>]+>", "", text))


def local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]


def first_child_text(element: ET.Element, *names: str) -> str:
    target_names = set(names)
    for child in element:
        if local_name(child.tag) in target_names and child.text:
            return normalize_text(child.text)
    return ""


def first_link(element: ET.Element) -> str:
    for child in element:
        if local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if href:
            return href.strip()
        if child.text:
            return normalize_text(child.text)
    return ""


def parse_datetime(value: str) -> datetime | None:
    if not value:
        return None

    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        pass

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def display_datetime(value: str) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M")


def date_sort_key(value: str) -> float:
    parsed = parse_datetime(value)
    if parsed is None:
        return 0
    return parsed.timestamp()


def make_item_id(*parts: str) -> str:
    raw_key = "|".join(part for part in parts if part)
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def parse_feed(xml_data: bytes) -> list[FeedItem]:
    root = ET.fromstring(xml_data)
    entries = [
        item for item in root.iter() if local_name(item.tag) in {"item", "entry"}
    ]
    feed_items: list[FeedItem] = []

    for entry in entries:
        title = first_child_text(entry, "title") or "AI 早报"
        link = first_link(entry)
        published = first_child_text(entry, "pubDate", "published", "updated", "date")
        guid = first_child_text(entry, "guid", "id")
        content_html = first_child_text(
            entry,
            "encoded",
            "description",
            "summary",
            "content",
        )
        content = html_to_text(content_html)
        item_id = make_item_id(guid, link, title, published)

        feed_items.append(
            FeedItem(
                item_id=item_id,
                title=title,
                link=link,
                published=published,
                content=content,
            )
        )

    return sorted(
        feed_items,
        key=lambda item: date_sort_key(item.published),
        reverse=True,
    )


def fetch_feed(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


async def fetch_ai_daily_items() -> list[FeedItem]:
    xml_data = await asyncio.to_thread(
        fetch_feed,
        plugin_config.sunny_agent_ai_daily_rss_url,
    )
    return parse_feed(xml_data)


def load_state() -> dict[str, list[str]]:
    if not STATE_FILE.exists():
        return {}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            raw_state = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"Failed to read AI daily RSS state: {exc}")
        return {}

    sent_items = raw_state.get("sent_item_ids", {})
    if not isinstance(sent_items, dict):
        return {}

    state: dict[str, list[str]] = {}
    for group_id, item_ids in sent_items.items():
        if isinstance(item_ids, list):
            state[str(group_id)] = [str(item_id) for item_id in item_ids]
    return state


def save_state(state: dict[str, list[str]]) -> None:
    payload = {"sent_item_ids": state}
    write_json(STATE_FILE, payload)


def write_json(path: Path, payload: dict[str, dict[str, list[str]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def format_item(item: FeedItem) -> str:
    lines = ["【每日 AI 早报】", item.title]

    published = display_datetime(item.published)
    if published:
        lines.append(f"发布时间：{published}")

    if item.content:
        lines.extend(["", item.content])

    if item.link:
        lines.extend(["", f"来源：{item.link}"])

    return "\n".join(lines).strip()


def split_message(message: str, max_chars: int) -> list[str]:
    if len(message) <= max_chars:
        return [message]

    chunks: list[str] = []
    current = ""
    for line in message.splitlines(keepends=True):
        if len(line) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            chunks.extend(
                line[index : index + max_chars].strip()
                for index in range(0, len(line), max_chars)
            )
            continue

        if len(current) + len(line) > max_chars:
            chunks.append(current.strip())
            current = line
        else:
            current += line

    if current.strip():
        chunks.append(current.strip())

    total = len(chunks)
    if total <= 1:
        return chunks

    return [f"{chunk}\n\n({index}/{total})" for index, chunk in enumerate(chunks, 1)]


def connected_onebot_bots() -> list[Bot]:
    return [bot for bot in get_bots().values() if isinstance(bot, Bot)]


async def send_group_text(group_id: int, message: str) -> bool:
    last_error: Exception | None = None
    for bot in connected_onebot_bots():
        try:
            await bot.send_group_msg(group_id=group_id, message=message)
        except (ActionFailed, ApiNotAvailable, NetworkError) as exc:  # noqa: PERF203
            last_error = exc
            logger.warning(f"Failed to send AI daily RSS to group {group_id}: {exc}")
        else:
            return True

    if last_error is None:
        logger.warning("No OneBot v11 bots are connected for AI daily RSS push.")
    return False


async def send_item_to_group(group_id: int, item: FeedItem) -> bool:
    message = format_item(item)
    for chunk in split_message(
        message,
        plugin_config.sunny_agent_ai_daily_message_max_chars,
    ):
        if not await send_group_text(group_id, chunk):
            return False
    return True


async def push_ai_daily_rss() -> None:
    group_ids = plugin_config.sunny_agent_ai_daily_group_ids
    if not group_ids:
        logger.warning("AI daily RSS push is enabled, but no target group IDs are set.")
        return

    try:
        items = await fetch_ai_daily_items()
    except (OSError, TimeoutError, ET.ParseError, ValueError) as exc:
        logger.exception(f"Failed to fetch AI daily RSS: {exc}")
        return

    if not items:
        logger.warning("AI daily RSS feed returned no items.")
        return

    state = load_state()
    state_changed = False

    for group_id in group_ids:
        group_key = str(group_id)
        sent_item_ids = state.setdefault(group_key, [])
        pending_items = [
            item
            for item in items[: plugin_config.sunny_agent_ai_daily_max_items]
            if item.item_id not in sent_item_ids
        ]

        for item in reversed(pending_items):
            if await send_item_to_group(group_id, item):
                sent_item_ids.append(item.item_id)
                del sent_item_ids[:-200]
                state_changed = True

    if state_changed:
        save_state(state)


if plugin_config.sunny_agent_ai_daily_enabled:
    scheduler.add_job(
        push_ai_daily_rss,
        "cron",
        hour=plugin_config.sunny_agent_ai_daily_hour,
        minute=plugin_config.sunny_agent_ai_daily_minute,
        timezone=plugin_config.sunny_agent_ai_daily_timezone,
        id=JOB_ID,
        replace_existing=True,
    )
