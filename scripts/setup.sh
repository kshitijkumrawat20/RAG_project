#!/usr/bin/env bash
# One-shot setup for both stacks. Run from anywhere:
#
#   bash scripts/setup.sh
#   bash scripts/setup.sh --vault /srv/vault
#
# Safe to run as many times as you like. It never overwrites an existing .env and never
# rotates a secret that is already set — re-running only fills in what is still missing.
#
# What it does:
#   1. creates the shared Docker network            (skips if it exists)
#   2. creates <vault>/corporate                    (skips if it exists)
#   3. copies .env.example -> .env in both stacks   (skips if .env exists)
#   4. fills in the vault paths and the uid/gid the preprocessor runs as
#   5. clamps the cpu/memory limits to what this host actually has
#   6. generates JWT_SECRET / SIG_KEY / SIG_SALT    (skips any already set)
#   7. creates the runtime folders both stacks need
#   8. detects the local LLM on port 8000 and fills in its model id, if it is running
#
# It does NOT start any container — Steps 2 and 4 of SETUP.md do that.

if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  printf 'setup.sh must be executed, not sourced. Run:\n  bash %s\n' "${BASH_SOURCE[0]}" >&2
  return 1 2>/dev/null || exit 1
fi

set -uo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VAULT="$HOME/vault"
NETWORK=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vault)   VAULT="${2:?--vault needs a path}"; shift 2 ;;
    --network) NETWORK="${2:?--network needs a name}"; shift 2 ;;
    # Print the header comment block, so --help cannot drift out of sync with it.
    -h|--help) awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' \
                 "${BASH_SOURCE[0]}"; exit 0 ;;
    *)         printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done

