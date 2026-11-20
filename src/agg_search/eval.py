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
import re
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


def load_queries(path):
    """A JSON list of queries, or {"queries": [...]}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("queries", [])
    if not isinstance(data, list) or not all(isinstance(q, str) for q in data):
        raise SystemExit("queries file must be a JSON list of strings")
    return data


def record(a):
    queries = load_queries(a.queries)
    engines = [e for e in a.engines.split(",") if e in m.ENGINES]
    skipped = {}
    if not a.ignore_cooldown:
        # calling engine functions directly bypasses the CLI's cooldown; an
        # engine out of quota would otherwise be recorded as failing everywhere
        now = time.time()
        for e, until in m.load_cooldown().items():
            if e in engines and until > now:
                skipped[e] = "skipped: cooling"
        engines = [e for e in engines if e not in skipped]
    if not engines:
        raise SystemExit("record: no engine to call (%s)"
                         % ("all cooling: " + ",".join(skipped) if skipped
                            else "none of --engines is known"))
    out = {"created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "per_engine": a.per_engine, "engines": engines,
           "recency": a.recency, "queries": {}}
    for q in queries:
        results, status = m.recall(q, a.per_engine, engines, a.recency)
        status.update(skipped)
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


RUBRICS = {
    "general": "Score 2 if the result directly answers or covers the query; "
               "1 if related but only partially useful; 0 if off-topic or spam.",
    "academic": "Score 2 if it is a research paper, preprint or survey directly "
                "on this topic; 1 if related (tangential paper, tutorial, blog, "
                "docs); 0 if off-topic.",
    "code": "Score 2 if the page very likely answers the question directly; 1 if "
            "related but only partially useful; 0 if off-topic. The question may "
            "mix Chinese and English; an English page can fully answer a Chinese "
            "question.",
    "news": "Score 2 if it is a news report or update from the last few days "
            "about the query topic; 1 if on-topic but evergreen, reference or old "
            "(wiki, profile, price page, docs); 0 if off-topic or spam.",
}
_DATE_PREFIX = re.compile(r"^\[[^\]]{1,40}\]\s*")


def fixture_pool(fx, q):
    """canonical url -> (title, snippet) over every engine row of a query.
    The news date prefix is stripped so graders judge content, not format."""
    pool = {}
    for rows in fx["queries"][q]["engines"].values():
        for row in rows:
            key = m.canonical_url(row[0])
            pool.setdefault(key, (row[1] or "", _DATE_PREFIX.sub("", row[2] or "")))
    return pool


def _qwen_grade(query, title, snippet, rubric, model, today=None):
    base = m.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    # recency rubrics ("last few days") are meaningless without a date
    when = ("The search was made on %s.\n" % today) if today else ""
    prompt = ("You grade search results.\n%sQuery: %s\nResult title: %s\n"
              "Result snippet: %s\n%s Reply with one digit only."
              % (when, query, title[:200], snippet[:400], RUBRICS[rubric]))
    d = m.http_json(base + "/api/generate", method="POST", timeout=120,
                    body={"model": model, "prompt": prompt, "stream": False,
                          "think": False,
                          "options": {"temperature": 0, "num_predict": 4}})
    hit = re.search(r"[012]", str(d.get("response", "")))
    return int(hit.group(0)) if hit else 0


def _jina_scores(query, pool):
    keys = list(pool)
    if not keys:
        return {}
    key = m.getenv("JINA_API_KEY")
    if not key:
        raise SystemExit("judge --judge jina needs JINA_API_KEY")
    d = m.http_json("https://api.jina.ai/v1/rerank", method="POST",
                    body={"model": m.getenv("JINA_RERANK_MODEL", m.JINA_MODELS[0]),
                          "query": query,
                          "documents": ["%s\n%s" % pool[k] for k in keys],
                          "top_n": len(keys)},
                    headers={"Authorization": "Bearer " + key})
    return {keys[r["index"]]: float(r["relevance_score"]) for r in d["results"]}


def judge(a):
    """Grade every pooled result once; resumable (existing grades are kept)."""
    fx = json.loads(Path(a.fixture).read_text(encoding="utf-8"))
    out_path = Path(a.out)
    labels = (json.loads(out_path.read_text(encoding="utf-8"))
              if out_path.exists() else {})
    today = str(fx.get("created") or "")[:10] or time.strftime("%Y-%m-%d")
    for q in fx["queries"]:
        pool = fixture_pool(fx, q)
        have = labels.setdefault(q, {})
        todo = {k: v for k, v in pool.items() if k not in have}
        if not todo:
            continue
        if a.judge == "jina":
            have.update(_jina_scores(q, todo))
        else:
            items = list(todo.items())
            with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
                grades = list(ex.map(lambda kv: _qwen_grade(
                    q, kv[1][0], kv[1][1], a.rubric, a.model, today), items))
            have.update({k: g for (k, _), g in zip(items, grades)})
        out_path.write_text(json.dumps(labels, ensure_ascii=False),
                            encoding="utf-8")  # checkpoint per query
        print("judged %-40s %d new" % (q[:40], len(todo)), file=sys.stderr)
    print("judgments -> %s" % out_path)


def load_grades(path):
    """{query: {canonical_url: grade}}; the legacy binary format
    {query: [url, ...]} becomes grade 1 per listed url."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = {}
    for q, v in data.items():
        if isinstance(v, list):
            out[q] = {m.canonical_url(u): 1.0 for u in v}
        else:
            # judge writes canonical keys already; hand-written files may not
            out[q] = {m.canonical_url(k): float(g) for k, g in v.items()}
    return out


