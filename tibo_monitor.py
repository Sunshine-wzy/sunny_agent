import asyncio
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as datetime_timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

from .chat import atranslate_to_chinese
from .config import Config


JOB_ID = "sunny_agent_tibo_monitor"
STATE_FILE = store.get_data_file("sunny_agent", "tibo_monitor_state.json")
USER_AGENT = "sunny-agent/1.0 (Tibo post monitor)"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_SENT_TWEET_IDS = 200
MAX_TRANSLATION_CACHE_ITEMS = 200
PST_TIMEZONE = datetime_timezone(timedelta(hours=-8), name="PST")

plugin_config = get_plugin_config(Config)
push_lock = asyncio.Lock()
translation_cache: dict[str, str] = {}


@dataclass(slots=True, frozen=True)
class TiboPost:
    tweet_id: str
    text: str
    date_epoch: int
    author_name: str
    is_reply: bool
    is_repost: bool

    @property
    def url(self) -> str:
        username = normalized_username()
        return f"https://x.com/{username}/status/{self.tweet_id}"


@dataclass(slots=True)
class TiboMonitorState:
    enabled_group_ids: set[int]
    enabled_since: dict[str, int]
    sent_tweet_ids: dict[str, list[str]]


def normalized_username() -> str:
    return plugin_config.sunny_agent_tibo_username.strip().lstrip("@") or "thsottiaux"


def _parse_group_ids(value: object) -> set[int]:
    if not isinstance(value, list):
        return set()

    group_ids: set[int] = set()
    for item in value:
        try:
            group_ids.add(int(item))
        except (TypeError, ValueError):
            logger.warning(f"Invalid Tibo monitor group id in state: {item}")
    return group_ids


def _parse_enabled_since(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}

    enabled_since: dict[str, int] = {}
    for group_id, timestamp in value.items():
        try:
            enabled_since[str(int(group_id))] = int(timestamp)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid Tibo monitor enabled timestamp in state: "
                f"{group_id}={timestamp}",
            )
    return enabled_since


def _parse_sent_tweet_ids(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}

    sent_tweet_ids: dict[str, list[str]] = {}
    for group_id, tweet_ids in value.items():
        if not isinstance(tweet_ids, list):
            continue
        try:
            group_key = str(int(group_id))
        except (TypeError, ValueError):
            logger.warning(f"Invalid Tibo monitor sent group id in state: {group_id}")
            continue
        sent_tweet_ids[group_key] = [str(tweet_id) for tweet_id in tweet_ids]
    return sent_tweet_ids


def empty_state() -> TiboMonitorState:
    return TiboMonitorState(
        enabled_group_ids=set(),
        enabled_since={},
        sent_tweet_ids={},
    )


def load_state() -> TiboMonitorState:
    if not STATE_FILE.exists():
        return empty_state()

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            raw_state = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"Failed to read Tibo monitor state: {exc}")
        return empty_state()

    if not isinstance(raw_state, dict):
        return empty_state()

    enabled_group_ids = _parse_group_ids(raw_state.get("enabled_group_ids"))
    enabled_since = _parse_enabled_since(raw_state.get("enabled_since"))
    fallback_timestamp = int(time.time())
    for group_id in enabled_group_ids:
        enabled_since.setdefault(str(group_id), fallback_timestamp)

    return TiboMonitorState(
        enabled_group_ids=enabled_group_ids,
        enabled_since=enabled_since,
        sent_tweet_ids=_parse_sent_tweet_ids(raw_state.get("sent_tweet_ids")),
    )


def save_state(state: TiboMonitorState) -> None:
    payload = {
        "enabled_group_ids": sorted(state.enabled_group_ids),
        "enabled_since": state.enabled_since,
        "sent_tweet_ids": state.sent_tweet_ids,
    }
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = STATE_FILE.with_name(f"{STATE_FILE.name}.tmp")
    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    temporary_file.replace(STATE_FILE)


def target_group_ids(state: TiboMonitorState | None = None) -> list[int]:
    current_state = state or load_state()
    return sorted(current_state.enabled_group_ids)


def is_group_tibo_monitor_enabled(group_id: int) -> bool:
    return int(group_id) in load_state().enabled_group_ids


