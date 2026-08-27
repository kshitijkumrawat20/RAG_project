# Document Preprocessing + Knowledge-Base Agent

> **Just want to get it running?** → **[SETUP.md](SETUP.md)** is a step-by-step walkthrough
> with copy-paste commands and a "check it worked" after every step. This README is the
> reference: what each setting does, why it's set that way, and how to verify the whole thing.

Two drop-in Docker Compose stacks for the self-hosted AI host:

| Stack | Does | Talks to |
|---|---|---|
| `unstructured-stack/` | Converts raw corporate files (PDF/DOCX/PPTX/HTML/…) into clean, chunked Markdown + JSONL | writes `/vault/corporate` |
| `anythingllm-stack/` | Knowledge-base agent: embeds that output, answers queries over HTTP | reads `/vault/corporate`, generates via the existing local LLM |

The two stacks never call each other. `/vault/corporate` is the handoff.

```
              ┌──────────────────────── unstructured-stack ────────────────────────┐
 raw files →  │  data/inbox/  →  preprocessor  ──HTTP──►  unstructured-api (CPU)   │
              │                      │                                            │
              └──────────────────────┼────────────────────────────────────────────-┘
                                     │ writes
                            /vault/corporate/documents/*.md
                            /vault/corporate/chunks/*.chunks.jsonl
                                     │ reads (read-only)
              ┌──────────────────────┼──────── anythingllm-stack ─────────────────┐
              │  scripts/allm.py ingest  →  anythingllm :3001                     │
              │                              ├─ embeddings: native MiniLM, CPU    │
              │                              ├─ vectors:    LanceDB (./data)      │
              │                              └─ generation: ──────────────────────┼──► host.docker.internal:8000/v1
              └───────────────────────────────────────────────────────────────────┘        (existing GPU LLM)
```

Contents:

```
unstructured-stack/
  docker-compose.yml           unstructured-api + preprocessor worker
  .env.example
  data/                        inbox/ archive/ failed/ state/   (persistent)
  preprocessor/                Dockerfile, pyproject.toml, preprocess.py
anythingllm-stack/
  docker-compose.yml           anythingllm (single container)
  .env.example
  data/                        workspaces, SQLite, LanceDB vectors  (persistent)
  scripts/allm.py              auth | bootstrap | ingest | query | docs | matrix
  fixtures/                    sample files for the file-type matrix test
scripts/verify.sh              the verification checklist, automated
```

---

## 1. One-time setup

Everything below runs on the Docker host (Ubuntu guest under WSL2). Python tooling uses
`uv` only — no `pip`, no virtualenv to create.

> **Shortcut:** steps 1–5 below are also scripted. From the repo root,
> `bash scripts/setup.sh` creates the network, the vault folder, both `.env` files, the
> three secrets and the runtime folders, and auto-detects the local LLM's model id. It is
> idempotent and never overwrites an existing `.env` or rotates a live secret. Read on for
> what each of those values means and how to change it.

**1. Put the stacks in place.** The compose files expect the layout `~/<stack>/`:

```bash
cp -r unstructured-stack anythingllm-stack scripts ~/
```

This is a convention, not a requirement — the compose files use only relative paths, and
`scripts/verify.sh` locates the stacks relative to itself. Running both stacks straight
from a clone works identically; the rest of this README says `~/<stack>` for brevity.

**2. Create the shared external network** (skip if it already exists):

```bash
docker network create ai-stack-net
```

**3. Generate secrets.** Three values, 32 bytes of hex each:

```bash
for v in JWT_SECRET SIG_KEY SIG_SALT; do echo "$v=$(openssl rand -hex 32)"; done
```

**4. Fill in both `.env` files.** They must agree on `SHARED_NETWORK_NAME` and
`VAULT_HOST_PATH`.

```bash
cd ~/unstructured-stack && cp .env.example .env && nano .env
cd ~/anythingllm-stack  && cp .env.example .env && nano .env
```

(Use whichever editor you have — `nano`, `vim`, or the file browser in your IDE. Or skip
the editing entirely: `bash scripts/setup.sh` fills in everything in this section except
the LLM model id, which it detects, and the API key from §5, which needs the UI.)

