#!/usr/bin/env bash
# End-to-end verification for both stacks. Run on the Docker host:
#
#   ./scripts/verify.sh
#   ./scripts/verify.sh "What is our travel expense policy?"
#   STACK_ROOT=~ ./scripts/verify.sh          # stacks deployed under $HOME
#
# Execute it, do not `source` it — it cd's, sets shell options, and exits non-zero.
#
# STACK_ROOT is the directory containing unstructured-stack/ and anythingllm-stack/.
# It defaults to this script's parent directory, then falls back to $HOME.
# Exit code 0 means every check passed. Each failure is printed with the reason.

if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  printf 'verify.sh must be executed, not sourced. Run:\n  bash %s\n' "${BASH_SOURCE[0]}" >&2
  return 1 2>/dev/null || exit 1
fi

set -uo pipefail

QUESTION="${1:-What documents are in this knowledge base?}"
PASS=0
FAIL=0

ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL + 1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

script_parent="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "${STACK_ROOT:-}" ]]; then
  if [[ -d "$script_parent/unstructured-stack" ]]; then
    STACK_ROOT="$script_parent"
  else
    STACK_ROOT="$HOME"
  fi
fi
if [[ ! -d "$STACK_ROOT/unstructured-stack" || ! -d "$STACK_ROOT/anythingllm-stack" ]]; then
  printf 'error: %s does not contain unstructured-stack/ and anythingllm-stack/.\n' "$STACK_ROOT" >&2
  printf '       Set STACK_ROOT to the directory that does.\n' >&2
  exit 2
fi
cd "$STACK_ROOT" || exit 2
printf 'stack root: %s\n' "$STACK_ROOT"

# Parse a dotenv file instead of sourcing it: values legitimately contain spaces
# (WORKSPACE_NAME), and sourcing would execute anything inside $( ) or backticks.
load_env() {
  local file="$1" line key value
  [[ -f "$file" ]] || { bad "$file is missing (cp .env.example .env)"; return 1; }
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"                                  # tolerate CRLF
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key//[[:space:]]/}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ ${#value} -ge 2 && ( "$value" == \"*\" || "$value" == \'*\' ) ]]; then
      value="${value:1:${#value}-2}"                      # strip one quote pair
    fi
    export "$key=$value"
  done < "$file"
  ok "loaded $file"
}

head_ "0. Configuration"
load_env "unstructured-stack/.env" || true
UNSTRUCTURED_PORT="${UNSTRUCTURED_HOST_PORT:-8003}"
VAULT_FROM_UNSTRUCTURED="${VAULT_HOST_PATH:-}"
load_env "anythingllm-stack/.env" || true
ALLM_URL="${ANYTHINGLLM_BASE_URL:-http://localhost:${ANYTHINGLLM_HOST_PORT:-3001}}"
SLUG="${WORKSPACE_SLUG:-corporate-knowledge-base}"

if [[ -n "${VAULT_FROM_UNSTRUCTURED}" && "${VAULT_FROM_UNSTRUCTURED}" == "${VAULT_HOST_PATH:-}" ]]; then
  ok "VAULT_HOST_PATH matches across both stacks (${VAULT_HOST_PATH})"
else
  bad "VAULT_HOST_PATH differs between the two .env files"
fi

for var in JWT_SECRET SIG_KEY SIG_SALT; do
  if [[ -n "${!var:-}" ]]; then ok "$var is set"; else bad "$var is empty — openssl rand -hex 32"; fi
done
if [[ "${LLM_MODEL:-}" == CHANGE-ME* || -z "${LLM_MODEL:-}" ]]; then
  bad "LLM_MODEL is not set to your served model id"
else
  ok "LLM_MODEL=${LLM_MODEL}"
fi

head_ "1. Shared network"
if docker network inspect "${SHARED_NETWORK_NAME:-}" >/dev/null 2>&1; then
  ok "external network ${SHARED_NETWORK_NAME} exists"
else
  bad "network ${SHARED_NETWORK_NAME:-<unset>} missing — docker network create ${SHARED_NETWORK_NAME:-ai-stack-net}"
fi

head_ "2. Container states (expect: unstructured-api healthy, preprocessor running, anythingllm healthy)"
for name in unstructured-api unstructured-preprocessor anythingllm; do
  state="$(docker inspect -f '{{.State.Status}}{{if .State.Health}}/{{.State.Health.Status}}{{end}}' "$name" 2>/dev/null)"
  case "$state" in
    running/healthy|running) ok "$name: $state" ;;
    "")                      bad "$name: not created" ;;
    *)                       bad "$name: $state" ;;
  esac
done

