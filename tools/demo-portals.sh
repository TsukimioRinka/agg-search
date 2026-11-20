#!/usr/bin/env bash
# Reproducible demo/verification for portals, registry parity and hardening.
# Read-only for the repo: artifacts are regenerated into a temp dir and diffed.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY=python3
INSTALL="$ROOT/.venv/bin/agg-search-install"
MCP="$ROOT/.venv/bin/agg-search-mcp"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

step() { printf '\n== %s\n' "$1"; }

step "lint"
if command -v ruff >/dev/null 2>&1; then
    ruff check --select F,E9,I001 src tests
else
    echo "ruff not installed; skipped"
fi

step "test suite"
"$PY" -m unittest discover -s tests

step "engine registry parity"
"$PY" - <<'PY'
from agg_search import engine_specs, mcp, portals, search
assert list(search.ENGINES) == engine_specs.names()
assert list(search.CREDENTIAL_ENV) == list(engine_specs.credential_env())
assert portals.ENGINE_ENV_REQUIREMENTS == engine_specs.env_requirements()
assert mcp.SEARCH_SCHEMA["properties"]["engines"]["items"]["enum"] \
    == engine_specs.names()
assert search.DEFAULT_ENGINES == engine_specs.default_engines()
print("engines:", ", ".join(engine_specs.names()))
PY

step "artifact determinism"
"$INSTALL" --portals > "$TMP/portals.json"
"$INSTALL" --portals --markdown > "$TMP/portals.md"
"$INSTALL" --catalog > "$TMP/catalog.json"
"$INSTALL" --catalog --markdown > "$TMP/catalog.md"
cmp "$TMP/portals.json" portals.json
cmp "$TMP/portals.md" docs/portals.md
cmp "$TMP/catalog.json" catalog.json
cmp "$TMP/catalog.md" docs/catalog.md
echo "portals.json / docs/portals.md / catalog.json / docs/catalog.md match"

step "artifact path hygiene"
"$PY" - <<'PY'
import pathlib
home = str(pathlib.Path.home())
for name in ("catalog.json", "portals.json"):
    text = pathlib.Path(name).read_text()
    assert home not in text, "%s embeds %s" % (name, home)
print("no absolute home paths in generated artifacts")
PY

step "portal isolation (academic = 3 tools, code = 2, answer blocked)"
call() {
    "$MCP" --portal "$1" <<< '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
}
call claude-code.academic > "$TMP/academic.json"
call claude-code.code > "$TMP/code.json"
"$PY" - "$TMP/academic.json" "$TMP/code.json" <<'PY'
import json, sys
academic = json.load(open(sys.argv[1]))["result"]["tools"]
code = json.load(open(sys.argv[2]))["result"]["tools"]
assert [t["name"] for t in academic] == ["search", "fetch", "answer"]
assert [t["name"] for t in code] == ["search", "fetch"]
print("academic:", [t["name"] for t in academic])
print("code    :", [t["name"] for t in code])
PY
"$MCP" --portal claude-code.code <<< '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"answer","arguments":{"query":"x"}}}' \
  > "$TMP/blocked.json"
"$PY" - "$TMP/blocked.json" <<'PY'
import json, sys
err = json.load(open(sys.argv[1]))["error"]
assert err["code"] == -32602, err
print("answer on code portal ->", err["code"], err["message"][:60])
PY

step "answer caps and runtime fallback"
"$PY" -m unittest tests.test_registry_demo -v

step "local CLI portal surface"
"$ROOT/.venv/bin/agg-search" --list-portals | head -3
"$ROOT/.venv/bin/agg-search" --show-config --portal claude-code.academic \
  | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print("portal:", d["portal"], "| tools:", d["tools"])'

if [ "${DEMO_CHECK_PYPI:-0}" = "1" ] && command -v uvx >/dev/null 2>&1; then
    step "published surface (PyPI latest)"
    uvx --from agg-search@latest agg-search --list-portals | head -2
fi