STEPS=0
WARNS=0
step() { STEPS=$((STEPS + 1)); printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
skip() { printf '  \033[2m·\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; WARNS=$((WARNS + 1)); }
die()  { printf '\n\033[31merror:\033[0m %s\n' "$1" >&2; exit 1; }

UNS="$REPO_ROOT/unstructured-stack"
ALLM="$REPO_ROOT/anythingllm-stack"
[[ -d "$UNS" && -d "$ALLM" ]] || die "$REPO_ROOT does not contain unstructured-stack/ and anythingllm-stack/"

# --- dotenv helpers ------------------------------------------------------------------
# Written as plain bash rather than sed so that values containing / | & or spaces cannot
# break the substitution. Rewrites in place, preserving the file's comments and order.

get_kv() {
  local file="$1" key="$2" line
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    if [[ "$line" == "$key="* ]]; then printf '%s' "${line#*=}"; return 0; fi
  done < "$file"
  return 1
}

set_kv() {
  local file="$1" key="$2" value="$3" line found=0 tmp
  tmp="$(mktemp)" || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    if [[ "$line" == "$key="* ]]; then
      printf '%s=%s\n' "$key" "$value" >> "$tmp"
      found=1
    else
      printf '%s\n' "$line" >> "$tmp"
    fi
  done < "$file"
  (( found )) || printf '%s=%s\n' "$key" "$value" >> "$tmp"
  cat "$tmp" > "$file"          # truncate-and-write keeps the original inode and mode
  rm -f "$tmp"
}

random_hex_32() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    od -An -tx1 -N32 /dev/urandom | tr -d ' \n'; printf '\n'
  fi
}

printf 'repo:    %s\nvault:   %s\n' "$REPO_ROOT" "$VAULT"

# --- 3. .env files ------------------------------------------------------------------
# Done before the network step so that .env can supply SHARED_NETWORK_NAME.
step "Config files"
for d in "$UNS" "$ALLM"; do
  [[ -f "$d/.env.example" ]] || die "$d/.env.example is missing"
  if [[ -f "$d/.env" ]]; then
    skip "$(basename "$d")/.env already exists — leaving it alone"
    # But an .env carried over from an older version of the example can be missing keys
    # entirely. Compose expands an undefined ${VAR} to the empty string with only a
    # warning, so a missing LLM_BASE_PATH shows up as a mystery connection failure much
    # later. Backfill anything absent, using the example's own default.
    added=0
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line%$'\r'}"
      [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
      [[ "$line" == *=* ]] || continue
      example_key="${line%%=*}"
      example_key="${example_key//[[:space:]]/}"
      [[ "$example_key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
      if ! get_kv "$d/.env" "$example_key" >/dev/null; then
        set_kv "$d/.env" "$example_key" "${line#*=}"
        printf '    + added missing %s\n' "$example_key"
        added=$((added + 1))
      fi
    done < "$d/.env.example"
    (( added )) && warn "$(basename "$d")/.env was missing $added key(s) — backfilled from .env.example"
  else
    cp "$d/.env.example" "$d/.env" || die "could not create $d/.env"
    ok "created $(basename "$d")/.env"
  fi
done

[[ -n "$NETWORK" ]] || NETWORK="$(get_kv "$ALLM/.env" SHARED_NETWORK_NAME)" || NETWORK="ai-stack-net"
[[ -n "$NETWORK" ]] || NETWORK="ai-stack-net"
# Both stacks must agree on the name, whatever it came from.
set_kv "$UNS/.env" SHARED_NETWORK_NAME "$NETWORK"
set_kv "$ALLM/.env" SHARED_NETWORK_NAME "$NETWORK"

# --- 1. shared network --------------------------------------------------------------
step "Shared Docker network ($NETWORK)"
if ! command -v docker >/dev/null 2>&1; then
  warn "docker is not installed — create the network yourself later: docker network create $NETWORK"
elif docker network inspect "$NETWORK" >/dev/null 2>&1; then
  skip "already exists"
elif docker network create "$NETWORK" >/dev/null 2>&1; then
  ok "created"
else
  warn "could not create it — is the Docker daemon running? (docker ps)"
fi

# --- 2. vault ------------------------------------------------------------------------
step "Vault folder"
if [[ -d "$VAULT/corporate" ]]; then
  skip "$VAULT/corporate already exists"
elif mkdir -p "$VAULT/corporate" 2>/dev/null; then
  ok "created $VAULT/corporate"
else
  die "cannot create $VAULT/corporate — pick a writable path with --vault, e.g. --vault \$HOME/vault"
fi
[[ -w "$VAULT/corporate" ]] || warn "$VAULT/corporate is not writable by you — the preprocessor will fail"

# --- 4. paths and ownership ----------------------------------------------------------
step "Paths and ownership"
for d in "$UNS" "$ALLM"; do
  set_kv "$d/.env" VAULT_HOST_PATH "$VAULT" && ok "$(basename "$d")/.env: VAULT_HOST_PATH=$VAULT"
done
set_kv "$ALLM/.env" VAULT_CORPORATE_DIR "$VAULT/corporate" && ok "anythingllm-stack/.env: VAULT_CORPORATE_DIR=$VAULT/corporate"
set_kv "$UNS/.env" PREPROCESSOR_UID "$(id -u)"
set_kv "$UNS/.env" PREPROCESSOR_GID "$(id -g)"
ok "unstructured-stack/.env: preprocessor runs as $(id -u):$(id -g) (you)"

# --- 5. resource limits --------------------------------------------------------------
# Docker refuses to create a container whose `cpus` limit exceeds the host's core count:
#   "range of CPUs is from 0.01 to 4.00, as there are only 4 CPUs available"
# The committed defaults are sized for the target workstation, so only ever clamp DOWN.
# Never raise a deliberately modest limit just because this host happens to be bigger —
# the whole point of those numbers is to leave the GPU LLM container room to breathe.
step "Resource limits"
CPUS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 0)"
TOTAL_GB=0
mem_kb="$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo 2>/dev/null)"
[[ "${mem_kb:-}" =~ ^[0-9]+$ ]] && TOTAL_GB=$(( mem_kb / 1048576 ))

clamp_cpu() {
  local file="$1" key="$2" cap="$3" current
  current="$(get_kv "$file" "$key")" || return 0
  [[ "$current" =~ ^[0-9]+(\.[0-9]+)?$ ]] || return 0
  if awk -v c="$current" -v cap="$cap" 'BEGIN { exit !(c > cap) }'; then
    set_kv "$file" "$key" "$cap"
    ok "$key: $current -> $cap (host has $CPUS core(s))"
  else
    skip "$key=$current fits in $CPUS core(s)"
  fi
}

clamp_mem() {
  local file="$1" key="$2" cap="$3" current
  current="$(get_kv "$file" "$key")" || return 0
  if [[ ! "$current" =~ ^([0-9]+)[gG]$ ]]; then
    skip "$key=$current is not a plain gigabyte value — left alone"
    return 0
  fi
  if (( BASH_REMATCH[1] > cap )); then
    set_kv "$file" "$key" "${cap}g"
    ok "$key: $current -> ${cap}g (host has ${TOTAL_GB}g RAM)"
  else
    skip "$key=$current fits in ${TOTAL_GB}g RAM"
  fi
}

if [[ ! "$CPUS" =~ ^[0-9]+$ ]] || (( CPUS < 1 )); then
  warn "could not determine the CPU count — leaving the cpus limits alone"
else
  # Leave one core for the host and, on the real deployment, for the LLM container.
  cpu_cap=$(( CPUS > 1 ? CPUS - 1 : 1 ))
  clamp_cpu "$UNS/.env"  UNSTRUCTURED_API_CPU_LIMIT "$cpu_cap"
  clamp_cpu "$UNS/.env"  PREPROCESSOR_CPU_LIMIT     "$cpu_cap"
  clamp_cpu "$ALLM/.env" ANYTHINGLLM_CPU_LIMIT      "$cpu_cap"
fi

if (( TOTAL_GB < 1 )); then
  warn "could not determine total RAM — leaving the memory limits alone"
else
  # A limit is a ceiling, not a reservation, so half of RAM each is not overcommitting.
  mem_cap=$(( TOTAL_GB / 2 )); (( mem_cap < 1 )) && mem_cap=1
  clamp_mem "$UNS/.env"  UNSTRUCTURED_API_MEM_LIMIT "$mem_cap"
  clamp_mem "$UNS/.env"  PREPROCESSOR_MEM_LIMIT     "$mem_cap"
  clamp_mem "$ALLM/.env" ANYTHINGLLM_MEM_LIMIT      "$mem_cap"
fi

# --- 6. secrets ----------------------------------------------------------------------
step "Secrets"
for v in JWT_SECRET SIG_KEY SIG_SALT; do
  current="$(get_kv "$ALLM/.env" "$v")"
  if [[ -n "$current" ]]; then
    skip "$v already set — not rotating it (that would orphan existing stored credentials)"
  else
    set_kv "$ALLM/.env" "$v" "$(random_hex_32)" && ok "$v generated"
  fi
done

# --- 7. runtime folders --------------------------------------------------------------
step "Runtime folders"
mkdir -p "$UNS"/data/{inbox,archive,failed,state} && ok "unstructured-stack/data/{inbox,archive,failed,state}"
mkdir -p "$ALLM/data" && ok "anythingllm-stack/data"
if [[ -d "$ALLM/data/.env" ]]; then
  warn "anythingllm-stack/data/.env is a DIRECTORY — Docker created it. Remove it: rmdir '$ALLM/data/.env'"
elif [[ -f "$ALLM/data/.env" ]]; then
  skip "anythingllm-stack/data/.env exists"
else
  : > "$ALLM/data/.env" && ok "anythingllm-stack/data/.env (must be a file, not a folder)"
fi

# --- 8. local LLM --------------------------------------------------------------------
step "Local LLM on port 8000"
models_json="$(curl -fsS --max-time 5 http://localhost:8000/v1/models 2>/dev/null)"
model_id="$(printf '%s' "$models_json" \
  | grep -o '"id"[[:space:]]*:[[:space:]]*"[^"]*"' \
  | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"
current_model="$(get_kv "$ALLM/.env" LLM_MODEL)"
if [[ -n "$model_id" ]]; then
  set_kv "$ALLM/.env" LLM_MODEL "$model_id" && ok "found and set LLM_MODEL=$model_id"
elif [[ "$current_model" == CHANGE-ME* || -z "$current_model" ]]; then
  set_kv "$ALLM/.env" LLM_MODEL "placeholder-no-llm-on-this-host"
  warn "nothing answering on http://localhost:8000/v1/models — set a placeholder."
  printf '    Ingestion, embedding and vector search will all work. Only the written\n'
  printf '    answer needs the LLM. On the real host, re-run this script to fill it in.\n'
else
  skip "LLM_MODEL=$current_model (kept; port 8000 is not answering right now)"
fi

# --- summary -------------------------------------------------------------------------
printf '\n\033[1mDone.\033[0m %d steps, %d warning(s).\n\n' "$STEPS" "$WARNS"
printf 'Next, from %s:\n\n' "$REPO_ROOT"
printf '  1. cd unstructured-stack && docker compose up -d --build && cd ..\n'
printf '  2. cp some-document.pdf unstructured-stack/data/inbox/\n'
printf '     docker logs -f unstructured-preprocessor\n'
printf '     ls %s/corporate/documents\n' "$VAULT"
printf '  3. cd anythingllm-stack && docker compose up -d\n'
printf '  4. open http://localhost:3001, then Settings > Tools > Developer API >\n'
printf '     Generate New API Key, and hand it to:\n'
printf '       uv run scripts/allm.py set-key <the-key>\n'
printf '  5. uv run scripts/allm.py bootstrap\n'
printf '     uv run scripts/allm.py ingest\n'
printf '     uv run scripts/allm.py query "What is in this knowledge base?"\n'
printf '  6. cd .. && bash scripts/verify.sh\n\n'
printf 'Full explanations: SETUP.md\n'
