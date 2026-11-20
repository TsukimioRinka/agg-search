#!/usr/bin/env python3
"""Aggregate search: parallel multi-engine recall + RRF fusion + Jina rerank.

Engines: exa, brave, serper (Google SERP bridge), firecrawl, tavily,
anysearch (MCP-over-HTTP), searxng (self-hosted), deepseek (official
in-house engine, POST /v1/tools/search; results arrive pre-scored with
rerank_score), plus three keyless domain sources: openalex (academic
works), hn (Hacker News via Algolia) and stack (Stack Exchange).
Stdlib only, no dependencies. Any single engine may fail or
time out without blocking the rest; fusion degrades gracefully.

Environment (keys also fall back to ~/.config/agent-env and
~/.config/ai-keys.env):
  EXA_API_KEY, BRAVE_API_KEY, MCP_ANYSEARCH_API_KEY,
  SERPER_API_KEY (optional, skipped when absent),
  FIRECRAWL_API_KEY (optional, skipped when absent),
  TAVILY_API_KEY (+ TAVILY_API_KEY_BACKUP failover on key-limit errors),
  SEARXNG_URL (optional, default http://127.0.0.1:8080),
  DEEPSEEK_API_KEY (optional, skipped when absent),
  DEEPSEEK_BASE_URL (optional, default https://api.deepseek.com),
  JINA_API_KEY (optional; absent/disabled/failed -> RRF order kept),
  JINA_RERANK_MODEL (optional, default jina-reranker-v3.5; the chain
  falls back v3 -> v2-base-multilingual when a model is unavailable).

Config defaults may come from the environment instead of flags:
AGG_SEARCH_ENGINES (comma list), AGG_SEARCH_TOP_N, AGG_SEARCH_PER_ENGINE,
AGG_SEARCH_SNIPPET_CHARS, AGG_SEARCH_FAST, AGG_SEARCH_NO_JINA,
AGG_SEARCH_PROFILE and AGG_SEARCH_NO_LEARN (1/true/yes). They apply only
when the matching flag is omitted, and invalid values are ignored.
`--profile` presets engines/weights/recency/snippets for a domain
(academic, code, news); `--show-config` prints the effective settings
as JSON (credentials as booleans, never key values) and exits before any
network call; `--json` output carries per-engine latency and phase timings
under engine_health.
"""
import argparse
import concurrent.futures as cf
import datetime
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from xml.etree import ElementTree

try:  # package import (installed, `python -m agg_search.search`)
    from . import engine_specs, learn, portals, profiles
except ImportError:  # pragma: no cover - direct script execution
    import engine_specs  # type: ignore[no-redef]
    import learn  # type: ignore[no-redef]
    import portals  # type: ignore[no-redef]
    import profiles  # type: ignore[no-redef]

ENV_FILES = (os.path.expanduser("~/.config/agent-env"),
             os.path.expanduser("~/.config/ai-keys.env"))
# Engine bodies are a few hundred KB at most; a compromised or MITM'd
# endpoint must not be able to OOM the process.
MAX_RESPONSE = 4 * 1024 * 1024
ENGINE_TIMEOUT = 25
RRF_K = 60
# preference order; JINA_RERANK_MODEL overrides the head of the chain
JINA_MODELS = ("jina-reranker-v3.5", "jina-reranker-v3",
               "jina-reranker-v2-base-multilingual")
# --- resilience layer (incremental, default behavior unchanged) ---
CACHE_DIR = os.environ.get("AGG_SEARCH_CACHE_DIR",
                           os.path.expanduser("~/.cache/agg-search"))
CACHE_PATH = os.path.join(CACHE_DIR, "results_cache.json")
COOLDOWN_PATH = os.path.join(CACHE_DIR, "cooldown.json")
CACHE_TTL = 300  # 5min LRU, mirrors dsh-free-search
MAX_CACHE_ENTRIES = 50
# failure category -> cooldown seconds; empty/healthy never cools
COOLDOWN_TTL = {"quota": 3600, "antibot": 600, "timeout": 90}
# engine -> env vars proving credentials (any one suffices); absent = keyless.
# Derived from engine_specs so the CLI, MCP schema and portals cannot drift.
CREDENTIAL_ENV = engine_specs.credential_env()
# last SearXNG unresponsive_engines report [[name, reason], ...]
LAST_SEARXNG_UNRESPONSIVE = []
# last per-engine wall time in ms (engine -> ms); variant runs fold by max.
# Written by recall/recall_multi/recall_flat, read by main() for --json.
LAST_TIMINGS = {}
# CLI defaults; AGG_SEARCH_* env vars may replace them when a flag is omitted
DEFAULT_ENGINES = engine_specs.default_engines()
DEFAULT_TOP_N = 10
DEFAULT_PER_ENGINE = 5
DEFAULT_SNIPPET_CHARS = 400
_TRUTHY = ("1", "true", "yes")
# effective --fast (flag or AGG_SEARCH_FAST); the script tail reads it so an
# env-driven budget is not voided by non-daemon pool joins at exit (P1-1)
_FAST_MODE = False


def _env_file_value(path, key):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r'\s*(?:export\s+)?' + re.escape(key) + r'=(.*)',
                             line)
                if m:
                    return m.group(1).strip().strip('"\'')
    except OSError:
        pass
    return None


def getenv(key, default=None):
    v = os.environ.get(key)
    if v:
        return v
    for path in ENV_FILES:
        v = _env_file_value(path, key)
        if v:
            return v
    return default
def missing_credential(eng):
    """Return the missing KEY name if eng needs one and has none, else None."""
    reqs = CREDENTIAL_ENV.get(eng)
    if not reqs:
        return None  # keyless (searxng) or unknown -> never prefiltered
    for k in reqs:
        if getenv(k):
            return None
    return reqs[0]


def classify_error(err):
    """Map an engine error string to (category, cooldown_secs).
    Categories: no_key/empty (0, never cool) vs quota/antibot/timeout."""
    s = str(err or "")
    if not s or s.startswith("no ") and "KEY" in s:
        return "no_key", 0
    low = s.lower()
    if ("402" in s or "432" in s or "401" in s or "403" in s
            or "quota" in low or "balance" in low or "insufficient" in low
            or "all tavily keys failed" in low):
        return "quota", COOLDOWN_TTL["quota"]
    if ("429" in s or "captcha" in low or "suspended" in low
            or "too many requests" in low or "bot" in low):
        return "antibot", COOLDOWN_TTL["antibot"]
    if ("timeout" in low or "timed out" in low or "408" in s
            or "500" in s or "502" in s or "503" in s or "504" in s):
        return "timeout", COOLDOWN_TTL["timeout"]
    return "unknown", 0


def load_cooldown():
    try:
        with open(COOLDOWN_PATH, encoding="utf-8") as f:
            d = json.load(f)
        now = time.time()
        return {k: v for k, v in d.items() if v > now}
    except (OSError, ValueError):
        return {}


def _atomic_write(path, obj):
    """tmp + os.replace with pid-unique tmp name and 0600 perms (P3-6)."""
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def save_cooldown(state):
    try:
        _atomic_write(COOLDOWN_PATH, state)
    except OSError:
        pass


def cache_key(query, engines, recency, per_engine, rrf_top, top_n, no_jina,
              expand, diversify, fast=False, budget_secs=30.0, min_sources=0,
              include_domains=(), exclude_domains=(), snippet_chars=400,
              extra=""):
    parts = [query, ",".join(sorted(engines)), recency or "",
             str(per_engine), str(rrf_top), str(top_n), str(bool(no_jina)),
             str(expand), str(bool(diversify)),
             "fast" if fast else "full", str(budget_secs), str(min_sources),
             ",".join(sorted(parse_domains(include_domains))),
             ",".join(sorted(parse_domains(exclude_domains))),
             str(clamp_snippet_chars(snippet_chars)),
             "%.1f" % ENGINE_TIMEOUT,
             extra,
             getenv("JINA_RERANK_MODEL", JINA_MODELS[0]),
             getenv("SEARXNG_URL", "http://127.0.0.1:8080"),
             getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:32]


def load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            store = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(store, dict):
        return {}
    clean = {}
    for k, v in store.items():  # drop malformed entries, never crash (sec-4)
        try:
            if (isinstance(v, dict) and isinstance(v.get("at"), (int, float))
                    and isinstance(v.get("results"), dict)
                    and isinstance(v.get("status"), dict)
                    and all(isinstance(r, list) and len(r) == 3
                            and all(isinstance(x, str) for x in r)
                            for rows in v["results"].values() for r in rows)):
                clean[k] = v
        except (AttributeError, TypeError):
            continue
    return clean


def save_cache(store):
    try:
        while len(store) > MAX_CACHE_ENTRIES:
            oldest = min(store, key=lambda k: store[k].get("at", 0))
            del store[oldest]
        _atomic_write(CACHE_PATH, store)
    except OSError:
        pass

_MULTI_SUFFIX = frozenset([
    "com.cn", "co.jp", "co.uk", "com.au", "co.kr", "com.br", "co.in",
    "com.tw", "com.hk", "co.nz", "co.za", "com.sg", "co.il",
])


def domain_of(url):
    """eTLD+1 for diversity grouping (stdlib only, trimmed suffix list)."""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return (url or "").lower()
    host = host.lower().strip(".").removeprefix("www.")
    if not host or "." not in host or ":" in host:
        return host or (url or "").lower()
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
        return host  # IPv4: grouping by octet-pair would be wrong, keep as-is
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    last2 = ".".join(parts[-2:])
    return ".".join(parts[-3:]) if last2 in _MULTI_SUFFIX else last2


def parse_domains(value):
    """Normalize a comma string (or iterable) of domain rules.
    Lowercased, whitespace-stripped, empty rules dropped, order kept."""
    if isinstance(value, str):
        raw = value.split(",")
    else:
        raw = list(value or ())
    out = []
    for item in raw:
        rule = str(item).strip().lower()
        if rule.strip(".") and rule not in out:
            out.append(rule)
    return tuple(out)


