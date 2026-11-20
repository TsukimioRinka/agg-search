#!/usr/bin/env python3
"""Print or write the agg-search MCP registration for a given agent client.

The server is a plain stdio process, so every client ends up with the same
command; only the config file, server name, environment variables and key names
differ. `--print` (default) never touches disk, `--write` backs up and merges
one server entry, and `--portal <agent>.<domain>` configures an isolated
domain portal.
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
    from . import portals, profiles
except ImportError:  # pragma: no cover - direct script execution
    import portals  # type: ignore[no-redef]
    import profiles  # type: ignore[no-redef]

SERVER = "agg-search"
DEFAULT_ARGS = ["--from", "agg-search", "agg-search-mcp"]


def _path(*parts):
    return os.path.join("~", *parts)


def _claude_desktop_path():
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Claude",
                            "claude_desktop_config.json")
    if os.name == "nt":
        return os.path.join(os.environ.get("APPDATA", home), "Claude",
                            "claude_desktop_config.json")
    return os.path.join(home, ".config", "Claude", "claude_desktop_config.json")


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
        "portable_path": "~/.config/Claude/claude_desktop_config.json",
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
    "omp": {
        "mode": "json",
        "path": _path(".omp", "agent", "mcp.json"),
        "section": ("mcpServers",),
        "shape": "mcpServers",
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
        _prune_backups(path)
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


def _prune_backups(path, keep=5):
    """Keep the newest `keep` .bak-* siblings; a config with secrets in env
    must not leave an unbounded trail of copies next to it."""
    directory = os.path.dirname(path) or "."
    prefix = os.path.basename(path) + ".bak-"
    try:
        names = sorted(name for name in os.listdir(directory)
                       if name.startswith(prefix))
    except OSError:
        return
    for name in names[:-keep]:
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass


def check_client(name, info, runtime, server=None):
    """Report whether the client is actually wired up. Exit 0 only when the
    config parses, the entry exists and the runtime resolves."""
    target = server or info.get("server") or SERVER
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
                    entry = node[target]
                except (KeyError, TypeError):
                    problems.append("no %r entry under %s"
                                    % (target, ".".join(info["section"])))
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


def _resolve_path(spec, scope, expand=True):
    if callable(spec):
        val = spec()
    elif isinstance(spec, dict):
        chosen = spec.get(scope) if scope else None
        if chosen is None and scope == "user":
            raise SystemExit("this client has no user-level file; use "
                             "--scope project or register through its CLI")
        if chosen is None:
            raise SystemExit("this client needs --scope user or --scope project")
        val = chosen
    else:
        val = spec
    return os.path.expanduser(val) if expand and isinstance(val, str) else val


def _portable_path(spec):
    if "portable_path" in spec:
        return spec["portable_path"]
    path = spec.get("path")
    if isinstance(path, dict):
        return path.get("user") or path.get("project")
    if isinstance(path, str):
        return path
    return None


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


def _with_env_flags(command, env_flag, env):
    if not env or not env_flag or "--" not in command:
        return command
    at = command.index("--")
    pairs = []
    for key, value in env.items():
        pairs += [env_flag, "%s=%s" % (key, value)]
    return command[:at] + pairs + command[at:]


def _codex_toml(server, argv, env):
    args_json = json.dumps(argv[1:], ensure_ascii=False)
    lines = [
        "[mcp_servers.%s]" % server,
        "command = %s" % json.dumps(argv[0], ensure_ascii=False),
        "args = %s" % args_json,
    ]
    if env:
        pairs = ", ".join(
            "%s = %s" % (k, json.dumps(v, ensure_ascii=False))
            for k, v in env.items()
        )
        lines.append("env = { %s }" % pairs)
    return "\n".join(lines) + "\n"


def _continue_yaml(server, argv, env):
    lines = [
        "mcpServers:",
        "  - name: %s" % server,
        "    command: %s" % argv[0],
        "    args:",
    ]
    for arg in argv[1:]:
        lines.append("      - %s" % arg)
    if env:
        lines.append("    env:")
        for key, value in env.items():
            lines.append("      %s: %s" % (key, value))
    else:
        lines.append("    env: {}")
    return "\n".join(lines) + "\n"


def display_path(path):
    """Repo artifacts must not embed this machine's absolute home path."""
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def server_name(profile):
    return SERVER if profile == "general" else "%s-%s" % (SERVER, profile)


def entry_env(profile, portal_id=None):
    env = {}
    if portal_id:
        env["AGG_SEARCH_PORTAL"] = portal_id
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