def set_group_tibo_monitor_enabled(group_id: int, enabled: bool) -> None:
    state = load_state()
    normalized_group_id = int(group_id)
    group_key = str(normalized_group_id)

    if enabled:
        if normalized_group_id not in state.enabled_group_ids:
            state.enabled_group_ids.add(normalized_group_id)
            state.enabled_since[group_key] = int(time.time())
    else:
        state.enabled_group_ids.discard(normalized_group_id)
        state.enabled_since.pop(group_key, None)

    save_state(state)


def _parse_post(raw_post: object) -> TiboPost | None:
    if not isinstance(raw_post, dict):
        return None

    tweet_id = str(raw_post.get("tweetID", "")).strip()
    if not tweet_id:
        return None

    try:
        date_epoch = int(raw_post["date_epoch"])
    except (KeyError, TypeError, ValueError):
        logger.warning(f"Tibo post {tweet_id} has no valid date_epoch")
        return None

    return TiboPost(
        tweet_id=tweet_id,
        text=str(raw_post.get("text") or "").strip(),
        date_epoch=date_epoch,
        author_name=str(raw_post.get("user_name") or "Tibo").strip() or "Tibo",
        is_reply=bool(raw_post.get("replyingToID") or raw_post.get("replyingTo")),
        is_repost=bool(raw_post.get("retweetURL") or raw_post.get("retweet")),
    )


def parse_posts(payload: object) -> list[TiboPost]:
    if not isinstance(payload, dict):
        raise ValueError("Tibo API response is not a JSON object")

    raw_posts = payload.get("latest_tweets")
    if not isinstance(raw_posts, list):
        raise ValueError("Tibo API response has no latest_tweets list")

    posts_by_id: dict[str, TiboPost] = {}
    for raw_post in raw_posts:
        post = _parse_post(raw_post)
        if post is not None:
            posts_by_id[post.tweet_id] = post

    return sorted(
        posts_by_id.values(),
        key=lambda post: (post.date_epoch, int(post.tweet_id) if post.tweet_id.isdigit() else 0),
    )


