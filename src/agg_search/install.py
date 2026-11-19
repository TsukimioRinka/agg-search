#!/usr/bin/env python3
"""Print or write the agg-search MCP registration for a given agent client.

The server is a plain stdio process, so every client ends up with the same
command; only the config file and its key names differ. `--print` (default)
never touches disk, `--write` backs up and merges one server entry.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import time
import urllib.parse

try:  # package import (installed, `python -m agg_search.install`)
    from . import profiles
except ImportError:  # pragma: no cover - direct script execution
    import profiles  # type: ignore[no-redef]

SERVER = "agg-search"
DEFAULT_ARGS = ["--from", "agg-search", "agg-search-mcp"]
HOME = os.path.expanduser("~")


def _path(*parts):
    return os.path.join(HOME, *parts)


def _claude_desktop_path():
    if sys.platform == "darwin":
        return _path("Library", "Application Support", "Claude",
                     "claude_desktop_config.json")
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA", HOME), "Claude",
                            "claude_desktop_config.json")
    return _path(".config", "Claude", "claude_desktop_config.json")


# client -> dict(mode, path, section, shell, extra, payload)
# mode: json (mergeable), snippet (printed only), command (shell command)
CLIENTS = {
    "claude-code": {
        "mode": "command",
        "doc": "claude mcp add writes the user/project config itself",
        "command": ["claude", "mcp", "add", "--scope", "{scope}", "{name}",
                    "--", "{command...}"],
        "env_flag": "--env",
    },
    "claude-desktop": {
        "mode": "json",
        "path": _claude_desktop_path,
        "section": ("mcpServers",),
        "shape": "mcpServers",
    },
    "codex": {
        "mode": "command",
        "doc": "TOML also works: [mcp_servers.agg-search] in ~/.codex/config.toml",
        "command": ["codex", "mcp", "add", "{name}", "--", "{command...}"],
        "env_flag": "--env",
        "toml": "[mcp_servers.agg-search]\ncommand = \"uvx\"\n"
                "args = [\"--from\", \"agg-search\", \"agg-search-mcp\"]\n",
    },
    "cursor": {
        "mode": "json",
        "path": {"user": _path(".cursor", "mcp.json"),
                 "project": ".cursor/mcp.json"},
        "section": ("mcpServers",),
        "shape": "mcpServers",
    },
    "vscode": {
        "mode": "json",
        "path": {"user": None, "project": ".vscode/mcp.json"},
        "section": ("servers",),
        "shape": "vscode",
        "command": ["code", "--add-mcp", "{json}"],
    },
    "cline": {
        "mode": "json",
        "path": _path(".cline", "data", "settings",
                      "cline_mcp_settings.json"),
        "section": ("mcpServers",),
        "shape": "cline",
    },
    "windsurf": {
        "mode": "json",
        "path": _path(".codeium", "windsurf", "mcp_config.json"),
        "section": ("mcpServers",),
        "shape": "mcpServers",
    },
    "zed": {
        "mode": "json",
        "path": _path(".config", "zed", "settings.json"),
        "section": ("context_servers",),
        "shape": "zed",
    },
    "continue": {
        "mode": "snippet",
        "path": _path(".continue", "config.yaml"),
        "yaml": "mcpServers:\n  - name: agg-search\n    command: uvx\n"
                "    args:\n      - --from\n      - agg-search\n"
                "      - agg-search-mcp\n    env: {}\n",
    },
    "gemini": {
        "mode": "json",
        "path": _path(".gemini", "settings.json"),
        "section": ("mcpServers",),
        "shape": "gemini",
    },
    "antigravity": {
        "mode": "json",
        "path": _path(".gemini", "config", "mcp_config.json"),
        "section": ("mcpServers",),
        "shape": "mcpServers",
    },
    "opencode": {
        "mode": "json",
        "path": {"user": _path(".config", "opencode", "opencode.json"),
                 "project": "opencode.json"},
        "section": ("mcp", "servers"),
        "shape": "opencode",
    },
    "generic": {
        "mode": "snippet",
        "doc": "any client that reads mcpServers",
    },
}


def payload(shape, argv, env):
    """Client-specific server entry for the shared stdio command."""
    if shape == "vscode":
        return {"type": "stdio", "command": argv[0], "args": argv[1:],
                "env": env}
    if shape == "cline":
        return {"command": argv[0], "args": argv[1:], "env": env,
                "disabled": False, "autoApprove": []}
    if shape == "zed":
        return {"command": argv[0], "args": argv[1:], "env": env}
    if shape == "gemini":
        return {"command": argv[0], "args": argv[1:], "env": env,
                "timeout": 30000, "trust": False}
    if shape == "opencode":
        return {"type": "local", "command": argv, "environment": env}
    return {"command": argv[0], "args": argv[1:], "env": env}


def _section_view(doc, section):
    node = doc
    for key in section:
        child = node.get(key)
        if child is None:
            child = node[key] = {}
        if not isinstance(child, dict):
            raise ValueError("config key %r is not an object" % key)
        node = child
    return node


def write_config(path, section, name, entry):
    """Merge one server entry into a JSON config, with a timestamped backup."""
    path = os.path.abspath(os.path.expanduser(path))
    doc = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            try:
                doc = json.load(f)
            except ValueError as exc:
                raise SystemExit("%s is not valid JSON (%s); fix it first"
                                 % (path, exc))
        if not isinstance(doc, dict):
            raise SystemExit("%s does not hold a JSON object" % path)
        backup = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)  # keeps the original mode (0600 stays 0600)
    mode = (os.stat(path).st_mode & 0o777) if os.path.exists(path) else 0o600
    _section_view(doc, section)[name] = entry
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return path


def check_client(name, info, runtime):
    """Report whether the client is actually wired up. Exit 0 only when the
    config parses, the entry exists and the runtime resolves."""
    problems = []
    if info.get("mode") != "json" or not info.get("path"):
        print("%s: no config file to inspect; %s"
              % (name, CLIENTS[name].get("doc", "register with its CLI")))
    else:
        path = os.path.abspath(os.path.expanduser(info["path"]))
        print("%s: %s" % (name, path))
        if not os.path.exists(path):
            problems.append("config file does not exist (run --write or register "
                            "through the client)")
        else:
            try:
                with open(path, encoding="utf-8") as f:
                    doc = json.load(f)
            except (OSError, ValueError) as exc:
                problems.append("config does not parse: %s" % exc)
            else:
                try:
                    node = doc
                    for key in info["section"]:
                        node = node[key]
                    entry = node[SERVER]
                except (KeyError, TypeError):
                    problems.append("no %r entry under %s"
                                    % (SERVER, ".".join(info["section"])))
                else:
                    command = entry.get("command")
                    if isinstance(command, (list, tuple)):
                        command = command[0] if command else None
                    if not command:
                        problems.append("entry has no command")
                    elif not (os.path.isabs(command)
                              or shutil.which(command)):
                        problems.append("runtime %r does not resolve on PATH"
                                        % command)

    if not (os.path.isabs(runtime) or shutil.which(runtime)):
        problems.append("runtime %r does not resolve on PATH (GUI clients "
                        "need an absolute path)" % runtime)
    for item in problems:
        print("  ! " + item)
    if problems:
        return 1
    print("  ok")
    return 0


def _resolve_path(spec, scope):
    if callable(spec):
        return spec()
    if isinstance(spec, dict):
        chosen = spec.get(scope) if scope else None
        if chosen is None and scope == "user":
            raise SystemExit("this client has no user-level file; use "
                             "--scope project or register through its CLI")
        if chosen is None:
            raise SystemExit("this client needs --scope user or --scope project")
        return chosen
    return spec


def _expand_command(template, argv, entry=None, name=SERVER):
    out = []
    for part in template:
        if part == "{command...}":
            out.extend(argv)
        elif part == "{scope}":
            out.append("user")
        elif part == "{name}":
            out.append(name)
        elif part == "{json}":
            out.append(json.dumps(entry, ensure_ascii=False))
        else:
            out.append(part)
    return out


def server_name(profile):
    return SERVER if profile == "general" else "%s-%s" % (SERVER, profile)


def entry_env(profile):
    env = {}
    if profile != "general":
        env["AGG_SEARCH_PROFILE"] = profile
    return env


def cursor_deeplink(name, entry):
    cfg = {key: entry[key] for key in ("command", "args", "env") if key in entry}
    blob = base64.b64encode(
        json.dumps(cfg, ensure_ascii=False).encode()).decode()
    return ("https://cursor.com/en/install-mcp?name=%s&config=%s"
            % (urllib.parse.quote(name), urllib.parse.quote(blob, safe="")))


def vscode_deeplink(name, entry):
    cfg = {"type": "stdio", "command": entry["command"],
           "args": entry.get("args", []), "env": entry.get("env", {})}
    blob = urllib.parse.quote(json.dumps(cfg, ensure_ascii=False), safe="")
    return ("https://vscode.dev/redirect/mcp/install?name=%s&config=%s"
            % (urllib.parse.quote(name), blob))


def catalog(argv=None):
    """Machine-readable matrix of every agent x every domain profile."""
    argv = list(argv or (["uvx"] + DEFAULT_ARGS))
    rows = []
    for agent, spec in sorted(CLIENTS.items()):
        entries = []
        for profile in profiles.names():
            env = entry_env(profile)
            entry = payload(spec.get("shape", "mcpServers"), argv, env)
            name = server_name(profile)
            item = {"profile": profile, "name": name, "env": env,
                    "entry": entry}
            if spec.get("mode") == "command" and "command" in spec:
                command = _expand_command(spec["command"], argv, entry, name)
                flag = spec.get("env_flag")
                if env and flag and "--" in command:
                    at = command.index("--")
                    pairs = []
                    for key, value in env.items():
                        pairs += [flag, "%s=%s" % (key, value)]
                    command = command[:at] + pairs + command[at:]
                    item["command"] = " ".join(command)
                elif env:
                    # this client takes env only through its config file
                    item["command"] = ("agg-search-install -c %s --profile %s "
                                       "--write" % (agent, profile))
                else:
                    item["command"] = " ".join(command)
            if agent == "cursor":
                item["deeplink"] = cursor_deeplink(name, entry)
            elif agent == "vscode":
                item["deeplink"] = vscode_deeplink(name, entry)
            if spec.get("mode") == "json":
                path = spec.get("path")
                if callable(path):
                    item["path"] = path()
                elif isinstance(path, dict):
                    item["path"] = path.get("user") or path.get("project")
            entries.append(item)
        rows.append({"id": agent, "mode": spec["mode"],
                     "doc": spec.get("doc", ""), "entries": entries})
    return {
        "servers": {p: {"name": server_name(p),
                        "env": entry_env(p)} for p in profiles.names()},
        "agents": rows,
    }


def catalog_markdown(data=None):
    data = data or catalog()
    agents = {row["id"]: row for row in data["agents"]}
    lines = [
        "# 下载入口 — Agent × 领域",
        "",
        "同一个 stdio server，不同 Agent 的注册方式不同；不同领域用 `AGG_SEARCH_PROFILE` "
        "把引擎组合与权重钉死。下表由 `agg-search-install --catalog --markdown` 生成，"
        "机器可读版本见仓库根目录的 [`catalog.json`](../catalog.json)。",
        "",
        "| profile | 含义 |",
        "|---|---|",
    ]
    for profile in profiles.names():
        lines.append("| `%s` | %s |" % (profile,
                                        profiles.PROFILES[profile]["description"]))
    lines += ["", "## 按 Agent", "",
              "| Agent | 类型 | 一行入口 |", "|---|---|---|"]
    for agent, row in agents.items():
        item = next(e for e in row["entries"] if e["profile"] == "general")
        if item.get("deeplink"):
            entry = "[\u2b07 安装](%s)" % item["deeplink"]
        elif item.get("command"):
            entry = "`%s`" % item["command"]
        elif row["mode"] == "snippet":
            entry = "`agg-search-install -c %s`" % agent
        else:
            entry = "`agg-search-install -c %s --write`" % agent
        lines.append("| %s | %s | %s |"
                     % (agent, row["mode"], entry))
    lines += ["", "## 按领域", "",
              "| profile | Cursor | VS Code | Claude Code | Codex | 其他客户端 |",
              "|---|---|---|---|---|---|"]
    for profile in profiles.names():
        cursor = next(e for e in agents["cursor"]["entries"]
                      if e["profile"] == profile)
        vscode = next(e for e in agents["vscode"]["entries"]
                      if e["profile"] == profile)
        claude = next(e for e in agents["claude-code"]["entries"]
                      if e["profile"] == profile)
        codex = next(e for e in agents["codex"]["entries"]
                     if e["profile"] == profile)
        lines.append("| `%s` | [安装](%s) | [安装](%s) | `%s` | `%s` | "
                     "`agg-search-install -c <client> --profile %s --write` |"
                     % (profile, cursor["deeplink"], vscode["deeplink"],
                        claude["command"], codex["command"], profile))
    lines += ["", "完整路径与 JSON/YAML 片段见 [adapters.md](adapters.md)；"
                  "领域画像与反馈回路见 [profiles.md](profiles.md)。"]
    return "\n".join(lines) + "\n"


def describe(name, argv, env, scope):
    spec = CLIENTS[name]
    entry = payload(spec.get("shape", "mcpServers"), argv, env)
    mode = spec["mode"]
    if mode == "command" and "command" in spec:
        return {"mode": "command",
                "command": _expand_command(spec["command"], argv, entry)}
    if mode == "json":
        path = _resolve_path(spec["path"], scope)
        doc = {}
        _section_view(doc, spec["section"])[SERVER] = entry
        return {"mode": "json", "path": path, "scope": scope,
                "config": doc, "section": list(spec["section"]),
                "entry": entry,
                "snippet": json.dumps(doc, indent=2, ensure_ascii=False)}
    if "toml" in spec:
        return {"mode": "toml", "path": "~/.codex/config.toml",
                "snippet": spec["toml"]}
    if "yaml" in spec:
        return {"mode": "yaml", "path": spec.get("path", ""),
                "snippet": spec["yaml"]}
    doc = {"mcpServers": {SERVER: entry}}
    return {"mode": "json", "path": "", "config": doc,
            "section": ["mcpServers"], "entry": entry,
            "snippet": json.dumps(doc, indent=2, ensure_ascii=False)}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Register agg-search in an MCP client's config.")
    ap.add_argument("--client", "-c", required=False,
                    choices=sorted(CLIENTS), default=None,
                    help="target client; --list shows them all")
    ap.add_argument("--list", action="store_true", help="list supported clients")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    default=True, help="print the snippet (default)")
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="print the machine-readable result object")
    ap.add_argument("--write", action="store_true",
                    help="merge the entry into the client config (JSON clients)")
    ap.add_argument("--scope", choices=("user", "project"), default="user",
                    help="user-level or project-level file where the client "
                         "supports both")
    ap.add_argument("--command", dest="command", default="uvx",
                    help="runtime binary (default: uvx)")
    ap.add_argument("--args", default="--from,agg-search,agg-search-mcp",
                    help="comma-separated runtime arguments")
    ap.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                    help="environment entry for the client config (repeatable)")
    ap.add_argument("--catalog", action="store_true",
                    help="print the agent x domain catalog as JSON")
    ap.add_argument("--markdown", action="store_true",
                    help="with --catalog, print docs/catalog.md instead")
    ap.add_argument("--check", action="store_true",
                    help="verify the client config exists, parses, holds the "
                         "server entry and that the runtime binary resolves")
    ap.add_argument("--profile", default=None,
                    help="pin this client to a domain profile "
                         "(sets AGG_SEARCH_PROFILE in its env)")
    a = ap.parse_args(argv)

    if a.catalog:
        data = catalog()
        print(catalog_markdown(data) if a.markdown
              else json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if a.list or not a.client:
        width = max(len(n) for n in CLIENTS)
        for name in sorted(CLIENTS):
            spec = CLIENTS[name]
            print("%-*s  %-8s %s" % (width, name, spec["mode"],
                                     spec.get("doc", "")))
        return 0

    argv_cmd = [a.command] + [x for x in a.args.split(",") if x]
    env = {}
    if a.profile:
        env["AGG_SEARCH_PROFILE"] = a.profile
    for item in a.env:
        key, _, value = item.partition("=")
        if not key:
            raise SystemExit("--env expects KEY=VALUE, got %r" % item)
        env[key] = value  # explicit --env wins over --profile

    info = describe(a.client, argv_cmd, env, a.scope)
    if a.as_json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    if a.check:
        return check_client(a.client, info, argv_cmd[0])
    if a.write:
        if info.get("mode") != "json" or not info.get("path"):
            raise SystemExit("%s cannot be written automatically; %s"
                             % (a.client, CLIENTS[a.client].get(
                                 "doc", "paste the snippet from --print")))
        path = write_config(info["path"], tuple(info["section"]), SERVER,
                            info["entry"])
        print("wrote %s" % path)
        return 0
    if info["mode"] == "command":
        print(" ".join(info["command"]))
        if CLIENTS[a.client].get("toml"):
            print()
            print(CLIENTS[a.client]["toml"])
        return 0
    print(info["snippet"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
