import asyncio
import html
import ipaddress
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Annotated, Any

from agents import function_tool

__all__ = [
    "agent_reach_status",
    "agent_reach_search",
    "agent_reach_read",
    "agent_reach_browse",
    "agent_reach_rss",
]

# Agent Reach is a capability/router layer: the actual work is performed by
# its selected upstream CLIs and MCP services.  The wrappers below deliberately
# expose a read-only, allow-listed surface instead of a generic shell tool.
_AGENT_REACH_MAX_OUTPUT_CHARS = 30_000
_AGENT_REACH_DEFAULT_TIMEOUT_SECONDS = 60.0
_AGENT_REACH_USER_AGENT = "sunny-agent/agent-reach"
_AGENT_REACH_PLATFORMS = (
    "web, twitter, youtube, bilibili, reddit, github, xiaohongshu, douyin, "
    "wechat, weibo, linkedin, instagram, facebook, v2ex, rss"
)


def _clip_agent_reach_text(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit] + f"\n... [truncated {len(value) - limit} chars]", True


def _agent_reach_setup_hint(platform: str = "") -> str:
    suffix = f" for {platform}" if platform else ""
    return (
        f"No usable Agent Reach backend was found{suffix}. Install/configure "
        "Agent Reach, then run `agent-reach doctor --json`."
    )


async def _run_agent_reach_command(
    backend: str,
    executable: str,
    arguments: list[str],
    *,
    timeout: float = _AGENT_REACH_DEFAULT_TIMEOUT_SECONDS,
    max_output_chars: int = _AGENT_REACH_MAX_OUTPUT_CHARS,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _run_agent_reach_command_sync,
        backend,
        executable,
        arguments,
        timeout,
        max_output_chars,
    )