def _agent_install_summary(agent, spec, portal, argv):
    """Build the install block shared by catalog() and portals_catalog()."""
    env = dict(portal["env"])
    name = portal["server_name"]
    entry = payload(spec.get("shape", "mcpServers"), argv, env)
    info = {
        "mode": spec["mode"],
        "server": name,
        "env": env,
        "entry": entry,
    }
    if spec.get("mode") == "command" and "command" in spec:
        command = _with_env_flags(
            _expand_command(spec["command"], argv, entry, name),
            spec.get("env_flag"),
            env,
        )
        info["command"] = " ".join(command)
    elif spec.get("mode") == "json":
        info["command"] = "agg-search-install --portal %s --write" % portal["id"]
    else:
        info["command"] = "agg-search-install --portal %s" % portal["id"]
    if agent == "cursor":
        info["deeplink"] = cursor_deeplink(name, entry)
    elif agent == "vscode":
        info["deeplink"] = vscode_deeplink(name, entry)
    return info


def catalog(argv=None):
    """Machine-readable matrix of every agent x every domain profile."""
    argv = list(argv or (["uvx"] + DEFAULT_ARGS))
    rows = []
    for agent, spec in sorted(CLIENTS.items()):
        entries = []
        for profile in profiles.names():
            portal_id = "%s.%s" % (agent, profile)
            portal = portals.load(portal_id)
            env = dict(portal["env"])
            entry = payload(spec.get("shape", "mcpServers"), argv, env)
            name = portal["server_name"]
            item = {
                "portal": portal_id,
                "profile": profile,
                "name": name,
                "env": env,
                "entry": entry,
            }
            if spec.get("mode") == "command" and "command" in spec:
                command = _with_env_flags(
                    _expand_command(spec["command"], argv, entry, name),
                    spec.get("env_flag"),
                    env,
                )
                item["command"] = " ".join(command)
            if agent == "cursor":
                item["deeplink"] = cursor_deeplink(name, entry)
            elif agent == "vscode":
                item["deeplink"] = vscode_deeplink(name, entry)
            if spec.get("mode") == "json":
                path = spec.get("path")
                if callable(path):
                    item["path"] = display_path(path())
                elif isinstance(path, dict):
                    chosen = path.get("user") or path.get("project")
                    item["path"] = (display_path(os.path.expanduser(chosen))
                                    if chosen else None)
                elif isinstance(path, str):
                    item["path"] = display_path(os.path.expanduser(path))
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
        "同一个 stdio server，不同 Agent 的注册方式不同；不同领域用 `AGG_SEARCH_PORTAL` "
        "与 `AGG_SEARCH_PROFILE` 把门户工具面、提示词和引擎权重钉死。下表由 "
        "`agg-search-install --catalog --markdown` 生成，"
        "机器可读版本见仓库根目录的 [`catalog.json`](../catalog.json)；"
        "完整门户隔离定义见 [`portals.md`](portals.md) 与 [`portals.json`](../portals.json)。",
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
            entry = "`agg-search-install --portal %s.general`" % agent
        else:
            entry = "`agg-search-install --portal %s.general --write`" % agent
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
                     "`agg-search-install --portal <client>.%s --write` |"
                     % (profile, cursor["deeplink"], vscode["deeplink"],
                        claude["command"], codex["command"], profile))
    lines += ["", "完整路径与 JSON/YAML 片段见 [adapters.md](adapters.md)；"
                  "门户规格见 [portals.md](portals.md)；"
                  "领域画像与反馈回路见 [profiles.md](profiles.md)。"]
    return "\n".join(lines) + "\n"


def portals_catalog(argv=None):
    """Machine-readable registry of every ``agent.domain`` portal."""
    argv = list(argv or (["uvx"] + DEFAULT_ARGS))
    portal_map = {}
    for pid in portals.portal_ids():
        portal = portals.load(pid)
        spec = CLIENTS[portal["agent"]]
        install_info = _agent_install_summary(portal["agent"], spec, portal, argv)
        p_path = _portable_path(spec)
        if p_path:
            install_info["path"] = p_path
        if "section" in spec:
            install_info["section"] = list(spec["section"])
        record = dict(portal)
        record["install"] = install_info
        portal_map[pid] = record
    return {
        "schema_version": 1,
        "agents": portals.agent_names(),
        "domains": portals.domain_names(),
        "portals": portal_map,
    }


