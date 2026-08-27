# Document Preprocessing + Knowledge-Base Agent

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
  scripts/allm.py              auth | set-key | bootstrap | ingest | query | docs | matrix
  fixtures/                    sample files for the file-type matrix test
scripts/setup.sh               one-shot setup: network, .env files, secrets, folders
scripts/verify.sh              the verification checklist, automated
```

---

## 1. One-time setup

Everything runs from inside this repo. There are no folders to copy anywhere. Start here and
stay here — every path below is relative to it:

```bash
cd ~/RAG_project
```

**Prerequisites.** Three commands must work: `docker ps` (it should print a table, even an
empty one), `uv --version`, and `openssl version`. Python tooling is `uv` only — no `pip`
install step, no virtualenv to create. If `uv` is missing:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Step 1 — Run the setup script

One command does every fiddly part: creates the shared Docker network, creates the vault
folder, copies both `.env.example` files to `.env`, fills in the vault paths and the uid/gid
the preprocessor runs as, clamps the CPU/memory limits to what this host actually has,
generates the three secrets, creates the runtime folders, and detects your local LLM's model
id if one is running.

```bash
bash scripts/setup.sh
```

It is safe to re-run any time: it never overwrites an existing `.env` and never regenerates a
secret that is already set. Use `--vault /srv/vault` to put the vault somewhere other than
`~/vault`, and `--network <name>` to use an existing network by a different name.

**Check:** the last line says `Done.` with 0 warnings — or with one warning about port 8000,
which is fine if there is no LLM on this machine (see the end of this section).

<details>
<summary>What it wrote, and how to do it by hand instead</summary>

```bash
grep -E '^(SHARED_NETWORK_NAME|VAULT_HOST_PATH|PREPROCESSOR_UID|PREPROCESSOR_GID)=' unstructured-stack/.env
grep -E '^(SHARED_NETWORK_NAME|VAULT_HOST_PATH|VAULT_CORPORATE_DIR|JWT_SECRET|SIG_KEY|SIG_SALT|LLM_MODEL)=' anythingllm-stack/.env
```

Every one of those needs a real value after the `=`; nothing should still read `CHANGE-ME`.
The equivalent by hand is:

```bash
docker network create ai-stack-net
mkdir -p ~/vault/corporate
cp unstructured-stack/.env.example unstructured-stack/.env && nano unstructured-stack/.env
cp anythingllm-stack/.env.example  anythingllm-stack/.env  && nano anythingllm-stack/.env
for v in JWT_SECRET SIG_KEY SIG_SALT; do echo "$v=$(openssl rand -hex 32)"; done
mkdir -p unstructured-stack/data/{inbox,archive,failed,state} anythingllm-stack/data
touch anythingllm-stack/data/.env      # must exist as a FILE before the first `up`
```

The only values that must change from the committed defaults:

| Variable | In | Set to |
|---|---|---|
| `SHARED_NETWORK_NAME` | both | your existing external network |
| `VAULT_HOST_PATH` | both | host path of the vault root (only `corporate/` gets mounted) |
| `JWT_SECRET`, `SIG_KEY`, `SIG_SALT` | anythingllm | the generated hex values |
| `LLM_MODEL` | anythingllm | your served model id — `curl -s http://localhost:8000/v1/models` |
| `VAULT_CORPORATE_DIR` | anythingllm | `$VAULT_HOST_PATH/corporate` (used by `allm.py`, which runs on the host) |
| `*_CPU_LIMIT`, `*_MEM_LIMIT` | both | must not exceed the host's cores/RAM — Docker hard-errors otherwise |

`unstructured-stack/.env` contains no secrets at all: the self-hosted Unstructured API is
unauthenticated by design and reachable only from the shared Docker network plus a
loopback-bound host port.

If uid 1000 is not the right owner for your vault, set `PREPROCESSOR_UID`/`PREPROCESSOR_GID`
in `unstructured-stack/.env` to whoever owns `corporate/`. `setup.sh` sets them to you.

</details>

### Step 2 — Start the preprocessing stack

```bash
cd unstructured-stack && docker compose up -d --build && cd ..
```

First run pulls a large image and builds the worker; expect several minutes. It also
downloads the Unstructured layout/OCR ONNX models (~500 MB) the first time a PDF needs
`hi_res`.

**Check:** after a minute,