def graded_ndcg(final_urls, grades, top=10):
    gains = [grades.get(m.canonical_url(u), 0.0) for u in final_urls[:top]]
    ideal = sorted(grades.values(), reverse=True)[:top]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
    return dcg / idcg if idcg else 0.0


def parse_variant(text, profile):
    """NAME[:key=val;key=val] over a profile. Keys: engines (+e/-e or a full
    comma list), w.<engine>, blend (none|float), boost, dedupe, diversify."""
    name, _, spec = text.partition(":")
    cfg = {"name": name or "variant",
           "engines": list(profile.get("engines") or m.ENGINES),
           "weights": dict(profile.get("weights") or {}),
           "blend": profile.get("rerank_blend"),
           "boost": float(profile.get("authority_boost") or 1.0),
           "domains": tuple(profile.get("authority_domains") or ()),
           "dedupe": True, "diversify": True}
    for item in filter(None, (x.strip() for x in spec.split(";"))):
        key, _, val = item.partition("=")
        if key == "engines":
            toks = [t for t in val.split(",") if t]
            signed = [t[:1] in "+-" for t in toks]
            if any(signed) and not all(signed):
                raise SystemExit("variant %r: engines mixes +/- edits with a "
                                 "plain list" % name)
            if any(signed):
                for tok in toks:
                    if tok[0] == "+" and tok[1:] not in cfg["engines"]:
                        cfg["engines"].append(tok[1:])
                    elif tok[0] == "-":
                        cfg["engines"] = [e for e in cfg["engines"]
                                          if e != tok[1:]]
            else:
                cfg["engines"] = toks
        elif key.startswith("w."):
            cfg["weights"][key[2:]] = float(val)
        elif key == "blend":
            cfg["blend"] = None if val in ("none", "") else float(val)
        elif key == "boost":
            cfg["boost"] = float(val)
        elif key in ("dedupe", "diversify"):
            cfg[key] = val.lower() in ("1", "true", "on", "yes")
        else:
            raise SystemExit("unknown variant key %r" % key)
    return cfg


def replay(fx, cfg, rerank_scores, top_n, rrf_top=15):
    """Offline replay of fusion + (optional) rerank blend for one variant."""
    weights = {name: entry[1] for name, entry in m.ENGINES.items()}
    weights.update(cfg["weights"])
    out = {}
    for q, blob in fx["queries"].items():
        results = {e: [tuple(r[:3]) for r in rows]
                   for e, rows in blob["engines"].items()
                   if e.split("#", 1)[0] in cfg["engines"] and rows}
        if not results:
            out[q] = []
            continue
        ranked = m.rrf_fuse(results, rrf_top, weights,
                            diversify=cfg["diversify"], dedupe=cfg["dedupe"],
                            domain_boost=cfg["domains"],
                            boost_factor=cfg["boost"])
        scores = (rerank_scores or {}).get(q)
        if scores:
            scored = [(i, scores.get(m.canonical_url(u), 0.0))
                      for i, (u, _, _) in enumerate(ranked)]
            if cfg["blend"] is None:
                order = [i for i, _ in sorted(scored, key=lambda p: p[1],
                                              reverse=True)]
            else:
                order = m._blend_order(scored, len(ranked), cfg["blend"])
            ranked = [ranked[i] for i in order]
        out[q] = [u for u, _, _ in ranked[:top_n]]
    return out


