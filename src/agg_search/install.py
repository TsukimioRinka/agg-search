#!/usr/bin/env python3
"""Print or write the agg-search MCP registration for a given agent client.

The server is a plain stdio process, so every client ends up with the same
command; only the config file and its key names differ. `--print` (default)
never touches disk, `--write` backs up and merges one server entry.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

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
        "command": ["claude", "mcp", "add", "--scope", "{scope}", SERVER,
                    "--", "{command...}"],
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
        "command": ["codex", "mcp", "add", SERVER, "--", "{command...}"],
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
        "command": ["gemini", "mcp", "add", "--scope", "{scope}", SERVER,
                    "{command...}"],
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
        "command": ["opencode", "mcp", "add", SERVER, "--", "{command...}"],
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


def _expand_command(template, argv, entry=None):
    out = []
    for part in template:
        if part == "{command...}":
            out.extend(argv)
        elif part == "{scope}":
            out.append("user")
        elif part == "{json}":
            out.append(json.dumps(entry, ensure_ascii=False))
        else:
            out.append(part)
    return out


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
    ap.add_argument("--check", action="store_true",
                    help="verify the client config exists, parses, holds the "
                         "server entry and that the runtime binary resolves")
    a = ap.parse_args(argv)

    if a.list or not a.client:
        width = max(len(n) for n in CLIENTS)
        for name in sorted(CLIENTS):
            spec = CLIENTS[name]
            print("%-*s  %-8s %s" % (width, name, spec["mode"],
                                     spec.get("doc", "")))
        return 0

    argv_cmd = [a.command] + [x for x in a.args.split(",") if x]
    env = {}
    for item in a.env:
        key, _, value = item.partition("=")
        if not key:
            raise SystemExit("--env expects KEY=VALUE, got %r" % item)
        env[key] = value

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
