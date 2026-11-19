#!/usr/bin/env python3
"""Aggregate search: parallel multi-engine recall + RRF fusion + Jina rerank.

Engines: exa, brave, serper (Google SERP bridge), firecrawl, tavily,
anysearch (MCP-over-HTTP), searxng (self-hosted), deepseek (official
in-house engine, POST /v1/tools/search; results arrive pre-scored with
rerank_score). Stdlib only, no dependencies. Any single engine may fail or
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
AGG_SEARCH_SNIPPET_CHARS, AGG_SEARCH_FAST and AGG_SEARCH_NO_JINA
(1/true/yes). They apply only when the matching flag is omitted, and
invalid values are ignored. `--show-config` prints the effective settings
as JSON (credentials as booleans, never key values) and exits before any
network call; `--json` output carries per-engine latency and phase timings
under engine_health.
"""
import argparse
import concurrent.futures as cf
import datetime
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

ENV_FILES = (os.path.expanduser("~/.config/agent-env"),
             os.path.expanduser("~/.config/ai-keys.env"))
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
# engine -> env vars proving credentials (any one suffices); absent = keyless
CREDENTIAL_ENV = {
    "exa": ("EXA_API_KEY",),
    "brave": ("BRAVE_API_KEY",),
    "serper": ("SERPER_API_KEY",),
    "firecrawl": ("FIRECRAWL_API_KEY",),
    "anysearch": ("MCP_ANYSEARCH_API_KEY",),
    "tavily": ("TAVILY_API_KEY", "TAVILY_API_KEY_BACKUP"),
    "deepseek": ("DEEPSEEK_API_KEY",),
}
# last SearXNG unresponsive_engines report [[name, reason], ...]
LAST_SEARXNG_UNRESPONSIVE = []
# last per-engine wall time in ms (engine -> ms); variant runs fold by max.
# Written by recall/recall_multi/recall_flat, read by main() for --json.
LAST_TIMINGS = {}
# CLI defaults; AGG_SEARCH_* env vars may replace them when a flag is omitted
DEFAULT_ENGINES = "exa,brave,serper,deepseek,firecrawl,anysearch,tavily,searxng"
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
              include_domains=(), exclude_domains=(), snippet_chars=400):
    parts = [query, ",".join(sorted(engines)), recency or "",
             str(per_engine), str(rrf_top), str(top_n), str(bool(no_jina)),
             str(expand), str(bool(diversify)),
             "fast" if fast else "full", str(budget_secs), str(min_sources),
             ",".join(sorted(parse_domains(include_domains))),
             ",".join(sorted(parse_domains(exclude_domains))),
             str(clamp_snippet_chars(snippet_chars)),
             "%.1f" % ENGINE_TIMEOUT,
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



def http_json(url, *, method="GET", body=None, headers=None, timeout=None):
    if timeout is None:
        timeout = ENGINE_TIMEOUT
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    with urllib.request.urlopen(req, timeout=_effective_timeout(timeout)) as r:
        return json.loads(r.read().decode())


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


# Zero-width and bidi-control characters hide injected instructions in page
# text an agent is about to read (Trojan Source and friends); engine-supplied
# titles/snippets are cleaned before they reach the model.
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064"
                        "\u2066-\u2069\ufeff]")


def clean_text(text):
    return _INVISIBLE.sub("", text or "")


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
    with urllib.request.urlopen(req, timeout=_effective_timeout(ENGINE_TIMEOUT)) as r:
        d = json.loads(r.read().decode())
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
    with urllib.request.urlopen(req, timeout=_effective_timeout(timeout)) as r:
        d = json.loads(r.read().decode())
    LAST_SEARXNG_UNRESPONSIVE = d.get("unresponsive_engines") or []
    return ([(norm_url(x["url"]), x.get("title", ""),
              (x.get("content") or "")[:400]) for x in d.get("results", [])[:n]
             if x.get("url")], None)


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


ENGINES = {
    # name: (fn(q, n, recency), rrf weight); index 1 is weight,
    # relied on by rrf_eval.py and main() — keep append-only.
    "exa": (lambda q, n, r: eng_exa(q, n), 1.0),
    "brave": (lambda q, n, r: eng_brave(q, n, r), 1.0),
    "serper": (lambda q, n, r: eng_serper(q, n, r), 1.0),
    "firecrawl": (lambda q, n, r: eng_firecrawl(q, n, r), 1.0),
    "anysearch": (lambda q, n, r: eng_anysearch(q, n), 1.0),
    "tavily": (lambda q, n, r: eng_tavily(q, n, r), 1.0),
    "deepseek": (lambda q, n, r: eng_deepseek(q, n, r), 1.0),
    "searxng": (lambda q, n, r: eng_searxng(q, n, r), 0.5),
}


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


