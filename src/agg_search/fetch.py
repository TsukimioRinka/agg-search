#!/usr/bin/env python3
"""Tiered fetch orchestrator — host side, stdlib only, zero dependencies.

Rung 0 (when DEEPSEEK_API_KEY is set): DeepSeek's official Fetch API
(POST /v1/tools/fetch) — server-side fetch returning clean Markdown, one
HTTP call, no container. Thin/failed results fall through to the container
ladder. `--no-deepseek` skips it; `--tier deepseek` runs it alone.

Rungs 1-3 run inside the crawl-agent container (see docs/fetching.md);
the host never loads scrapling or a browser. This script only assembles and
runs `crawl-env` and relays its JSON output.

Every URL is pre-flighted by check_url before either rung: http(s) only,
no userinfo, and the resolved addresses must be public. `--allow-private`
lifts the private-target rules for local dev servers and host proxies.

Exit codes: 0 = >=1 URL ok (prints JSON lines); 1 = all URLs failed;
2 = launcher/image missing; 3 = blocked on every URL (container reports blocked).
"""
import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:  # package import (installed, `python -m agg_search.fetch`)
    from . import learn
except ImportError:  # pragma: no cover - direct script execution
    import learn  # type: ignore[no-redef]

PKG_DIR = str(Path(__file__).resolve().parent)
CRAWL_ENV_FALLBACK = os.path.expanduser("~/.local/bin/crawl-env")
ENV_FILES = (os.path.expanduser("~/.config/agent-env"),
             os.path.expanduser("~/.config/ai-keys.env"))
MAX_RESPONSE = 4 * 1024 * 1024
METADATA_HOSTS = ("metadata.google.internal",)
# Unconditional, even with --allow-private: cloud metadata is never a
# legitimate fetch target.
METADATA_IPS = frozenset({ipaddress.ip_address("169.254.169.254"),
                          ipaddress.ip_address("fd00:ec2::254")})
# page cache: agents re-read the same URLs while iterating; a 15 min shelf
# saves engine/container cost without going stale on a task timescale.
CACHE_DIR = os.environ.get("AGG_SEARCH_CACHE_DIR",
                           os.path.expanduser("~/.cache/agg-search"))
FETCH_CACHE_PATH = os.path.join(CACHE_DIR, "fetch_cache.json")
CACHE_TTL = 900
MAX_CACHE_ENTRIES = 100


def cache_key(url, a):
    parts = [url, a.tier, a.out, str(a.text_limit),
             str(bool(getattr(a, "allow_private", False)))]
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:32]


def load_cache():
    try:
        with open(FETCH_CACHE_PATH, encoding="utf-8") as f:
            store = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(store, dict):
        return {}
    return {k: v for k, v in store.items()
            if isinstance(v, dict) and isinstance(v.get("at"), (int, float))
            and isinstance(v.get("result"), dict)}


def save_cache(store):
    try:
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        while len(store) > MAX_CACHE_ENTRIES:
            oldest = min(store, key=lambda k: store[k].get("at", 0))
            del store[oldest]
        tmp = "%s.%d.tmp" % (FETCH_CACHE_PATH, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, FETCH_CACHE_PATH)
    except OSError:
        pass


def usable(d):
    """A page worth keeping: real text and no error/block marker."""
    return bool(d) and not d.get("error") and not d.get("blocked") \
        and bool(d.get("text"))


def _env_flag(key):
    return os.environ.get(key, "").strip().lower() in ("1", "true", "yes")


def getenv(key, default=None):
    v = os.environ.get(key)
    if v:
        return v
    for path in ENV_FILES:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    m = re.match(r'\s*(?:export\s+)?' + re.escape(key) + r'=(.*)',
                                 line)
                    if m:
                        return m.group(1).strip().strip('"\'')
        except OSError:
            pass
    return default


def squash(s):
    return re.sub(r"\s+", " ", strip_invisible(s or "")).strip()


# Zero-width and bidi-control characters are the usual hiding places for
# prompt-injection payloads (Trojan Source, "invisible" instructions). Page
# text shown to an agent must not carry them.
INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064"
                       "\u2066-\u2069\ufeff]")


def strip_invisible(text):
    return INVISIBLE.sub("", text)