def domain_matches_rule(host, rule):
    """True when `host` equals `rule` or is a subdomain of it.
    Case-insensitive; leading dots are ignored, so `.edu` matches any host
    ending in `.edu` and `example.com` also matches its subdomains.
    Empty or dot-only rules never match."""
    h = (host or "").strip().lower().strip(".")
    r = (rule or "").strip().lower().strip(".")
    if not h or not r:
        return False
    return h == r or h.endswith("." + r)


def _filter_host(url):
    """Host of a result URL for rule matching; '' when it has none."""
    u = (url or "").strip()
    if not u:
        return ""
    try:
        host = urllib.parse.urlsplit(u).hostname
        if not host:  # schemeless "example.com/path"
            host = urllib.parse.urlsplit("//" + u).hostname
    except ValueError:
        host = None
    return (host or "").strip(".").lower()


def filter_rows(rows, include, exclude):
    """Apply domain rules to (url, title, snippet) rows.
    An empty include list means no allowlist restriction; otherwise a host
    must match at least one rule. Exclude always wins over include.
    Returns (kept_rows, removed_count)."""
    inc, exc = parse_domains(include), parse_domains(exclude)
    if not inc and not exc:
        return list(rows), 0
    kept, removed = [], 0
    for row in rows:
        host = _filter_host(row[0])
        if (any(domain_matches_rule(host, r) for r in exc)
                or (inc and not any(domain_matches_rule(host, r)
                                    for r in inc))):
            removed += 1
        else:
            kept.append(row)
    return kept, removed


def apply_domain_filter(results, include, exclude):
    """Filter every engine/variant row list before fusion; cache hits are
    filtered by the same call so stale data honours the current flags.
    Returns (filtered_results, {engine: removed}) with `engine#i` variant
    keys folded into the base engine name; a no-op without rules."""
    inc, exc = parse_domains(include), parse_domains(exclude)
    if not inc and not exc:
        return dict(results), {}
    out, counts = {}, {}
    for key, rows in results.items():
        kept, removed = filter_rows(rows, inc, exc)
        if removed:
            base = key.split("#", 1)[0]
            counts[base] = counts.get(base, 0) + removed
        if kept:
            out[key] = kept
    return out, counts


def clamp_snippet_chars(n):
    """Clamp --snippet-chars into the supported 80..2000 window."""
    try:
        return max(80, min(2000, int(n)))
    except (TypeError, ValueError):
        return 400


def clip_snippet(text, n):
    """Truncate a snippet for final rendering to `n` chars."""
    return (text or "")[:n]


def diversify_ranked(ranked):
    """Round-robin interleave by eTLD+1; reorders only, never drops."""
    groups, order = {}, []
    for item in ranked:
        d = domain_of(item[0])
        groups.setdefault(d, []).append(item)
        if d not in order:
            order.append(d)
    out, i = [], 0
    while len(out) < len(ranked):
        progressed = False
        for d in order:
            if i < len(groups[d]):
                out.append(groups[d][i])
                progressed = True
        if not progressed:
            break
        i += 1
    return out



class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn 3xx into an HTTPError.

    urllib follows redirects by itself and keeps every request header, so a
    302 from (or through) an engine endpoint would forward API keys to the
    redirect target. Refusing is cheaper than teaching urllib to redact.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _read_json(resp):
    raw = resp.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError("engine response larger than %d bytes"
                         % MAX_RESPONSE)
    return json.loads(raw.decode())


def http_json(url, *, method="GET", body=None, headers=None, timeout=None):
    if timeout is None:
        timeout = ENGINE_TIMEOUT
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    with _OPENER.open(req, timeout=_effective_timeout(timeout)) as r:
        return _read_json(r)


_TLS = threading.local()


def _effective_timeout(want):
    """Cap a per-call timeout by the active recall deadline (--fast only).
    No deadline set -> `want` unchanged, legacy behavior preserved."""
    dl = getattr(_TLS, "deadline", None)
    if dl is None:
        return want
    return max(0.5, min(want, dl - time.monotonic()))


def http_json_retry(url, *, body=None, headers=None, timeout=None):
    """POST with one retry for transient statuses (the DeepSeek guidance:
    retry 408/429/5xx; never retry other 4xx)."""
    if timeout is None:
        timeout = ENGINE_TIMEOUT
    try:
        return http_json(url, method="POST", body=body, headers=headers,
                         timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code not in (408, 429) and e.code < 500:
            raise
        time.sleep(1.2)
        return http_json(url, method="POST", body=body, headers=headers,
                         timeout=timeout)


def norm_url(u):
    return u.split("#", 1)[0].rstrip("/")


_TRACKING_PREFIXES = ("utm_", "ref", "fbclid", "gclid", "mc_cid", "mc_eid",
                      "spm")
_ARXIV_PATH_RE = re.compile(r"^/(?:abs|pdf|html)/([^/?#]+?)(?:\.pdf)?$")
_DEDUPE_STOP = frozenset({
    "a", "an", "the", "and", "or", "for", "of", "to", "in", "on", "with",
    "by", "from", "at", "is", "are", "as", "how", "what", "why", "when",
})


def canonical_url(url):
    """Canonical key collapsing scheme/www/tracking/arXiv mirrors."""
    raw = (url or "").strip()
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return norm_url(raw)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return norm_url(raw)
    scheme = ("https" if parts.scheme.lower() in ("http", "https")
              else parts.scheme.lower())
    path = parts.path or ""
    if host == "doi.org":
        m = re.match(r"^/10\.48550/arxiv\.([^/?#]+)$", path, re.I)
        if m:
            return "arxiv:" + re.sub(r"v\d+$", "", m.group(1).lower())
    if host in ("arxiv.org", "export.arxiv.org"):
        m = _ARXIV_PATH_RE.match(path)
        if m:
            return "arxiv:" + re.sub(r"v\d+$", "", m.group(1).lower())
    if parts.query:
        kept = [
            (k, v)
            for k, v in urllib.parse.parse_qsl(parts.query,
                                               keep_blank_values=True)
            if not any(k.lower().startswith(pref)
                       for pref in _TRACKING_PREFIXES)
        ]
        query = urllib.parse.urlencode(kept)
    else:
        query = ""
    path = path.rstrip("/")
    port = ""
    try:
        if parts.port and not ((scheme == "https" and parts.port == 443)
                               or (scheme == "http" and parts.port == 80)):
            port = ":%d" % parts.port
    except ValueError:
        pass
    return urllib.parse.urlunsplit((scheme, host + port, path, query, ""))


def _dedupe_tokens(text):
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if w not in _DEDUPE_STOP and len(w) > 1:
            out.add(w)
    for run in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        if len(run) == 1:
            out.add(run)
        else:
            for i in range(len(run) - 1):
                out.add(run[i:i + 2])
    return out


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_content_duplicate(t1, s1, t2, s2):
    """Conservative near-duplicate detector for substantive web results.

    Requires substantive title length (>=5 distinct tokens and >=25 chars) so
    short synthetic test strings never collapse, plus exact normalized title
    or high title + snippet agreement.
    """
    nt1 = re.sub(r"\s+", " ", (t1 or "").strip().lower())
    nt2 = re.sub(r"\s+", " ", (t2 or "").strip().lower())
    tt1, tt2 = _dedupe_tokens(t1), _dedupe_tokens(t2)
    if len(tt1) < 5 or len(tt2) < 5 or len(nt1) < 25 or len(nt2) < 25:
        return False
    if nt1 == nt2:
        return True
    tj = _jaccard(tt1, tt2)
    st1, st2 = _dedupe_tokens(s1), _dedupe_tokens(s2)
    if tj >= 0.92 and len(st1) >= 8 and len(st2) >= 8:
        return _jaccard(st1, st2) >= 0.50
    if tj >= 0.78 and len(st1) >= 12 and len(st2) >= 12:
        return _jaccard(st1, st2) >= 0.75
    return False


# Zero-width and bidi-control characters hide injected instructions in page
# text an agent is about to read (Trojan Source and friends); engine-supplied
# titles/snippets are cleaned before they reach the model.
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064"
                        "\u2066-\u2069\ufeff]")
# C0/C1 controls (keeping tab/newline) would otherwise reach a terminal or
# a log as ANSI escapes.
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_MD = re.compile(r"([\\`*_\[\]()])")


def clean_text(text):
    return _CONTROL.sub("", _INVISIBLE.sub("", text or ""))


def md_escape(text):
    """Escape the characters that can break out of `[title](url)`."""
    return _MD.sub(r"\\\1", text or "")


def eng_exa(q, n):
    key = getenv("EXA_API_KEY")
    if not key:
        return None, "no EXA_API_KEY"
    d = http_json("https://api.exa.ai/search", method="POST",
                  body={"query": q, "numResults": n,
                        "contents": {"text": {"maxCharacters": 600}}},
                  headers={"x-api-key": key})
    return ([(norm_url(r["url"]), r.get("title", ""),
              (r.get("text") or "")[:400]) for r in d.get("results", [])
             if r.get("url")], None)


def eng_brave(q, n, recency):
    key = getenv("BRAVE_API_KEY")
    if not key:
        return None, "no BRAVE_API_KEY"
    params = {"q": q, "count": min(n, 10), "resultFilter": "web"}
    if recency:
        params["freshness"] = {"day": "pd", "week": "pw",
                               "month": "pm", "year": "py"}[recency]
    url = ("https://api.search.brave.com/res/v1/web/search?"
           + urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"X-Subscription-Token": key})
    with _OPENER.open(req, timeout=_effective_timeout(ENGINE_TIMEOUT)) as r:
        d = _read_json(r)
    out = []
    for x in d.get("web", {}).get("results", [])[:n]:
        if not x.get("url"):
            continue
        snip = x.get("description", "")
        extra = x.get("extra_snippets") or []
        if extra:
            snip = (snip + " " + extra[0])[:400]
        out.append((norm_url(x["url"]), x.get("title", ""), snip))
    return out, None