The only values you *must* change from the defaults:

| Variable | In | Set to |
|---|---|---|
| `SHARED_NETWORK_NAME` | both | your existing external network |
| `VAULT_HOST_PATH` | both | host path of the vault root (only `corporate/` gets mounted) |
| `JWT_SECRET`, `SIG_KEY`, `SIG_SALT` | anythingllm | the generated hex values |
| `LLM_MODEL` | anythingllm | your served model id — `curl -s http://localhost:8000/v1/models` |
| `VAULT_CORPORATE_DIR` | anythingllm | `$VAULT_HOST_PATH/corporate` (used by `allm.py`, which runs on the host) |

`unstructured-stack/.env` contains no secrets at all: the self-hosted Unstructured API is
unauthenticated by design and is reachable only from the shared Docker network plus a
loopback-bound host port.

**5. Prepare the persistent directories.** AnythingLLM runs as uid 1000 and needs its
storage writable, plus an `.env` file that must exist *before* the first `up` — otherwise
Docker creates a directory at that mount point and the container fails to start:

```bash
cd ~/anythingllm-stack && set -a && source .env && set +a

mkdir -p data && touch data/.env && sudo chown -R 1000:1000 data
mkdir -p ~/unstructured-stack/data/{inbox,archive,failed,state}
sudo chown -R 1000:1000 ~/unstructured-stack/data
sudo mkdir -p "$VAULT_HOST_PATH/corporate" && sudo chown -R 1000:1000 "$VAULT_HOST_PATH/corporate"
```

If uid 1000 is not the right owner for your vault, set `PREPROCESSOR_UID`/`PREPROCESSOR_GID`
in `unstructured-stack/.env` to whoever owns `corporate/`.

## 2. Bring the stacks up

```bash
cd ~/unstructured-stack && docker compose up -d --build
cd ~/anythingllm-stack  && docker compose up -d
```

First boot downloads model weights: the Unstructured layout/OCR ONNX models (~500 MB, only
if a PDF needs `hi_res`) and AnythingLLM's MiniLM embedder (~90 MB, on first embed). Those
are the only outbound calls either stack makes, and they carry no document content.

```bash
docker compose ps           # in each directory
docker compose logs -f      # preprocessor logs every file it handles
```

Expected steady state: `unstructured-api` **healthy**, `unstructured-preprocessor`
**running** (it polls the inbox; it has no health endpoint of its own),
`anythingllm` **healthy**.

## 3. Run a document through preprocessing

Drop files into the inbox — subdirectories are preserved in the output:

```bash
cp ~/Downloads/q3-report.pdf ~/unstructured-stack/data/inbox/finance/
docker compose logs -f preprocessor
```

The worker polls every `WATCH_INTERVAL_SECONDS` (default 20), ignores files modified in the
last 5 seconds so half-copied uploads are not read, and skips anything it has already
processed (sha256 ledger in `data/state/ledger.json`). Successful originals move to
`data/archive/`; failures move to `data/failed/` with a `.error.txt` next to them.

To process a backlog once and exit instead of running as a watcher, set `ONE_SHOT=true`.

### Output format

Two files per source document:

```
/vault/corporate/documents/finance/q3-report.md              ← what AnythingLLM ingests
/vault/corporate/chunks/finance/q3-report.chunks.jsonl       ← machine-readable chunks
```

The Markdown carries YAML frontmatter and marks every chunk boundary inline:

```markdown
---
title: "q3 report"
source_filename: "q3-report.pdf"
source_relpath: "finance/q3-report.pdf"
source_sha256: "9f2c1ab5…"
source_filetype: "application/pdf"
page_count: 14
chunk_count: 37
chunk_strategy: "by_title"
chunk_max_characters: 900
chunk_overlap: 120
partition_strategy: "auto"
processed_at: "2026-08-26T09:14:02Z"
producer: "unstructured-api"
vault_document: "/vault/corporate/documents/finance/q3-report.md"
vault_chunks: "/vault/corporate/chunks/finance/q3-report.chunks.jsonl"
---

<!-- chunk 1/37 | page 1 | 812 chars -->

## Executive Summary

Revenue for the third quarter was …
```