def portals_markdown(data=None):
    """Human-readable portal reference generated from :func:`portals_catalog`."""
    data = data or portals_catalog()
    portal_map = data["portals"]
    lines = [
        "# 门户隔离 — Portal (`agent × domain`)",
        "",
        "每个门户是 `agent × domain` 的纯数据描述：绑定固定 domain profile、"
        "暴露独立的 MCP 工具子集与调用顺序、使用领域专属 prompt（查询改写 / 答案合成 / 筛选提示），"
        "并声明依赖引擎、密钥与降级路径（无 per-agent 分支代码）。",
        "",
        "本文件由 `agg-search-install --portals --markdown` 生成，"
        "机器可读版本见 [`portals.json`](../portals.json)；"
        "客户端矩阵见 [`catalog.md`](catalog.md) / [`catalog.json`](../catalog.json)。",
        "",
        "> 工具顺序是**给模型的编排建议**（写进 instructions 与工具描述），"
        "不是服务端强制流水线；唯一在工具内部自动执行的是 `answer` 的 "
        "opt-in `urls` 预取。门户隔离强制的是**工具子集**（调用未暴露的"
        "工具返回 `-32602`）与独立 server 名/instructions。",
        "",
        "## 按领域（工具面、顺序、默认窗与依赖）",
        "",
        "| 领域 | MCP server 名 | 工具子集与顺序 | 必需引擎（免 key） | 默认时效/参数 | 降级策略 |",
        "|---|---|---|---|---|---|",
    ]
    for domain in data["domains"]:
        sample = portal_map["%s.%s" % (data["agents"][0], domain)]
        tools_str = " → ".join("`%s`" % t for t in sample["tools"])
        req = ", ".join("`%s`" % e for e in sample["dependencies"]["required_engines"]) or "—"
        dfl = sample["defaults"]
        params = []
        if dfl.get("recency"):
            params.append("recency=`%s`" % dfl["recency"])
        if dfl.get("top_n"):
            params.append("top_n=%d" % dfl["top_n"])
        if dfl.get("rerank_blend") is not None:
            params.append("blend=%.1f" % dfl["rerank_blend"])
        if dfl.get("snippet_chars"):
            params.append("snippet=%d" % dfl["snippet_chars"])
        param_str = ", ".join(params) or "balanced"
        lines.append(
            "| `%s` | `%s` | %s | %s | %s | 缺 key 跳过走无 key 回退；缺 Jina 保留 RRF；缺 LLM 退回无扩展/无答案 |"
            % (domain, sample["server_name"], tools_str, req, param_str)
        )

    lines += [
        "",
        "## 按 Agent（本地 LLM 假设与回答回退顺序）",
        "",
        "| Agent | 注册类型 | 本地 Ollama | Answer 回退顺序 | 一行安装（示例 `academic`） |",
        "|---|---|---|---|---|",
    ]
    for agent in data["agents"]:
        row = portal_map["%s.academic" % agent]
        llm = row["dependencies"]["llm"]
        local_flag = "是" if llm["local_llm"] else "否（云端优先）"
        order = " → ".join("`%s`" % m for m in llm["answer_order"])
        inst = row["install"]
        if inst.get("deeplink"):
            entry = "[\u2b07 安装](%s) / `agg-search-install --portal %s --write`" % (
                inst["deeplink"], row["id"])
        else:
            entry = "`%s`" % inst["command"]
        lines.append("| `%s` | %s | %s | %s | %s |"
                     % (agent, inst["mode"], local_flag, order, entry))

    lines += [
        "",
        "## 全量门户命令索引（%d 个 `agent.domain`）" % len(portal_map),
        "",
        "| Portal | Server 名 | 安装 / 写入命令 |",
        "|---|---|---|",
    ]
    for pid in sorted(portal_map):
        row = portal_map[pid]
        lines.append("| `%s` | `%s` | `agg-search-install --portal %s%s` |"
                     % (pid, row["server_name"], pid,
                        " --write" if row["install"]["mode"] == "json" else ""))

    lines += [
        "",
        "常用示例：`agg-search-install --portal codex.academic`、"
        "`agg-search-install --portal claude-code.code`、"
        "`agg-search-install --portal cursor.news --write`。",
    ]
    return "\n".join(lines) + "\n"