def fetch_posts() -> list[TiboPost]:
    username = urllib.parse.quote(normalized_username(), safe="")
    base_url = plugin_config.sunny_agent_tibo_api_base_url.rstrip("/")
    query = urllib.parse.urlencode(
        {
            "with_tweets": "true",
            "timestamp": int(time.time()),
        }
    )
    request = urllib.request.Request(
        f"{base_url}/{username}?{query}",
        headers={"User-Agent": USER_AGENT},
    )

    with urllib.request.urlopen(
        request,
        timeout=plugin_config.sunny_agent_tibo_request_timeout_seconds,
    ) as response:
        raw_payload = response.read(MAX_RESPONSE_BYTES + 1)

    if len(raw_payload) > MAX_RESPONSE_BYTES:
        raise ValueError("Tibo API response is too large")

    try:
        payload: Any = json.loads(raw_payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Tibo API response is not valid JSON") from exc
    return parse_posts(payload)


async def fetch_tibo_posts() -> list[TiboPost]:
    return await asyncio.to_thread(fetch_posts)


def should_notify_group(
    state: TiboMonitorState,
    group_id: int,
    post: TiboPost,
) -> bool:
    if group_id not in state.enabled_group_ids:
        return False

    group_key = str(group_id)
    enabled_since = state.enabled_since.get(group_key, int(time.time()))
    if post.date_epoch <= enabled_since:
        return False

    return post.tweet_id not in state.sent_tweet_ids.get(group_key, [])


def mark_tweet_sent(group_id: int, tweet_id: str) -> None:
    state = load_state()
    if group_id not in state.enabled_group_ids:
        return

    sent_tweet_ids = state.sent_tweet_ids.setdefault(str(group_id), [])
    if tweet_id in sent_tweet_ids:
        return

    sent_tweet_ids.append(tweet_id)
    del sent_tweet_ids[:-MAX_SENT_TWEET_IDS]
    save_state(state)


def display_datetime(date_epoch: int) -> str:
    timezone_name = plugin_config.sunny_agent_tibo_timezone
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.warning(
            "Invalid Tibo monitor timezone "
            f"{timezone_name!r}; falling back to UTC",
        )
        timezone_name = "UTC"
        timezone = ZoneInfo("UTC")

    display_timezone = "北京时间" if timezone_name == "Asia/Shanghai" else timezone_name
    formatted_datetime = datetime.fromtimestamp(date_epoch, timezone).strftime(
        "%Y-%m-%d %H:%M:%S",
    )
    return f"{formatted_datetime}（{display_timezone}）"


def display_pst_datetime(date_epoch: int) -> str:
    return datetime.fromtimestamp(date_epoch, PST_TIMEZONE).strftime(
        "%Y-%m-%d %H:%M:%S PST",
    )


def format_post_message(post: TiboPost, translation: str) -> str:
    original_text = post.text or "（原推文无文字内容）"
    translated_text = translation or "（原推文无文字内容）"
    return (
        f"🐦 {post.author_name} (@{normalized_username()}) 发布了新推文\n\n"
        f"# 原文\n{original_text}\n\n"
        f"# 译文\n{translated_text}\n\n"
        f"发布时间：{display_datetime(post.date_epoch)}\n"
        f"PST：{display_pst_datetime(post.date_epoch)}\n"
        f"原文：{post.url}"
    )


def connected_onebot_bots(preferred_bot: Bot | None = None) -> list[Bot]:
    bots = [bot for bot in get_bots().values() if isinstance(bot, Bot)]
    if preferred_bot is None:
        return bots
    return [preferred_bot, *(bot for bot in bots if bot is not preferred_bot)]


async def send_group_message(
    group_id: int,
    message: str,
    *,
    preferred_bot: Bot | None = None,
) -> bool:
    bots = connected_onebot_bots(preferred_bot)
    if not bots:
        logger.warning("No OneBot v11 bots are connected for Tibo post push.")
        return False

    for bot in bots:
        for retry_index in range(plugin_config.sunny_agent_tibo_send_retry_times + 1):
            try:
                await bot.send_group_msg(group_id=group_id, message=message)
            except ApiNotAvailable as exc:
                logger.warning(f"Failed to send Tibo post to group {group_id}: {exc}")
                break
            except (ActionFailed, NetworkError) as exc:  # noqa: PERF203
                if (
                    isinstance(exc, NetworkError)
                    and retry_index < plugin_config.sunny_agent_tibo_send_retry_times
                ):
                    logger.warning(
                        f"Failed to send Tibo post to group {group_id}; retrying: {exc}",
                    )
                    await asyncio.sleep(
                        plugin_config.sunny_agent_tibo_send_retry_delay_seconds,
                    )
                    continue

                logger.warning(f"Failed to send Tibo post to group {group_id}: {exc}")
                break
            else:
                return True
    return False


def _post_is_included(post: TiboPost) -> bool:
    if plugin_config.sunny_agent_tibo_exclude_replies and post.is_reply:
        return False
    return not (plugin_config.sunny_agent_tibo_exclude_reposts and post.is_repost)


def latest_included_post(posts: list[TiboPost]) -> TiboPost | None:
    return next((post for post in reversed(posts) if _post_is_included(post)), None)


async def translate_post(post: TiboPost) -> str:
    if not post.text:
        return "（原推文无文字内容）"
    if cached_translation := translation_cache.get(post.tweet_id):
        return cached_translation

    translation = await atranslate_to_chinese(post.text)
    if not translation:
        raise ValueError(f"LLM returned an empty translation for Tibo post {post.tweet_id}")

    translation_cache[post.tweet_id] = translation
    while len(translation_cache) > MAX_TRANSLATION_CACHE_ITEMS:
        translation_cache.pop(next(iter(translation_cache)))
    return translation


async def push_tibo_updates() -> None:
    async with push_lock:
        if not target_group_ids():
            return
        if not connected_onebot_bots():
            logger.warning("No OneBot v11 bots are connected for Tibo post push.")
            return

        try:
            posts = await fetch_tibo_posts()
        except Exception as exc:
            logger.exception(f"Failed to fetch Tibo posts: {exc}")
            return

        for post in posts:
            if not _post_is_included(post):
                continue

            state = load_state()
            group_ids = [
                group_id
                for group_id in target_group_ids(state)
                if should_notify_group(state, group_id, post)
            ]
            if not group_ids:
                continue

            try:
                translation = await translate_post(post)
            except Exception as exc:
                logger.exception(f"Failed to translate Tibo post {post.tweet_id}: {exc}")
                continue

            message = format_post_message(post, translation)
            for group_id in group_ids:
                current_state = load_state()
                if not should_notify_group(current_state, group_id, post):
                    continue
                if await send_group_message(group_id, message):
                    mark_tweet_sent(group_id, post.tweet_id)


scheduler.add_job(
    push_tibo_updates,
    "interval",
    seconds=plugin_config.sunny_agent_tibo_poll_interval_seconds,
    id=JOB_ID,
    replace_existing=True,
    max_instances=1,
    coalesce=True,
)