def compare(a):
    try:
        from agg_search import profiles
    except ImportError:  # plain-script run from a checkout
        import profiles  # type: ignore[no-redef]
    fx = json.loads(Path(a.fixture).read_text(encoding="utf-8"))
    grades = load_grades(a.judgments)
    rerank = load_grades(a.rerank_scores) if a.rerank_scores else None
    profile = profiles.load(a.profile)
    top_n = a.top_n or int(profile.get("top_n") or 10)
    variants = [parse_variant(v, profile) for v in (a.variant or ["current"])]
    hi = max((g for d in grades.values() for g in d.values()), default=1.0)
    base = None
    print("# compare: profile=%s, %d queries, rerank=%s, top_n=%d"
          % (a.profile, len(fx["queries"]),
             "replayed scores" if rerank else "off (RRF order)", top_n))
    print("\n| variant | nDCG@10 | best-grade@5 | domains@10 | wins/losses vs first |")
    print("|---|---|---|---|---|")
    for cfg in variants:
        finals = replay(fx, cfg, rerank, top_n)
        per = {q: graded_ndcg(f, grades.get(q, {})) for q, f in finals.items()}
        best5 = sum(sum(grades.get(q, {}).get(m.canonical_url(u), 0.0) >= hi
                        for u in f[:5]) for q, f in finals.items())
        doms = sum(len({m.domain_of(u) for u in f[:10]})
                   for f in finals.values()) / max(1, len(finals))
        if base is None:
            base, wl = per, "-"
        else:
            wins = sum(per[q] > base[q] + 1e-9 for q in per)
            losses = sum(per[q] < base[q] - 1e-9 for q in per)
            wl = "%d/%d" % (wins, losses)
        print("| %s | %.3f | %d/%d | %.1f | %s |"
              % (cfg["name"], sum(per.values()) / max(1, len(per)), best5,
                 5 * len(finals), doms, wl))


def main():
    try:
        from agg_search import __version__
    except ImportError:  # plain-script run from a checkout
        __version__ = "0.0.0+source"
    ap = argparse.ArgumentParser(description="RRF record/sweep harness")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--queries", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--engines",
                   default="exa,brave,serper,deepseek,firecrawl,anysearch,tavily,searxng")
    r.add_argument("--per-engine", type=int, default=5)
    r.add_argument("--recency", choices=["day", "week", "month", "year"],
                   default=None)
    r.add_argument("--ignore-cooldown", action="store_true",
                   help="also call engines that are cooling (spends quota)")
    j = sub.add_parser("judge", help="grade the pooled results once")
    j.add_argument("--fixture", required=True)
    j.add_argument("--out", required=True)
    j.add_argument("--judge", choices=["qwen", "jina"], default="qwen",
                   help="qwen: local Ollama 0/1/2 grades (free); jina: "
                        "relevance scores (also usable as --rerank-scores)")
    j.add_argument("--rubric", choices=sorted(RUBRICS), default="general")
    j.add_argument("--model", default="qwen3.5:9b")
    j.add_argument("--workers", type=int, default=2)
    c = sub.add_parser("compare", help="replay profile variants offline")
    c.add_argument("--fixture", required=True)
    c.add_argument("--judgments", required=True)
    c.add_argument("--profile", default="general")
    c.add_argument("--rerank-scores", default=None,
                   help="jina judgments file; replays the rerank blend")
    c.add_argument("--variant", action="append",
                   help="NAME[:engines=+arxiv;w.arxiv=1.3;blend=0.3;boost=1.2;"
                        "dedupe=0;diversify=0] (repeatable; first is baseline)")
    c.add_argument("--top-n", type=int, default=None)
    s = sub.add_parser("sweep")
    s.add_argument("--fixture", required=True)
    s.add_argument("--judgments", required=True)
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--no-jina", action="store_true")
    s.add_argument("--workers", type=int, default=4)
    s.add_argument("--diversify", action="store_true",
                   help="eTLD+1 interleave before top-15 cut (measures the layer)")
    a = ap.parse_args()
    {"record": record, "judge": judge, "compare": compare,
     "sweep": sweep}[a.cmd](a)


if __name__ == "__main__":
    main()