def eng_serper(q, n, recency):
    """Google SERP bridge (serper.dev); 2500 free queries, then $1/1k."""
    key = getenv("SERPER_API_KEY")
    if not key:
        return None, "no SERPER_API_KEY"
    body = {"q": q, "num": min(n, 10)}
    if recency:
        body["tbs"] = {"day": "qdr:d", "week": "qdr:w",
                       "month": "qdr:m", "year": "qdr:y"}[recency]
    d = http_json("https://google.serper.dev/search", method="POST",
                  body=body, headers={"X-API-KEY": key})
    out = []
    for src in ("organic", "news"):
        for r in d.get(src) or []:
            if r.get("link"):
                out.append((norm_url(r["link"]), r.get("title", ""),
                            (r.get("snippet") or r.get("description") or "")
                            [:400]))
    return out[:n], None


def eng_firecrawl(q, n, recency):
    key = getenv("FIRECRAWL_API_KEY")
    if not key:
        return None, "no FIRECRAWL_API_KEY"
    body = {"query": q, "limit": min(n, 10)}
    if recency:  # Google-style time filter, verified live
        body["tbs"] = {"day": "qdr:d", "week": "qdr:w",
                       "month": "qdr:m", "year": "qdr:y"}[recency]
    d = http_json("https://api.firecrawl.dev/v2/search", method="POST",
                  body=body, headers={"Authorization": "Bearer " + key})
    data = d.get("data") or {}
    out = []
    for src in ("web", "news"):
        for r in data.get(src) or []:
            if r.get("url"):
                out.append((norm_url(r["url"]), r.get("title", ""),
                            (r.get("description") or "")[:400]))
    return out[:n], None


def eng_tavily(q, n, recency):
    keys = [k for k in (getenv("TAVILY_API_KEY"),
                        getenv("TAVILY_API_KEY_BACKUP")) if k]
    if not keys:
        return None, "no TAVILY_API_KEY"
    body = {"query": q, "max_results": n}
    if recency:
        body["time_range"] = recency
    err = None
    for key in keys:
        try:
            d = http_json("https://api.tavily.com/search", method="POST",
                          body={**body, "api_key": key})
        except urllib.error.HTTPError as e:
            err = e  # key-level limit/exhaustion (432/429/401/402/403)
            if e.code in (401, 402, 403, 429, 432):
                continue
            raise
        return ([(norm_url(r["url"]), r.get("title", ""),
                  (r.get("content") or "")[:400]) for r in d.get("results", [])
                 if r.get("url")], None)
    return None, "all Tavily keys failed: %s" % err


def eng_searxng(q, n, recency, timeout=ENGINE_TIMEOUT):
    global LAST_SEARXNG_UNRESPONSIVE
    base = getenv("SEARXNG_URL", "http://127.0.0.1:8080").rstrip("/")
    params = {"q": q, "format": "json"}
    if recency:
        params["time_range"] = recency
    url = base + "/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "omp-agg/1.0"})
    with _OPENER.open(req, timeout=_effective_timeout(timeout)) as r:
        d = _read_json(r)
    LAST_SEARXNG_UNRESPONSIVE = d.get("unresponsive_engines") or []
    rows = [(norm_url(x["url"]), x.get("title", ""),
             (x.get("content") or "")[:400]) for x in d.get("results", [])[:n]
            if x.get("url")]
    if not rows and LAST_SEARXNG_UNRESPONSIVE:
        # zero rows because the upstreams are suspended/CAPTCHA'd is a
        # failure: it must cool down and must not read as "query too narrow"
        reasons = "; ".join(
            "%s=%s" % (clean_text(str(item[0]))[:30],
                       clean_text(str(item[1]))[:40])
            for item in LAST_SEARXNG_UNRESPONSIVE[:4]
            if isinstance(item, (list, tuple)) and len(item) >= 2)
        return None, ("upstreams unresponsive: " + reasons)[:300]
    return rows, None


def eng_anysearch(q, n):
    key = getenv("MCP_ANYSEARCH_API_KEY")
    if not key:
        return None, "no MCP_ANYSEARCH_API_KEY"
    d = http_json("https://api.anysearch.com/mcp", method="POST",
                  body={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "search",
                                   "arguments": {"query": q,
                                                 "max_results": min(n, 10)}}},
                  headers={"Authorization": "Bearer " + key,
                           "Accept": "application/json, text/event-stream"})
    try:
        text = d["result"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None, "unexpected response shape"
    titles = re.findall(r"^###\s+\d+\.\s+(.*)$", text, flags=re.M)
    urls = re.findall(r"^- \*\*URL\*\*:\s*(\S+)", text, flags=re.M)
    snips = [s for s in re.findall(r"^-\s+(?!\*\*URL\*\*)(.*)$", text, flags=re.M)]
    out, si = [], 0
    for t, u in zip(titles, urls):
        s = ""
        while si < len(snips) and not s:
            s = snips[si].strip()
            si += 1
        out.append((norm_url(u), t.strip(), s[:400]))
    if not out:
        return None, "no parseable results"
    return out, None


def eng_deepseek(q, n, recency):
    key = getenv("DEEPSEEK_API_KEY")
    if not key:
        return None, "no DEEPSEEK_API_KEY"
    base = getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    if recency:
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[recency]
        since = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        q = "%s after:%s" % (q, since)  # DeepSeek query operator
    d = http_json_retry(base + "/v1/tools/search",
                        body={"query": q, "count": min(n, 50)},
                        headers={"Authorization": "Bearer " + key})
    rows = [r for r in d.get("results", []) if r.get("url")]
    rows.sort(key=lambda r: r.get("rerank_score") or 0.0, reverse=True)
    return ([(norm_url(r["url"]), r.get("title", ""),
              (r.get("summary") or r.get("snippet") or r.get("content") or "")
              [:400]) for r in rows[:n]], None)


def _strip_tags(markup):
    return re.sub(r"<[^>]+>", " ", markup or "")


def _openalex_abstract(inverted):
    """Rebuild the abstract from OpenAlex's inverted index (word -> positions)."""
    if not isinstance(inverted, dict):
        return ""
    positions = []
    for word, idxs in inverted.items():
        for i in idxs or []:
            positions.append((i, word))
    positions.sort()
    return " ".join(word for _, word in positions)


def eng_openalex(q, n, recency):
    """Academic works (keyless, polite pool; OPENALEX_API_KEY raises the cap)."""
    params = {
        "search": q,
        "per-page": min(n, 25),
        "select": "id,doi,title,publication_year,primary_location,"
                  "abstract_inverted_index",
    }
    if recency:
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[recency]
        since = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        params["filter"] = "from_publication_date:" + since
    key = getenv("OPENALEX_API_KEY")
    if key:
        params["api_key"] = key
    mailto = getenv("OPENALEX_MAILTO")
    if mailto:
        params["mailto"] = mailto
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "agg-search/1.0"})
    with _OPENER.open(req,
                      timeout=_effective_timeout(ENGINE_TIMEOUT)) as r:
        d = _read_json(r)
    out = []
    for work in d.get("results", [])[:n]:
        link = (work.get("doi")
                or (work.get("primary_location") or {}).get("landing_page_url")
                or work.get("id"))
        if not link:
            continue
        abstract = _openalex_abstract(work.get("abstract_inverted_index"))
        year = work.get("publication_year")
        snippet = abstract or ("published %s" % year if year else "")
        out.append((norm_url(link), clean_text(work.get("title") or ""),
                    clean_text(snippet[:400])))
    return out, None


def eng_hn(q, n, recency):
    """Hacker News stories via the Algolia API (keyless)."""
    params = {"query": q, "hitsPerPage": min(n, 30), "tags": "story"}
    if recency:
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[recency]
        params["numericFilters"] = "created_at_i>%d" % int(
            time.time() - days * 86400)
    d = http_json("https://hn.algolia.com/api/v1/search?"
                  + urllib.parse.urlencode(params))
    out = []
    for hit in d.get("hits", [])[:n]:
        link = hit.get("url") or ("https://news.ycombinator.com/item?id=%s"
                                  % hit.get("objectID"))
        text = _strip_tags(hit.get("story_text") or "").strip()
        meta = "%s points · %s comments" % (hit.get("points") or 0,
                                            hit.get("num_comments") or 0)
        snippet = text or meta
        out.append((norm_url(link), clean_text(hit.get("title") or ""),
                    clean_text(snippet[:400])))
    return out, None


def eng_stack(q, n, recency):
    """Stack Exchange Q&A (keyless; STACKEXCHANGE_SITE picks the site)."""
    params = {
        "order": "desc",
        "sort": "relevance",
        "q": q,
        "site": getenv("STACKEXCHANGE_SITE", "stackoverflow"),
        "pagesize": min(n, 20),
        "filter": "withbody",
    }
    if recency:
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[recency]
        params["fromdate"] = int(time.time() - days * 86400)
    key = getenv("STACKEXCHANGE_KEY")
    if key:
        params["key"] = key
    d = http_json("https://api.stackexchange.com/2.3/search/advanced?"
                  + urllib.parse.urlencode(params))
    out = []
    for item in d.get("items", [])[:n]:
        body = re.sub(r"\s+", " ", _strip_tags(item.get("body") or "")).strip()
        out.append((norm_url(item.get("link") or ""),
                    clean_text(html.unescape(item.get("title") or "")),
                    clean_text(body[:400])))
    return out, None


_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_STOP = frozenset({"a", "an", "the", "and", "or", "for", "of", "to",
                         "in", "on", "with", "by", "from", "at", "is", "are",
                         "how", "what", "why", "not"})


def arxiv_query(q):
    """Latin-script terms ANDed over all fields; arXiv's free-text OR mode
    matches tens of thousands of loosely related papers. CJK is dropped
    (arXiv metadata is English); a CJK-only query yields ''."""
    toks = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", q or "")
            if t.lower() not in _ARXIV_STOP]
    return " AND ".join("all:" + t for t in toks[:8])


def eng_arxiv(q, n, recency):
    """arXiv preprints via the Atom API (keyless)."""
    sq = arxiv_query(q)
    if not sq:
        return [], None  # healthy empty: nothing arXiv can match
    if recency:
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[recency]
        now = datetime.datetime.now(datetime.timezone.utc)
        since = now - datetime.timedelta(days=days)
        sq += " AND submittedDate:[%s TO %s]" % (since.strftime("%Y%m%d%H%M"),
                                                now.strftime("%Y%m%d%H%M"))
    params = {"search_query": sq, "max_results": min(n, 25),
              "sortBy": "relevance"}
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "agg-search/1.0"})
    with _OPENER.open(req, timeout=_effective_timeout(ENGINE_TIMEOUT)) as r:
        raw = r.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError("engine response larger than %d bytes" % MAX_RESPONSE)
    return parse_arxiv_atom(raw, n), None