head_ "3. Health endpoints"
if curl -fsS "http://localhost:${UNSTRUCTURED_PORT}/healthcheck" >/dev/null 2>&1; then
  ok "unstructured-api  http://localhost:${UNSTRUCTURED_PORT}/healthcheck"
else
  bad "unstructured-api healthcheck unreachable on port ${UNSTRUCTURED_PORT}"
fi
if curl -fsS "${ALLM_URL}/api/ping" >/dev/null 2>&1; then
  ok "anythingllm       ${ALLM_URL}/api/ping"
else
  bad "anythingllm /api/ping unreachable at ${ALLM_URL}"
fi

head_ "4. Existing local LLM reachable from inside the AnythingLLM container"
if docker exec anythingllm curl -fsS --max-time 10 "${LLM_BASE_PATH:-http://host.docker.internal:8000/v1}/models" >/dev/null 2>&1; then
  ok "${LLM_BASE_PATH:-...}/models reachable from the container"
else
  bad "cannot reach ${LLM_BASE_PATH:-...}/models from inside anythingllm"
fi

head_ "5. Vault scoping — only corporate/ is mounted"
for name in unstructured-preprocessor anythingllm; do
  mounts="$(docker inspect -f '{{range .Mounts}}{{.Source}}=>{{.Destination}}:{{if .RW}}rw{{else}}ro{{end}}{{"\n"}}{{end}}' "$name" 2>/dev/null)"
  if [[ -z "$mounts" ]]; then bad "$name: cannot inspect mounts"; continue; fi
  if grep -q ':/vault$' <<<"$mounts"; then
    bad "$name: the whole vault root is mounted at /vault"
  else
    ok "$name: no broad /vault mount"
  fi
  offenders="$(grep -E '/vault/(shared|tickets|learnings|skills)' <<<"$mounts" || true)"
  if [[ -n "$offenders" ]]; then
    bad "$name: mounts a non-corporate vault subfolder: $offenders"
  else
    ok "$name: no non-corporate vault subfolder mounted"
  fi
  # Nothing outside corporate/ should even be visible from inside the container.
  visible="$(docker exec "$name" sh -c 'ls -1 /vault 2>/dev/null' || true)"
  if [[ "$(tr -d '[:space:]' <<<"$visible")" == "corporate" ]]; then
    ok "$name: /vault contains only corporate/"
  else
    bad "$name: /vault contains: $(tr '\n' ' ' <<<"$visible")"
  fi
done
if docker inspect -f '{{range .Mounts}}{{.Destination}}:{{if .RW}}rw{{else}}ro{{end}} {{end}}' anythingllm 2>/dev/null | grep -q '/vault/corporate:ro'; then
  ok "anythingllm: /vault/corporate is read-only"
else
  bad "anythingllm: /vault/corporate is not mounted read-only"
fi

head_ "6. No secrets, network names or vault paths hardcoded in compose"
if grep -nEi '(secret|api_key|password|token)[[:space:]]*[:=][[:space:]]*[^$[:space:]#]' \
     unstructured-stack/docker-compose.yml anythingllm-stack/docker-compose.yml \
     | grep -v '\${' | grep -v '^\s*#' >/dev/null 2>&1; then
  bad "a literal credential appears in a compose file"
else
  ok "compose files reference secrets only via \${VARS}"
fi
for f in unstructured-stack/docker-compose.yml anythingllm-stack/docker-compose.yml; do
  if grep -q 'external: true' "$f" && grep -q 'name: ${SHARED_NETWORK_NAME}' "$f"; then
    ok "$(basename "$(dirname "$f")"): network is external + parameterized"
  else
    bad "$(basename "$(dirname "$f")"): network is not external:true + \${SHARED_NETWORK_NAME}"
  fi
  if grep -q '${VAULT_HOST_PATH}/corporate:/vault/corporate' "$f"; then
    ok "$(basename "$(dirname "$f")"): vault path is parameterized and scoped to corporate/"
  else
    bad "$(basename "$(dirname "$f")"): vault mount is not \${VAULT_HOST_PATH}/corporate"
  fi
done

head_ "7. Preprocessing output present in the vault"
DOCS_DIR="${VAULT_HOST_PATH:-/srv/vault}/corporate/documents"
CHUNKS_DIR="${VAULT_HOST_PATH:-/srv/vault}/corporate/chunks"
doc_count="$(find "$DOCS_DIR" -name '*.md' -type f 2>/dev/null | wc -l | tr -d ' ')"
chunk_count="$(find "$CHUNKS_DIR" -name '*.chunks.jsonl' -type f 2>/dev/null | wc -l | tr -d ' ')"
if [[ "${doc_count:-0}" -gt 0 ]]; then
  ok "$doc_count Markdown document(s) in $DOCS_DIR"