```bash
docker compose -f unstructured-stack/docker-compose.yml ps
```

You want `unstructured-api` **healthy** and `unstructured-preprocessor` **running** (it polls
the inbox and has no health endpoint of its own). If the preprocessor keeps restarting,
`docker logs --tail 50 unstructured-preprocessor` says why.

### Step 3 — Push one document through it

Drop any PDF, Word file, PowerPoint or spreadsheet into the inbox. The worker picks it up
within 20 seconds on its own — you do not run anything.

```bash
cp /path/to/your/document.pdf unstructured-stack/data/inbox/
docker logs -f unstructured-preprocessor
```

`Ctrl+C` stops watching the log, not the worker.

**Check:** one Markdown file and one chunks file appeared, both named after your document:

```bash
ls ~/vault/corporate/documents ~/vault/corporate/chunks
head -30 ~/vault/corporate/documents/*.md
```

You should see a block between `---` lines at the top — where the file came from, how many
chunks, its checksum — then the text with `<!-- chunk 1/N -->` markers between sections.
§2 documents the format in full. If your file landed in `unstructured-stack/data/failed/`
instead, there is a `.error.txt` next to it explaining why.

### Step 4 — Start AnythingLLM

```bash
cd anythingllm-stack && docker compose up -d && cd ..
```

**Check:** after a minute or two, `curl -s http://localhost:3001/api/ping` returns
`{"online":true}`. If the container exited immediately, `docker logs anythingllm` names the
reason. Expected steady state is **healthy**.

### Step 5 — Get a Developer API key from the UI

The only step that needs a browser. Open **http://localhost:3001** — on a cloud dev machine,
use your IDE's port forwarding for port 3001.

Click past the setup wizard's provider questions. The compose file already set all of that,
and anything chosen in the wizard is overwritten on the next restart anyway (§3 explains
why). Then: **Settings** (gear, bottom-left) → **Tools** → **Developer API** → **Generate New
API Key**. Copy it and paste it in place of `PASTE-YOUR-KEY-HERE`:

```bash
cd anythingllm-stack && uv run scripts/allm.py set-key PASTE-YOUR-KEY-HERE && cd ..
```

That writes the key into `anythingllm-stack/.env` and immediately validates it, so a bad
paste surfaces here rather than three steps later. This is the one value that cannot be
pre-seeded from the environment. No restart needed — only the helper scripts use it.

### Step 6 — Create the workspace and load the documents

```bash
cd anythingllm-stack
uv run scripts/allm.py bootstrap
uv run scripts/allm.py ingest
cd ..
```

**Check:** `ingest` lists each file it uploaded and embedded. Run it a second time and it
should skip everything as unchanged — that is the ledger doing its job. §4 covers the
ingestion modes and what `bootstrap` sets.

### Step 7 — Ask it a question

```bash
cd anythingllm-stack && uv run scripts/allm.py query "What is in this knowledge base?" && cd ..
```

**Check:** you get an answer *and* a list of sources under it.

- Answer but no sources → retrieval matched nothing. Re-run `ingest`.
- Sources but a connection error instead of an answer → the LLM on port 8000 is not
  reachable. Retrieval is working; you just have no chat model on this machine.

### Step 8 — Run the full check

```bash
bash scripts/verify.sh
```

Note `bash`, not `source` — sourcing it would dump its shell settings into your terminal. It
prints PASS or FAIL for every requirement in §8's checklist, with a count at the end, and
exits non-zero if anything failed.

### If there is no LLM on this machine

The chat model is not part of either stack — it is an existing container on the target
server, on port 8000. Check with `curl -s http://localhost:8000/v1/models`. If nothing
answers, `setup.sh` has already put a placeholder in `LLM_MODEL` and warned you. Everything
works except the final written answer:

| Works | Does not work |
| --- | --- |
| Preprocessing (Steps 2–3) | Step 7's written answer |
| Upload, chunking, embedding (Step 6) | `verify.sh` section 4 |
| Vector search — the `sources` list in Step 7 | `verify.sh` section 8's chat check |

So exactly two FAILs in `verify.sh` are expected on a host without an LLM, and are not a
setup problem. On the real host, re-run `bash scripts/setup.sh` — it finds the LLM and fills
in the model id automatically.

Model weights (Unstructured's ONNX models, AnythingLLM's MiniLM embedder) are the only
outbound calls either stack makes, and they carry no document content.