def parse_arxiv_atom(raw, n):
    # the feed never needs a DTD; refusing one rules out entity tricks
    if b"<!DOCTYPE" in raw[:4096].upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("arxiv response carries a DTD")
    root = ElementTree.fromstring(raw)
    out = []
    for entry in root.findall(_ATOM + "entry")[:n]:
        link = (entry.findtext(_ATOM + "id") or "").strip()
        m = re.search(r"arxiv\.org/abs/([^\s?#]+?)(?:v\d+)?$", link)
        if not m:
            continue
        title = re.sub(r"\s+", " ", entry.findtext(_ATOM + "title") or "")
        summary = re.sub(r"\s+", " ", entry.findtext(_ATOM + "summary") or "")
        out.append(("https://arxiv.org/abs/" + m.group(1),
                    clean_text(title.strip()),
                    clean_text(summary.strip()[:400])))
    return out


def _dated(date, text):
    """Prefix a news snippet with its publication date so rerankers and
    agents can see freshness; the date comes from the provider."""
    date = clean_text(str(date or "")).strip()
    return ("[%s] %s" % (date, text) if date else text)[:400]


def eng_brave_news(q, n, recency):
    """Brave News vertical (same key and quota as `brave`)."""
    key = getenv("BRAVE_API_KEY")
    if not key:
        return None, "no BRAVE_API_KEY"
    params = {"q": q, "count": min(n, 20)}
    if recency:
        params["freshness"] = {"day": "pd", "week": "pw",
                               "month": "pm", "year": "py"}[recency]
    url = ("https://api.search.brave.com/res/v1/news/search?"
           + urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"X-Subscription-Token": key})
    with _OPENER.open(req, timeout=_effective_timeout(ENGINE_TIMEOUT)) as r:
        d = _read_json(r)
    out = []
    for x in d.get("results", [])[:n]:
        if not x.get("url"):
            continue
        out.append((norm_url(x["url"]), x.get("title", ""),
                    _dated((x.get("page_age") or "")[:10],
                           x.get("description") or "")))
    return out, None


def eng_serper_news(q, n, recency):
    """Google News via serper.dev (same key and credit cost as `serper`)."""
    key = getenv("SERPER_API_KEY")
    if not key:
        return None, "no SERPER_API_KEY"
    body = {"q": q, "num": min(n, 10)}
    if recency:
        body["tbs"] = {"day": "qdr:d", "week": "qdr:w",
                       "month": "qdr:m", "year": "qdr:y"}[recency]
    d = http_json("https://google.serper.dev/news", method="POST",
                  body=body, headers={"X-API-KEY": key})
    out = []
    for x in d.get("news") or []:
        if x.get("link"):
            out.append((norm_url(x["link"]), x.get("title", ""),
                        _dated(x.get("date"), x.get("snippet") or "")))
    return out[:n], None


_ENGINE_FNS = {
    "exa": lambda q, n, r: eng_exa(q, n),
    "brave": lambda q, n, r: eng_brave(q, n, r),
    "serper": lambda q, n, r: eng_serper(q, n, r),
    "firecrawl": lambda q, n, r: eng_firecrawl(q, n, r),
    "anysearch": lambda q, n, r: eng_anysearch(q, n),
    "tavily": lambda q, n, r: eng_tavily(q, n, r),
    "deepseek": lambda q, n, r: eng_deepseek(q, n, r),
    "searxng": lambda q, n, r: eng_searxng(q, n, r),
    "openalex": lambda q, n, r: eng_openalex(q, n, r),
    "hn": lambda q, n, r: eng_hn(q, n, r),
    "stack": lambda q, n, r: eng_stack(q, n, r),
    "arxiv": lambda q, n, r: eng_arxiv(q, n, r),
    "brave_news": lambda q, n, r: eng_brave_news(q, n, r),
    "serper_news": lambda q, n, r: eng_serper_news(q, n, r),
}
# name: (fn(q, n, recency), rrf weight) — weights come from engine_specs.
ENGINES = {name: (_ENGINE_FNS[name], engine_specs.ENGINE_SPECS[name]["weight"])
           for name in _ENGINE_FNS}


def _timed_call(name, query, n, recency):
    """(rows, err, elapsed_ms) for one engine; per-engine熔断 lives here."""
    t0 = time.perf_counter()
    try:
        rows, err = ENGINES[name][0](query, n, recency)
    except Exception as e:  # per-engine熔断: never block fusion
        rows, err = None, "%s: %s" % (type(e).__name__, e)
    return rows, err, (time.perf_counter() - t0) * 1000.0


def _recall_one(query, n, engines, recency):
    """One query across engines -> (results, status, timing_ms)."""
    results, status, timing = {}, {}, {}

    def run(name):
        rows, err, ms = _timed_call(name, query, n, recency)
        return name, rows, err, ms

    with cf.ThreadPoolExecutor(max_workers=len(engines)) as ex:
        for name, rows, err, ms in ex.map(run, engines):
            timing[name] = ms
            if rows:
                results[name] = rows
                status[name] = "ok (%d)" % len(rows)
            elif rows == [] and err is None:
                status[name] = "ok (0)"  # healthy empty, never cooled
            else:
                status[name] = "skipped/failed: %s" % err
    return results, status, timing


def recall(query, n, engines, recency):
    results, status, timing = _recall_one(query, n, engines, recency)
    LAST_TIMINGS.clear()
    LAST_TIMINGS.update(timing)
    return results, status


def expand_queries(q, n_variants, prompt_template=None):
    """Rewrite the query into n_variants-1 alternatives via local ollama
    (native API, thinking disabled — the OpenAI-compat endpoint ignores
    `think` and burns the budget on reasoning). Returns (variants, note)."""
    base = getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    model = getenv("EXPAND_MODEL", "qwen3.5:9b")
    want = n_variants - 1
    template = prompt_template or portals.domain_prompts("general")["expand"]
    prompt = portals.render_prompt(template, count=want, query=q)
    try:
        d = http_json(base + "/api/chat", method="POST",
                      body={"model": model, "stream": False, "think": False,
                            "messages": [{"role": "user", "content": prompt}],
                            "options": {"num_predict": 200}},
                      timeout=_effective_timeout(30))
        lines = []
        for ln in (d.get("message", {}).get("content") or "").splitlines():
            ln = re.sub(r'^\s*(?:[-*\d]+[.)]?\s*)', '', ln).strip().strip('"')
            if ln and ln.lower() != q.strip().lower():
                lines.append(ln)
        uniq = list(dict.fromkeys(lines))[:want]
        if not uniq:
            return [], "expand: model returned no usable variants, original only"
        return uniq, "expand: %d queries via %s | %s" % (
            1 + len(uniq), model, " | ".join(uniq))
    except Exception as e:  # noqa: BLE001 — expansion is best-effort
        return [], "expand failed (%s: %s), original only" % (
            type(e).__name__, e)


def recall_multi(queries, n, engines, recency):
    """Recall for every query variant; expanded runs key each (engine,
    variant) list as `engine#i` so RRF fuses them as separate rankings."""
    if len(queries) == 1:
        return recall(queries[0], n, engines, recency)
    results, status, timing = {}, {}, {}
    with cf.ThreadPoolExecutor(max_workers=len(queries)) as ex:
        for i, (res, st, tm) in enumerate(
                ex.map(lambda qq: _recall_one(qq, n, engines, recency),
                       queries)):
            for eng, s in st.items():
                # merge: keep the best outcome per engine across variants
                if eng not in status or status[eng].startswith("skipped/failed"):
                    status[eng] = s if i == 0 or s.startswith("ok") else status[eng]
            for eng, ms in tm.items():
                # fold like the status merge: the slowest variant is the cost
                timing[eng] = max(timing.get(eng, 0.0), ms)
            for eng, rows in res.items():
                results["%s#%d" % (eng, i)] = rows
    LAST_TIMINGS.clear()
    LAST_TIMINGS.update(timing)
    return results, status


def recall_flat(queries, n, engines, recency, budget_secs=30.0, min_sources=0,
                deadline=None, must_engines=()):
    """Single-pool recall over (variant, engine) tasks with a shared deadline.
    Only used behind --fast; legacy recall_multi stays the default path.
    Returns (results, status, note): note carries sourcesTarget/achieved when
    min_sources is unmet (advisory, never fails the search for it).
    Only engines with actual rows count toward min_sources — a healthy
    empty (`ok (0)`) must not trigger early return (P2-4).
    `must_engines` (a profile's primary engines, already filtered to the
    requested set) gates the early return: a profile must not be served by
    its secondary engines alone just because they answered faster."""
    tasks = [(qi, eng) for qi in range(len(queries)) for eng in engines]
    results, status, ok_engines = {}, {}, set()
    timing = {}
    if deadline is None:
        deadline = time.monotonic() + budget_secs
    wait = max(0.5, deadline - time.monotonic())
    pool = cf.ThreadPoolExecutor(max_workers=min(len(tasks), 16))
    try:
        futs = {pool.submit(_flat_one, queries[qi], n, recency, eng, qi,
                            deadline): (qi, eng)
                for qi, eng in tasks}
        for fut in cf.as_completed(futs, timeout=wait):
            qi, eng = futs[fut]
            try:
                key, rows, err, ms = fut.result()
            except Exception as e:  # noqa: BLE001 — same熔断contract as recall
                key = "%s#%d" % (eng, qi) if len(queries) > 1 else eng
                rows, err, ms = None, "%s: %s" % (type(e).__name__, e), 0.0
            timing[eng] = max(timing.get(eng, 0.0), ms)
            if rows:
                results[key] = rows
                status[eng] = "ok (%d)" % len(rows)
                ok_engines.add(eng)
            elif rows == [] and err is None:
                status.setdefault(eng, "ok (0)")  # healthy empty, not a source
            else:
                status.setdefault(eng, "skipped/failed: %s" % err)
            if min_sources and len(ok_engines) >= min_sources:
                if not must_engines or ok_engines & set(must_engines):
                    break
    except cf.TimeoutError:
        timed_out = True
    else:
        timed_out = False
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    pending_note = ("over recall budget" if timed_out
                    else "min_sources met, not awaited")
    for qi, eng in tasks:
        status.setdefault(eng, "skipped: %s" % pending_note)
    note = ""
    if min_sources and len(ok_engines) < min_sources:
        note = ("sourcesTarget: %d, achieved: %d (budget %.0fs)"
                % (min_sources, len(ok_engines), budget_secs))
    LAST_TIMINGS.clear()
    LAST_TIMINGS.update(timing)
    return results, status, note