def _non_public(ip):
    """True for an address a host-side fetch must not reach by default."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def parse_cidrs(value):
    if isinstance(value, str):
        value = value.split(",")
    out = []
    for item in value or ():
        item = str(item).strip()
        if not item:
            continue
        try:
            out.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(out)


def check_url(url, allow_private=False, allow_cidrs=()):
    """Pre-flight SSRF guard: returns (ok, reason).

    http(s) only, no userinfo, and the host must resolve (DNS checked here)
    to public addresses only. `allow_private` lifts the private-target rules
    for local dev servers; `allow_cidrs` permits specific ranges — TUN
    proxies that hand out fake-IPs (198.18.0.0/15) need it. The scheme,
    credential, cloud-metadata-name and metadata-IP rules always apply.
    """
    allowed_nets = parse_cidrs(allow_cidrs)
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError as e:
        return False, "invalid URL: %s" % e
    scheme = (p.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, "unsupported scheme: %s" % (scheme or "(none)")
    if "@" in p.netloc:
        return False, "userinfo credentials are not allowed"
    try:
        host = (p.hostname or "").rstrip(".").lower()
        port = p.port or (443 if scheme == "https" else 80)
    except ValueError:
        return False, "invalid port"
    if not host:
        return False, "missing host"
    if any(ord(c) > 127 for c in host):
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return False, "invalid IDN host"
    if host in METADATA_HOSTS:
        return False, "metadata host: %s" % host
    if not allow_private and (host == "localhost"
                              or host.endswith(".localhost")
                              or host.endswith(".local")):
        return False, "private host: %s" % host
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return False, "cannot resolve host: %s" % host
    if not infos:
        return False, "cannot resolve host: %s" % host
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            return False, "invalid address for host: %s" % host
        if ip in METADATA_IPS:
            return False, "metadata address: %s" % ip
    if not allow_private:
        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if any(ip in net for net in allowed_nets):
                continue
            if _non_public(ip):
                return False, "non-public address: %s" % ip
    return True, ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse 3xx: a redirect must not silently become the fetch target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def deepseek_fetch(url, a, key):
    """One Fetch-API call (with one retry on 408/429/5xx); returns a
    container-shaped result dict."""
    d = {"url": url, "tier_used": "deepseek", "status": None, "title": "",
         "text": "", "markdown": "", "blocked": False, "error": ""}
    if not key:
        d["error"] = "no DEEPSEEK_API_KEY (see ENV.md)"
        return d
    base = getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    req = urllib.request.Request(
        base + "/v1/tools/fetch", method="POST",
        data=json.dumps({"url": url}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    try:
        try:
            r = _OPENER.open(req, timeout=a.timeout)
        except urllib.error.HTTPError as e:
            if e.code not in (408, 429) and e.code < 500:
                raise
            time.sleep(1.2)
            r = _OPENER.open(req, timeout=a.timeout)
        with r:
            d["status"] = r.status
            raw = r.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                d["error"] = "response too large"
                return d
            body = json.loads(raw.decode())
    except urllib.error.HTTPError as e:
        # 403 = rejected by security policy, 404 = no usable content
        # (unavailable URL / site policy / unsupported type): both are
        # escalation signals for the ladder, not hard errors.
        d["status"] = e.code
        d["blocked"] = e.code in (403, 404)
        try:
            msg = json.loads(e.read().decode())["error"]["message"]
        except Exception:  # noqa: BLE001 — error body is best-effort
            msg = ""
        d["error"] = "HTTP %d%s" % (e.code, (": " + msg[:200]) if msg else "")
        return d
    except Exception as e:  # noqa: BLE001 — per-URL error lands in JSON
        d["error"] = "%s: %s" % (type(e).__name__, e)
        return d
    md = body.get("content") or ""
    text = squash(md)
    if len(text) < 300 and len(md) < 300:  # thin/empty page -> climb the ladder
        d["blocked"] = True
        d["error"] = "thin content (%d chars)" % len(text)
        return d
    d["title"] = strip_invisible(body.get("title") or "")
    d["text"] = text[:a.text_limit]
    if a.out == "md":
        d["markdown"] = strip_invisible(md[:a.text_limit])
    return d


def find_launcher():
    hit = shutil.which("crawl-env")
    if hit:
        return hit
    if os.path.isfile(CRAWL_ENV_FALLBACK) and os.access(CRAWL_ENV_FALLBACK, os.X_OK):
        return CRAWL_ENV_FALLBACK
    return None


def main():
    from agg_search import __version__
    ap = argparse.ArgumentParser(description="tiered page fetch: DeepSeek Fetch API rung + container ladder")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {__version__}")
    ap.add_argument("--url", action="append", required=True)
    ap.add_argument("--tier", default="auto",
                    choices=("auto", "deepseek", "static", "dynamic", "stealth"))
    ap.add_argument("--no-deepseek", action="store_true",
                    help="skip the DeepSeek Fetch API rung in auto mode")
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--out", default="json", choices=("json", "md"))
    ap.add_argument("--text-limit", type=int, default=8000,
                    help="characters of text kept per page (clamped 500..200000)")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--persist", action="store_true")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--host-net", action="store_true",
                    help="share host network so a loopback-only proxy is reachable")
    ap.add_argument("--allow-private", action="store_true",
                    help="permit loopback/private targets (local dev servers, host proxies)")
    ap.add_argument("--no-cache", action="store_true",
                    help="bypass the 15 min page cache")
    ap.add_argument("--no-learn", action="store_true",
                    help="do not record which engines surfaced a page you "
                         "read (env AGG_SEARCH_NO_LEARN)")
    ap.add_argument("--allow-cidrs", default=getenv("AGG_SEARCH_ALLOW_CIDRS", ""),
                    help="comma-separated address ranges the SSRF guard should "
                         "permit (TUN proxies hand out 198.18.0.0/15 fake-IPs); "
                         "metadata IPs stay blocked")
    a = ap.parse_args()
    a.text_limit = max(500, min(200000, a.text_limit))
    allow_cidrs = parse_cidrs(a.allow_cidrs)

    ok = blocked = False
    guarded = []
    for u in a.url:
        good, reason = check_url(u, a.allow_private, allow_cidrs)
        if good:
            guarded.append(u)
            continue
        print(json.dumps({"url": u, "error": "blocked: " + reason,
                          "blocked": True, "tier_used": "guard"},
                         ensure_ascii=False))
        blocked = True
    if not guarded:
        return 3

    # page cache: hits are printed as results with cached=true and never
    # reach an engine rung; only usable pages are stored.
    use_cache = not a.no_cache
    use_learn = not a.no_learn and not _env_flag("AGG_SEARCH_NO_LEARN")
    store = load_cache() if use_cache else {}
    now = time.time()

    def remember(d):
        if usable(d):
            if use_cache:
                store[cache_key(d["url"], a)] = {"at": now, "result": d}
                save_cache(store)
            if use_learn:
                learn.record_used(d["url"])

    pending = []
    for u in guarded:
        hit = store.get(cache_key(u, a))
        if hit and now - hit.get("at", 0) < CACHE_TTL:
            print(json.dumps(dict(hit["result"], cached=True),
                             ensure_ascii=False))
            ok = True
        else:
            pending.append(u)
    guarded = pending

    leftover = guarded
    ds_key = getenv("DEEPSEEK_API_KEY")
    if a.tier == "deepseek" or (a.tier == "auto" and ds_key and not a.no_deepseek):
        leftover = []
        for u in guarded:
            d = deepseek_fetch(u, a, ds_key)
            if d["blocked"] or d["error"]:
                leftover.append(u)
                blocked = blocked or d["blocked"]
                if a.tier == "deepseek":  # auto defers silently to the ladder
                    print(json.dumps(d, ensure_ascii=False))
                continue
            print(json.dumps(d, ensure_ascii=False))
            remember(d)
            ok = True
        if a.tier == "deepseek":
            return 0 if ok else (3 if blocked else 1)

    if leftover:
        launcher = find_launcher()
        if launcher is None:
            print("crawl-env 未安装：见 docs/fetching.md", file=sys.stderr)
            return 0 if ok else 2

    if not leftover:
        return 0 if ok else (3 if blocked else 1)

    inner = ["python3", PKG_DIR + "/fetch_container.py",
             "--tier", "auto" if a.tier == "auto" else a.tier,
             "--timeout", str(a.timeout),
             "--out", a.out, "--text-limit", str(a.text_limit)]
    if a.allow_private:
        inner.append("--allow-private")
    if allow_cidrs:
        inner += ["--allow-cidrs", ",".join(str(n) for n in allow_cidrs)]
    if a.persist:
        inner.append("--persist-container")
    if a.headful:
        inner.append("--headful-container")
    # Only an explicit --proxy is forwarded: the shell's HTTPS_PROXY typically
    # points at a host-loopback proxy the container cannot reach. crawl-env
    # forwards CRAWL_PROXY itself (with loopback rewriting).
    if a.proxy:
        inner += ["--proxy", a.proxy]
    for u in leftover:
        inner += ["--url", u]

    cmd = [launcher]
    if a.persist:
        cmd.append("--persist")
    if a.headful:
        cmd.append("--headful")
    if a.host_net:
        cmd.append("--host-net")
    cmd += ["--ro-mount", PKG_DIR, "--"] + inner

    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout * len(leftover) + 120)
    except FileNotFoundError:
        print("docker 不可用（crawl-env 依赖 docker）", file=sys.stderr)
        return 0 if ok else 2
    except subprocess.TimeoutExpired:
        print("fetch 超时：容器未在预期时间内返回", file=sys.stderr)
        return 0 if ok else 1

    if p.returncode == 125 or "docker: command not found" in (p.stderr or ""):
        print("docker 不可用：见 docs/fetching.md 故障排查", file=sys.stderr)
        return 0 if ok else 2
    if "no such image" in (p.stderr or "") or "Unable to find image" in (p.stderr or ""):
        print("crawl-agent 镜像缺失：docker build -t crawl-agent:latest "
              "~/workspace/crawl-agent", file=sys.stderr)
        return 0 if ok else 2

    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        print(line)
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("error"):
            continue
        if d.get("blocked"):
            blocked = True
        else:
            ok = True
            remember(d)
    if p.stderr:
        print(p.stderr, file=sys.stderr, end="")
    if ok:
        return 0
    return 3 if blocked else 1


if __name__ == "__main__":
    sys.exit(main())