The JSONL sidecar has one object per chunk with the full metadata — page/slide number,
table HTML, source sha256, and the exact pipeline settings used:

```json
{"chunk_id":"finance/q3-report::0000","chunk_index":0,"chunk_count":37,
 "type":"CompositeElement","text":"Executive Summary\nRevenue for …","char_count":812,
 "text_as_html":null,
 "locator":{"page_number":1,"section":null,"url":null,"parent_id":"…"},
 "source":{"filename":"q3-report.pdf","relpath":"finance/q3-report.pdf",
           "sha256":"9f2c1ab5…","filetype":"application/pdf","size_bytes":1840233,
           "modified_at":"2026-08-25T17:02:11Z"},
 "pipeline":{"partition_strategy":"auto","chunking_strategy":"by_title",
             "max_characters":900,"overlap":120,"combine_under_n_chars":250},
 "produced_at":"2026-08-26T09:14:02Z"}
```

Confirm it by hand:

```bash
head -20 "$VAULT_HOST_PATH/corporate/documents/finance/q3-report.md"
wc -l "$VAULT_HOST_PATH/corporate/chunks/finance/q3-report.chunks.jsonl"   # == chunk_count
```

You can also call the API directly, without the worker, to inspect raw partitioning:

```bash
curl -s -F 'files=@sample.pdf' -F 'strategy=auto' -F 'chunking_strategy=by_title' \
     -F 'max_characters=900' http://localhost:8003/general/v0/general | head -c 2000
```

## 4. LLM and embedding providers

Both are wired declaratively in `anythingllm-stack/docker-compose.yml`, sourced from `.env`:

| Concern | Setting | Value |
|---|---|---|
| Generation | `LLM_PROVIDER` | `generic-openai` |
| | `GENERIC_OPEN_AI_BASE_PATH` | `${LLM_BASE_PATH}` → `http://host.docker.internal:8000/v1` |
| | `GENERIC_OPEN_AI_MODEL_PREF` | `${LLM_MODEL}` |
| | `GENERIC_OPEN_AI_API_KEY` | `${LLM_API_KEY}` — non-empty placeholder, not validated by the server |
| Embeddings | `EMBEDDING_ENGINE` | `native` — in-process, CPU, no second container, no GPU block |
| | `EMBEDDING_MODEL_PREF` | `Xenova/all-MiniLM-L6-v2` (~90 MB) |
| Vectors | `VECTOR_DB` | `lancedb`, embedded, persisted in `./data` |

Embedding calls never touch `LLM_BASE_PATH` — the engine is a separate in-process model, so
the GPU's committed VRAM is untouched and no document text leaves the host.

**Change these in `.env`, not in the UI.** Compose-injected environment variables take
precedence over the container's own `/app/server/.env`, so a provider change made in the UI
works until the next restart and then silently reverts. To change the model:

```bash
nano ~/anythingllm-stack/.env      # change LLM_MODEL
cd ~/anythingllm-stack && docker compose up -d
```

Verify from inside the container that the LLM is reachable:

```bash
docker exec anythingllm curl -s http://host.docker.internal:8000/v1/models
```

## 5. Create the workspace, ingest, and query

**Get a Developer API key.** Open `http://localhost:3001`, complete onboarding (choose the
already-configured provider when prompted), then
**Settings → Tools → Developer API → Generate New API Key**. This is the one value that
cannot be pre-seeded from the environment. Write it into `anythingllm-stack/.env` and
validate it in a single step:

```bash
uv run scripts/allm.py set-key <the-key-you-just-generated>
```

Then, from `~/anythingllm-stack`:

```bash
uv run scripts/allm.py auth                                    # key valid, endpoints reachable
uv run scripts/allm.py bootstrap                               # create the workspace
uv run scripts/allm.py ingest                                  # embed /vault/corporate/documents
uv run scripts/allm.py query "What is our travel expense policy?"
```

`uv run` resolves the script's inline dependencies into a throwaway environment each time —
nothing to install, no venv to manage.

