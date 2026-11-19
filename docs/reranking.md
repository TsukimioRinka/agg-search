# Reranking — RRF coarse rank + Jina fine rank

## RRF (always)

`RRF(d) = Σ W_e / (60 + Rank_e(d))`; multi-engine co-occurrence surfaces naturally. Take Top 15.
Before truncation the CLI interleaves by eTLD+1 (head-5 pure RRF, tail
round-robin; `--no-diversify` disables): the top-15 candidate set keeps
per-domain bests instead of same-domain fill. 20-query check: consensus
recall@15 unchanged at 1.000, distinct domains 11.5 → 12.8.
Equal weights (`W_e = 1`); SearXNG as backup weight 0.5. The DeepSeek engine
pre-sorts its rows by its own `rerank_score` (relevance + authority + freshness)
before they enter RRF, so its ranking is quality-aware rather than raw order.
With `--expand N`, each query variant's engine list enters RRF as a separate
ranking (`engine#variant`), so agreement across both engines and phrasings
accumulates — the RAG-Fusion effect, at N× engine cost.

```python
def rrf(ranks, k=60):
    score = {}
    for weight, ranking in ranks:  # ranking: [url, ...] in engine order
        for i, url in enumerate(ranking):
            score[url] = score.get(url, 0) + weight / (k + i + 1)
    return sorted(score, key=score.get, reverse=True)[:15]
```

## Parameter tuning (record once, sweep free)

`agg-search-eval` replays this pipeline offline:

```sh
agg-search-eval record --queries queries.json --out fixture.json   # spends engine quota, once
agg-search-eval sweep  --fixture fixture.json --judgments judgments.json [--no-jina] [--diversify]  # offline + Jina tokens only
```

`record` stores every engine's raw rows for the judgment-set queries; `sweep`
grids `k ∈ {0,10,30,60,100}` × weight variants (current / flat / searxng .3 /
exa 1.5 / deepseek 1.5), reranks each config's top-15 with the live Jina chain
and reports mean recall@15, nDCG@10, hit@5 plus dom15/dom10 (distinct eTLD+1
domains in top-15/top-10) against TREC-style pooled judgments (labels are
model-made from title/snippet, not human gold). `--diversify` measures the
eTLD+1 interleave layer (also on by default in the CLI).
`rrf_fuse(results, top, weights=None, k=None, diversify=False)` accepts
per-engine weight overrides for this harness; the CLI path keeps the `ENGINES`
defaults with `diversify=True`.

## Calibration result (2026-10-18)

Swept `k ∈ {0,10,30,60,100}` × 5 weight variants over 10 pooled judgment
queries (~200 relevance labels), Jina v3.5 reranking every config: nDCG@10
landed within 0.898–0.939 for all 25 configs, and the top-vs-current gap
(+0.005) traced to a single query (leave-one-out: −0.003) — noise on this
set. `k` shows no systematic effect (Jina re-orders the top-15 anyway); the
one consistent signal is Exa weight 1.5 → recall@15 0.800 → 0.825, with no
matching gain in final order. Defaults kept: `k=60`, weights 1.0/0.5.
Re-run cheaply anytime: fixture + judgments in
`~/.cache/agg-search/{fixture,judgments}.json`.

## Jina fine rank (only with key)

Rescore Top 15 with the Jina Reranker chain — `jina-reranker-v3.5`
(listwise) → `jina-reranker-v3` → `jina-reranker-v2-base-multilingual`;
`JINA_RERANK_MODEL` overrides the head. A 400/404/422 (model unavailable)
advances to the next model; 408/429/5xx retries once after 1.5s. Malformed
200 bodies and empty rankings also fall back to RRF order, never block.
Timeout, missing key, or persistent error → fall back to RRF order,
never block. Truncate to Top 10.
