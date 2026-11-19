#!/usr/bin/env python3
"""In-container fetch — runs ONLY inside crawl-agent (has scrapling + browsers).

Tier ladder on --tier auto (first hit wins):
  static  -> Fetcher (curl_cffi, TLS impersonation, no browser)
  dynamic -> DynamicFetcher (playwright chromium, JS rendering)
  stealth -> StealthyFetcher (patchright chromium, Cloudflare solving)

Prints one JSON line per URL on stdout (scrapling INFO logs go to stderr).
Exit 0 = >=1 ok; 1 = all failed; 3 = blocked on every URL (non-200, block
markers, or JS-shell thin content after the last tier).
"""
import argparse
import json
import os
import re
import sys

BLOCK_MARKERS = (
    "just a moment", "cf-challenge", "attention required",
    "verify you are human", "enable javascript and cookies",
    "access denied", "captcha", "人机验证", "请开启javascript",
)


def squash(s):
    return re.sub(r"\s+", " ", s or "").strip()


def needs_escalation(status, html, text):
    """True when the page did not return real content (so the ladder climbs).

    Thin-text heuristic catches JS shells: a 5 KB+ HTML skeleton rendering
    under 300 chars of text is a client-rendered page, not an article.
    """
    if status != 200:
        return True
    low = (html or "").lower()
    if any(m in low for m in BLOCK_MARKERS):
        return True
    return len(text or "") < 300 and len(html or "") > 3000


def result(url, tier_used, resp=None, include_md=False, error="", text_limit=8000):
    d = {"url": url, "tier_used": tier_used, "status": None,
         "title": "", "text": "", "markdown": "", "blocked": False,
         "error": error}
    if resp is None:
        return d
    d["status"] = resp.status
    try:
        d["title"] = squash(resp.css("title::text").get(default=""))
    except Exception:  # noqa: BLE001 — best-effort extraction, never aborts
        pass
    try:
        d["text"] = squash(resp.get_all_text())[:text_limit]
    except Exception:  # noqa: BLE001
        pass
    if include_md:
        try:
            d["markdown"] = resp.markdown(main_content_only=True)[:text_limit]
        except Exception:  # noqa: BLE001
            pass
    return d


def fetch_one(url, args):
    from scrapling.fetchers import (  # noqa: PLC0415
        DynamicFetcher,
        Fetcher,
        StealthyFetcher,
    )

    tiers = {"static": ("static",), "dynamic": ("dynamic",),
             "stealth": ("stealth",)}.get(args.tier,
                                          ("static", "dynamic", "stealth"))
    explicit = args.proxy or os.environ.get("CRAWL_PROXY")
    wildcard = os.environ.get("NO_PROXY") == "*" or os.environ.get("no_proxy") == "*"
    # explicit CRAWL_PROXY/--proxy wins; otherwise wildcard NO_PROXY means direct;
    # else fall back to the inherited HTTPS_PROXY.
    proxy = explicit or (None if wildcard else os.environ.get("HTTPS_PROXY"))
    headless = not (args.headful or args.headful_container)
    last = None
    for tier in tiers:
        try:
            if tier == "static":
                kw = {"stealthy_headers": True, "impersonate": "chrome",
                      "timeout": args.timeout}
                if proxy:
                    kw["proxy"] = proxy
                resp = Fetcher.get(url, **kw)
            elif tier == "dynamic":
                kw = {"headless": headless, "network_idle": True,
                      "timeout": args.timeout * 1000, "extra_flags": ["--no-sandbox"],
                      "user_data_dir": "/tmp/home/chromium", "load_dom": True}
                if proxy:
                    kw["proxy"] = proxy
                resp = DynamicFetcher.fetch(url, **kw)
            else:
                kw = {"headless": headless, "solve_cloudflare": True,
                      "network_idle": True, "timeout": args.timeout * 1000,
                      "extra_flags": ["--no-sandbox"],
                      "user_data_dir": "/tmp/home/chromium"}
                if proxy:
                    kw["proxy"] = proxy
                resp = StealthyFetcher.fetch(url, **kw)
        except Exception as e:  # noqa: BLE001 — per-URL error lands in JSON
            last = result(url, tier, error="%s: %s" % (type(e).__name__, e),
                          text_limit=args.text_limit)
            continue
        body = resp.body
        html = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
        d = result(url, tier, resp, include_md=(args.out == "md"),
                   text_limit=args.text_limit)
        if not needs_escalation(resp.status, html, d["text"]):
            return d
        d["blocked"] = True
        last = d
    return last or result(url, None, error="no tier attempted",
                          text_limit=args.text_limit)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", action="append", required=True)
    ap.add_argument("--tier", default="auto",
                    choices=("auto", "static", "dynamic", "stealth"))
    ap.add_argument("--timeout", type=int, default=45)
    ap.add_argument("--out", default="json", choices=("json", "md"))
    ap.add_argument("--text-limit", type=int, default=8000)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--headful-container", action="store_true")
    ap.add_argument("--persist-container", action="store_true")
    args = ap.parse_args()

    n_ok, n_blocked = 0, 0
    for u in args.url:
        d = fetch_one(u, args)
        print(json.dumps(d, ensure_ascii=False))
        sys.stdout.flush()
        if d["error"]:
            continue
        if d["blocked"]:
            n_blocked += 1
        else:
            n_ok += 1
    if n_ok:
        return 0
    return 3 if n_blocked else 1


if __name__ == "__main__":
    sys.exit(main())