`ingest` is incremental: it keeps a sha256 ledger (`scripts/.ingest-ledger.json`), re-embeds
only changed documents, and removes the superseded copies from the workspace. `--force`
re-embeds everything; `--prune` also deletes the superseded documents from storage.

Two ingestion modes:

- `--mode document` (default) — one AnythingLLM document per source file. AnythingLLM
  re-splits it with `TEXT_SPLITTER_CHUNK_SIZE`/`OVERLAP`, which are set to the same 900/120
  as the preprocessor, so the boundaries land in nearly the same places. Citations are
  per-source-file. Easiest to manage.
- `--mode chunks` — one AnythingLLM document per preprocessed chunk. Boundaries are exactly
  the ones Unstructured chose (headings never split mid-section) and citations carry the
  page number, at the cost of many more rows in the Documents view.

Expected `query` output — an answer plus a non-empty `sources` list:

```
========================================================================
Employees may claim economy airfare booked at least 14 days in advance …
========================================================================

sources: 3
  - travel-policy.pdf  score=0.7412
    docSource: /vault/corporate/documents/hr/travel-policy.md
    text: 4.2 Air travel. Employees may claim economy airfare…
```

An empty `sources` list means retrieval matched nothing — see Troubleshooting.

## 6. Vault scoping

Both stacks are scoped to `corporate/` structurally, by the mount, not by convention:

```yaml
# unstructured-stack — read-write, it produces the output
- ${VAULT_HOST_PATH}/corporate:/vault/corporate:rw
# anythingllm-stack — read-only, it only consumes
- ${VAULT_HOST_PATH}/corporate:/vault/corporate:ro
```

The vault root is never mounted, so `shared/`, `tickets/`, `learnings/` and `skills/` are
not present in either container's filesystem at all — `ls /vault` inside either container
lists exactly one entry, `corporate`. No amount of misconfiguration downstream can reach
them.

The ingestion job is scoped too, defensively: `allm.py ingest` resolves
`VAULT_CORPORATE_DIR`, refuses to run unless the resolved directory is literally named
`corporate`, rejects any path containing `shared`/`tickets`/`learnings`/`skills`, globs only
`corporate/documents/**/*.md`, and skips any file whose symlink resolves outside the
corporate tree. It never walks the vault root.

The preprocessor writes only `corporate/documents/` and `corporate/chunks/`. Its ledger and
manifest live in the stack's own `data/state/`, not in the vault, so the ingestion side never
has to filter stray bookkeeping files.

`scripts/verify.sh` section 5 asserts all of this against the running containers.

## 7. Which file types need preprocessing

AnythingLLM ships its own document collector, so some formats do not need the preprocessing
stack. Run the matrix test on the host to confirm against your own documents:

```bash
cd ~/anythingllm-stack
cp /path/to/samples/* fixtures/       # one small file per extension you care about
uv run scripts/allm.py matrix --fixtures ./fixtures
```

It queries the collector's declared accepted types, uploads each fixture, records how many
words were actually extracted, and writes `file-type-matrix.json` plus a Markdown table.
`DIRECT` = accepted with text extracted; `EMPTY` = accepted but nothing extracted (worse
than a rejection, because it silently yields an empty document).

Expected results, to be confirmed by that run:

| Input | Direct into AnythingLLM | Needs the preprocessing stack | Why |
|---|---|---|---|
| `.txt`, `.md` | yes | no | read as-is |
| `.csv`, `.tsv` | yes | only if you want row context per chunk | flattened to text |
| `.html`, `.htm` | yes | if boilerplate/nav must be stripped | collector keeps more chrome |
| `.docx` | yes | if you need section/heading-aware chunks | text extracted, structure flattened |
| `.pptx` | yes | if you need per-slide citations | no slide numbers in metadata |
| `.xlsx` | yes | for large/multi-sheet workbooks | sheet boundaries not preserved |
| `.pdf` with a text layer | yes | if you need page numbers or tables | text-only extraction, no layout |
| **`.pdf` scanned / image-only** | **no — yields an empty doc** | **yes** | no OCR in the collector; Unstructured `hi_res`/`ocr_only` does OCR |
| **`.doc`, `.ppt`, `.xls`** (legacy binary) | **no** | **yes** | not in the collector's accepted list |
| **`.rtf`, `.odt`, `.odp`, `.ods`** | partial | **yes** for reliability | inconsistent extraction |
| **`.eml`, `.msg`** | no | **yes** | Unstructured parses headers + body |
| **`.png`, `.jpg`, `.tiff`** | no | **yes** | OCR only exists in the preprocessing stack |
| audio/video | yes (local Whisper) | no | out of scope for this vault |