else
  bad "no Markdown in $DOCS_DIR — drop a file in unstructured-stack/data/inbox and wait"
fi
if [[ "${doc_count:-0}" -eq 0 ]]; then
  bad "no chunk sidecars to compare yet (nothing has been preprocessed)"
elif [[ "${chunk_count:-0}" -eq "${doc_count:-0}" ]]; then
  ok "$chunk_count chunk sidecar(s), one per document"
else
  bad "chunk sidecars ($chunk_count) do not match documents ($doc_count)"
fi
sample="$(find "$DOCS_DIR" -name '*.md' -type f 2>/dev/null | head -1)"
if [[ -n "$sample" ]]; then
  if head -1 "$sample" | grep -q '^---$' && grep -q '^source_sha256:' "$sample" \
     && grep -q '^chunk_count:' "$sample" && grep -q '<!-- chunk ' "$sample"; then
    ok "output format: frontmatter + chunk markers present in $(basename "$sample")"
  else
    bad "$sample is missing frontmatter or chunk markers"
  fi
fi

head_ "8. Retrieval and generation (checked separately, they fail independently)"
if [[ -z "${ANYTHINGLLM_API_KEY:-}" ]]; then
  bad "ANYTHINGLLM_API_KEY unset — generate it in Settings > Tools > Developer API"
else
  if curl -fsS -H "Authorization: Bearer ${ANYTHINGLLM_API_KEY}" "${ALLM_URL}/api/v1/auth" >/dev/null 2>&1; then
    ok "developer API key accepted"
  else
    bad "developer API key rejected"
  fi

  # 8a. Retrieval alone. No LLM is involved, so this must pass even on a host with no chat
  # model — which is the whole reason it is separate from the chat check below.
  search_body="$(QUESTION="$QUESTION" python3 -c \
    'import json, os; print(json.dumps({"query": os.environ["QUESTION"], "topN": 6}))')"
  search="$(curl -sS -X POST "${ALLM_URL}/api/v1/workspace/${SLUG}/vector-search" \
      -H "Authorization: Bearer ${ANYTHINGLLM_API_KEY}" \
      -H 'Content-Type: application/json' \
      -d "$search_body" 2>/dev/null)"
  if [[ -z "$search" ]]; then
    bad "vector-search request failed for slug '${SLUG}'"
  elif grep -Eq '"results"[[:space:]]*:[[:space:]]*\[[[:space:]]*\]' <<<"$search"; then
    bad "vector-search matched nothing — run allm.py ingest, or lower similarityThreshold"
  elif grep -q '"results"' <<<"$search"; then
    ok "vector-search returned matches — embedding and similarity search work"
  else
    bad "vector-search gave no results array: $(printf '%s' "$search" | head -c 200)"
  fi

  # 8b. The full round-trip, which needs the LLM. No -f here: AnythingLLM answers HTTP 500
  # with a JSON body naming the failure, and -f would discard exactly that body.
  chat_body="$(QUESTION="$QUESTION" python3 -c \
    'import json, os; print(json.dumps({"message": os.environ["QUESTION"], "mode": "query"}))')"
  response="$(curl -sS -X POST "${ALLM_URL}/api/v1/workspace/${SLUG}/chat" \
      -H "Authorization: Bearer ${ANYTHINGLLM_API_KEY}" \
      -H 'Content-Type: application/json' \
      -d "$chat_body" 2>/dev/null)"
  if [[ -z "$response" ]]; then
    bad "workspace chat request failed for slug '${SLUG}'"
  else
    printf '  ---- response ----\n%s\n  ------------------\n' "$(printf '%s' "$response" | head -c 1200)"
    if grep -Eq '"error"[[:space:]]*:[[:space:]]*"' <<<"$response"; then
      err_text="$(sed -n 's/.*"error"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' <<<"$response" | head -1)"
      bad "chat aborted: ${err_text:-unknown} — generation only; 8a above is the retrieval verdict"
    elif grep -Eq '"textResponse"[[:space:]]*:[[:space:]]*null' <<<"$response"; then
      bad "textResponse is null — the LLM returned nothing. Check section 4 and LLM_MODEL"
    elif grep -q '"textResponse"' <<<"$response"; then
      ok "chat returned textResponse — generation works"
      if grep -Eq '"sources"[[:space:]]*:[[:space:]]*\[[[:space:]]*\]' <<<"$response"; then
        bad "but sources[] is empty — the answer was not grounded in the vault"
      else
        ok "sources[] is populated — the answer is grounded in retrieved chunks"
      fi
    else
      bad "no textResponse in reply"
    fi
  fi
fi

head_ "Summary"
printf '  %d passed, %d failed\n\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