def expand_queries(q, n_variants):
    """Rewrite the query into n_variants-1 alternatives via local ollama
    (native API, thinking disabled — the OpenAI-compat endpoint ignores
    `think` and burns the budget on reasoning). Returns (variants, note)."""
    base = getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    model = getenv("EXPAND_MODEL", "qwen3.5:9b")
    want = n_variants - 1
    prompt = ("Generate %d alternative web-search queries for the query "
              "below. One query per line, no numbering, no explanations, "
              "keep the original language and key entities.\n\nQuery: %s"
              % (want, q))
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
                deadline=None):
    """Single-pool recall over (variant, engine) tasks with a shared deadline.
    Only used behind --fast; legacy recall_multi stays the default path.
    Returns (results, status, note): note carries sourcesTarget/achieved when
    min_sources is unmet (advisory, never fails the search for it).
    Only engines with actual rows count toward min_sources — a healthy
    empty (`ok (0)`) must not trigger early return (P2-4)."""
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


def _flat_one(query, n, recency, eng, qi, deadline):
    _TLS.deadline = deadline  # thread-local: visible only in this worker
    t0 = time.perf_counter()
    try:
        rows, err = ENGINES[eng][0](query, n, recency)
    except Exception as e:  # noqa: BLE001
        rows, err = None, "%s: %s" % (type(e).__name__, e)
    return ("%s#%d" % (eng, qi)), rows, err, (time.perf_counter() - t0) * 1000.0


def rrf_fuse(results, top, weights=None, k=None, diversify=False):
    """Fuse per-engine rankings; `weights` overrides ENGINES defaults
    (used by scripts/rrf_eval.py for parameter sweeps and by --expand,
    where each engine#variant list is a separate ranking).
    `k` overrides RRF_K (lets the sweep harness call the real function);
    `diversify` round-robins by eTLD+1 BEFORE truncation so Jina still
    sees diverse candidates (MF5: never diversify after Jina)."""
    kk = RRF_K if k is None else k
    score, meta = {}, {}
    for eng, rows in results.items():
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
    ordered = sorted(score, key=score.get, reverse=True)
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
                max_attempts=2):
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
                              body={"model": model, "query": query,
                                    "documents": docs, "top_n": top_n},
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
                    order = [r["index"] for r in
                             sorted(d["results"],
                                    key=lambda r: r["relevance_score"],
                                    reverse=True)]
                except (KeyError, TypeError, AttributeError) as e:
                    return None, "jina failed (bad response: %s), kept RRF order" % type(e).__name__
                if not order:
                    return None, "jina returned no ranking, kept RRF order"
                return [ranked[i] for i in order if i < len(ranked)], None
    return None, "jina failed (%s), kept RRF order" % err