def describe(name, argv, env, scope, server=SERVER):
    spec = CLIENTS[name]
    entry = payload(spec.get("shape", "mcpServers"), argv, env)
    mode = spec["mode"]
    if mode == "command" and "command" in spec:
        command = _with_env_flags(
            _expand_command(spec["command"], argv, entry, server),
            spec.get("env_flag"),
            env,
        )
        out = {"mode": "command", "server": server, "env": env,
               "entry": entry, "command": command}
        if "toml" in spec:
            out["toml"] = _codex_toml(server, argv, env)
        return out
    if mode == "json":
        path = _resolve_path(spec["path"], scope)
        doc = {}
        _section_view(doc, spec["section"])[server] = entry
        return {"mode": "json", "server": server, "path": path, "scope": scope,
                "config": doc, "section": list(spec["section"]),
                "entry": entry,
                "snippet": json.dumps(doc, indent=2, ensure_ascii=False)}
    if "toml" in spec:
        return {"mode": "toml", "server": server,
                "path": "~/.codex/config.toml",
                "snippet": _codex_toml(server, argv, env)}
    if "yaml" in spec:
        return {"mode": "yaml", "server": server,
                "path": _resolve_path(spec.get("path", ""), scope, expand=False),
                "snippet": _continue_yaml(server, argv, env)}
    doc = {"mcpServers": {server: entry}}
    return {"mode": "json", "server": server, "path": "", "config": doc,
            "section": ["mcpServers"], "entry": entry,
            "snippet": json.dumps(doc, indent=2, ensure_ascii=False)}


def main(argv=None):
    from agg_search import __version__
    ap = argparse.ArgumentParser(
        description="Register agg-search in an MCP client's config.")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {__version__}")
    ap.add_argument("--client", "-c", required=False,
                    choices=sorted(CLIENTS), default=None,
                    help="target client; --list shows them all")
    ap.add_argument("--portal", default=None, metavar="AGENT.DOMAIN",
                    help="portal ID (<agent>.<domain>, e.g. codex.academic); "
                         "sets client, isolated server name and env")
    ap.add_argument("--list", action="store_true", help="list supported clients")
    ap.add_argument("--list-portals", action="store_true",
                    help="list all agent x domain portals")
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
    ap.add_argument("--portals", action="store_true",
                    help="print the full portals registry as JSON")
    ap.add_argument("--markdown", action="store_true",
                    help="with --catalog or --portals, print Markdown docs")
    ap.add_argument("--check", action="store_true",
                    help="verify the client config exists, parses, holds the "
                         "server entry and that the runtime binary resolves")
    ap.add_argument("--profile", default=None,
                    help="pin this client to a domain profile "
                         "(sets AGG_SEARCH_PROFILE in its env)")
    a = ap.parse_args(argv)

    if a.portals:
        data = portals_catalog()
        if a.markdown:
            sys.stdout.write(portals_markdown(data))
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if a.catalog:
        data = catalog()
        if a.markdown:
            sys.stdout.write(catalog_markdown(data))
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if a.list_portals:
        print("\n".join(portals.describe()))
        return 0

    portal = None
    if a.portal:
        try:
            portal = portals.load(a.portal)
        except portals.UnknownPortal as exc:
            raise SystemExit(str(exc.args[0]))
        if a.client and a.client != portal["agent"]:
            raise SystemExit("--client %s conflicts with --portal %s"
                             % (a.client, portal["id"]))
        if a.profile and a.profile != portal["domain"]:
            raise SystemExit("--profile %s conflicts with --portal %s"
                             % (a.profile, portal["id"]))
        a.client = portal["agent"]

    if a.list or not a.client:
        width = max(len(n) for n in CLIENTS)
        for name in sorted(CLIENTS):
            spec = CLIENTS[name]
            print("%-*s  %-8s %s" % (width, name, spec["mode"],
                                     spec.get("doc", "")))
        return 0

    argv_cmd = [a.command] + [x for x in a.args.split(",") if x]
    env = {}
    if portal:
        env.update(portal["env"])
    elif a.profile:
        env["AGG_SEARCH_PROFILE"] = a.profile
    for item in a.env:
        key, _, value = item.partition("=")
        if not key:
            raise SystemExit("--env expects KEY=VALUE, got %r" % item)
        env[key] = value  # explicit --env wins over --portal/--profile

    target_server = portal["server_name"] if portal else SERVER
    info = describe(a.client, argv_cmd, env, a.scope, server=target_server)
    if portal:
        info["portal"] = portal["id"]
        info["tools"] = list(portal["tools"])
    if a.as_json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    if a.check:
        return check_client(a.client, info, argv_cmd[0], server=target_server)
    if a.write:
        if info.get("mode") != "json" or not info.get("path"):
            raise SystemExit("%s cannot be written automatically; %s"
                             % (a.client, CLIENTS[a.client].get(
                                 "doc", "paste the snippet from --print")))
        path = write_config(info["path"], tuple(info["section"]), target_server,
                            info["entry"])
        print("wrote %s (%s)" % (path, target_server))
        return 0
    if info["mode"] == "command":
        print(" ".join(info["command"]))
        if info.get("toml"):
            print()
            print(info["toml"])
        return 0
    print(info["snippet"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