def _run_agent_reach_command_sync(
    backend: str,
    executable: str,
    arguments: list[str],
    timeout: float,
    max_output_chars: int,
) -> dict[str, Any]:
    resolved = shutil.which(executable)
    if resolved is None:
        return {
            "ok": False,
            "backend": backend,
            "error": f"Required command {executable!r} is not installed.",
        }

    process_kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        process_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

    try:
        completed = subprocess.run(
            [resolved, *arguments],
            capture_output=True,
            check=False,
            timeout=timeout,
            **process_kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace").strip()
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        result: dict[str, Any] = {
            "ok": False,
            "backend": backend,
            "error": f"Backend timed out after {timeout:g} seconds.",
        }
        if stdout:
            result["content"] = _clip_agent_reach_text(stdout, max_output_chars)[0]
        if stderr:
            result["diagnostics"] = _clip_agent_reach_text(stderr, 4_000)[0]
        return result
    except OSError as exc:
        return {
            "ok": False,
            "backend": backend,
            "error": f"Could not start {executable!r}: {exc}",
        }

    stdout = completed.stdout.decode("utf-8", errors="replace").strip()
    stderr = completed.stderr.decode("utf-8", errors="replace").strip()
    content, truncated = _clip_agent_reach_text(stdout, max_output_chars)
    diagnostics, diagnostics_truncated = _clip_agent_reach_text(stderr, 4_000)
    result: dict[str, Any] = {
        "ok": completed.returncode == 0,
        "backend": backend,
        "exit_code": completed.returncode,
    }
    if content:
        result["content"] = content
    if diagnostics:
        result["diagnostics"] = diagnostics
    if truncated or diagnostics_truncated:
        result["truncated"] = True
    if completed.returncode != 0:
        result["error"] = diagnostics or content or f"{backend} failed."
    return result


async def _run_agent_reach_candidates(
    platform: str,
    candidates: list[tuple[str, str, list[str], float]],
    *,
    max_output_chars: int = _AGENT_REACH_MAX_OUTPUT_CHARS,
) -> dict[str, Any]:
    installed = [candidate for candidate in candidates if shutil.which(candidate[1])]
    if not installed:
        return {
            "ok": False,
            "platform": platform,
            "error": _agent_reach_setup_hint(platform),
            "required_commands": list(dict.fromkeys(c[1] for c in candidates)),
        }

    failures: list[dict[str, Any]] = []
    last_result: dict[str, Any] | None = None
    for backend, executable, arguments, timeout in installed:
        result = await _run_agent_reach_command(
            backend,
            executable,
            arguments,
            timeout=timeout,
            max_output_chars=max_output_chars,
        )
        result["platform"] = platform
        if result.get("ok"):
            if failures:
                result["fallbacks_tried"] = failures
            return result
        failures.append(
            {
                "backend": backend,
                "error": str(result.get("error", "Backend failed."))[:1_000],
            }
        )
        last_result = result

    assert last_result is not None
    last_result["attempts"] = failures
    last_result["setup_hint"] = _agent_reach_setup_hint(platform)
    return last_result


def _mcp_call_expression(name: str, **arguments: Any) -> str:
    rendered = ", ".join(
        f"{key}: {json.dumps(value, ensure_ascii=False)}"
        for key, value in arguments.items()
    )
    return f"{name}({rendered})"


def _mcp_candidate(
    backend: str,
    name: str,
    *,
    timeout: float = 120.0,
    **arguments: Any,
) -> tuple[str, str, list[str], float]:
    return (
        backend,
        "mcporter",
        [
            "call",
            _mcp_call_expression(name, **arguments),
            "--timeout",
            str(int(timeout * 1_000)),
        ],
        timeout + 5,
    )


def _normalize_agent_reach_platform(value: str) -> str:
    platform = value.strip().lower().replace("-", "_")
    aliases = {
        "auto": "auto",
        "x": "twitter",
        "twitter_x": "twitter",
        "b站": "bilibili",
        "小红书": "xiaohongshu",
        "xhs": "xiaohongshu",
        "微信": "wechat",
        "微信公众号": "wechat",
        "领英": "linkedin",
        "ig": "instagram",
        "fb": "facebook",
    }
    return aliases.get(platform, platform)


def _validate_agent_reach_url(url: str) -> str:
    clean_url = url.strip()
    if len(clean_url) > 4_096:
        raise ValueError("URL is too long.")

    parsed = urllib.parse.urlsplit(clean_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("A valid HTTP or HTTPS URL is required.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded credentials are not allowed.")

    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".local"):
        raise ValueError("Private or local network URLs are not allowed.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    ):
        raise ValueError("Private or local network URLs are not allowed.")
    return clean_url


def _fetch_agent_reach_text(
    url: str,
    *,
    max_chars: int,
    timeout: float = 30.0,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/plain, text/markdown, application/json, application/xml, text/xml",
            "User-Agent": _AGENT_REACH_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(max_chars * 4 + 1)
            charset = response.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, errors="replace")
            content, truncated = _clip_agent_reach_text(text, max_chars)
            return {
                "ok": True,
                "http_status": response.status,
                "content": content,
                "truncated": truncated or len(raw) > max_chars * 4,
            }
    except urllib.error.HTTPError as exc:
        body = exc.read(4_000).decode("utf-8", errors="replace")
        return {
            "ok": False,
            "http_status": exc.code,
            "error": body.strip() or str(exc),
        }
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "error": f"Could not fetch URL: {exc}"}


async def _agent_reach_jina_read(url: str, max_chars: int) -> dict[str, Any]:
    try:
        clean_url = _validate_agent_reach_url(url)
    except ValueError as exc:
        return {"ok": False, "platform": "web", "error": str(exc)}

    reader_url = "https://r.jina.ai/" + clean_url.replace(" ", "%20")
    result = await asyncio.to_thread(
        _fetch_agent_reach_text,
        reader_url,
        max_chars=max_chars,
    )
    result.update({"platform": "web", "backend": "Jina Reader", "source_url": clean_url})
    return result


def _agent_reach_platform_from_target(target: str) -> str:
    try:
        hostname = urllib.parse.urlsplit(target.strip()).hostname or ""
    except ValueError:
        hostname = ""
    hostname = hostname.lower()
    if hostname.endswith(("x.com", "twitter.com")):
        return "twitter"
    if hostname.endswith(("youtube.com", "youtu.be")):
        return "youtube"
    if hostname.endswith(("bilibili.com", "b23.tv")):
        return "bilibili"
    if hostname.endswith(("reddit.com", "redd.it")):
        return "reddit"
    if hostname.endswith("github.com"):
        return "github"
    if hostname.endswith(("xiaohongshu.com", "xhslink.com")):
        return "xiaohongshu"
    if hostname.endswith(("douyin.com", "iesdouyin.com")):
        return "douyin"
    if hostname.endswith("mp.weixin.qq.com"):
        return "wechat"
    if hostname.endswith("weibo.com"):
        return "weibo"
    if hostname.endswith("linkedin.com"):
        return "linkedin"
    if hostname.endswith("instagram.com"):
        return "instagram"
    if hostname.endswith("facebook.com"):
        return "facebook"
    if hostname.endswith("v2ex.com"):
        return "v2ex"
    return "web"


def _agent_reach_limit(value: int, maximum: int = 20) -> int:
    if value < 1 or value > maximum:
        raise ValueError(f"limit must be between 1 and {maximum}.")
    return value


def _agent_reach_github_repo(target: str) -> str:
    clean_target = target.strip()
    parsed = urllib.parse.urlsplit(clean_target)
    if parsed.hostname and parsed.hostname.lower() == "github.com":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1].removesuffix('.git')}"
    return clean_target.removesuffix(".git")


def _agent_reach_github_item(target: str, item_kind: str) -> tuple[str, str] | None:
    clean_target = target.strip()
    parsed = urllib.parse.urlsplit(clean_target)
    if parsed.hostname and parsed.hostname.lower() == "github.com":
        parts = [part for part in parsed.path.split("/") if part]
        marker = "issues" if item_kind == "issue" else "pull"
        if len(parts) >= 4 and parts[2] == marker and parts[3].isdigit():
            return f"{parts[0]}/{parts[1]}", parts[3]
    match = re.fullmatch(r"([^\s#]+/[^\s#]+)#(\d+)", clean_target)
    if match:
        return match.group(1), match.group(2)
    return None