## 2. Preprocessing and the output format

The worker polls every `WATCH_INTERVAL_SECONDS` (default 20), ignores files modified in the
last 5 seconds so half-copied uploads are not read, and skips anything it has already
processed (sha256 ledger in `data/state/ledger.json`). Subdirectories of the inbox are
preserved in the output. Successful originals move to `data/archive/`; failures move to
`data/failed/` with a `.error.txt` next to them. To process a backlog once and exit instead
of running as a watcher, set `ONE_SHOT=true`.

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

The JSONL sidecar has one object per chunk with the full metadata — page/slide number, table
HTML, source sha256, and the exact pipeline settings used:

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

Confirm the two agree — the sidecar's line count equals the frontmatter's `chunk_count`:

```bash
grep '^chunk_count:' ~/vault/corporate/documents/finance/q3-report.md
wc -l ~/vault/corporate/chunks/finance/q3-report.chunks.jsonl
```

You can also call the API directly, without the worker, to inspect raw partitioning:

```bash
curl -s -F 'files=@sample.pdf' -F 'strategy=auto' -F 'chunking_strategy=by_title' \
     -F 'max_characters=900' http://localhost:8003/general/v0/general | head -c 2000
```

## 3. LLM and embedding providers

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
nano anythingllm-stack/.env      # change LLM_MODEL
cd anythingllm-stack && docker compose up -d && cd ..
```

Verify from inside the container that the LLM is reachable:

```bash
docker exec anythingllm curl -s http://host.docker.internal:8000/v1/models
```

## 4. Workspace, ingestion and querying

The four helper commands, all run from `anythingllm-stack/`:

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

An empty `sources` list means retrieval matched nothing — see §10.

## 5. Vault scoping

Both stacks are scoped to `corporate/` structurally, by the mount, not by convention:

```yaml
# unstructured-stack — read-write, it produces the output
- ${VAULT_HOST_PATH}/corporate:/vault/corporate:rw
# anythingllm-stack — read-only, it only consumes
- ${VAULT_HOST_PATH}/corporate:/vault/corporate:ro
```

The vault root is never mounted, so `shared/`, `tickets/`, `learnings/` and `skills/` are not
present in either container's filesystem at all — `ls /vault` inside either container lists
exactly one entry, `corporate`. No amount of misconfiguration downstream can reach them.

The ingestion job is scoped too, defensively: `allm.py ingest` resolves
`VAULT_CORPORATE_DIR`, refuses to run unless the resolved directory is literally named
`corporate`, rejects any path containing `shared`/`tickets`/`learnings`/`skills`, globs only
`corporate/documents/**/*.md`, and skips any file whose symlink resolves outside the
corporate tree. It never walks the vault root.

The preprocessor writes only `corporate/documents/` and `corporate/chunks/`. Its ledger and
manifest live in the stack's own `data/state/`, not in the vault, so the ingestion side never
has to filter stray bookkeeping files.

`scripts/verify.sh` section 5 asserts all of this against the running containers.

## 6. Which file types need preprocessing

AnythingLLM ships its own document collector, so some formats do not need the preprocessing
stack. Run the matrix test on the host to confirm against your own documents:

```bash
cd anythingllm-stack
cp /path/to/samples/* fixtures/       # one small file per extension you care about
uv run scripts/allm.py matrix --fixtures ./fixtures
```

It queries the collector's declared accepted types, uploads each fixture, records how many
words were actually extracted, and writes `file-type-matrix.json` plus a Markdown table.
`DIRECT` = accepted with text extracted; `EMPTY` = accepted but nothing extracted (worse than
a rejection, because it silently yields an empty document).

Expected results, to be replaced by that run's output:

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
Plain `.txt`/`.md` that is already clean can be dropped straight into `corporate/documents/`
and picked up by `allm.py ingest` — but it will then have no frontmatter, so prefer the inbox
for consistency.

## 7. Programmatic access for other internal services

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

`mode` is `query` (answer only from retrieved context, refuse otherwise) or `chat` (fall back
to the model's own knowledge). Use `query` for a knowledge base. Add
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
URL at `http://anythingllm:3001/api/v1/openai` and pass the workspace slug as the model name,
getting retrieval-augmented answers with no AnythingLLM-specific code. Confirm it is present
on your image version with `GET /api/v1/openai/models`.

Full interactive API reference on the running instance: `http://localhost:3001/api/docs`.

**MCP.** AnythingLLM is an MCP *client*, not an MCP server: it consumes external MCP servers
via `data/plugins/anythingllm_mcp_servers.json` (Settings → Agent Skills → MCP Servers) so
workspace agents can call your tools. It does not expose a per-workspace MCP endpoint — a
request to `http://anythingllm:3001/api/workspace/<slug>/mcp` will 404. If a future version
adds one, that is the path it would live at, and it would take the same
`Authorization: Bearer <ANYTHINGLLM_API_KEY>` header as the endpoints above. Until then, the
OpenAI-compatible endpoint is the equivalent integration surface, and wrapping it in a thin
MCP server is ~30 lines on the caller's side.

## 8. Verification checklist

Automated, from the repo root (it locates both stacks relative to itself, or set `STACK_ROOT`
explicitly):

```bash
bash scripts/verify.sh "What is our travel expense policy?"
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
# no chunk exceeds the embedder's window
python3 -c 'import json,sys; m=max(json.loads(l)["char_count"] for l in open(sys.argv[1])); print("max chars:", m)' \
  ~/vault/corporate/chunks/finance/q3-report.chunks.jsonl

# embeddings did not go to the GPU endpoint: no /embeddings calls in the LLM container's log
docker logs <your-llm-container> --since 10m | grep -c '/v1/embeddings'    # expect 0
```

## 9. Assumptions

1. **A worker is part of the preprocessing stack.** The Unstructured API is stateless
   request/response and cannot write to the vault. Since the brief makes `/vault/corporate`
   the handoff point rather than a container-to-container call, the stack includes a small
   watcher that owns the "raw file in → chunked output in the vault" job. Raw files arrive in
   `unstructured-stack/data/inbox/`; that is the stack's own persistent volume, not part of
   the vault.
2. **Chunk size 900 characters, 120 overlap, `by_title`.** AnythingLLM's CPU embedder
   (`all-MiniLM-L6-v2`) truncates at 256 wordpiece tokens ≈ 1000 English characters. 900
   keeps every chunk inside that window with margin, so nothing is silently dropped at embed
   time — the failure mode you would otherwise never notice. `by_title` splits at section
   boundaries rather than blindly at N characters, so a heading and its body stay together;
   `combine_under_n_chars=250` merges runt sections. 120 characters (~13%) overlap preserves
   sentences that straddle a boundary. `TEXT_SPLITTER_CHUNK_SIZE` in the AnythingLLM stack
   mirrors these so the two do not fight.
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
6. **Ports.** The existing LLM owns host `8000`, so the Unstructured API publishes on `8003`
   (container port stays `8000`). Both stacks bind published ports to `127.0.0.1`;
   inter-container traffic uses the shared network by container name.
7. **Resource limits are sized for the target workstation and clamped down elsewhere.** The
   committed `*_CPU_LIMIT`/`*_MEM_LIMIT` defaults assume a machine bigger than a typical dev
   box. Docker hard-errors when a `cpus` limit exceeds the host's core count, so
   `scripts/setup.sh` lowers them to fit the current host — never raises them, because the
   modest numbers exist to leave the GPU LLM container room to breathe.
8. **`latest` image tags, pinned after first pull.** `UNSTRUCTURED_API_IMAGE_TAG` and
   `ANYTHINGLLM_IMAGE_TAG` are `.env` variables. Pin them before you rely on the stack:
   `docker image inspect mintplexlabs/anythingllm:latest -f '{{index .RepoDigests 0}}'`, then
   set the tag or digest in `.env`.
9. **uid 1000 owns the persistent directories and `corporate/`.** Overridable via
   `PREPROCESSOR_UID`/`PREPROCESSOR_GID` (`setup.sh` sets them to the invoking user);
   AnythingLLM's image is fixed at uid 1000.
10. **Model weights download on first run** (Unstructured ONNX layout models, AnythingLLM's
    MiniLM). Those are the only egress; no document content leaves the host, and telemetry is
    off. On an air-gapped host, pre-seed `anythingllm-stack/data/models/` and bake the ONNX
    models into a derived Unstructured image.
11. **AnythingLLM has no MCP server endpoint** in current versions (§7). The
    OpenAI-compatible endpoint is documented as the integration surface instead.
12. **`cap_add: SYS_ADMIN`** on the AnythingLLM container follows the upstream reference
    compose file; it is needed only by the collector's headless Chromium for link scraping.
    Remove it if you only ever ingest from the vault.

## 10. Troubleshooting

Quick reference first:

| I want to… | Command |
| --- | --- |
| See what's running | `docker ps` |
| Read the worker's log | `docker logs -f unstructured-preprocessor` |
| Read AnythingLLM's log | `docker logs -f anythingllm` |
| Process a new document | Copy it into `unstructured-stack/data/inbox/` |
| Load new documents into the KB | `cd anythingllm-stack && uv run scripts/allm.py ingest` |
| Retry a failed document | Fix the cause, move it from `data/failed/` back into `data/inbox/` |
| Stop everything | `docker compose down` in each stack folder |
| Start over completely | `docker compose down -v` in each, then delete both `data/` folders and `~/vault` |

Setup mistakes:

| Symptom | Cause / fix |
| --- | --- |
| `cd: no such file or directory: ~/anythingllm-stack` | The stacks live in the repo: `cd ~/RAG_project` first, then `cd anythingllm-stack` |
| `command not found: cp.env.example.env` | `cp` `.env.example` `.env` are three words — you need both spaces. Or just run `scripts/setup.sh` |
| `command not found: .env` | `$EDITOR` is not set. Use `nano .env`, or let `setup.sh` do the editing |
| `sed: -e expression #1: unknown option to 's'` | Mismatched delimiters — `s\|…\|…\|` needs the same character all three times. Again: use `setup.sh` |
| `range of CPUs is from 0.01 to 4.00, as there are only 4 CPUs available` | A `cpus` limit is higher than this host's core count. Re-run `setup.sh` — it clamps them |
| `network ai-stack-net declared as external, but could not be found` | `docker network create <name>`, or fix `SHARED_NETWORK_NAME`. `setup.sh` does it if Docker is reachable |
| `set JWT_SECRET in .env` | Secrets missing. Re-run `setup.sh` |
| `uv run` fails with `failed to symlink … /uv/venvs/…: No such file or directory` | uv's cache sits inside a system Python prefix that has no writable `venvs/` — common on hosted dev studios. `export UV_CACHE_DIR="$HOME/.cache/uv"`, and add that line to `~/.bashrc` or `~/.zshrc`. `setup.sh` warns when it detects this |

Runtime problems:

| Symptom | Cause / fix |
| --- | --- |
| `anythingllm` restart-loops on first boot | `data/.env` was created as a *directory*. `docker compose down && sudo rm -rf data/.env && touch data/.env && sudo chown 1000:1000 data/.env && docker compose up -d` |
| `EACCES` / `permission denied` in anythingllm logs | `sudo chown -R 1000:1000 anythingllm-stack/data` |
| Preprocessor logs `permission denied` writing the vault | `PREPROCESSOR_UID`/`GID` do not own `$VAULT_HOST_PATH/corporate`. Re-run `setup.sh`, then `docker compose up -d` |
| `port is already allocated` on 8000 | `UNSTRUCTURED_HOST_PORT` was set to 8000; the LLM owns it. Use 8003 |
| PDF lands in `data/failed/` with "produced no text" | Scanned PDF. Set `PARTITION_STRATEGY=hi_res` (or `ocr_only`), `docker compose up -d`, move the file back into `data/inbox/` |
| Chat answers but `sources: []` | Ingest never ran (`uv run scripts/allm.py ingest`), the wrong workspace slug, or `similarityThreshold` too high — try `--similarity-threshold 0.1` in `bootstrap` |
| Chat returns a provider/connection error | `LLM_MODEL` does not match a model id from `/v1/models`, or `host.docker.internal` is unreachable — test with `docker exec anythingllm curl -s $LLM_BASE_PATH/models`. If there is no LLM on this host at all, see the end of §1 |
| Provider changes made in the UI revert after restart | Expected — compose env wins. Change `.env` and `docker compose up -d` (§3) |
| Ingest re-uploads unchanged files | The ledger moved. It lives at `anythingllm-stack/scripts/.ingest-ledger.json`; point `--state-file` at it |
| Embedding is slow | It is CPU-bound and intentionally so. Raise `ANYTHINGLLM_CPU_LIMIT`. Do not point embeddings at the GPU endpoint |
