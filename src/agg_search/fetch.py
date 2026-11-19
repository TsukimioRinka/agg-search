#!/usr/bin/env python3
"""Tiered fetch orchestrator — host side, stdlib only, zero dependencies.

Rung 0 (when DEEPSEEK_API_KEY is set): DeepSeek's official Fetch API
(POST /v1/tools/fetch) — server-side fetch returning clean Markdown, one
HTTP call, no container. Thin/failed results fall through to the container
ladder. `--no-deepseek` skips it; `--tier deepseek` runs it alone.

Rungs 1-3 run inside the crawl-agent container (see docs/fetching.md);
the host never loads scrapling or a browser. This script only assembles and
runs `crawl-env` and relays its JSON output.

Exit codes: 0 = >=1 URL ok (prints JSON lines); 1 = all URLs failed;
2 = launcher/image missing; 3 = blocked on every URL (container reports blocked).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PKG_DIR = str(Path(__file__).resolve().parent)
CRAWL_ENV_FALLBACK = os.path.expanduser("~/.local/bin/crawl-env")
ENV_FILES = (os.path.expanduser("~/.config/agent-env"),
             os.path.expanduser("~/.config/ai-keys.env"))


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
    return re.sub(r"\s+", " ", s or "").strip()


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
            r = urllib.request.urlopen(req, timeout=a.timeout)
        except urllib.error.HTTPError as e:
            if e.code not in (408, 429) and e.code < 500:
                raise
            time.sleep(1.2)
            r = urllib.request.urlopen(req, timeout=a.timeout)
        with r:
            d["status"] = r.status
            body = json.loads(r.read().decode())
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
    d["title"] = body.get("title") or ""
    d["text"] = text[:a.text_limit]
    if a.out == "md":
        d["markdown"] = md[:a.text_limit]
    return d


def find_launcher():
    hit = shutil.which("crawl-env")
    if hit:
        return hit
    if os.path.isfile(CRAWL_ENV_FALLBACK) and os.access(CRAWL_ENV_FALLBACK, os.X_OK):
        return CRAWL_ENV_FALLBACK
    return None


def main():
    ap = argparse.ArgumentParser(description="tiered page fetch: DeepSeek Fetch API rung + container ladder")
    ap.add_argument("--url", action="append", required=True)
    ap.add_argument("--tier", default="auto",
                    choices=("auto", "deepseek", "static", "dynamic", "stealth"))
    ap.add_argument("--no-deepseek", action="store_true",
                    help="skip the DeepSeek Fetch API rung in auto mode")
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--out", default="json", choices=("json", "md"))
    ap.add_argument("--text-limit", type=int, default=8000)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--persist", action="store_true")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--host-net", action="store_true",
                    help="share host network so a loopback-only proxy is reachable")
    a = ap.parse_args()

    ok = blocked = False
    leftover = list(a.url)
    ds_key = getenv("DEEPSEEK_API_KEY")
    if a.tier == "deepseek" or (a.tier == "auto" and ds_key and not a.no_deepseek):
        leftover = []
        for u in a.url:
            d = deepseek_fetch(u, a, ds_key)
            if d["blocked"] or d["error"]:
                leftover.append(u)
                blocked = blocked or d["blocked"]
                if a.tier == "deepseek":  # auto defers silently to the ladder
                    print(json.dumps(d, ensure_ascii=False))
                continue
            print(json.dumps(d, ensure_ascii=False))
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
    if p.stderr:
        print(p.stderr, file=sys.stderr, end="")
    if ok:
        return 0
    return 3 if blocked else 1


if __name__ == "__main__":
    sys.exit(main())
