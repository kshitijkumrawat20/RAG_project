# Technical Assessment: Document Preprocessing + Knowledge-Base Agent Deployment

## Context

We run a self-hosted AI stack on a single Docker host. Two pieces of that
stack are already built. Your job is to build the two pieces that plug
into it:

1. **Unstructured** — an open-source document preprocessing service that
   ingests raw corporate files (PDFs, Office docs, HTML, etc.) and converts
   them into clean, structured, chunked output suitable for downstream RAG
   ingestion.
2. **AnythingLLM**, configured as a knowledge-base agent that ingests the
   preprocessed output into a workspace and exposes it for programmatic
   querying by other internal services.

Both must be delivered as Docker Compose stacks that can be dropped into our
existing host and brought up with `docker compose up -d`, following the
conventions below exactly.

## Target Environment (given — do not need to verify)

| Item | Value |
|---|---|
| Host OS | Windows 11 Pro, WSL2 + Docker Desktop |
| Guest OS | Ubuntu 26.04 LTS |
| GPU | 1× RTX 5090, 32GB VRAM |
| System RAM | 64GB |
| Existing local LLM | Already running as a separate container, OpenAI-compatible API at `http://host.docker.internal:8000/v1` (works from any container without extra network config) |
| GPU headroom | **Assume ~0 spare VRAM.** The existing LLM container is served with a high GPU-memory-utilization target, leaving very little slack for anything else. This only affects *embeddings*, not generation — see below. |

## Required Conventions

**Folder layout** — create these under the user's home directory, matching
the pattern of our other stacks:

```
~/unstructured-stack/
  docker-compose.yml
  .env.example
  data/            # persistent volume mount target
~/anythingllm-stack/
  docker-compose.yml
  .env.example
  data/            # persistent volume mount target (workspaces, vector store)
```

**Shared Docker network** — both stacks must attach to a pre-existing
external Docker network so they can be reached by container name from other
services on the host:

- Declare it as `external: true` in `docker-compose.yml`
- Parameterize the name via a `.env` variable, e.g. `SHARED_NETWORK_NAME`
- Do not hardcode any network name directly in the compose file

**Shared knowledge vault** — a shared folder tree already exists on the host
and is used by other internal services (out of scope, don't worry about
what writes to it). It's mounted into containers as a bind mount, structured
like this:

```
/vault/
  corporate/       # <- your two stacks' concern (see below)
  shared/          # used by other systems — not your concern
  tickets/         # used by other systems — not your concern
  learnings/       # used by other systems — not your concern
  skills/          # used by other systems — not your concern
```

Your stacks only ever touch `corporate/`:

- The preprocessing stack **writes** its cleaned output there.
- AnythingLLM **reads** from there for ingestion.
- Neither stack should read, write, or ingest anything from the other
  subfolders — don't mount more of the tree than you need, and don't build
  an ingestion job that walks the whole `/vault` root.

Mount convention:
- Bind-mount the vault into both containers at `/vault`
- The host path is not something you need to know for this test —
  parameterize it via a `.env` variable, e.g. `VAULT_HOST_PATH`, the same
  way you're parameterizing the network name
- Preprocessing container: `/vault/corporate` read-write
- AnythingLLM container: `/vault/corporate` read-only is fine, since it only
  needs to read files for ingestion

**Secrets** — no credentials, API keys, or passwords in `docker-compose.yml`.
Everything sensitive goes in `.env`, with a `.env.example` template checked
in instead. Generate any needed secrets with `openssl rand -hex 32` and
document that in your README.

**LLM connectivity** — AnythingLLM's chat/completion provider should point
at our existing local LLM (`http://host.docker.internal:8000/v1`, OpenAI-
compatible, treat the API key as a non-empty placeholder string — the server
doesn't validate it). Make the model name a `.env` variable rather than
hardcoding it, since it may change.

**Generation vs. embeddings — do not conflate these:**

- **Generation** (answering questions using retrieved context): reuse the
  existing local LLM at `http://host.docker.internal:8000/v1`. Do not deploy
  a second generative model.
- **Embeddings** (and reranking, if used): must run as a separate, small,
  CPU-based model — do **not** route embedding calls through the endpoint
  above. Two reasons: (1) the existing model's GPU memory is already
  tightly allocated and has no spare headroom for additional load, and
  (2) embedding models are orders of magnitude smaller than a chat model
  by design, so routing through a large generative model would be slow and
  gains nothing — embedding quality doesn't scale with model size the way
  chat quality does. AnythingLLM's built-in local embedder (e.g.
  BGE-small/MiniLM-class) is the right fit and runs fine on CPU. Do not add
  a `deploy.resources.reservations.devices` GPU block for the embedder
  unless you flag it clearly as optional/commented-out.
- No document content should leave the host — no external embeddings API
  calls of any kind.

**Preprocessing scope** — Unstructured should run as a local, self-hosted
service (not their commercial cloud platform). At minimum it must handle
PDF, DOCX, PPTX, and HTML input and produce clean Markdown or structured
JSON, chunked, with metadata preserved (source filename, page/section
reference). Output should land in `/vault/corporate` (see the vault
convention above) so AnythingLLM's stack can subsequently ingest it — treat
this as the handoff point between the two stacks rather than a
container-to-container API call.

**Future integration hook** — a separate internal service will later query
AnythingLLM programmatically. Expose (or document how to reach) the
workspace chat/query endpoint over HTTP, and note where an MCP-style
endpoint would be exposed if AnythingLLM supports it (e.g.
`http://anythingllm:3001/api/workspace/<slug>/mcp` with `Authorization:
Bearer <API key from .env>`). You do not need to build the client side of
this — just make sure the server side is reachable and documented.

## Deliverables

1. `unstructured-stack/` — `docker-compose.yml` + `.env.example` for the
   preprocessing service.
2. `anythingllm-stack/` — `docker-compose.yml` + `.env.example` for
   AnythingLLM, pre-wired to the local LLM endpoint above.
3. A short `README.md` covering:
   - One-time setup steps
   - How to run a document through the preprocessing stack and confirm the
     output format
   - How to point AnythingLLM's embedding/LLM provider at the values above
   - How to create a workspace, ingest from `/vault/corporate`, and confirm
     retrieval works with a test query
   - Confirmation that neither stack touches any vault subfolder other than
     `corporate/`
   - Which file types AnythingLLM can ingest directly without going through
     the preprocessing stack first, and which types require it — based on
     your own testing, not assumption
   - A verification checklist (health check URLs, expected container
     states, a sample end-to-end query and expected response shape)
4. Any assumptions you made, called out explicitly (e.g. chunk size
   choices, workspace naming scheme).

## Evaluation Criteria

- Correctness: stack comes up cleanly with `docker compose up -d` and
  passes your own verification checklist
- No hardcoded secrets, network names, or vault host paths; `.env.example`
  is complete
- Vault access is correctly scoped to `corporate/` only — no broad mount of
  the whole vault root, no ingestion job that wanders into other subfolders
- Sensible resource defaults given the "no spare GPU" constraint
- Clear, minimal README — we should be able to deploy this without asking
  you questions
- Reasonable choices on chunking/metadata format for the preprocessing
  output, with rationale