def missing_required(required, status):
    """Portal-required engines that did not return rows."""
    return [name for name in required
            if not str(status.get(name, "")).startswith("ok")]


def supplement_keyless(queries, n, recency, fallback, deadline):
    """Recall keyless fallback engines when a portal's primary engine failed.

    Portal degradation is not just a credential-time decision: an engine can
    fail or cool down at runtime. This runs a bounded second pool so the
    domain answer still has a source.
    """
    results, status, note = recall_flat(
        queries, n, fallback, recency, budget_secs=8.0, min_sources=1,
        deadline=deadline)
    return results, status, (note or "")


def _flat_one(query, n, recency, eng, qi, deadline):
    _TLS.deadline = deadline  # thread-local: visible only in this worker
    t0 = time.perf_counter()
    try:
        rows, err = ENGINES[eng][0](query, n, recency)
    except Exception as e:  # noqa: BLE001
        rows, err = None, "%s: %s" % (type(e).__name__, e)
    return ("%s#%d" % (eng, qi)), rows, err, (time.perf_counter() - t0) * 1000.0


def rrf_fuse(results, top, weights=None, k=None, diversify=False,
             dedupe=True, domain_boost=None, boost_factor=1.0):
    """Fuse per-engine rankings; `weights` overrides ENGINES defaults
    (used by scripts/rrf_eval.py for parameter sweeps and by --expand,
    where each engine#variant list is a separate ranking).
    `k` overrides RRF_K (lets the sweep harness call the real function);
    `dedupe` collapses canonical URL mirrors (http/https, www, tracking query,
    arXiv abs/pdf/html/DOI) and exact/near-exact duplicate pages while merging
    engine provenance; `domain_boost`/`boost_factor` apply a soft domain
    authority prior; `diversify` round-robins by eTLD+1 BEFORE truncation so
    Jina still sees diverse candidates (MF5: never diversify after Jina)."""
    kk = RRF_K if k is None else k
    work_results = results
    if dedupe:
        raw_rrf = {}
        for eng, rows in results.items():
            base = eng.split("#", 1)[0]
            if weights is None:
                w = ENGINES.get(base, (None, 1.0))[1]
            else:
                w = weights.get(eng, ENGINES.get(base, (None, 1.0))[1])
            for i, (u, _t, _s) in enumerate(rows):
                raw_rrf[u] = raw_rrf.get(u, 0.0) + w / (kk + i + 1)
        canon_rep = {}
        for u in sorted(raw_rrf, key=raw_rrf.get, reverse=True):
            cu = canonical_url(u)
            if cu not in canon_rep:
                canon_rep[cu] = u
        remapped = {}
        for eng, rows in results.items():
            seen_rep = set()
            out_rows = []
            for u, t, s in rows:
                rep = canon_rep[canonical_url(u)]
                if rep not in seen_rep:
                    seen_rep.add(rep)
                    out_rows.append((rep, t, s))
            if out_rows:
                remapped[eng] = out_rows
        work_results = remapped

    score, meta = {}, {}
    for eng, rows in work_results.items():
        base = eng.split("#", 1)[0]
        if weights is None:
            w = ENGINES.get(base, (None, 1.0))[1]
        else:
            w = weights.get(eng, ENGINES.get(base, (None, 1.0))[1])
        for i, (u, t, s) in enumerate(rows):
            score[u] = score.get(u, 0.0) + w / (kk + i + 1)
            m = meta.setdefault(u, {"title": clean_text(t),
                                    "snippet": clean_text(s),
                                    "engines": []})
            if base not in m["engines"]:
                m["engines"].append(base)
    if domain_boost and boost_factor and float(boost_factor) > 1.0:
        factor = float(boost_factor)
        rules = parse_domains(domain_boost)
        for u in list(score):
            host = _filter_host(u)
            if any(domain_matches_rule(host, r) for r in rules):
                score[u] *= factor
    ordered = sorted(score, key=score.get, reverse=True)
    if dedupe:
        kept = []
        for u in ordered:
            m = meta[u]
            dup_of = None
            for ku in kept:
                km = meta[ku]
                if is_content_duplicate(m["title"], m["snippet"],
                                        km["title"], km["snippet"]):
                    dup_of = ku
                    break
            if dup_of is not None:
                for eng in m["engines"]:
                    if eng not in meta[dup_of]["engines"]:
                        meta[dup_of]["engines"].append(eng)
            else:
                kept.append(u)
        ordered = kept
    if diversify:
        # interleave BEFORE truncation: changes the candidate SET Jina sees
        # (reordering the same top-15 is a no-op — Jina scores pairs
        # independently of input order). Head-5 stays pure RRF so consensus
        # relevance is never displaced; only the tail is interleaved, and the
        # [:top] cut then keeps per-domain bests, not same-domain fill.
        head, tail = ordered[:5], ordered[5:]
        tail = [u for u, _, _ in
                diversify_ranked([(u, score[u], meta[u]) for u in tail])]
        ordered = head + tail
    ranked = ordered[:top]
    return [(u, score[u], meta[u]) for u in ranked]


def jina_rerank(query, ranked, top_n, timeout=ENGINE_TIMEOUT, models=None,
                max_attempts=2, blend=None):
    """Listwise rerank. `blend` in (0,1] interpolates the Jina relevance with
    the incoming RRF order: 1.0 = pure Jina (historic behaviour), 0.4 keeps
    engine consensus dominant. Domain profiles use it because listwise scores
    on short title+snippet text can demote specialised sources (papers,
    Q&A) below generic pages that merely repeat the query words."""
    key = getenv("JINA_API_KEY")
    if not key:
        return None, "no JINA_API_KEY, kept RRF order"
    docs = ["%s\n%s" % (m["title"], m["snippet"]) for _, _, m in ranked]
    preferred = getenv("JINA_RERANK_MODEL", JINA_MODELS[0])
    chain = list(models) if models else [preferred] + [m for m in JINA_MODELS if m != preferred]
    err = "no model attempted"
    for model in chain:
        for attempt in range(1, max_attempts + 1):  # retry transient statuses
            try:
                d = http_json("https://api.jina.ai/v1/rerank", method="POST",
                              # ask for every candidate's score: blending
                              # needs the full vector, not just Jina's top-N
                              body={"model": model, "query": query,
                                    "documents": docs, "top_n": len(docs)},
                              headers={"Authorization": "Bearer " + key},
                              timeout=timeout)
            except urllib.error.HTTPError as e:
                err = "%s: %s" % (type(e).__name__, e)
                if e.code in (400, 404, 422):  # model unavailable on this key
                    break  # -> next model in the chain
                if (e.code in (408, 429) or e.code >= 500) and attempt < max_attempts:
                    time.sleep(1.5)
                    continue  # -> retry same model
                return None, "jina failed (%s), kept RRF order" % err
            except Exception as e:
                return None, "jina failed (%s: %s), kept RRF order" % (
                    type(e).__name__, e)
            else:
                try:  # 200 with malformed body must degrade, never crash
                    scored = [(r["index"], float(r["relevance_score"]))
                              for r in d["results"]
                              if isinstance(r.get("index"), int)]
                except (KeyError, TypeError, AttributeError, ValueError):
                    return None, ("jina failed (bad response), "
                                  "kept RRF order")
                if not scored:
                    return None, "jina returned no ranking, kept RRF order"
                if blend is None:
                    order = [i for i, _ in
                             sorted(scored, key=lambda pair: pair[1],
                                    reverse=True)]
                else:
                    order = _blend_order(scored, len(ranked), blend)
                return [ranked[i] for i in order if i < len(ranked)], None
    return None, "jina failed (%s), kept RRF order" % err


def _blend_order(scored, total, blend):
    """Interpolate Jina relevance with the incoming RRF position.

    Both signals are min-max normalised over the candidate set; positions
    come from the (already fused) input order, so engine consensus keeps a
    predictable share of the final ranking.
    """
    blend = max(0.0, min(1.0, float(blend)))
    rel = [r for _, r in scored]
    lo, hi = min(rel), max(rel)
    span = (hi - lo) or 1.0
    pos_norm = {i: 1.0 - (i / max(1, total - 1)) for i in range(total)}
    final = []
    for index, relevance in scored:
        jina_norm = (relevance - lo) / span
        final.append((index, blend * jina_norm
                      + (1.0 - blend) * pos_norm.get(index, 0.0)))
    return [i for i, _ in sorted(final, key=lambda pair: pair[1],
                                 reverse=True)]


