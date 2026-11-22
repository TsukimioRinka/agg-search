#!/usr/bin/env python3
"""Optional Agent-Reach bridge: platform-native recall and reads.

Agent-Reach (https://github.com/Panniantong/Agent-Reach) is a separate CLI
with one JSON envelope per call. It covers platforms a web search index
cannot reach: GitHub, YouTube, Bilibili, V2EX and (when the user adds them)
Reddit, X, Xiaohongshu and others. This module shells out to that binary;
it never imports Agent-Reach and adds no dependency to agg-search.

Set AGENT_REACH_BIN when the binary is not on PATH. When it is missing,
search returns `(None, "no agent-reach")` so callers report a skip and the
rest of the fusion pipeline keeps running.

Platform text is untrusted page content: every field is stripped of
control/invisible characters before it reaches a snippet.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.parse

BIN_ENV = "AGENT_REACH_BIN"
DEFAULT_BIN = "agent-reach"
DEFAULT_TIMEOUT = 8.0
MAX_LIMIT = 20

# engine name (engine_specs) -> Agent-Reach channel
CHANNEL_ENGINES = {
    "ar_github": "github",
    "ar_youtube": "youtube",
    "ar_bilibili": "bilibili",
}

# host suffix -> channel for `read`; default channels only. Optional
# channels are mapped too: when a user adds one, the rung starts working
# without another code change.
READ_HOSTS = (
    ("bilibili.com", "bilibili"),
    ("b23.tv", "bilibili"),
    ("youtube.com", "youtube"),
    ("youtu.be", "youtube"),
    ("github.com", "github"),
    ("githubusercontent.com", "github"),
    ("v2ex.com", "v2ex"),
    ("reddit.com", "reddit"),
    ("redd.it", "reddit"),
    ("xiaohongshu.com", "xiaohongshu"),
    ("xhslink.com", "xiaohongshu"),
    ("x.com", "twitter"),
    ("twitter.com", "twitter"),
    ("xueqiu.com", "xueqiu"),
    ("linkedin.com", "linkedin"),
)

_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ERROR_WORDS = {
    # Agent-Reach error code -> a phrase search.classify_error understands.
    "RATE_LIMITED": "quota",
    "TIMEOUT": "timeout",
    "PLATFORM_BLOCKED": "antibot",
}


def binary():
    return os.environ.get(BIN_ENV) or DEFAULT_BIN


def _clean(text):
    return _CONTROL.sub("", _INVISIBLE.sub("", str(text or ""))).strip()


def _last_json(stdout):
    """Agent-Reach prints one JSON line; tolerate extra notice lines."""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and "ok" in payload:
            return payload
    return None


def _run(channel, action, positional, opts, timeout):
    """(payload, error). Errors use the vocabulary of classify_error.

    `opts` go before `--timeout`; everything after `--` is positional, so a
    query or URL that starts with "-" cannot become a flag."""
    cmd = [binary(), channel, action, *opts,
           "--timeout", "%.1f" % timeout, "--", *positional]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=float(timeout) + 3.0,
        )
    except FileNotFoundError:
        return None, "no agent-reach (install it or set %s)" % BIN_ENV
    except subprocess.TimeoutExpired:
        return None, "timeout: agent-reach %s exceeded %.0fs" % (
            channel, float(timeout) + 3.0)
    except OSError as exc:
        return None, "agent-reach unavailable: %s: %s" % (
            type(exc).__name__, exc)
    payload = _last_json(proc.stdout)
    if payload is None:
        detail = _clean((proc.stderr or proc.stdout or ""))[-200:]
        return None, "agent-reach %s returned unreadable output%s" % (
            channel, (": " + detail) if detail else " (exit %d)" % proc.returncode)
    if payload.get("ok"):
        return payload, None
    err = payload.get("error") or {}
    code = str(err.get("code") or "UPSTREAM_BROKEN")
    message = _clean(err.get("message") or code)[:300]
    word = _ERROR_WORDS.get(code)
    if word:
        return None, "%s: agent-reach %s: %s" % (word, channel, message)
    return None, "agent-reach %s: %s: %s" % (code, channel, message)


def search(engine, query, limit, timeout=DEFAULT_TIMEOUT):
    """(rows, error) for one engine, rows in the `(url, title, snippet)` shape."""
    channel = CHANNEL_ENGINES.get(engine)
    if not channel:
        return None, "no agent-reach channel for %s" % engine
    query = (query or "").strip()
    if not query:
        return [], None
    limit = max(1, min(int(limit or 5), MAX_LIMIT))
    # `--` keeps a query that starts with "-" from becoming a flag.
    payload, err = _run(channel, "search", [query],
                        ["--limit", str(limit)], timeout)
    if err:
        return None, err
    rows, seen = [], set()
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        url = _clean(item.get("url"))
        if not url.startswith(("http://", "https://")):
            continue
        key = url.split("#", 1)[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        title = _clean(item.get("title"))
        text = _clean(item.get("text"))
        if not text:
            raw = item.get("raw")
            if isinstance(raw, dict):
                text = _clean(raw.get("description") or raw.get("content"))
        author = _clean(item.get("author"))
        date = _clean(item.get("published_at"))[:10]
        prefix = " · ".join(part for part in (author, date) if part)
        if text and prefix:
            snippet = "[%s] %s" % (prefix, text)
        elif text:
            snippet = text
        elif prefix:
            snippet = "[%s]" % prefix
        else:
            snippet = title or url
        rows.append((url, (title or snippet[:80])[:300], snippet[:400]))
    return rows, None


def channel_for(url):
    """Agent-Reach channel that can read this URL, or None."""
    try:
        host = (urllib.parse.urlsplit((url or "").strip()).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    for suffix, channel in READ_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return channel
    return None


def read(url, timeout=20.0, text_limit=8000):
    """(result, error) shaped like the container ladder's page dicts."""
    channel = channel_for(url)
    if not channel:
        return None, "no agent-reach channel for this host"
    payload, err = _run(channel, "read", [url], [], timeout)
    if err:
        return None, err
    items = [item for item in (payload.get("items") or [])
             if isinstance(item, dict)]
    if not items:
        return None, "agent-reach %s returned no items" % channel
    item = items[0]
    text = _clean(item.get("text"))
    if not text:
        raw = item.get("raw")
        if isinstance(raw, dict):
            text = _clean(raw.get("description") or raw.get("content"))
    if not text or text in {"-", "--", "—", "…", "..."}:
        return None, "agent-reach %s returned no text" % channel
    return {
        "url": url,
        "tier_used": "agent-reach:%s" % channel,
        "status": 200,
        "title": _clean(item.get("title"))[:300],
        "text": text[:max(500, int(text_limit))],
        "markdown": "",
        "blocked": False,
        "error": "",
    }, None
