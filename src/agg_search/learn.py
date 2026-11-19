#!/usr/bin/env python3
"""Local feedback loop: which engines surfaced results an agent then read.

`search` records the engines behind every returned URL; `fetch` records when
one of those URLs is actually read. `agg-search-tune` turns surfaced/used
counts into small RRF weight multipliers, which every later search applies
on top of its profile weights.

Only counters, engine names and hashed URLs are stored — never queries, page
text or raw URLs. Disable with `--no-learn` (or AGG_SEARCH_NO_LEARN=1).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse

CACHE_DIR = os.environ.get("AGG_SEARCH_CACHE_DIR",
                           os.path.expanduser("~/.cache/agg-search"))
FEEDBACK_PATH = os.path.join(CACHE_DIR, "feedback.json")
RECENT_PATH = os.path.join(CACHE_DIR, "recent.json")
WEIGHTS_DIR = os.environ.get("AGG_SEARCH_CONFIG_DIR",
                             os.path.expanduser("~/.config/agg-search"))
WEIGHTS_PATH = os.path.join(WEIGHTS_DIR, "weights.json")

MAX_RECENT = 400
BASELINE_USED_RATE = 0.15  # share of surfaced results an agent reads
PRIOR = 20.0               # pseudo-observations shrinking tiny samples to 1.0
MIN_SURFACED = 30
MIN_MULTIPLIER, MAX_MULTIPLIER = 0.5, 2.0


def _atomic_write(path, obj):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def url_key(url):
    """Stable key for a result URL (fragment and trailing slash folded)."""
    u = (url or "").strip().split("#", 1)[0].rstrip("/")
    try:
        parts = urllib.parse.urlsplit(u)
        if parts.netloc:
            u = urllib.parse.urlunsplit(
                (parts.scheme, parts.netloc.lower(), parts.path, parts.query,
                 ""))
    except ValueError:
        pass
    return hashlib.sha256(u.encode("utf-8")).hexdigest()[:32]


def load_feedback():
    data = _load(FEEDBACK_PATH)
    if not isinstance(data.get("profiles"), dict):
        data["profiles"] = {}
    return data


def load_recent():
    data = _load(RECENT_PATH)
    if not isinstance(data.get("urls"), dict):
        data["urls"] = {}
    return data


def record_surfaced(profile, ranked, enabled=True):
    """Count which engines surfaced the final ranked URLs and remember the
    URL->engines mapping so a later `fetch` can attribute the read."""
    if not enabled or not ranked:
        return
    feedback = load_feedback()
    bucket = feedback["profiles"].setdefault(profile, {})
    recent = load_recent()
    now = time.time()
    for url, _score, meta in ranked:
        key = url_key(url)
        engines = [e for e in (meta.get("engines") or []) if e]
        if not engines:
            continue
        for engine in engines:
            counts = bucket.setdefault(engine, {"surfaced": 0, "used": 0})
            counts["surfaced"] += 1
        recent["urls"][key] = {"engines": engines, "profile": profile,
                               "at": now}
    # bound memory: drop the oldest entries
    if len(recent["urls"]) > MAX_RECENT:
        keep = sorted(recent["urls"].items(),
                      key=lambda kv: kv[1].get("at", 0))[-MAX_RECENT:]
        recent["urls"] = dict(keep)
    feedback["updated"] = now
    recent["updated"] = now
    _atomic_write(FEEDBACK_PATH, feedback)
    _atomic_write(RECENT_PATH, recent)


def record_used(url, enabled=True):
    """Attribute a successful page read to the engines that surfaced it."""
    if not enabled:
        return False
    recent = load_recent()
    entry = recent["urls"].get(url_key(url))
    if not entry:
        return False
    feedback = load_feedback()
    profile = entry.get("profile") or "general"
    bucket = feedback["profiles"].setdefault(profile, {})
    for engine in entry.get("engines") or []:
        counts = bucket.setdefault(engine, {"surfaced": 0, "used": 0})
        counts["used"] += 1
    feedback["updated"] = time.time()
    _atomic_write(FEEDBACK_PATH, feedback)
    return True


def multipliers(min_surfaced=MIN_SURFACED):
    """Per-profile engine weight multipliers from surfaced/used counts."""
    feedback = load_feedback()
    out = {}
    for profile, engines in feedback.get("profiles", {}).items():
        if not isinstance(engines, dict):
            continue
        per_engine = {}
        for engine, counts in engines.items():
            if not isinstance(counts, dict):
                continue
            surfaced = int(counts.get("surfaced") or 0)
            used = int(counts.get("used") or 0)
            if surfaced < min_surfaced:
                per_engine[engine] = 1.0
                continue
            rate = (used + BASELINE_USED_RATE * PRIOR) / (surfaced + PRIOR)
            per_engine[engine] = round(
                max(MIN_MULTIPLIER,
                    min(MAX_MULTIPLIER, rate / BASELINE_USED_RATE)), 3)
        out[profile] = per_engine
    return out


def tune(min_surfaced=MIN_SURFACED):
    data = {"tuned_at": time.time(),
            "baseline": BASELINE_USED_RATE,
            "profiles": multipliers(min_surfaced)}
    _atomic_write(WEIGHTS_PATH, data)
    return data


def load_multipliers(profile):
    data = _load(WEIGHTS_PATH)
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, dict):
        return {}
    entry = profiles.get(profile)
    return entry if isinstance(entry, dict) else {}


def report():
    feedback = load_feedback()
    rows = []
    for profile in sorted(feedback.get("profiles", {})):
        for engine, counts in sorted(feedback["profiles"][profile].items()):
            surfaced = int(counts.get("surfaced") or 0)
            used = int(counts.get("used") or 0)
            rate = (used / surfaced) if surfaced else 0.0
            rows.append((profile, engine, surfaced, used, rate))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Turn local surfaced/used feedback into RRF weight "
                    "multipliers (stored in ~/.config/agg-search/weights.json).")
    ap.add_argument("--show", action="store_true",
                    help="print the feedback table and current weights, "
                         "write nothing")
    ap.add_argument("--min-surfaced", type=int, default=MIN_SURFACED,
                    help="engines below this many surfaced results keep "
                         "weight 1.0 (default: %(default)s)")
    ap.add_argument("--reset", action="store_true",
                    help="delete the tuned weights (feedback is kept)")
    a = ap.parse_args(argv)

    if a.reset:
        try:
            os.remove(WEIGHTS_PATH)
        except FileNotFoundError:
            pass
        print("removed %s" % WEIGHTS_PATH)
        return 0

    rows = report()
    if rows:
        print("%-9s %-11s %9s %6s %7s" % ("profile", "engine", "surfaced",
                                          "used", "rate"))
        for profile, engine, surfaced, used, rate in rows:
            print("%-9s %-11s %9d %6d %6.1f%%" % (profile, engine, surfaced,
                                                  used, rate * 100))
    else:
        print("no feedback yet: run searches with the MCP server, then fetch "
              "the pages you actually read")

    if a.show:
        print()
        print(json.dumps(multipliers(a.min_surfaced), indent=2, sort_keys=True))
        return 0

    data = tune(a.min_surfaced)
    print()
    print("wrote %s" % WEIGHTS_PATH)
    print(json.dumps(data["profiles"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