def answer_query(query, ranked, mode, prompt_template=None,
                 fetched_texts=None):
    """Synthesize a cited answer from the top results.
    mode: 'local' = ollama (free) | 'deepseek' = chat API (token-billed).
    Returns (text, note); best-effort, never blocks the result list."""
    ctx = []
    fetched = fetched_texts or {}
    for i, (u, _sc, m) in enumerate(ranked[:8], 1):
        full = fetched.get(u)
        body = full if full else (m.get("snippet") or "")
        limit = 2500 if full else 300
        ctx.append("[%d] %s\n%s\n%s"
                   % (i, clean_text(m.get("title") or u), u,
                      clean_text(body)[:limit]))
    template = prompt_template or portals.domain_prompts("general")["answer"]
    prompt = portals.render_prompt(template, query=query,
                                   results="\n\n".join(ctx))
    try:
        if mode == "deepseek":
            base = getenv("DEEPSEEK_BASE_URL",
                          "https://api.deepseek.com").rstrip("/")
            key = getenv("DEEPSEEK_API_KEY")
            if not key:
                return None, "answer failed (no DEEPSEEK_API_KEY), skipped"
            d = http_json_retry(
                base + "/chat/completions",
                body={"model": getenv("ANSWER_DS_MODEL", "deepseek-flash"),
                      "messages": [{"role": "user", "content": prompt}],
                      "thinking": {"type": "disabled"},  # skip reasoning
                      "max_tokens": 800},
                headers={"Authorization": "Bearer " + key}, timeout=90)
            text = (d["choices"][0]["message"].get("content") or "").strip()
        else:
            base = getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
            d = http_json(base + "/api/chat", method="POST",
                          body={"model": getenv("ANSWER_MODEL", "qwen3.5:9b"),
                                "stream": False, "think": False,
                                "messages": [{"role": "user", "content": prompt}],
                                "options": {"num_predict": 500}},
                          timeout=120)
            text = (d.get("message", {}).get("content") or "").strip()
        if not text:
            return None, "answer: model returned empty, skipped"
        return text, None
    except Exception as e:  # noqa: BLE001 — answer layer is best-effort
        return None, "answer failed (%s: %s), skipped" % (
            type(e).__name__, e)


def answer_with_fallback(query, ranked, mode="auto", prompt_template=None,
                         fallback_order=("local", "deepseek"),
                         fetched_texts=None):
    """Run answer synthesis with portal fallback ordering.

    Returns (text, note, mode_used, degraded_list).
    """
    degraded = []
    if mode in ("local", "deepseek"):
        text, note = answer_query(
            query, ranked, mode, prompt_template=prompt_template,
            fetched_texts=fetched_texts)
        if text:
            return text, note or "", mode, degraded
        if note:
            degraded.append(note)
        return None, note or "answer skipped", None, degraded

    for candidate in fallback_order or ("local", "deepseek"):
        text, note = answer_query(
            query, ranked, candidate, prompt_template=prompt_template,
            fetched_texts=fetched_texts)
        if text:
            used_note = ""
            if degraded:
                used_note = "answer fallback to %s (%s)" % (
                    candidate, "; ".join(degraded))
            return text, used_note, candidate, degraded
        if note:
            degraded.append("%s: %s" % (candidate, note))
    return None, ("; ".join(degraded) or "answer skipped"), None, degraded


def _env_int(name):
    """Positive int from the environment, else None (unset/invalid/non-int)."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        v = int(raw.strip())
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _env_flag(name):
    """True only for 1/true/yes (case-insensitive); anything else is unset."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    return True if raw.strip().lower() in _TRUTHY else None


def apply_env_defaults(a):
    """Fold AGG_SEARCH_* env vars into `a` where the flag was omitted.

    A flag counts as omitted only while it still equals its hardcoded
    argparse default (so --help keeps showing real defaults); an explicit
    flag always wins. Invalid env values are ignored silently — a bad
    AGG_SEARCH_TOP_N must never crash a search or half-apply.
    Returns the set of attributes it changed, so a profile applied later
    does not overwrite an environment choice."""
    applied = set()
    if a.engines == DEFAULT_ENGINES:
        raw = os.environ.get("AGG_SEARCH_ENGINES")
        names = [e.strip() for e in (raw or "").split(",") if e.strip()]
        if names and all(e in ENGINES for e in names):
            a.engines = ",".join(names)
            applied.add("engines")
    if a.top_n == DEFAULT_TOP_N:
        v = _env_int("AGG_SEARCH_TOP_N")
        if v is not None:
            a.top_n = v
            applied.add("top_n")
    if a.per_engine == DEFAULT_PER_ENGINE:
        v = _env_int("AGG_SEARCH_PER_ENGINE")
        if v is not None:
            a.per_engine = v
            applied.add("per_engine")
    if a.snippet_chars == DEFAULT_SNIPPET_CHARS:
        v = _env_int("AGG_SEARCH_SNIPPET_CHARS")
        if v is not None:
            a.snippet_chars = v
            applied.add("snippet_chars")
    if a.fast is False and _env_flag("AGG_SEARCH_FAST"):
        a.fast = True
    if a.no_jina is False and _env_flag("AGG_SEARCH_NO_JINA"):
        a.no_jina = True
    return applied


def _prefetch_for_answer(urls):
    """Best-effort fetch of top URLs before answer synthesis.

    Honors server-side SSRF policy env vars and never blocks answer fallback.
    """
    if not urls:
        return {}, []
    cmd = [sys.executable, "-m", "agg_search.fetch", "--text-limit", "4000"]
    if _env_flag("AGG_SEARCH_ALLOW_PRIVATE"):
        cmd.append("--allow-private")
    cidrs = getenv("AGG_SEARCH_ALLOW_CIDRS", "")
    if cidrs:
        cmd += ["--allow-cidrs", cidrs]
    for u in urls:
        cmd += ["--url", u]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001 - prefetch is best-effort
        return {}, ["answer_fetch failed: %s" % exc]
    fetched = {}
    notes = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        body = row.get("text") or row.get("markdown") or ""
        if not row.get("blocked") and not row.get("error") and body:
            fetched[row["url"]] = body
        else:
            notes.append("answer_fetch skipped %s (%s)" % (
                row.get("url", ""), row.get("error") or "unusable"))
    if proc.returncode and not fetched and not notes:
        notes.append("answer_fetch exited %s" % proc.returncode)
    return fetched, notes


def show_config(a, profile_name="general", profile=None, use_learn=True,
                portal=None):
    """Effective settings for bug reports: credentials as booleans only.

    Every credential probe is `missing_credential(...) is None`; no key
    value is read into the payload, so this output is safe to paste."""
    req = [e.strip() for e in a.engines.split(",") if e.strip()]
    engines = [e for e in req if e in ENGINES] or list(ENGINES)
    weights = profiles.weights(profile or {}, ENGINES)
    learned = learn.load_multipliers(profile_name) if use_learn else {}
    for engine, mult in learned.items():
        weights[engine] = round(weights.get(engine, 1.0) * float(mult), 3)
    out = {
        "portal": portal["id"] if portal else None,
        "profile": profile_name,
        "prompts_domain": profile_name,
        "tools": list(portal["tools"]) if portal else ["search", "fetch"],
        "primary": list((profile or {}).get("primary") or []),
        "rerank_blend": (profile or {}).get("rerank_blend"),
        "authority_boost": (profile or {}).get("authority_boost"),
        "engines": engines,
        "weights": weights,
        "learned_multipliers": learned,
        "learn": use_learn,
        "top_n": a.top_n,
        "per_engine": a.per_engine,
        "rrf_top": a.rrf_top,
        "recency": a.recency,
        "snippet_chars": clamp_snippet_chars(a.snippet_chars),
        "fast": bool(a.fast),
        "no_jina": bool(a.no_jina),
        "budget_secs": a.budget_secs,
        "min_sources": (a.min_sources if a.min_sources is not None
                        else (2 if a.fast else 0)),
        "expand": a.expand,
        "dedupe": not getattr(a, "no_dedupe", False),
        "diversify": not a.no_diversify,
        "include_domains": list(parse_domains(a.include_domains)),
        "exclude_domains": list(parse_domains(a.exclude_domains)),
        "cache": not a.no_cache,
        "cooldown": not a.no_cooldown,
        "answer": a.answer,
        "answer_fetch": getattr(a, "answer_fetch", 0),
        "credentials": {e: missing_credential(e) is None for e in engines},
    }
    if portal:
        out["dependencies"] = portals.probe_dependencies(
            portal, lambda k: bool(getenv(k)), ollama_ok=None)
    return out


def _run_answer_sources(path_or_dash, portal, profile_name, prompts,
                        fallback_order, cli_mode=None):
    """Synthesize a cited answer directly from provided JSON sources."""
    try:
        if path_or_dash == "-":
            raw = sys.stdin.read()
        else:
            with open(path_or_dash, encoding="utf-8") as f:
                raw = f.read()
        payload = json.loads(raw)
    except (OSError, ValueError) as exc:
        sys.exit("invalid --answer-sources payload: %s" % exc)
    if not isinstance(payload, dict):
        sys.exit("--answer-sources expects a JSON object")
    query = clean_text(str(payload.get("query") or "")).strip()
    if not query:
        sys.exit("--answer-sources payload requires 'query'")
    sources = payload.get("sources") or []
    if not isinstance(sources, list) or not sources:
        sys.exit("--answer-sources payload requires non-empty 'sources'")

    ranked = []
    fetched_texts = {}
    for i, item in enumerate(sources[:8]):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "source://%d" % (i + 1))
        title = clean_text(str(item.get("title") or url))
        body = clean_text(str(item.get("text") or item.get("snippet") or ""))
        if body:
            fetched_texts[url] = body
        ranked.append((url, 1.0 / (i + 1), {
            "title": title, "snippet": body[:400], "engines": ["sources"],
        }))
    if not ranked:
        sys.exit("--answer-sources found no usable source entries")

    mode = cli_mode or str(payload.get("mode") or "auto")
    if mode not in ("auto", "local", "deepseek"):
        mode = "auto"
    text, note, mode_used, degraded = answer_with_fallback(
        query, ranked, mode=mode, prompt_template=prompts["answer"],
        fallback_order=fallback_order, fetched_texts=fetched_texts)
    print(json.dumps({
        "query": query,
        "portal": portal["id"] if portal else None,
        "profile": profile_name,
        "answer": text,
        "mode_used": mode_used,
        "note": note or None,
        "degraded": degraded,
        "sources_used": len(ranked),
    }, ensure_ascii=False))


