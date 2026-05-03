import asyncio
import hashlib
import json
import random
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


@dataclass(slots=True)
class AiDailyRssState:
    sent_item_ids: dict[str, list[str]]
    enabled_group_ids: set[int]
    disabled_group_ids: set[int]


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
    for name in names:
        for child in element:
            if local_name(child.tag) == name and child.text:
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
            "description",
            "encoded",
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


def parse_group_id_set(value: object) -> set[int]:
    if not isinstance(value, list):
        return set()

    group_ids: set[int] = set()
    for item in value:
        try:
            group_ids.add(int(item))
        except (TypeError, ValueError):
            logger.warning(f"Invalid AI daily RSS group id in state: {item}")

    return group_ids


def load_state() -> AiDailyRssState:
    if not STATE_FILE.exists():
        return AiDailyRssState(
            sent_item_ids={},
            enabled_group_ids=set(),
            disabled_group_ids=set(),
        )

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            raw_state = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"Failed to read AI daily RSS state: {exc}")
        return AiDailyRssState(
            sent_item_ids={},
            enabled_group_ids=set(),
            disabled_group_ids=set(),
        )

    if not isinstance(raw_state, dict):
        return AiDailyRssState(
            sent_item_ids={},
            enabled_group_ids=set(),
            disabled_group_ids=set(),
        )

    sent_items = raw_state.get("sent_item_ids", {})
    sent_item_ids: dict[str, list[str]] = {}
    if isinstance(sent_items, dict):
        for group_id, item_ids in sent_items.items():
            if isinstance(item_ids, list):
                sent_item_ids[str(group_id)] = [str(item_id) for item_id in item_ids]

    return AiDailyRssState(
        sent_item_ids=sent_item_ids,
        enabled_group_ids=parse_group_id_set(raw_state.get("enabled_group_ids")),
        disabled_group_ids=parse_group_id_set(raw_state.get("disabled_group_ids")),
    )


def save_state(state: AiDailyRssState) -> None:
    payload = {
        "sent_item_ids": state.sent_item_ids,
        "enabled_group_ids": sorted(state.enabled_group_ids),
        "disabled_group_ids": sorted(state.disabled_group_ids),
    }
    write_json(STATE_FILE, payload)


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def configured_group_ids() -> set[int]:
    if not plugin_config.sunny_agent_ai_daily_enabled:
        return set()

    return set(plugin_config.sunny_agent_ai_daily_group_ids)


def target_group_ids(state: AiDailyRssState | None = None) -> list[int]:
    current_state = state or load_state()
    group_ids = configured_group_ids() | current_state.enabled_group_ids
    group_ids -= current_state.disabled_group_ids
    return sorted(group_ids)


def is_group_ai_daily_enabled(group_id: int) -> bool:
    return int(group_id) in target_group_ids()


def set_group_ai_daily_enabled(group_id: int, enabled: bool) -> None:
    state = load_state()
    normalized_group_id = int(group_id)
    if enabled:
        state.enabled_group_ids.add(normalized_group_id)
        state.disabled_group_ids.discard(normalized_group_id)
    else:
        state.enabled_group_ids.discard(normalized_group_id)
        state.disabled_group_ids.add(normalized_group_id)

    save_state(state)


def mark_item_sent(
    state: AiDailyRssState,
    group_id: int,
    item_id: str,
) -> bool:
    sent_item_ids = state.sent_item_ids.setdefault(str(group_id), [])
    if item_id in sent_item_ids:
        return False

    sent_item_ids.append(item_id)
    del sent_item_ids[:-200]
    return True


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


def connected_onebot_bots(preferred_bot: Bot | None = None) -> list[Bot]:
    bots = [bot for bot in get_bots().values() if isinstance(bot, Bot)]
    if preferred_bot is None:
        return bots

    return [preferred_bot, *(bot for bot in bots if bot is not preferred_bot)]


def should_retry_send_error(exc: Exception) -> bool:
    return isinstance(exc, NetworkError)


async def wait_random_seconds(min_seconds: float, max_seconds: float) -> None:
    if max_seconds < min_seconds:
        min_seconds, max_seconds = max_seconds, min_seconds
    if max_seconds <= 0:
        return

    await asyncio.sleep(random.uniform(min_seconds, max_seconds))