def _vtt_to_plain_text(raw_vtt: str) -> str:
    lines: list[str] = []
    previous = ""
    for raw_line in raw_vtt.splitlines():
        line = raw_line.strip()
        if (
            not line
            or line == "WEBVTT"
            or line.startswith(("Kind:", "Language:", "NOTE"))
            or "-->" in line
            or line.isdigit()
        ):
            continue
        line = html.unescape(re.sub(r"<[^>]+>", "", line)).strip()
        if line and line != previous:
            lines.append(line)
            previous = line
    return "\n".join(lines)


async def _agent_reach_youtube_transcript(
    target: str,
    languages: str,
    max_chars: int,
) -> dict[str, Any]:
    if not shutil.which("yt-dlp"):
        return {
            "ok": False,
            "platform": "youtube",
            "error": _agent_reach_setup_hint("youtube"),
            "required_commands": ["yt-dlp"],
        }

    with tempfile.TemporaryDirectory(prefix="sunny-agent-reach-") as temp_dir:
        output_template = str(Path(temp_dir) / "%(id)s.%(ext)s")
        result = await _run_agent_reach_command(
            "yt-dlp",
            "yt-dlp",
            [
                "--write-sub",
                "--write-auto-sub",
                "--sub-langs",
                languages,
                "--sub-format",
                "vtt",
                "--skip-download",
                "--no-playlist",
                "-o",
                output_template,
                target,
            ],
            timeout=120,
            max_output_chars=4_000,
        )
        if not result.get("ok"):
            result["platform"] = "youtube"
            return result

        subtitle_files = sorted(Path(temp_dir).glob("*.vtt"))
        if not subtitle_files:
            return {
                "ok": False,
                "platform": "youtube",
                "backend": "yt-dlp",
                "error": (
                    "No matching subtitles were available. If transcription is configured, "
                    "use `agent-reach transcribe` for this URL."
                ),
            }

        transcript_parts = [
            _vtt_to_plain_text(path.read_text(encoding="utf-8", errors="replace"))
            for path in subtitle_files
        ]
        content, truncated = _clip_agent_reach_text(
            "\n\n".join(part for part in transcript_parts if part), max_chars
        )
        return {
            "ok": True,
            "platform": "youtube",
            "backend": "yt-dlp",
            "content_kind": "transcript",
            "languages": [path.suffixes[-2].lstrip(".") for path in subtitle_files],
            "content": content,
            "truncated": truncated,
        }


