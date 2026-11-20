"""Single source of truth for search engines.

Every engine's metadata lives here: credential variables, default RRF weight,
whether it is enabled in the default engine set, and what kind of source it
is. `search`, `mcp` and `portals` derive their registries from this table so a
new engine is added in exactly two places: its implementation in
`agg_search.search` (the callable) and its row here.
"""

from __future__ import annotations

# name -> metadata. `pack` groups sources for docs/UI only.
ENGINE_SPECS = {
    "exa": {
        "env": ("EXA_API_KEY",), "weight": 1.0, "default": True,
        "keyless": False, "pack": "web",
    },
    "brave": {
        "env": ("BRAVE_API_KEY",), "weight": 1.0, "default": True,
        "keyless": False, "pack": "web",
    },
    "serper": {
        "env": ("SERPER_API_KEY",), "weight": 1.0, "default": True,
        "keyless": False, "pack": "web",
    },
    "firecrawl": {
        "env": ("FIRECRAWL_API_KEY",), "weight": 1.0, "default": True,
        "keyless": False, "pack": "web",
    },
    "anysearch": {
        "env": ("MCP_ANYSEARCH_API_KEY",), "weight": 1.0, "default": True,
        "keyless": False, "pack": "web",
    },
    "tavily": {
        "env": ("TAVILY_API_KEY", "TAVILY_API_KEY_BACKUP"),
        "weight": 1.0, "default": True, "keyless": False, "pack": "web",
    },
    "deepseek": {
        "env": ("DEEPSEEK_API_KEY",), "weight": 1.0, "default": True,
        "keyless": False, "pack": "web",
    },
    "searxng": {
        "env": (), "weight": 0.5, "default": True,
        "keyless": True, "pack": "local",
    },
    "openalex": {
        "env": (), "weight": 1.0, "default": False,
        "keyless": True, "pack": "academic",
    },
    "hn": {
        "env": (), "weight": 1.0, "default": False,
        "keyless": True, "pack": "community",
    },
    "stack": {
        "env": (), "weight": 1.0, "default": False,
        "keyless": True, "pack": "qa",
    },
}


def names():
    """Engine names in declaration order (stable for schemas and docs)."""
    return list(ENGINE_SPECS)


def credential_env():
    """Engine -> credential env vars; keyless engines are omitted."""
    return {name: spec["env"] for name, spec in ENGINE_SPECS.items()
            if spec["env"]}


def env_requirements():
    """Engine -> env vars, keyless engines represented as an empty tuple."""
    return {name: tuple(spec["env"]) for name, spec in ENGINE_SPECS.items()}


def weights():
    return {name: spec["weight"] for name, spec in ENGINE_SPECS.items()}


def default_engines():
    return ",".join(name for name, spec in ENGINE_SPECS.items()
                    if spec["default"])


def keyless():
    return [name for name, spec in ENGINE_SPECS.items() if spec["keyless"]]


def describe():
    lines = []
    for name, spec in ENGINE_SPECS.items():
        keys = ",".join(spec["env"]) or "keyless"
        lines.append("%-10s weight=%.1f default=%-5s env=%s"
                     % (name, spec["weight"], spec["default"], keys))
    return lines