def answer_query(query, ranked, mode):
    """Synthesize a cited answer from the top results.
    mode: 'local' = ollama (free) | 'deepseek' = chat API (token-billed).
    Returns (text, note); best-effort, never blocks the result list."""
    ctx = []
    for i, (u, _sc, m) in enumerate(ranked[:8], 1):
        ctx.append("[%d] %s\n%s\n%s"
                   % (i, m["title"] or u, u, (m["snippet"] or "")[:300]))
    prompt = ("Answer the query using ONLY the search results below. Cite "
              "sources as [n]. Be concise. If the results are insufficient, "
              "say so. Answer in the query's language.\n\nQuery: %s\n\n"
              "Results:\n%s" % (query, "\n\n".join(ctx)))
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
    AGG_SEARCH_TOP_N must never crash a search or half-apply."""
    if a.engines == DEFAULT_ENGINES:
        raw = os.environ.get("AGG_SEARCH_ENGINES")
        names = [e.strip() for e in (raw or "").split(",") if e.strip()]
        if names and all(e in ENGINES for e in names):
            a.engines = ",".join(names)
    if a.top_n == DEFAULT_TOP_N:
        v = _env_int("AGG_SEARCH_TOP_N")
        if v is not None:
            a.top_n = v
    if a.per_engine == DEFAULT_PER_ENGINE:
        v = _env_int("AGG_SEARCH_PER_ENGINE")
        if v is not None:
            a.per_engine = v
    if a.snippet_chars == DEFAULT_SNIPPET_CHARS:
        v = _env_int("AGG_SEARCH_SNIPPET_CHARS")
        if v is not None:
            a.snippet_chars = v
    if a.fast is False and _env_flag("AGG_SEARCH_FAST"):
        a.fast = True
    if a.no_jina is False and _env_flag("AGG_SEARCH_NO_JINA"):
        a.no_jina = True
    return a


def show_config(a):
    """Effective settings for bug reports: credentials as booleans only.

    Every credential probe is `missing_credential(...) is None`; no key
    value is read into the payload, so this output is safe to paste."""
    req = [e.strip() for e in a.engines.split(",") if e.strip()]
    engines = [e for e in req if e in ENGINES] or list(ENGINES)
    return {
        "engines": engines,
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
        "diversify": not a.no_diversify,
        "include_domains": list(parse_domains(a.include_domains)),
        "exclude_domains": list(parse_domains(a.exclude_domains)),
        "cache": not a.no_cache,
        "cooldown": not a.no_cooldown,
        "answer": a.answer,
        "credentials": {e: missing_credential(e) is None for e in engines},
    }


def main():
    global ENGINE_TIMEOUT, _FAST_MODE
    ap = argparse.ArgumentParser(description="parallel multi-engine search + RRF + jina rerank")
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
    ap.add_argument("--answer", nargs="?", const="local", default=None,
                    choices=["local", "deepseek"], metavar="local|deepseek",
                    help="synthesize a cited answer from the top results "
                         "(bare flag = local ollama, free)")
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
    a = ap.parse_args()
    apply_env_defaults(a)
    ENGINE_TIMEOUT = max(1.0, min(120.0, a.engine_timeout))
    _FAST_MODE = bool(a.fast) and not a.show_config
    if a.show_config:
        print(json.dumps(show_config(a), ensure_ascii=False, sort_keys=True))
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
    include_domains = parse_domains(a.include_domains)
    exclude_domains = parse_domains(a.exclude_domains)
    snippet_chars = clamp_snippet_chars(a.snippet_chars)
    fast_min_sources = (a.min_sources if a.min_sources is not None
                        else (2 if a.fast else 0))
    use_cache = not a.no_cache
    use_cooldown = not a.no_cooldown

    # 1a. credential prefilter: missing keys never take a thread (MF1b companion)
    status = {}
    for e in list(engines):
        miss = missing_credential(e)
        if miss:
            status[e] = "skipped: no %s" % miss
    engines = [e for e in engines if e not in status]
    # cooldown filter: quota/antibot/timeout only, empty never cools (MF3)
    cooldown = load_cooldown() if use_cooldown else {}
    now = time.time()
    for e in list(engines):
        if cooldown.get(e, 0) > now:
            status[e] = "skipped: cooling (%s)" % time.strftime(
                "%M:%S", time.gmtime(cooldown[e] - now))
            engines.remove(e)
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
            variants, expand_note = expand_queries(a.query, a.expand)
        finally:
            _TLS.deadline = None
        queries += variants

    # disk cache (MF4): key covers every output-affecting input incl. mode
    ckey = cache_key(a.query, engines, a.recency, a.per_engine, a.rrf_top,
                     a.top_n, a.no_jina, a.expand, diversify,
                     fast=a.fast, budget_secs=a.budget_secs,
                     min_sources=fast_min_sources,
                     include_domains=include_domains,
                     exclude_domains=exclude_domains,
                     snippet_chars=snippet_chars)
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
                deadline=fast_deadline)
        else:
            results, recall_status = recall_multi(queries, a.per_engine,
                                                 engines, a.recency)
            fast_note = ""
        recall_ms = (time.perf_counter() - recall_t0) * 1000.0
        timings = dict(LAST_TIMINGS)
        if use_cache and len(queries) == 1 and not a.answer and results:
            store = load_cache()
            store[ckey] = {"at": now,
                           "results": {k: [list(r) for r in v]
                                       for k, v in results.items()},
                           "status": recall_status,
                           "searxng_unresponsive": LAST_SEARXNG_UNRESPONSIVE}
            save_cache(store)
    # domain filter: every engine/variant list, cache hits included (the
    # cache stays raw recall), always before RRF so filtered URLs never leak
    results, filtered = apply_domain_filter(results, include_domains,
                                            exclude_domains)
    status.update(recall_status)
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
    weights = None
    if len(queries) > 1:
        weights = {name: ENGINES.get(name.split("#", 1)[0], (None, 1.0))[1]
                   for name in results}
    fuse_t0 = time.perf_counter()
    ranked = rrf_fuse(results, a.rrf_top, weights, diversify=diversify)
    dedupe_ms = (time.perf_counter() - fuse_t0) * 1000.0

    note = ""
    rerank_ms = 0.0
    if not a.no_jina:
        rerank_t0 = time.perf_counter()
        if a.fast:
            preferred = getenv("JINA_RERANK_MODEL", JINA_MODELS[0])
            reranked, err = jina_rerank(a.query, ranked, a.top_n,
                                       timeout=8, models=[preferred],
                                       max_attempts=1)
        else:
            reranked, err = jina_rerank(a.query, ranked, a.top_n)
        rerank_ms = (time.perf_counter() - rerank_t0) * 1000.0
        if reranked is not None:
            ranked = reranked
        else:
            note = err
    ranked = ranked[:a.top_n]
    distinct_domains = len({domain_of(u) for u, _, _ in ranked})

    answer_text, answer_note = None, ""
    if a.answer:
        answer_text, answer_note = answer_query(a.query, ranked, a.answer)

    health = {"cached": cached, "diversify": diversify,
              "distinct_domains": distinct_domains}
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
    out.append("")
    if answer_text:
        out.append("## Answer")
        out.append(answer_text)
        out.append("")
    for i, (u, sc, m) in enumerate(ranked, 1):
        out.append("%d. [%s](%s) — via %s (rrf %.4f)"
                   % (i, m["title"] or u, u, "+".join(m["engines"]), sc))
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