async def wait_between_messages() -> None:
    await wait_random_seconds(
        plugin_config.sunny_agent_ai_daily_message_delay_min_seconds,
        plugin_config.sunny_agent_ai_daily_message_delay_max_seconds,
    )


async def wait_before_send_retry() -> None:
    await wait_random_seconds(
        plugin_config.sunny_agent_ai_daily_send_retry_delay_min_seconds,
        plugin_config.sunny_agent_ai_daily_send_retry_delay_max_seconds,
    )


async def send_group_text(
    group_id: int,
    message: str,
    *,
    preferred_bot: Bot | None = None,
) -> bool:
    last_error: Exception | None = None
    for bot in connected_onebot_bots(preferred_bot):
        for retry_index in range(plugin_config.sunny_agent_ai_daily_send_retry_times + 1):
            try:
                await bot.send_group_msg(group_id=group_id, message=message)
            except ApiNotAvailable as exc:
                last_error = exc
                logger.warning(
                    f"Failed to send AI daily RSS to group {group_id}: {exc}",
                )
                break
            except (ActionFailed, NetworkError) as exc:  # noqa: PERF203
                last_error = exc
                if (
                    retry_index < plugin_config.sunny_agent_ai_daily_send_retry_times
                    and should_retry_send_error(exc)
                ):
                    logger.warning(
                        "Failed to send AI daily RSS to group "
                        f"{group_id}, retrying after a random delay: {exc}",
                    )
                    await wait_before_send_retry()
                    continue

                logger.warning(
                    f"Failed to send AI daily RSS to group {group_id}: {exc}",
                )
                break
            else:
                return True

    if last_error is None:
        logger.warning("No OneBot v11 bots are connected for AI daily RSS push.")
    return False


async def send_item_to_group(
    group_id: int,
    item: FeedItem,
    *,
    preferred_bot: Bot | None = None,
    wait_before_first: bool = False,
) -> tuple[bool, bool]:
    message = format_item(item)
    sent_any = False
    for index, chunk in enumerate(
        split_message(
            message,
            plugin_config.sunny_agent_ai_daily_message_max_chars,
        )
    ):
        if index > 0 or wait_before_first:
            await wait_between_messages()
            wait_before_first = False

        if not await send_group_text(group_id, chunk, preferred_bot=preferred_bot):
            return False, sent_any
        sent_any = True

    return True, sent_any


async def send_items_to_group(
    group_id: int,
    items: list[FeedItem],
    state: AiDailyRssState,
    *,
    preferred_bot: Bot | None = None,
    only_unsent: bool = True,
) -> tuple[int, bool]:
    group_key = str(group_id)
    sent_item_ids = state.sent_item_ids.setdefault(group_key, [])
    selected_items = items[: plugin_config.sunny_agent_ai_daily_max_items]
    if only_unsent:
        selected_items = [
            item for item in selected_items if item.item_id not in sent_item_ids
        ]

    sent_count = 0
    state_changed = False
    sent_message_to_group = False
    for item in reversed(selected_items):
        sent_item, sent_any = await send_item_to_group(
            group_id,
            item,
            preferred_bot=preferred_bot,
            wait_before_first=sent_message_to_group,
        )
        sent_message_to_group = sent_message_to_group or sent_any

        if not sent_item:
            break

        sent_count += 1
        state_changed = mark_item_sent(state, group_id, item.item_id) or state_changed

    return sent_count, state_changed


async def push_ai_daily_rss() -> None:
    state = load_state()
    group_ids = target_group_ids(state)
    if not group_ids:
        return

    try:
        items = await fetch_ai_daily_items()
    except (OSError, TimeoutError, ET.ParseError, ValueError) as exc:
        logger.exception(f"Failed to fetch AI daily RSS: {exc}")
        return

    if not items:
        logger.warning("AI daily RSS feed returned no items.")
        return

    state_changed = False

    for group_id in group_ids:
        _, group_state_changed = await send_items_to_group(group_id, items, state)
        state_changed = state_changed or group_state_changed

    if state_changed:
        save_state(state)


scheduler.add_job(
    push_ai_daily_rss,
    "cron",
    hour=plugin_config.sunny_agent_ai_daily_hour,
    minute=plugin_config.sunny_agent_ai_daily_minute,
    timezone=plugin_config.sunny_agent_ai_daily_timezone,
    id=JOB_ID,
    replace_existing=True,
)