def _agent_reach_rss_sync(url: str, limit: int) -> dict[str, Any]:
    try:
        import feedparser
    except ImportError:
        return {
            "ok": False,
            "platform": "rss",
            "error": "feedparser is not installed. Install the agent-reach package.",
        }

    try:
        clean_url = _validate_agent_reach_url(url)
    except ValueError as exc:
        return {"ok": False, "platform": "rss", "error": str(exc)}

    request = urllib.request.Request(
        clean_url,
        headers={"User-Agent": _AGENT_REACH_USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read(2_000_001)
            if len(payload) > 2_000_000:
                return {
                    "ok": False,
                    "platform": "rss",
                    "error": "RSS response exceeded the 2 MB safety limit.",
                }
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        return {"ok": False, "platform": "rss", "error": f"Could not read RSS feed: {exc}"}

    parsed = feedparser.parse(payload)
    entries = []
    for entry in parsed.entries[:limit]:
        summary = re.sub(r"<[^>]+>", " ", str(entry.get("summary", "")))
        summary = html.unescape(re.sub(r"\s+", " ", summary)).strip()
        entries.append(
            {
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "published": entry.get("published", entry.get("updated", "")),
                "author": entry.get("author", ""),
                "summary": summary[:1_000],
            }
        )
    if not entries and getattr(parsed, "bozo", False):
        return {
            "ok": False,
            "platform": "rss",
            "error": f"Could not parse RSS feed: {parsed.bozo_exception}",
        }
    return {
        "ok": True,
        "platform": "rss",
        "backend": "feedparser",
        "feed": {
            "title": parsed.feed.get("title", ""),
            "url": clean_url,
            "description": parsed.feed.get("description", ""),
        },
        "entries": entries,
        "count": len(entries),
    }


def _agent_reach_v2ex_sync(
    section: str,
    identifier: str,
    limit: int,
) -> dict[str, Any]:
    base_url = "https://www.v2ex.com/api"
    if section == "hot":
        url = f"{base_url}/topics/hot.json"
    elif section == "node":
        if not identifier.strip():
            return {"ok": False, "platform": "v2ex", "error": "node requires an identifier."}
        query = urllib.parse.urlencode({"node_name": identifier.strip(), "page": 1})
        url = f"{base_url}/topics/show.json?{query}"
    elif section == "topic":
        topic_match = re.search(r"(?:/t/)?(\d+)$", identifier.strip())
        if not topic_match:
            return {"ok": False, "platform": "v2ex", "error": "A V2EX topic ID is required."}
        topic_id = topic_match.group(1)
        url = f"{base_url}/topics/show.json?" + urllib.parse.urlencode({"id": topic_id})
    elif section == "user":
        if not identifier.strip():
            return {"ok": False, "platform": "v2ex", "error": "user requires a username."}
        url = f"{base_url}/members/show.json?" + urllib.parse.urlencode(
            {"username": identifier.strip()}
        )
    else:
        return {
            "ok": False,
            "platform": "v2ex",
            "error": "V2EX section must be hot, node, topic, or user.",
        }

    fetched = _fetch_agent_reach_text(url, max_chars=100_000)
    if not fetched.get("ok"):
        fetched.update({"platform": "v2ex", "backend": "V2EX public API"})
        return fetched
    try:
        data = json.loads(str(fetched.get("content", "")))
    except json.JSONDecodeError as exc:
        return {"ok": False, "platform": "v2ex", "error": f"Invalid V2EX response: {exc}"}
    if isinstance(data, list):
        data = data[:limit]
    return {
        "ok": True,
        "platform": "v2ex",
        "backend": "V2EX public API",
        "section": section,
        "data": data,
    }


def _agent_reach_bilibili_search_sync(query: str, limit: int) -> dict[str, Any]:
    search_url = "https://api.bilibili.com/x/web-interface/search/all/v2?" + urllib.parse.urlencode(
        {"keyword": query, "page": 1}
    )
    fetched = _fetch_agent_reach_text(search_url, max_chars=500_000)
    if not fetched.get("ok"):
        fetched.update({"platform": "bilibili", "backend": "Bilibili search API"})
        return fetched
    try:
        payload = json.loads(str(fetched.get("content", "")))
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "platform": "bilibili",
            "backend": "Bilibili search API",
            "error": f"Invalid Bilibili response: {exc}",
        }
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return {
            "ok": False,
            "platform": "bilibili",
            "backend": "Bilibili search API",
            "error": str(payload.get("message", "Bilibili search failed."))
            if isinstance(payload, dict)
            else "Bilibili search returned an unexpected response.",
        }

    result_groups = payload.get("data", {}).get("result", [])
    videos: list[dict[str, Any]] = []
    for group in result_groups if isinstance(result_groups, list) else []:
        if not isinstance(group, dict) or group.get("result_type") != "video":
            continue
        for item in group.get("data", []):
            if not isinstance(item, dict):
                continue
            title = html.unescape(re.sub(r"<[^>]+>", "", str(item.get("title", ""))))
            bvid = str(item.get("bvid", ""))
            videos.append(
                {
                    "bvid": bvid,
                    "title": title,
                    "url": item.get("arcurl") or (f"https://www.bilibili.com/video/{bvid}" if bvid else ""),
                    "author": item.get("author", ""),
                    "description": item.get("description", ""),
                    "duration": item.get("duration", ""),
                    "play": item.get("play", 0),
                    "danmaku": item.get("danmaku", 0),
                    "published_at": item.get("pubdate", 0),
                }
            )
            if len(videos) >= limit:
                break
        if len(videos) >= limit:
            break
    return {
        "ok": True,
        "platform": "bilibili",
        "backend": "Bilibili search API",
        "results": videos,
        "count": len(videos),
    }


@function_tool
async def agent_reach_status() -> dict[str, Any]:
    """Checks Agent Reach channels and reports the active backend for each platform."""
    result = await _run_agent_reach_command(
        "agent-reach",
        "agent-reach",
        ["doctor", "--json"],
        timeout=90,
        max_output_chars=50_000,
    )
    if not result.get("ok"):
        result["setup_hint"] = _agent_reach_setup_hint()
        result["detected_commands"] = {
            name: bool(shutil.which(name))
            for name in (
                "mcporter",
                "twitter",
                "xreach",
                "yt-dlp",
                "bili",
                "gh",
                "opencli",
                "rdt",
                "xhs",
            )
        }
        return result
    try:
        channels = json.loads(str(result.pop("content", "{}")))
    except json.JSONDecodeError as exc:
        return {
            **result,
            "ok": False,
            "error": f"agent-reach doctor returned invalid JSON: {exc}",
        }
    return {**result, "channels": channels}


@function_tool
async def agent_reach_search(
    query: Annotated[str, "Search query."],
    platform: Annotated[
        str,
        "Platform: web, twitter, youtube, bilibili, reddit, github, xiaohongshu, "
        "wechat, weibo, linkedin, instagram, facebook, or v2ex.",
    ] = "web",
    limit: Annotated[int, "Number of results, from 1 to 20."] = 5,
    search_kind: Annotated[
        str,
        "Optional subtype: web/code; GitHub repos/code/issues/prs; LinkedIn people/jobs.",
    ] = "content",
) -> dict[str, Any]:
    """Searches an allow-listed internet platform through Agent Reach backends."""
    clean_query = query.strip()
    if not clean_query:
        return {"ok": False, "error": "query cannot be empty."}
    try:
        clean_limit = _agent_reach_limit(limit)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    platform_name = _normalize_agent_reach_platform(platform)
    kind = search_kind.strip().lower().replace("-", "_")

    if platform_name == "web":
        if kind == "code":
            candidates = [
                _mcp_candidate(
                    "Exa code search",
                    "exa.get_code_context_exa",
                    query=clean_query,
                    tokensNum=max(1_000, min(10_000, clean_limit * 600)),
                )
            ]
        else:
            candidates = [
                _mcp_candidate(
                    "Exa web search",
                    "exa.web_search_exa",
                    query=clean_query,
                    numResults=clean_limit,
                )
            ]
        return await _run_agent_reach_candidates("web", candidates)

    if platform_name == "twitter":
        return await _run_agent_reach_candidates(
            platform_name,
            [
                ("twitter-cli", "twitter", ["search", clean_query, "-n", str(clean_limit), "--json"], 60),
                ("xreach", "xreach", ["search", clean_query, "-n", str(clean_limit), "--json"], 60),
                ("OpenCLI", "opencli", ["twitter", "search", clean_query, "-f", "yaml"], 90),
            ],
        )

    if platform_name == "youtube":
        return await _run_agent_reach_candidates(
            platform_name,
            [("yt-dlp", "yt-dlp", ["--dump-json", f"ytsearch{clean_limit}:{clean_query}"], 120)],
        )

    if platform_name == "bilibili":
        cli_result = await _run_agent_reach_candidates(
            platform_name,
            [
                ("bili-cli", "bili", ["search", clean_query, "--type", "video", "-n", str(clean_limit)], 90),
                ("OpenCLI", "opencli", ["bilibili", "search", clean_query, "-f", "yaml"], 90),
            ],
        )
        if cli_result.get("ok"):
            return cli_result
        api_result = await asyncio.to_thread(
            _agent_reach_bilibili_search_sync, clean_query, clean_limit
        )
        if api_result.get("ok"):
            api_result["fallback_from"] = cli_result.get(
                "attempts", cli_result.get("error")
            )
        return api_result

    if platform_name == "reddit":
        return await _run_agent_reach_candidates(
            platform_name,
            [
                ("OpenCLI", "opencli", ["reddit", "search", clean_query, "-f", "yaml"], 90),
                ("rdt-cli", "rdt", ["search", clean_query, "--limit", str(clean_limit)], 60),
            ],
        )

    if platform_name == "github":
        github_kind = {"content": "repos", "repositories": "repos", "repository": "repos"}.get(kind, kind)
        if github_kind not in {"repos", "code", "issues", "prs"}:
            return {"ok": False, "platform": platform_name, "error": "GitHub search_kind must be repos, code, issues, or prs."}
        arguments = ["search", github_kind, clean_query, "--limit", str(clean_limit)]
        if github_kind == "repos":
            arguments.extend(["--sort", "stars"])
        return await _run_agent_reach_candidates(
            platform_name,
            [("GitHub CLI", "gh", arguments, 60)],
        )

    if platform_name == "xiaohongshu":
        return await _run_agent_reach_candidates(
            platform_name,
            [
                ("OpenCLI", "opencli", ["xiaohongshu", "search", clean_query, "-f", "yaml"], 120),
                _mcp_candidate("xiaohongshu-mcp", "xiaohongshu.search_feeds", keyword=clean_query),
                ("xhs-cli", "xhs", ["search", clean_query], 90),
            ],
        )

    if platform_name == "wechat":
        script = (
            "import asyncio\n"
            "import json\n"
            "import sys\n"
            "from miku_ai import get_wexin_article\n"
            "async def main():\n"
            "    result = await get_wexin_article(sys.argv[1], int(sys.argv[2]))\n"
            "    print(json.dumps(result, ensure_ascii=False, default=str))\n"
            "asyncio.run(main())\n"
        )
        return await _run_agent_reach_candidates(
            platform_name,
            [("miku_ai", sys.executable, ["-c", script, clean_query, str(clean_limit)], 90)],
        )

    if platform_name == "weibo":
        return await _run_agent_reach_candidates(
            platform_name,
            [
                _mcp_candidate(
                    "Exa web search",
                    "exa.web_search_exa",
                    query=f"site:weibo.com {clean_query}",
                    numResults=clean_limit,
                )
            ],
        )

    if platform_name == "linkedin":
        method = "search_jobs" if kind == "jobs" else "search_people"
        return await _run_agent_reach_candidates(
            platform_name,
            [
                _mcp_candidate(
                    "linkedin-scraper MCP",
                    f"linkedin-scraper.{method}",
                    keyword=clean_query,
                    limit=clean_limit,
                ),
                _mcp_candidate(
                    "linkedin MCP (legacy)",
                    f"linkedin.{method}",
                    keyword=clean_query,
                    limit=clean_limit,
                ),
            ],
        )

    if platform_name in {"instagram", "facebook"}:
        return await _run_agent_reach_candidates(
            platform_name,
            [("OpenCLI", "opencli", [platform_name, "search", clean_query, "-f", "yaml"], 90)],
        )

    if platform_name == "v2ex":
        return await _run_agent_reach_candidates(
            platform_name,
            [
                _mcp_candidate(
                    "Exa web search",
                    "exa.web_search_exa",
                    query=f"site:v2ex.com {clean_query}",
                    numResults=clean_limit,
                )
            ],
        )

    return {
        "ok": False,
        "error": f"Unsupported search platform {platform!r}. Supported platforms: {_AGENT_REACH_PLATFORMS}.",
    }


@function_tool
async def agent_reach_read(
    target: Annotated[str, "URL, post ID, repository, username, or other platform identifier."],
    platform: Annotated[str, "Platform name, or auto to detect it from a URL."] = "auto",
    content_kind: Annotated[
        str,
        "Content type such as content, transcript, metadata, thread, article, comments, "
        "profile, company, issue, pull_request, or download_link.",
    ] = "content",
    context_token: Annotated[
        str,
        "Optional platform context token, such as a XiaoHongShu xsec_token from search results.",
    ] = "",
    include_comments: Annotated[bool, "Include comments when the backend supports them."] = False,
    languages: Annotated[str, "Comma-separated preferred subtitle languages."] = "zh-Hans,zh,en",
    max_chars: Annotated[int, "Maximum returned content characters, from 1000 to 30000."] = 20_000,
) -> dict[str, Any]:
    """Reads content from a URL or platform identifier using Agent Reach routing."""
    clean_target = target.strip()
    if not clean_target:
        return {"ok": False, "error": "target cannot be empty."}
    if max_chars < 1_000 or max_chars > _AGENT_REACH_MAX_OUTPUT_CHARS:
        return {"ok": False, "error": "max_chars must be between 1000 and 30000."}

    platform_name = _normalize_agent_reach_platform(platform)
    if platform_name == "auto":
        platform_name = _agent_reach_platform_from_target(clean_target)
    kind = content_kind.strip().lower().replace("-", "_")

    if platform_name == "web":
        return await _agent_reach_jina_read(clean_target, max_chars)

    if platform_name == "twitter":
        twitter_action = {
            "article": "article",
            "profile": "user",
            "user": "user",
            "user_posts": "user-posts",
        }.get(kind, "tweet")
        xreach_action = "thread" if kind == "thread" else ("tweets" if kind == "user_posts" else "tweet")
        opencli_action = "article" if kind == "article" else ("user-posts" if kind == "user_posts" else "tweet")
        return await _run_agent_reach_candidates(
            platform_name,
            [
                ("twitter-cli", "twitter", [twitter_action, clean_target, "--json"], 90),
                ("xreach", "xreach", [xreach_action, clean_target, "--json"], 90),
                ("OpenCLI", "opencli", ["twitter", opencli_action, clean_target, "-f", "yaml"], 90),
            ],
            max_output_chars=max_chars,
        )

    if platform_name == "youtube":
        if kind in {"content", "transcript", "subtitles"}:
            return await _agent_reach_youtube_transcript(clean_target, languages, max_chars)
        arguments = ["--dump-single-json", "--no-playlist"]
        if kind == "comments" or include_comments:
            arguments.extend(
                ["--write-comments", "--extractor-args", "youtube:max_comments=20"]
            )
        arguments.append(clean_target)
        return await _run_agent_reach_candidates(
            platform_name,
            [("yt-dlp", "yt-dlp", arguments, 120)],
            max_output_chars=max_chars,
        )

    if platform_name == "bilibili":
        if kind in {"transcript", "subtitles"}:
            candidates = [
                ("OpenCLI", "opencli", ["bilibili", "subtitle", clean_target, "-f", "yaml"], 120)
            ]
        else:
            candidates = [
                ("bili-cli", "bili", ["video", clean_target], 90),
                ("OpenCLI", "opencli", ["bilibili", "video", clean_target, "-f", "yaml"], 90),
            ]
        return await _run_agent_reach_candidates(
            platform_name, candidates, max_output_chars=max_chars
        )

    if platform_name == "reddit":
        return await _run_agent_reach_candidates(
            platform_name,
            [
                ("OpenCLI", "opencli", ["reddit", "read", clean_target, "-f", "yaml"], 120),
                ("rdt-cli", "rdt", ["read", clean_target], 90),
            ],
            max_output_chars=max_chars,
        )

    if platform_name == "github":
        if kind in {"issue", "pull_request", "pr"}:
            item_kind = "issue" if kind == "issue" else "pull_request"
            item = _agent_reach_github_item(clean_target, item_kind)
            if item is None:
                return {
                    "ok": False,
                    "platform": platform_name,
                    "error": "Use owner/repo#number or a full GitHub issue/PR URL.",
                }
            repo, number = item
            arguments = ["issue" if item_kind == "issue" else "pr", "view", number, "-R", repo]
        else:
            arguments = ["repo", "view", _agent_reach_github_repo(clean_target)]
        return await _run_agent_reach_candidates(
            platform_name,
            [("GitHub CLI", "gh", arguments, 60)],
            max_output_chars=max_chars,
        )

    if platform_name == "xiaohongshu":
        if kind == "comments":
            opencli_action = "comments"
            xhs_action = "comments"
        else:
            opencli_action = "note"
            xhs_action = "read"
        candidates: list[tuple[str, str, list[str], float]] = [
            ("OpenCLI", "opencli", ["xiaohongshu", opencli_action, clean_target, "-f", "yaml"], 120)
        ]
        if context_token:
            candidates.append(
                _mcp_candidate(
                    "xiaohongshu-mcp",
                    "xiaohongshu.get_feed_detail",
                    feed_id=clean_target,
                    xsec_token=context_token,
                    load_all_comments=include_comments or kind == "comments",
                )
            )
        candidates.append(("xhs-cli", "xhs", [xhs_action, clean_target], 90))
        return await _run_agent_reach_candidates(
            platform_name, candidates, max_output_chars=max_chars
        )

    if platform_name == "douyin":
        method = "get_douyin_download_link" if kind == "download_link" else "parse_douyin_video_info"
        return await _run_agent_reach_candidates(
            platform_name,
            [_mcp_candidate("douyin MCP", f"douyin.{method}", share_link=clean_target)],
            max_output_chars=max_chars,
        )

    if platform_name == "wechat":
        article_reader = Path.home() / ".agent-reach" / "tools" / "wechat-article-for-ai" / "main.py"
        if not article_reader.is_file():
            return {
                "ok": False,
                "platform": platform_name,
                "error": (
                    "The Agent Reach WeChat article reader is not installed. "
                    "Configure the WeChat channel before reading mp.weixin.qq.com URLs."
                ),
            }
        return await _run_agent_reach_command(
            "Camoufox WeChat reader",
            sys.executable,
            [str(article_reader), clean_target],
            timeout=120,
            max_output_chars=max_chars,
        )

    if platform_name == "weibo":
        result = await _agent_reach_jina_read(clean_target, max_chars)
        result["platform"] = platform_name
        return result

    if platform_name == "linkedin":
        method = "get_company_profile" if kind == "company" else "get_person_profile"
        argument_name = "linkedin_url"
        mcp_result = await _run_agent_reach_candidates(
            platform_name,
            [
                _mcp_candidate("linkedin-scraper MCP", f"linkedin-scraper.{method}", **{argument_name: clean_target}),
                _mcp_candidate("linkedin MCP (legacy)", f"linkedin.{method}", **{argument_name: clean_target}),
            ],
            max_output_chars=max_chars,
        )
        if mcp_result.get("ok"):
            return mcp_result
        jina_result = await _agent_reach_jina_read(clean_target, max_chars)
        jina_result["platform"] = platform_name
        jina_result["fallback_from"] = mcp_result.get("attempts", mcp_result.get("error"))
        return jina_result

    if platform_name == "instagram":
        action = "user" if kind in {"content", "posts", "user_posts"} else "profile"
        return await _run_agent_reach_candidates(
            platform_name,
            [("OpenCLI", "opencli", ["instagram", action, clean_target, "-f", "yaml"], 90)],
            max_output_chars=max_chars,
        )

    if platform_name == "facebook":
        return await _run_agent_reach_candidates(
            platform_name,
            [("OpenCLI", "opencli", ["facebook", "profile", clean_target, "-f", "yaml"], 90)],
            max_output_chars=max_chars,
        )

    if platform_name == "v2ex":
        section = "user" if kind in {"profile", "user"} else "topic"
        return await asyncio.to_thread(_agent_reach_v2ex_sync, section, clean_target, 20)

    if platform_name == "rss":
        return await asyncio.to_thread(_agent_reach_rss_sync, clean_target, 20)

    return {
        "ok": False,
        "error": f"Unsupported read platform {platform!r}. Supported platforms: {_AGENT_REACH_PLATFORMS}.",
    }


@function_tool
async def agent_reach_browse(
    platform: Annotated[
        str,
        "Platform: twitter, reddit, bilibili, xiaohongshu, instagram, facebook, v2ex, or github.",
    ],
    section: Annotated[
        str,
        "Section such as feed, user_posts, hot, popular, subreddit, rank, explore, saved, "
        "groups, node, issues, prs, runs, or releases.",
    ] = "feed",
    identifier: Annotated[str, "Optional username, subreddit, node, topic ID, or repository."] = "",
    limit: Annotated[int, "Number of items, from 1 to 20."] = 10,
) -> dict[str, Any]:
    """Browses read-only timelines, hot lists, communities, and repository lists."""
    try:
        clean_limit = _agent_reach_limit(limit)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    platform_name = _normalize_agent_reach_platform(platform)
    clean_section = section.strip().lower().replace("-", "_")
    clean_identifier = identifier.strip()

    if platform_name == "twitter":
        if clean_section == "user_posts":
            if not clean_identifier:
                return {"ok": False, "platform": platform_name, "error": "user_posts requires a username."}
            twitter_args = ["user-posts", clean_identifier, "-n", str(clean_limit), "--json"]
            xreach_args = ["tweets", clean_identifier, "-n", str(clean_limit), "--json"]
            opencli_args = ["twitter", "user-posts", clean_identifier, "-f", "yaml"]
        else:
            twitter_args = ["feed", "-n", str(clean_limit), "--json"]
            xreach_args = ["tweets", clean_identifier or "@", "-n", str(clean_limit), "--json"]
            opencli_args = ["twitter", "feed", "--limit", str(clean_limit), "-f", "yaml"]
        return await _run_agent_reach_candidates(
            platform_name,
            [
                ("twitter-cli", "twitter", twitter_args, 90),
                ("xreach", "xreach", xreach_args, 90),
                ("OpenCLI", "opencli", opencli_args, 90),
            ],
        )

    if platform_name == "reddit":
        if clean_section == "subreddit":
            if not clean_identifier:
                return {"ok": False, "platform": platform_name, "error": "subreddit requires a name."}
            opencli_args = ["reddit", "subreddit", clean_identifier, "-f", "yaml"]
            rdt_args = ["sub", clean_identifier, "--limit", str(clean_limit)]
        elif clean_section in {"hot", "popular"}:
            opencli_args = ["reddit", clean_section, "-f", "yaml"]
            rdt_args = [clean_section, "--limit", str(clean_limit)]
        else:
            return {"ok": False, "platform": platform_name, "error": "Reddit section must be hot, popular, or subreddit."}
        return await _run_agent_reach_candidates(
            platform_name,
            [("OpenCLI", "opencli", opencli_args, 90), ("rdt-cli", "rdt", rdt_args, 90)],
        )

    if platform_name == "bilibili":
        if clean_section not in {"hot", "rank"}:
            return {"ok": False, "platform": platform_name, "error": "Bilibili section must be hot or rank."}
        return await _run_agent_reach_candidates(
            platform_name,
            [("bili-cli", "bili", [clean_section, "-n", str(clean_limit)], 90)],
        )

    if platform_name == "xiaohongshu":
        if clean_section in {"user", "comments"} and not clean_identifier:
            return {"ok": False, "platform": platform_name, "error": f"{clean_section} requires an identifier."}
        opencli_args = ["xiaohongshu", clean_section]
        xhs_args = [clean_section]
        if clean_identifier:
            opencli_args.append(clean_identifier)
            xhs_args.append(clean_identifier)
        opencli_args.extend(["-f", "yaml"])
        return await _run_agent_reach_candidates(
            platform_name,
            [
                ("OpenCLI", "opencli", opencli_args, 120),
                ("xhs-cli", "xhs", xhs_args, 90),
            ],
        )

    if platform_name == "instagram":
        if clean_section not in {"explore", "saved", "user"}:
            return {"ok": False, "platform": platform_name, "error": "Instagram section must be explore, saved, or user."}
        arguments = ["instagram", clean_section]
        if clean_section == "user":
            if not clean_identifier:
                return {"ok": False, "platform": platform_name, "error": "user requires a username."}
            arguments.append(clean_identifier)
        arguments.extend(["--limit", str(clean_limit), "-f", "yaml"])
        return await _run_agent_reach_candidates(
            platform_name, [("OpenCLI", "opencli", arguments, 90)]
        )

    if platform_name == "facebook":
        if clean_section not in {"feed", "groups"}:
            return {"ok": False, "platform": platform_name, "error": "Facebook section must be feed or groups."}
        return await _run_agent_reach_candidates(
            platform_name,
            [("OpenCLI", "opencli", ["facebook", clean_section, "--limit", str(clean_limit), "-f", "yaml"], 90)],
        )

    if platform_name == "v2ex":
        return await asyncio.to_thread(
            _agent_reach_v2ex_sync,
            clean_section,
            clean_identifier,
            clean_limit,
        )

    if platform_name == "github":
        if clean_section not in {"issues", "prs", "runs", "releases"}:
            return {"ok": False, "platform": platform_name, "error": "GitHub section must be issues, prs, runs, or releases."}
        if not clean_identifier:
            return {"ok": False, "platform": platform_name, "error": "GitHub browsing requires owner/repo."}
        repo = _agent_reach_github_repo(clean_identifier)
        if clean_section == "issues":
            arguments = ["issue", "list", "-R", repo, "--limit", str(clean_limit)]
        elif clean_section == "prs":
            arguments = ["pr", "list", "-R", repo, "--limit", str(clean_limit)]
        elif clean_section == "runs":
            arguments = ["run", "list", "--repo", repo, "--limit", str(clean_limit)]
        else:
            arguments = ["release", "list", "-R", repo, "--limit", str(clean_limit)]
        return await _run_agent_reach_candidates(
            platform_name, [("GitHub CLI", "gh", arguments, 60)]
        )

    return {
        "ok": False,
        "error": "browse supports twitter, reddit, bilibili, xiaohongshu, instagram, facebook, v2ex, and github.",
    }


@function_tool
async def agent_reach_rss(
    url: Annotated[str, "Public RSS or Atom feed URL."],
    limit: Annotated[int, "Number of entries, from 1 to 20."] = 5,
) -> dict[str, Any]:
    """Reads a public RSS or Atom feed through Agent Reach's feedparser backend."""
    try:
        clean_limit = _agent_reach_limit(limit)
    except ValueError as exc:
        return {"ok": False, "platform": "rss", "error": str(exc)}
    return await asyncio.to_thread(_agent_reach_rss_sync, url, clean_limit)