Practical rule: anything where **page/slide citations, tables, or OCR** matter goes through
the preprocessing stack, which is why the default pipeline routes everything through it.
Plain `.txt`/`.md` that is already clean can be dropped straight into
`corporate/documents/` and picked up by `allm.py ingest` — but it will then have no
frontmatter, so prefer the inbox for consistency.

## 8. Programmatic access for other internal services

From another container on `${SHARED_NETWORK_NAME}`, reach AnythingLLM by container name at
`http://anythingllm:3001`. From the host, `http://localhost:3001` (bound to `127.0.0.1` by
default; widen `ANYTHINGLLM_BIND_ADDRESS` only if you need off-host access).

**Workspace query — the primary hook:**

```bash
curl -X POST http://anythingllm:3001/api/v1/workspace/corporate-knowledge-base/chat \
  -H "Authorization: Bearer $ANYTHINGLLM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"message":"What is our travel expense policy?","mode":"query"}'
```

Response shape:

```json
{ "id": "…", "type": "textResponse",
  "textResponse": "Employees may claim economy airfare …",
  "sources": [ { "title": "travel-policy.pdf",
                 "docSource": "/vault/corporate/documents/hr/travel-policy.md",
                 "text": "4.2 Air travel …", "score": 0.7412 } ],
  "close": true, "error": null }
```