def main():
    from agg_search import __version__
    global ENGINE_TIMEOUT, _FAST_MODE
    ap = argparse.ArgumentParser(description="parallel multi-engine search + RRF + jina rerank")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {__version__}")
    ap.add_argument("query", nargs="?", help="search query")
    ap.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                    help="results to return (default: %(default)s; "
                         "env AGG_SEARCH_TOP_N)")
    ap.add_argument("--rrf-top", type=int, default=15)
    ap.add_argument("--no-jina", action="store_true",
                    help="skip Jina rerank, keep RRF order "
                         "(env AGG_SEARCH_NO_JINA)")
    ap.add_argument("--engines", default=DEFAULT_ENGINES,
                    help="comma-separated engines (default: %(default)s; "
                         "env AGG_SEARCH_ENGINES)")
    ap.add_argument("--recency", choices=["day", "week", "month", "year"],
                    default=None)
    ap.add_argument("--per-engine", type=int, default=DEFAULT_PER_ENGINE,
                    help="rows requested per engine (default: %(default)s; "
                         "env AGG_SEARCH_PER_ENGINE)")
    ap.add_argument("--expand", type=int, default=0, metavar="N",
                    help="query-expansion fan-out: N>=2 runs N-1 local-LLM "
                         "rewrites as extra recall paths (RAG-Fusion)")
    ap.add_argument("--answer", nargs="?", const="auto", default=None,
                    choices=["auto", "local", "deepseek"],
                    metavar="auto|local|deepseek",
                    help="synthesize a cited answer from the top results. "
                         "auto (bare flag) follows the portal order; if local "
                         "ollama is unavailable it can fall through to the "
                         "billable DeepSeek chat API")
    ap.add_argument("--answer-sources", default=None, metavar="PATH|-",
                    help="synthesize an answer from a JSON {query, sources} "
                         "payload (used by the portal `answer` MCP tool)")
    ap.add_argument("--answer-fetch", type=int, default=0, metavar="N",
                    help="when --answer is set, fetch top N (0..3) result "
                         "pages before synthesizing (opt-in read-then-answer)")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="disable canonical-URL and near-duplicate collapsing "
                         "during RRF fusion")
    ap.add_argument("--json", action="store_true", dest="json_out",
                    help="machine-readable JSON output instead of markdown")
    ap.add_argument("--fast", action="store_true",
                    help="recall budget mode: single-pool fan-out with a shared "
                         "deadline + early return once --min-sources succeed. "
                         "Default off: legacy wait-for-all behavior is kept "
                         "(env AGG_SEARCH_FAST=1/true/yes).")
    ap.add_argument("--budget-secs", type=float, default=30.0,
                    help="shared recall deadline in --fast mode only")
    ap.add_argument("--min-sources", type=int, default=None,
                    help="early-return once this many engines return rows "
                         "(--fast only; default 2 in fast mode, 0 = wait for "
                         "all; unmet target only adds a note)")
    ap.add_argument("--no-cache", action="store_true",
                    help="bypass the 5min disk result cache")
    ap.add_argument("--no-cooldown", action="store_true",
                    help="bypass failure cooldowns (harness record must use this)")
    ap.add_argument("--no-diversify", action="store_true",
                    help="disable eTLD+1 diversity interleave before Jina")
    ap.add_argument("--include-domains", default="",
                    help="comma-separated host/suffix allowlist, applied per "
                         "engine before fusion (arxiv.org, .edu)")
    ap.add_argument("--exclude-domains", default="",
                    help="comma-separated host/suffix blocklist; wins over "
                         "--include-domains")
    ap.add_argument("--snippet-chars", type=int, default=DEFAULT_SNIPPET_CHARS,
                    help="rendered snippet length in output, clamped 80..2000 "
                         "(default: %(default)s; env AGG_SEARCH_SNIPPET_CHARS)")
    ap.add_argument("--show-config", action="store_true",
                    help="print the effective settings as one JSON object and "
                         "exit 0 before any network call; credentials appear "
                         "as booleans only, never as key values")
    ap.add_argument("--engine-timeout", type=float, default=ENGINE_TIMEOUT,
                    metavar="SECS",
                    help="per-engine HTTP timeout in seconds "
                         "(default: %(default)s)")
    ap.add_argument("--profile", default=None, metavar="NAME",
                    help="domain preset: %s (env AGG_SEARCH_PROFILE)"
                         % ", ".join(profiles.names()))
    ap.add_argument("--list-profiles", action="store_true",
                    help="print the built-in domain profiles and exit")
    ap.add_argument("--portal", default=None, metavar="AGENT.DOMAIN",
                    help="portal preset binding agent + domain prompts/tools "
                         "(env AGG_SEARCH_PORTAL)")
    ap.add_argument("--list-portals", action="store_true",
                    help="print available portals and exit")
    ap.add_argument("--no-learn", action="store_true",
                    help="do not read or record the local surfaced/used "
                         "feedback (env AGG_SEARCH_NO_LEARN)")
    a = ap.parse_args()
    if a.list_profiles:
        print("\n".join(profiles.describe()))
        return
    if a.list_portals:
        print("\n".join(portals.describe()))
        return
    a.default_engines = DEFAULT_ENGINES
    a.default_recency = None
    a.default_snippet_chars = DEFAULT_SNIPPET_CHARS
    a.default_top_n = DEFAULT_TOP_N
    a.default_per_engine = DEFAULT_PER_ENGINE
    env_fields = apply_env_defaults(a)
    explicit_engines = (a.engines != DEFAULT_ENGINES and "engines" not in env_fields)
    portal_name = a.portal or getenv("AGG_SEARCH_PORTAL")
    portal = None
    if portal_name:
        try:
            portal = portals.load(portal_name)
        except portals.UnknownPortal as exc:
            sys.exit(str(exc.args[0]))
    profile_name = (a.profile or getenv("AGG_SEARCH_PROFILE")
                    or (portal["profile"] if portal else "general"))
    try:
        profile = profiles.load(profile_name)
    except profiles.UnknownProfile as exc:
        sys.exit(str(exc.args[0]))
    profiles.apply(a, profile, env_fields)
    prompts = (portal["prompts"] if portal and profile_name == portal["profile"]
               else portals.domain_prompts(profile_name))
    answer_order = tuple(
        portal["defaults"]["answer_order"] if portal
        else ("local", "deepseek"))
    degraded = []
    if a.expand == 0 and portal:
        default_exp = int(portal["defaults"].get("expand") or 0)
        if default_exp >= 2:
            if portal["dependencies"]["llm"]["expand_available"]:
                a.expand = default_exp
            else:
                degraded.append(
                    "expand skipped: portal %s has no local ollama dependency"
                    % portal["id"])

    use_learn = not a.no_learn and not _env_flag("AGG_SEARCH_NO_LEARN")
    ENGINE_TIMEOUT = max(1.0, min(120.0, a.engine_timeout))
    _FAST_MODE = bool(a.fast) and not a.show_config
    if a.show_config:
        print(json.dumps(show_config(a, profile_name, profile, use_learn,
                                     portal=portal),
                         ensure_ascii=False, sort_keys=True))
        return
    if a.answer_sources:
        _run_answer_sources(a.answer_sources, portal, profile_name, prompts,
                            answer_order, cli_mode=a.answer)
        return
    if not a.query:
        sys.exit("a query is required (try --help, or --show-config)")
    req_names = [e.strip() for e in a.engines.split(",") if e.strip()]
    unknown = [e for e in req_names if e not in ENGINES]
    if unknown:
        sys.exit("unknown engines: %s (valid: %s)"
                 % (",".join(unknown), ",".join(ENGINES)))
    engines = [e for e in req_names if e in ENGINES]
    if not engines:
        sys.exit("no valid engines in --engines (valid: %s)"
                 % ",".join(ENGINES))
    diversify = not a.no_diversify
    dedupe = not a.no_dedupe
    auth_domains = profile.get("authority_domains") or ()
    auth_boost = float(profile.get("authority_boost") or 1.0)
    include_domains = parse_domains(a.include_domains)
    exclude_domains = parse_domains(a.exclude_domains)
    snippet_chars = clamp_snippet_chars(a.snippet_chars)
    fast_min_sources = (a.min_sources if a.min_sources is not None
                        else (2 if a.fast else 0))
    use_cache = not a.no_cache
    use_cooldown = not a.no_cooldown

    # effective engine weights: profile preset x local feedback multipliers.
    # Part of the cache key: retuning must not serve stale rankings.
    base_weights = profiles.weights(profile, ENGINES)
    if use_learn:
        for eng, mult in learn.load_multipliers(profile_name).items():
            base_weights[eng] = round(
                base_weights.get(eng, 1.0) * float(mult), 3)
    weight_sig = "profile=%s;dedupe=%d;auth=%.2f;%s" % (
        profile_name, int(dedupe), auth_boost,
        ",".join("%s=%.3f" % (k, v) for k, v in sorted(base_weights.items())))

    # 1a. credential prefilter: missing keys never take a thread (MF1b companion)
    status = {}
    for e in list(engines):
        miss = missing_credential(e)
        if miss:
            status[e] = "skipped: no %s" % miss
            if portal:
                degraded.append("engine %s skipped: missing %s" % (e, miss))
    engines = [e for e in engines if e not in status]
    # cooldown filter: quota/antibot/timeout only, empty never cools (MF3)
    cooldown = load_cooldown() if use_cooldown else {}
    now = time.time()
    for e in list(engines):
        if cooldown.get(e, 0) > now:
            status[e] = "skipped: cooling (%s)" % time.strftime(
                "%M:%S", time.gmtime(cooldown[e] - now))
            engines.remove(e)
            if portal:
                degraded.append("engine %s cooling" % e)
    if portal and not engines and not explicit_engines:
        fb = [e for e in portal["dependencies"].get("keyless_fallback", ())
              if e in ENGINES and missing_credential(e) is None
              and cooldown.get(e, 0) <= now]
        if fb:
            engines = fb
            degraded.append(
                "all configured engines skipped/cooling; fell back to keyless: %s"
                % ",".join(fb))
    if not engines and not status:
        sys.exit("no valid engines in --engines (valid: %s)"
                 % ",".join(ENGINES))
    if not engines:
        sys.exit("all engines skipped: %s" % json.dumps(status))

    # one shared deadline covers expand + recall in fast mode (P3-9)
    fast_deadline = time.monotonic() + a.budget_secs if a.fast else None
    expand_note = ""
    queries = [a.query]
    if a.expand >= 2:
        if a.fast and a.expand > 3:
            a.expand = 3  # MF2: cap fan-out under a shared budget
        if fast_deadline is not None:
            _TLS.deadline = fast_deadline
        try:
            variants, expand_note = expand_queries(
                a.query, a.expand, prompt_template=prompts["expand"])
        finally:
            _TLS.deadline = None
        if variants:
            queries += variants
        elif expand_note:
            degraded.append(expand_note)

    # disk cache (MF4): key covers every output-affecting input incl. mode
    ckey = cache_key(a.query, engines, a.recency, a.per_engine, a.rrf_top,
                     a.top_n, a.no_jina, a.expand, diversify,
                     fast=a.fast, budget_secs=a.budget_secs,
                     min_sources=fast_min_sources,
                     include_domains=include_domains,
                     exclude_domains=exclude_domains,
                     snippet_chars=snippet_chars,
                     extra=weight_sig)
    cached = False
    cache_age = 0.0
    timings = {}
    recall_ms = 0.0
    results, recall_status, fast_note = {}, {}, ""
    if use_cache and len(queries) == 1 and not a.answer:
        store = load_cache()
        hit = store.get(ckey)
        if hit and now - hit.get("at", 0) < CACHE_TTL:
            results = {k: [tuple(r) for r in v]
                       for k, v in hit["results"].items()}
            recall_status = dict(hit["status"])
            global LAST_SEARXNG_UNRESPONSIVE
            LAST_SEARXNG_UNRESPONSIVE = hit.get("searxng_unresponsive") or []
            cached = True
            cache_age = max(0.0, now - hit.get("at", 0))
    if not cached:
        recall_t0 = time.perf_counter()
        if a.fast:
            results, recall_status, fast_note = recall_flat(
                queries, a.per_engine, engines, a.recency,
                budget_secs=a.budget_secs, min_sources=fast_min_sources,
                deadline=fast_deadline,
                must_engines=[e for e in (profile.get("primary") or [])
                              if e in engines])
        else:
            results, recall_status = recall_multi(queries, a.per_engine,
                                                 engines, a.recency)
            fast_note = ""
        recall_ms = (time.perf_counter() - recall_t0) * 1000.0
        timings = dict(LAST_TIMINGS)
        pending_cache = bool(use_cache and len(queries) == 1
                             and not a.answer and results)
    else:
        pending_cache = False
    status.update(recall_status)
    # runtime degradation: a portal's required engine may fail or cool down
    # after the prefilter. Supplement with its keyless fallbacks (bounded)
    # and record why; never cache a supplemented result (it depends on the
    # failure, not only on the query).
    supplemented = False
    if portal and not cached:
        required = [e for e in portal["dependencies"].get(
            "required_engines", ()) if e in engines]
        failed = missing_required(required, status)
        fallback = [e for e in portal["dependencies"].get(
            "keyless_fallback", ())
            if e in ENGINES and e not in engines
            and missing_credential(e) is None]
        if failed and fallback:
            extra, extra_status, extra_note = supplement_keyless(
                queries, a.per_engine, a.recency, fallback,
                time.monotonic() + 8.0)
            for key, rows in extra.items():
                results.setdefault(key, []).extend(rows)
            status.update(extra_status)
            degraded.append(
                "required engine(s) %s unavailable; supplemented %s%s"
                % (",".join(failed), ",".join(fallback),
                   " (%s)" % extra_note if extra_note else ""))
            supplemented = True
    if pending_cache and not supplemented:
        store = load_cache()
        store[ckey] = {"at": now,
                       "results": {k: [list(r) for r in v]
                                   for k, v in results.items()},
                       "status": status,
                       "searxng_unresponsive": LAST_SEARXNG_UNRESPONSIVE}
        save_cache(store)
    # domain filter: every engine/variant list, cache hits included (the
    # cache stays raw recall), always before RRF so filtered URLs never leak
    results, filtered = apply_domain_filter(results, include_domains,
                                            exclude_domains)
    # cooldown persistence: single writer here, never in workers (MF3)
    if use_cooldown and not cached:
        for e, s in recall_status.items():
            if s.startswith("ok"):
                continue
            cat, ttl = classify_error(s)
            if ttl > 0:
                cooldown[e] = now + ttl
        save_cooldown(cooldown)  # single writer; also prunes expired entries
    if not results and not filtered:
        if any(s.startswith("ok") for s in status.values()):
            sys.exit("no results (engines healthy, query too narrow): %s"
                     % json.dumps(status))
        sys.exit("all engines failed: %s" % json.dumps(status))
    weights = {name: base_weights.get(name.split("#", 1)[0], 1.0)
               for name in results}
    fuse_t0 = time.perf_counter()
    ranked = rrf_fuse(results, a.rrf_top, weights, diversify=diversify,
                      dedupe=dedupe, domain_boost=auth_domains,
                      boost_factor=auth_boost)
    dedupe_ms = (time.perf_counter() - fuse_t0) * 1000.0

    note = ""
    rerank_ms = 0.0
    blend = profile.get("rerank_blend")
    if not a.no_jina:
        rerank_t0 = time.perf_counter()
        if a.fast:
            preferred = getenv("JINA_RERANK_MODEL", JINA_MODELS[0])
            reranked, err = jina_rerank(a.query, ranked, a.top_n,
                                       timeout=8, models=[preferred],
                                       max_attempts=1, blend=blend)
        else:
            reranked, err = jina_rerank(a.query, ranked, a.top_n,
                                       blend=blend)
        rerank_ms = (time.perf_counter() - rerank_t0) * 1000.0
        if reranked is not None:
            ranked = reranked
        else:
            note = err
            if portal and note:
                degraded.append(note)
    ranked = ranked[:a.top_n]
    distinct_domains = len({domain_of(u) for u, _, _ in ranked})
    if use_learn:
        learn.record_surfaced(profile_name, ranked)

    answer_text, answer_note = None, ""
    if a.answer:
        fetched_texts = {}
        fetch_n = max(0, min(3, int(a.answer_fetch or 0)))
        if fetch_n > 0:
            fetched_texts, fnotes = _prefetch_for_answer(
                [u for u, _, _ in ranked[:fetch_n]])
            degraded.extend(fnotes)
        answer_text, answer_note, _used_mode, ans_deg = answer_with_fallback(
            a.query, ranked, mode=a.answer, prompt_template=prompts["answer"],
            fallback_order=answer_order, fetched_texts=fetched_texts)
        degraded.extend(ans_deg)

    health = {"cached": cached, "diversify": diversify,
              "profile": profile_name,
              "distinct_domains": distinct_domains}
    if portal:
        health["portal"] = portal["id"]
    primary = [e for e in (profile.get("primary") or []) if e in engines]
    if primary:
        health["primary"] = {e: status.get(e, "not run") for e in primary}
    if include_domains or exclude_domains:
        health["domain_filter"] = {"include": list(include_domains),
                                   "exclude": list(exclude_domains)}
    if filtered:
        health["filtered"] = filtered
    if LAST_SEARXNG_UNRESPONSIVE:
        health["searxng_unresponsive"] = LAST_SEARXNG_UNRESPONSIVE
    if fast_note:
        health["recall_budget"] = fast_note
    if a.fast:
        health["fast"] = {"budget_secs": a.budget_secs,
                          "min_sources": fast_min_sources}
    if degraded:
        health["degraded"] = degraded
    # observability (--json only; markdown output stays unchanged)
    health["latency_ms"] = {e: int(round(ms))
                            for e, ms in sorted(timings.items())}
    health["timing"] = {"recall_ms": int(round(recall_ms)),
                        "rerank_ms": int(round(rerank_ms)),
                        "dedupe_ms": int(round(dedupe_ms))}
    if cached:
        health["cache_age_secs"] = int(round(cache_age))
    if a.json_out:
        print(json.dumps({
            "query": a.query,
            "engines": status,
            "expand": expand_note or None,
            "rerank": note or None,
            "answer": answer_text,
            "answer_note": answer_note or None,
            "engine_health": health,
            "results": [
                {"rank": i, "title": m["title"] or u, "url": u,
                 "snippet": clip_snippet(m["snippet"], snippet_chars),
                 "engines": m["engines"],
                 "rrf": round(sc, 6)}
                for i, (u, sc, m) in enumerate(ranked, 1)],
        }, ensure_ascii=False))
        return

    out = ["# Aggregate search: %s" % a.query, ""]
    out.append("engines: " + "; ".join("%s=%s" % kv for kv in status.items()))
    if portal:
        out.append("portal: %s" % portal["id"])
    elif profile_name != "general":
        out.append("profile: %s" % profile_name)
    if cached:
        out.append("cache: hit (5min TTL)")
    if filtered:
        out.append("domain filter: %d removed (%s)"
                   % (sum(filtered.values()),
                      ", ".join("%s=%d" % kv
                                for kv in sorted(filtered.items()))))
    if fast_note:
        out.append("recall: " + fast_note)
    if LAST_SEARXNG_UNRESPONSIVE:
        out.append("searxng upstream: " + "; ".join(
            "%s=%s" % (n, r) for n, r in LAST_SEARXNG_UNRESPONSIVE[:6]))
    if expand_note:
        out.append(expand_note)
    if note:
        out.append("rerank: " + note)
    if answer_note:
        out.append(answer_note)
    if degraded:
        out.append("degraded: " + "; ".join(degraded))
    out.append("")
    if answer_text:
        out.append("## Answer")
        out.append(answer_text)
        out.append("")
    for i, (u, sc, m) in enumerate(ranked, 1):
        safe_url = (u.replace("(", "%28").replace(")", "%29")
                    .replace(" ", "%20"))
        out.append("%d. [%s](%s) — via %s (rrf %.4f)"
                   % (i, md_escape(m["title"] or u), safe_url,
                      "+".join(m["engines"]), sc))
        if m["snippet"]:
            out.append("   %s" % clip_snippet(m["snippet"], snippet_chars))
    print("\n".join(out))


if __name__ == "__main__":
    main()
    if _FAST_MODE or "--fast" in sys.argv:
        # P1-1: non-daemon pool threads are join()ed at interpreter exit,
        # which would void the recall budget. Output is done: flush + go.
        sys.stdout.flush()
        os._exit(0)
