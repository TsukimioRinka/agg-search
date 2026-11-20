#!/usr/bin/env python3
"""Domain profiles: named presets for engines, RRF weights and output shape.

A profile is data, not code. `--profile academic` changes which engines run,
how much each engine counts in fusion and how long snippets are; an explicit
flag still wins, then an environment default, then the profile.

The per-agent story is the same data: `agg-search-install --profile code`
pins a client to a profile through AGG_SEARCH_PROFILE, and MCP callers can
override it per call with the `profile` argument.
"""

from __future__ import annotations

PROFILES = {
    "general": {
        "description": "all configured engines, balanced weights",
    },
    "academic": {
        "description": "papers and preprints: OpenAlex first, then semantic "
                       "and general engines for context",
        "engines": ["openalex", "exa", "deepseek", "brave", "tavily"],
        "weights": {"openalex": 1.8, "exa": 1.3, "deepseek": 1.1},
        "primary": ["openalex"],
        "rerank_blend": 0.4,
        "authority_boost": 1.35,
        "authority_domains": (
            "arxiv.org", "doi.org", "semanticscholar.org", "aclanthology.org",
            "openreview.net", "proceedings.neurips.cc",
            "proceedings.mlr.press", "nature.com", "science.org", "ieee.org",
            "acm.org", "pubmed.ncbi.nlm.nih.gov", "biorxiv.org",
            "springer.com", "jmlr.org", "huggingface.co", ".edu",
        ),
        "snippet_chars": 500,
        "top_n": 15,
    },
    "code": {
        "description": "programming questions, APIs and error messages: "
                       "Stack Exchange first, docs-friendly engines after",
        "engines": ["stack", "brave", "serper", "deepseek", "exa"],
        "weights": {"stack": 1.8, "brave": 1.2, "serper": 1.1},
        "primary": ["stack"],
        "rerank_blend": 0.4,
        "authority_boost": 1.25,
        "authority_domains": (
            "stackoverflow.com", "stackexchange.com", "docs.python.org",
            "doc.rust-lang.org", "developer.mozilla.org", "github.com",
            "sqlite.org", "postgresql.org", "go.dev", "pkg.go.dev",
            "kubernetes.io", "docs.rs", "man7.org", "docs.docker.com",
        ),
        "snippet_chars": 600,
    },
    "news": {
        "description": "recent events: freshness-first engines with a one-day "
                       "default window",
        # web endpoints: 0.7.0 switched to the news verticals, which won on
        # entity-name queries but drifted off-topic on event queries; over
        # 42 queries web averaged 0.806 vs 0.776 (benchmarks.md, J). Adding
        # brave_news,serper_news on top scored 0.836 for +2 paid requests,
        # so it stays opt-in via --engines.
        "engines": ["brave", "serper", "tavily", "firecrawl"],
        "weights": {"brave": 1.3, "serper": 1.2, "tavily": 1.1},
        "primary": ["brave"],
        "recency": "day",
        "snippet_chars": 300,
    },
}


class UnknownProfile(KeyError):
    pass


def names():
    return sorted(PROFILES)


def load(name):
    if name not in PROFILES:
        raise UnknownProfile(
            "unknown profile %r (valid: %s)" % (name, ", ".join(names())))
    return PROFILES[name]


def _preset(args, profile, field, reserved, applied, transform=None):
    """Set one argv attribute from the profile when it is still at default."""
    value = profile.get(field)
    if value is None or field in reserved:
        return
    default = getattr(args, "default_" + field)
    if getattr(args, field) != default:
        return  # the user (or the environment) already chose
    setattr(args, field, transform(value) if transform else value)
    applied.append(field)


def apply(args, profile, reserved=None):
    """Fold a profile into parsed args.

    `reserved` holds fields an environment default already set; those are
    never overwritten, so precedence stays: flag > env > profile > built-in.
    `args` must carry `default_<field>` attributes for every preset field.
    Returns the list of fields applied.
    """
    reserved = set(reserved or ())
    applied = []
    _preset(args, profile, "engines", reserved, applied,
            lambda v: ",".join(v))
    _preset(args, profile, "recency", reserved, applied)
    _preset(args, profile, "snippet_chars", reserved, applied)
    _preset(args, profile, "top_n", reserved, applied)
    _preset(args, profile, "per_engine", reserved, applied)
    return applied


def weights(profile, engine_table):
    """Engine weights for rrf_fuse, with profile overrides applied.

    `engine_table` is the ENGINES mapping from search.py; importing it here
    would be circular, so the caller passes it in.
    """
    base = {name: entry[1] for name, entry in engine_table.items()}
    for name, value in (profile.get("weights") or {}).items():
        base[name] = float(value)
    return base


def describe():
    lines = []
    for name in names():
        prof = PROFILES[name]
        engines = ",".join(prof.get("engines", ["(all configured)"]))
        weights = ",".join("%s=%s" % kv
                           for kv in sorted((prof.get("weights") or {}).items()))
        extra = ""
        if weights:
            extra += " | weights: " + weights
        if prof.get("recency"):
            extra += " | recency: " + prof["recency"]
        if prof.get("snippet_chars"):
            extra += " | snippet: %d" % prof["snippet_chars"]
        lines.append("%-9s %s | engines: %s%s"
                     % (name, prof["description"], engines, extra))
    return lines