`mode` is `query` (answer only from retrieved context, refuse otherwise) or `chat` (fall
back to the model's own knowledge). Use `query` for a knowledge base. Add
`"sessionId": "<caller-id>"` to keep per-caller history. `…/stream-chat` is the SSE variant.

Other useful endpoints, all `Authorization: Bearer <key>`:

| Endpoint | Purpose |
|---|---|
| `GET /api/ping` | unauthenticated liveness |
| `GET /api/v1/auth` | validate an API key |
| `GET /api/v1/workspaces` | list workspaces and slugs |
| `POST /api/v1/document/raw-text` | push text in without touching the filesystem |
| `POST /api/v1/workspace/<slug>/update-embeddings` | attach/detach documents |
| `GET /api/v1/openai/models` | OpenAI-compatible model list — each workspace appears as a model |
| `POST /api/v1/openai/chat/completions` | **OpenAI-compatible RAG**: `"model": "<workspace-slug>"` |

The last one is the easiest integration path: any existing OpenAI client can point its base
URL at `http://anythingllm:3001/api/v1/openai` and pass the workspace slug as the model
name, getting retrieval-augmented answers with no AnythingLLM-specific code. Confirm it is
present on your image version with `GET /api/v1/openai/models`.

Full interactive API reference on the running instance: `http://localhost:3001/api/docs`.

**MCP.** AnythingLLM is an MCP *client*, not an MCP server: it consumes external MCP
servers via `data/plugins/anythingllm_mcp_servers.json` (Settings → Agent Skills → MCP
Servers) so workspace agents can call your tools. It does not expose a per-workspace MCP
endpoint — a request to `http://anythingllm:3001/api/workspace/<slug>/mcp` will 404. If a
future version adds one, that is the path it would live at, and it would take the same
`Authorization: Bearer <ANYTHINGLLM_API_KEY>` header as the endpoints above. Until then, the
OpenAI-compatible endpoint is the equivalent integration surface, and wrapping it in a
thin MCP server is ~30 lines on the caller's side.

## 9. Verification checklist

Automated — run from wherever you copied `scripts/` (it finds the stacks in its parent
directory, or set `STACK_ROOT` explicitly):

```bash
~/scripts/verify.sh "What is our travel expense policy?"
```

It exits non-zero if any check fails. What it asserts:

| # | Check | Expected |
|---|---|---|
| 0 | Both `.env` files exist, secrets non-empty, `LLM_MODEL` set, `VAULT_HOST_PATH` matches across stacks | pass |
| 1 | `docker network inspect $SHARED_NETWORK_NAME` | exists |
| 2 | Container states | `unstructured-api` running/healthy, `unstructured-preprocessor` running, `anythingllm` running/healthy |
| 3 | `http://localhost:8003/healthcheck` | `HEALTHCHECK STATUS: EVERYTHING OK` |
| | `http://localhost:3001/api/ping` | `{"online":true}` |
| 4 | `/v1/models` reachable from *inside* the anythingllm container | HTTP 200 |
| 5 | Vault scoping: no `/vault` root mount, no non-corporate subfolder, `ls /vault` = `corporate` only, anythingllm mount is `ro` | pass |
| 6 | No literal credentials in compose; network is `external: true` + `${SHARED_NETWORK_NAME}`; vault mount is `${VAULT_HOST_PATH}/corporate` | pass |
| 7 | `corporate/documents/*.md` count > 0, one `.chunks.jsonl` per document, frontmatter + chunk markers present | pass |
| 8 | Developer API key accepted; workspace chat returns `textResponse` **and a non-empty `sources[]`** | pass |

Check 8 is the one that matters — a `textResponse` with `sources: []` means the stack is up
but retrieval is not working.

Manual spot-checks worth doing once:

```bash
# chunk count in the sidecar matches the frontmatter
grep '^chunk_count:' "$VAULT_HOST_PATH/corporate/documents/finance/q3-report.md"
wc -l "$VAULT_HOST_PATH/corporate/chunks/finance/q3-report.chunks.jsonl"

# no chunk exceeds the embedder's window
python3 -c 'import json,sys; m=max(json.loads(l)["char_count"] for l in open(sys.argv[1])); print("max chars:", m)' \
  "$VAULT_HOST_PATH/corporate/chunks/finance/q3-report.chunks.jsonl"

# embeddings did not go to the GPU endpoint: no /embeddings calls in the LLM container's log
docker logs <your-llm-container> --since 10m | grep -c '/v1/embeddings'    # expect 0
```

## 10. Assumptions

1. **A worker is part of the preprocessing stack.** The Unstructured API is stateless
   request/response and cannot write to the vault. Since the brief makes
   `/vault/corporate` the handoff point rather than a container-to-container call, the
   stack includes a small watcher that owns the "raw file in → chunked output in the vault"
   job. Raw files arrive in `unstructured-stack/data/inbox/`; that is the stack's own
   persistent volume, not part of the vault.
2. **Chunk size 900 characters, 120 overlap, `by_title`.** AnythingLLM's CPU embedder
   (`all-MiniLM-L6-v2`) truncates at 256 wordpiece tokens ≈ 1000 English characters. 900
   keeps every chunk inside that window with margin, so nothing is silently dropped at
   embed time — the failure mode you would otherwise never notice. `by_title` splits at
   section boundaries rather than blindly at N characters, so a heading and its body stay
   together; `combine_under_n_chars=250` merges runt sections. 120 characters (~13%)
   overlap preserves sentences that straddle a boundary. `TEXT_SPLITTER_CHUNK_SIZE` in the
   AnythingLLM stack mirrors these so the two do not fight.
   *If you switch to a 512-token embedder (`Xenova/bge-small-en-v1.5`), raise
   `CHUNK_MAX_CHARACTERS` to ~1800, `CHUNK_OVERLAP` to ~200, and mirror both — then re-embed
   everything, since vectors from two different models are not comparable.*
3. **Markdown + JSONL, not one or the other.** Markdown is the ingestion unit and stays
   human-readable and diffable; the JSONL sidecar carries what Markdown cannot (page/slide
   numbers, table HTML, source sha256, pipeline settings) for any other consumer of the
   vault. Frontmatter keys are stable enough to parse.
4. **`PARTITION_STRATEGY=auto` and `INFER_TABLE_STRUCTURE=false` by default.** `auto` uses
   the cheap path for PDFs with a real text layer and escalates to OCR only when needed.
   Table structure inference costs roughly 2–4× CPU per page; turn it on per-deployment for
   table-heavy financial or legal corpora.
5. **Workspace naming: one workspace, `corporate-knowledge-base`**, from
   `WORKSPACE_NAME`/`WORKSPACE_SLUG`. One workspace per vault subfolder is the scheme; since
   only `corporate/` is in scope, there is one. Retrieval defaults set by `bootstrap`:
   `topN=6`, `similarityThreshold=0.25`, `temperature=0.1`, `chatMode=query` — low
   temperature and query mode because a knowledge base should quote, not improvise.
6. **Ports.** The existing LLM owns host `8000`, so the Unstructured API publishes on
   `8003` (container port stays `8000`). Both stacks bind published ports to `127.0.0.1`;
   inter-container traffic uses the shared network by container name.
7. **`latest` image tags, pinned after first pull.** `UNSTRUCTURED_API_IMAGE_TAG` and
   `ANYTHINGLLM_IMAGE_TAG` are `.env` variables. Pin them before you rely on the stack:
   `docker image inspect mintplexlabs/anythingllm:latest -f '{{index .RepoDigests 0}}'`,
   then set the tag or digest in `.env`.
8. **uid 1000 owns the persistent directories and `corporate/`.** Overridable via
   `PREPROCESSOR_UID`/`PREPROCESSOR_GID`; AnythingLLM's image is fixed at uid 1000.
9. **Model weights download on first run** (Unstructured ONNX layout models, AnythingLLM's
   MiniLM). Those are the only egress; no document content leaves the host, and telemetry is
   off. On an air-gapped host, pre-seed `anythingllm-stack/data/models/` and bake the ONNX
   models into a derived Unstructured image.
10. **AnythingLLM has no MCP server endpoint** in current versions (§8). The
    OpenAI-compatible endpoint is documented as the integration surface instead.
11. **`cap_add: SYS_ADMIN`** on the AnythingLLM container follows the upstream reference
    compose file; it is needed only by the collector's headless Chromium for link scraping.
    Remove it if you only ever ingest from the vault.

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `anythingllm` restart-loops on first boot | `data/.env` was created as a *directory*. `docker compose down && sudo rm -rf data/.env && touch data/.env && sudo chown 1000:1000 data/.env && docker compose up -d` |
| `EACCES` / `permission denied` in anythingllm logs | `sudo chown -R 1000:1000 ~/anythingllm-stack/data` |
| Preprocessor logs `permission denied` writing the vault | `PREPROCESSOR_UID`/`GID` do not own `$VAULT_HOST_PATH/corporate` |
| `network ai-stack-net declared as external, but could not be found` | `docker network create <name>`, or fix `SHARED_NETWORK_NAME` |
| `port is already allocated` on 8000 | `UNSTRUCTURED_HOST_PORT` was set to 8000; the LLM owns it. Use 8003 |
| PDF lands in `data/failed/` with "produced no text" | Scanned PDF. Set `PARTITION_STRATEGY=hi_res` (or `ocr_only`), `docker compose up -d`, move the file back into `data/inbox/` |
| Chat answers but `sources: []` | Ingest never ran (`uv run scripts/allm.py ingest`), the wrong workspace slug, or `similarityThreshold` too high — try `--similarity-threshold 0.1` in `bootstrap` |
| Chat returns a provider/connection error | `LLM_MODEL` does not match a model id from `/v1/models`, or `host.docker.internal` is unreachable — test with `docker exec anythingllm curl -s $LLM_BASE_PATH/models` |
| Provider changes made in the UI revert after restart | Expected — compose env wins. Change `.env` and `docker compose up -d` (§4) |
| Ingest re-uploads unchanged files | The ledger moved. It lives at `anythingllm-stack/scripts/.ingest-ledger.json`; point `--state-file` at it |
| Embedding is slow | It is CPU-bound and intentionally so. Raise `ANYTHINGLLM_CPU_LIMIT`. Do not point embeddings at the GPU endpoint |
