#!/usr/bin/env python3
"""RRF parameter sweep harness for agg-search — record once, sweep free.

record: run the judgment-set queries through the live engines ONCE and store
        every engine's raw rows. This is the only step that spends engine
        quota (Firecrawl credits, DeepSeek balance, ...).
sweep:  replay the fixture entirely offline: RRF over a k x weights grid,
        then (unless --no-jina) the real Jina rerank of each config's top-15,
        scored against the relevance judgments. Jina tokens are the only
        remaining cost (~2.5k tokens per config-query pair).

Usage:
  python3 rrf_eval.py record --queries queries.json --out fixture.json
      [--engines exa,brave,serper,deepseek,firecrawl,anysearch,tavily,searxng]
      [--per-engine 5]
  python3 rrf_eval.py sweep --fixture fixture.json --judgments judgments.json
      [--top 10] [--no-jina] [--workers 4] [--diversify]

queries.json:  ["query one", "query two", ...]
judgments.json: {"query one": ["https://relevant-url", ...], ...}
  Relevance labels are built by pooling every engine's results for the query
  and judging title/snippet relevance (TREC-style pooling; labels are
  model-made, not human gold).
"""
import argparse
import concurrent.futures as cf
import importlib.util
import json
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

try:
    from . import search as m
except ImportError:  # run as a plain script from the repository root
    import importlib.util

    spec = importlib.util.spec_from_file_location("agg_search", HERE / "search.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

K_VALUES = (0, 10, 30, 60, 100)
WEIGHT_VARIANTS = {
    "current (searxng .5)": {"searxng": 0.5},
    "flat 1.0": {"searxng": 1.0},
    "searxng .3": {"searxng": 0.3},
    "exa 1.5": {"exa": 1.5, "searxng": 0.5},
    "deepseek 1.5": {"deepseek": 1.5, "searxng": 0.5},
}


def record(a):
    queries = json.loads(Path(a.queries).read_text(encoding="utf-8"))
    engines = [e for e in a.engines.split(",") if e in m.ENGINES]
    out = {"created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "per_engine": a.per_engine, "engines": engines, "queries": {}}
    for q in queries:
        results, status = m.recall(q, a.per_engine, engines, None)
        out["queries"][q] = {"engines": results, "status": status}
        print("recorded: %s | %s" % (q, "; ".join(
            "%s=%s" % kv for kv in status.items())), file=sys.stderr)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print("fixture -> %s" % a.out)


def ndcg10(order, relevant, top):
    """Binary-gain nDCG@top for one ranked list."""
    dcg = sum(1.0 / math.log2(i + 2)
              for i, u in enumerate(order[:top]) if u in relevant)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), top)))
    return dcg / ideal if ideal else 0.0


def evaluate_config(fx, judgments, k, weights, top, use_jina, workers,
                    diversify=False):
    """One (k, weights) config over all queries; returns aggregate metrics."""
    def one(item):
        q, blob = item
        ranked = m.rrf_fuse(blob["engines"], 15, weights, k=k,
                            diversify=diversify)
        order15 = [u for u, _, _ in ranked]
        rel = set(judgments.get(q, []))
        rec15 = len(rel & set(order15)) / len(rel) if rel else 0.0
        dom15 = len({m.domain_of(u) for u in order15})
        if use_jina:
            rr, _err = m.jina_rerank(q, ranked, top)
            final = [u for u, _, _ in (rr if rr is not None else ranked)][:top]
        else:
            final = order15[:top]
        return {"q": q, "recall15": rec15, "ndcg10": ndcg10(final, rel, top),
                "hit5": 1.0 if rel & set(final[:5]) else 0.0,
                "dom15": dom15,
                "dom10": len({m.domain_of(u) for u in final})}

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        per_query = list(ex.map(one, fx["queries"].items()))
    n = len(per_query) or 1
    return {"recall15": sum(x["recall15"] for x in per_query) / n,
            "ndcg10": sum(x["ndcg10"] for x in per_query) / n,
            "hit5": sum(x["hit5"] for x in per_query) / n,
            "dom15": sum(x["dom15"] for x in per_query) / n,
            "dom10": sum(x["dom10"] for x in per_query) / n,
            "per_query": per_query}


def sweep(a):
    fx = json.loads(Path(a.fixture).read_text(encoding="utf-8"))
    judgments = json.loads(Path(a.judgments).read_text(encoding="utf-8"))
    rows = []
    for k in K_VALUES:
        for name, w in WEIGHT_VARIANTS.items():
            r = evaluate_config(fx, judgments, k, w, a.top,
                                not a.no_jina, a.workers,
                                diversify=a.diversify)
            rows.append((k, name, r))
            print("k=%-4s %-22s recall@15=%.3f  nDCG@%s=%.3f  hit@5=%.2f  dom15=%.1f  dom10=%.1f"
                  % (k, name, r["recall15"], a.top, r["ndcg10"], r["hit5"],
                     r["dom15"], r["dom10"]),
                  file=sys.stderr)
    rows.sort(key=lambda x: (-x[2]["ndcg10"], -x[2]["recall15"]))
    print("\n# RRF sweep — %d queries, Jina=%s, diversify=%s, top=%d"
          % (len(fx["queries"]), "off" if a.no_jina else "v3.5",
             "on" if a.diversify else "off", a.top))
    print("\n| rank | k | weights | recall@15 | nDCG@%d | hit@5 | dom15 | dom10 |" % a.top)
    print("|---|---|---|---|---|---|---|---|")
    for i, (k, name, r) in enumerate(rows, 1):
        print("| %d | %d | %s | %.3f | **%.3f** | %.2f | %.1f | %.1f |"
              % (i, k, name, r["recall15"], r["ndcg10"], r["hit5"],
                 r["dom15"], r["dom10"]))
    base = [r for k, name, r in rows if k == 60 and name.startswith("current")]
    if base:
        print("\ncurrent config (k=60, searxng .5): nDCG@%d=%.3f, recall@15=%.3f"
              % (a.top, base[0]["ndcg10"], base[0]["recall15"]))


def main():
    ap = argparse.ArgumentParser(description="RRF record/sweep harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--queries", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--engines",
                   default="exa,brave,serper,deepseek,firecrawl,anysearch,tavily,searxng")
    r.add_argument("--per-engine", type=int, default=5)
    s = sub.add_parser("sweep")
    s.add_argument("--fixture", required=True)
    s.add_argument("--judgments", required=True)
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--no-jina", action="store_true")
    s.add_argument("--workers", type=int, default=4)
    s.add_argument("--diversify", action="store_true",
                   help="eTLD+1 interleave before top-15 cut (measures the layer)")
    a = ap.parse_args()
    if a.cmd == "record":
        record(a)
    else:
        sweep(a)


if __name__ == "__main__":
    main()
