#!/usr/bin/env bash
# Offline A/B for a domain change: record once -> grade once -> replay free.
#
#   tools/eval-offline.sh QUERIES.json PROFILE RUBRIC [VARIANT ...]
#
#   QUERIES.json  JSON list of query strings
#   PROFILE       general | academic | code | news (engines + fusion defaults)
#   RUBRIC        general | academic | code | news (qwen grading rubric)
#   VARIANT       NAME[:engines=+arxiv;w.arxiv=1.3;blend=0.3;boost=1.2;...]
#                 first one is the baseline; default "current"
#
# Environment:
#   LAB       work dir (default /tmp/agg-lab-eval); fixture/judgments are
#             reused when present, so only the first run spends quota
#   ENGINES   comma list to record (default: the profile's engines plus EXTRA)
#   EXTRA     engines recorded on top of the profile's (e.g. arxiv)
#   RECENCY   day|week|month|year for recording (default: the profile's)
#   JINA=1    also record Jina scores and replay the rerank blend (paid tokens)
#
# Engine quota is spent only by `record`; cooling engines are skipped.
# Grades come from local Ollama (qwen3.5:9b): free, model-made, not human gold.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
[ $# -ge 3 ] || { sed -n '2,22p' "$0"; exit 2; }
QUERIES=$1 PROFILE=$2 RUBRIC=$3
shift 3
EVAL="$ROOT/.venv/bin/agg-search-eval"
SEARCH="$ROOT/.venv/bin/agg-search"
PY="$ROOT/.venv/bin/python"
[ -x "$EVAL" ] || { echo "run: uv venv .venv && VIRTUAL_ENV=\$PWD/.venv uv pip install -e ." >&2; exit 2; }
LAB=${LAB:-/tmp/agg-lab-eval}
mkdir -p "$LAB"
FIXTURE="$LAB/fixture.$PROFILE.json"
GRADES="$LAB/grades.$PROFILE.$RUBRIC.json"
JINA_SCORES="$LAB/jina.$PROFILE.json"

cfg=$("$SEARCH" --show-config --profile "$PROFILE")
ENGINES=${ENGINES:-$("$PY" -c 'import json,sys; c=json.loads(sys.argv[1]); x=[e for e in sys.argv[2].split(",") if e]; print(",".join(c["engines"]+[e for e in x if e not in c["engines"]]))' "$cfg" "${EXTRA:-}")}
RECENCY=${RECENCY:-$("$PY" -c 'import json,sys; print(json.loads(sys.argv[1])["recency"] or "")' "$cfg")}

if [ ! -s "$FIXTURE" ]; then
    echo "== record ($ENGINES${RECENCY:+, recency=$RECENCY}) -> $FIXTURE" >&2
    "$EVAL" record --queries "$QUERIES" --out "$FIXTURE" --engines "$ENGINES" \
        ${RECENCY:+--recency "$RECENCY"}
else
    echo "== reuse $FIXTURE" >&2
fi
echo "== judge (qwen, rubric=$RUBRIC) -> $GRADES" >&2
"$EVAL" judge --fixture "$FIXTURE" --out "$GRADES" --rubric "$RUBRIC"
rerank=()
if [ "${JINA:-0}" = 1 ]; then
    echo "== judge (jina scores for rerank replay) -> $JINA_SCORES" >&2
    "$EVAL" judge --fixture "$FIXTURE" --out "$JINA_SCORES" --judge jina
    rerank=(--rerank-scores "$JINA_SCORES")
fi
args=()
for v in "${@:-current}"; do args+=(--variant "$v"); done
"$EVAL" compare --fixture "$FIXTURE" --judgments "$GRADES" --profile "$PROFILE" \
    "${rerank[@]}" "${args[@]}"
