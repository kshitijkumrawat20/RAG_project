# Setup, the simple version

The main [README.md](README.md) explains *why* everything is the way it is. This file just
tells you *what to type*. Copy one block, run it, check the result, move to the next.

Every command here is safe to run twice.

---

## Before you start

You need three things on the machine:

1. **Docker.** Check: `docker ps` prints a table (even an empty one).
2. **The two stack folders in your home directory.** Check: `ls ~` shows
   `unstructured-stack`, `anythingllm-stack` and `scripts`.
3. **`uv`**, to run the helper scripts. Check: `uv --version` prints a version.
   If it doesn't, install it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### One rule that saves a lot of pain

There are **two copies** of these folders:

| Folder | What it's for |
| --- | --- |
| `~/RAG_project/` | The git repo. This is what you submit. Don't run anything here. |
| `~/unstructured-stack/`, `~/anythingllm-stack/`, `~/scripts/` | The running system. Run everything here. |

So: **edit in the repo, run in the home folder.** If you change a file in the repo, copy it
over again — and run that copy command **from inside `~/RAG_project`**, not from inside one
of the stack folders:

```bash
cd ~/RAG_project && cp -r unstructured-stack anythingllm-stack scripts ~/
```

---

## Step 1 — Create the shared Docker network

Both stacks join one network that already exists on the host. Create it once:

```bash
docker network create ai-stack-net
```

**Check:** it prints a long hex ID. If it says *"network with name ai-stack-net already
exists"*, that's fine — it means you already did this. Move on.

---

## Step 2 — Create the vault folder

The vault is a folder on the host. The preprocessor writes into `corporate/` inside it, and
AnythingLLM reads from there.

```bash
mkdir -p ~/vault/corporate
```

**Check:** `ls ~/vault` prints `corporate`.

> The README uses `/srv/vault` because that's a normal place for it on a real server. On a
> cloud dev machine, `~/vault` avoids needing `sudo` and survives restarts. Either works —
> just make sure both `.env` files point at the same one, which Step 4 handles for you.

---

## Step 3 — Create the two config files

Each stack reads its settings from a file called `.env`. You start from the provided
example and then fill it in.

**The space before `.env` matters.** `cp .env.example.env` (no space) copies the file onto
itself under a new name and gets you nowhere.

```bash
cd ~/unstructured-stack && cp .env.example .env
```

```bash
cd ~/anythingllm-stack && cp .env.example .env
```

**Check:** `ls -a ~/unstructured-stack` and `ls -a ~/anythingllm-stack` each show both
`.env` and `.env.example`.

---

## Step 4 — Fill the config files in

You *can* open these in an editor. You don't have to — these four blocks fill in
everything that needs changing.

> If you'd rather edit by hand, use `nano ~/unstructured-stack/.env` or click the file in
> your IDE's sidebar. Don't use `$EDITOR` unless you've set that variable; an unset
> `$EDITOR` is why you saw `command not found: .env`.

**4a. Point both stacks at the same vault:**

```bash
cd ~ && for d in unstructured-stack anythingllm-stack; do sed -i "s|^VAULT_HOST_PATH=.*|VAULT_HOST_PATH=$HOME/vault|" "$d/.env"; done
```

**4b. Tell the helper script where the vault is:**

```bash
cd ~/anythingllm-stack && sed -i "s|^VAULT_CORPORATE_DIR=.*|VAULT_CORPORATE_DIR=$HOME/vault/corporate|" .env
```

**4c. Make the preprocessor run as you**, so it can write into your vault folder:

```bash
cd ~/unstructured-stack && sed -i "s|^PREPROCESSOR_UID=.*|PREPROCESSOR_UID=$(id -u)|; s|^PREPROCESSOR_GID=.*|PREPROCESSOR_GID=$(id -g)|" .env
```

**4d. Generate the three secrets.** AnythingLLM refuses to start without them.

Note the underscore in `SIG_SALT` — three names, not four. `SIG SALT` with a space makes
two useless variables called `SIG` and `SALT`.

```bash
cd ~/anythingllm-stack && for v in JWT_SECRET SIG_KEY SIG_SALT; do sed -i "s|^${v}=.*|${v}=$(openssl rand -hex 32)|" .env; done
```

**Check all four at once** — this should print five lines, each with a real value after the
`=`, and no `/srv/vault` anywhere:

```bash
grep -E '^(VAULT_HOST_PATH|JWT_SECRET|SIG_KEY|SIG_SALT)=' ~/unstructured-stack/.env ~/anythingllm-stack/.env
```

---

## Step 5 — Tell AnythingLLM which LLM to talk to

AnythingLLM does not run its own chat model. It calls the LLM that's already running on the
host at port 8000. It needs to know the exact model name that server advertises.

Ask the server:

```bash
curl -s http://localhost:8000/v1/models
```

Take the `"id"` value out of the reply and put it in the config (replace
`PASTE-THE-ID-HERE`):

```bash
cd ~/anythingllm-stack && sed -i "s|^LLM_MODEL=.*|LLM_MODEL=PASTE-THE-ID-HERE|" .env
```

**Check:** `grep ^LLM_MODEL= ~/anythingllm-stack/.env` no longer says `CHANGE-ME`.

> **If `curl` fails or returns nothing, there is no LLM running on this machine.** That's
> expected on a dev box — the LLM lives on the target server. Everything except the final
> chat answer still works, so put a placeholder in and keep going:
>
> ```bash
> cd ~/anythingllm-stack && sed -i "s|^LLM_MODEL=.*|LLM_MODEL=placeholder-no-llm-here|" .env
> ```
>
> Document upload, chunking, embedding and vector search will all work. Only the last step
> (asking a question and getting a written answer) will fail, and it will fail with a
> connection error to port 8000 — not a configuration mistake on your part.

---

## Step 6 — Start the preprocessing stack

```bash
cd ~/unstructured-stack && mkdir -p data/inbox data/archive data/failed data/state
```

```bash
cd ~/unstructured-stack && docker compose up -d --build
```

The first run downloads a large image and builds the worker. Expect several minutes.

**Check:** wait a minute, then:

```bash
docker compose -f ~/unstructured-stack/docker-compose.yml ps
```

You want `unstructured-api` as **healthy** and `unstructured-preprocessor` as **running**.
If the preprocessor keeps restarting, read why:

```bash
docker logs --tail 50 unstructured-preprocessor
```

---

## Step 7 — Push one document through it

Drop any PDF, Word file, PowerPoint or spreadsheet into the inbox. The worker picks it up
within 20 seconds on its own — you don't run anything.

```bash
cp /path/to/your/document.pdf ~/unstructured-stack/data/inbox/
```

Watch it happen:

```bash
docker logs -f unstructured-preprocessor
```

Press `Ctrl+C` to stop watching. Then look at what it produced:

```bash
ls ~/vault/corporate/documents ~/vault/corporate/chunks
```

**Check:** you get one `.md` file and one `.chunks.jsonl` file, both named after your
document. Look inside the Markdown one:

```bash
head -30 ~/vault/corporate/documents/*.md
```

You should see a block between `---` lines at the top (that's the metadata: where the file
came from, how many chunks, its checksum), then the text with `<!-- chunk 1/N -->` markers
between sections.

If your file went to `~/unstructured-stack/data/failed/` instead, there's a `.error.txt`
next to it saying why.

---

## Step 8 — Start AnythingLLM

That empty file matters — without it, Docker creates a *folder* with that name and
AnythingLLM loses its settings on every restart.

```bash
cd ~/anythingllm-stack && mkdir -p data && touch data/.env
```

```bash
cd ~/anythingllm-stack && docker compose up -d
```

**Check:** after a minute or two,

```bash
curl -s http://localhost:3001/api/ping
```

should reply. If the container exited immediately, one of the three secrets from Step 4d is
missing — `docker logs anythingllm` will name it.

---

## Step 9 — Get an API key from the UI

This is the one step that needs a browser. Open **http://localhost:3001**. On a cloud dev
machine, use your IDE's port-forwarding for port 3001.

Skip the setup wizard's provider questions — the config file already set all of that, and
anything you pick in the wizard gets overwritten on the next restart anyway.

Then: **Settings** (the gear, bottom-left) → **Tools** → **Developer API** → **Generate New
API Key**. Copy it.

Paste it into the config (replace `PASTE-YOUR-KEY-HERE`):

```bash
cd ~/anythingllm-stack && sed -i "s|^ANYTHINGLLM_API_KEY=.*|ANYTHINGLLM_API_KEY=PASTE-YOUR-KEY-HERE|" .env
```

**Check:**

```bash
cd ~/anythingllm-stack && uv run scripts/allm.py auth
```

It should say the key was accepted. No restart needed — this key is only used by the
scripts, not by the container.

---

## Step 10 — Create the workspace and load the documents

One command creates the workspace with the right retrieval settings:

```bash
cd ~/anythingllm-stack && uv run scripts/allm.py bootstrap
```

Another loads everything from the vault into it:

```bash
cd ~/anythingllm-stack && uv run scripts/allm.py ingest
```

**Check:** it lists each file it uploaded and embedded. Running it again should skip
everything as unchanged — that's the ledger doing its job.

---

## Step 11 — Ask it a question

```bash
cd ~/anythingllm-stack && uv run scripts/allm.py query "What is in this knowledge base?"
```

**Check:** you get an answer *and* a list of sources underneath it.

- **Answer but no sources** → retrieval found nothing. Re-run `ingest`.
- **Sources but a connection error instead of an answer** → the LLM on port 8000 isn't
  reachable. Retrieval works; you just don't have the chat model here (see Step 5).

---

## Step 12 — Run the full check

```bash
bash ~/scripts/verify.sh
```

Note `bash`, not `source`. Sourcing it dumps its shell settings into your terminal.

It prints a PASS/FAIL line for every requirement and a count at the end. Each FAIL says
what to do about it.

Two failures are expected if there's no LLM on this machine: *"cannot reach .../models from
inside anythingllm"* and the chat check in section 8. Everything else should pass.

---

## Quick reference

| I want to… | Command |
| --- | --- |
| See what's running | `docker ps` |
| Read the worker's log | `docker logs -f unstructured-preprocessor` |
| Read AnythingLLM's log | `docker logs -f anythingllm` |
| Process a new document | Copy it into `~/unstructured-stack/data/inbox/` |
| Load new documents into the KB | `cd ~/anythingllm-stack && uv run scripts/allm.py ingest` |
| Re-run a failed document | Fix the cause, move it from `data/failed/` back to `data/inbox/` |
| Stop everything | `docker compose down` in each stack folder |
| Start over completely | `docker compose down -v`, then delete `data/` and `~/vault` |

## When something is wrong

| Message | Cause |
| --- | --- |
| `command not found: .env` | `$EDITOR` isn't set. Use `nano .env`. |
| `cp: cannot stat 'unstructured-stack'` | You're in the wrong folder. `cd ~/RAG_project` first. |
| `network ai-stack-net not found` | Step 1 wasn't run. |
| `set JWT_SECRET in .env` | Step 4d wasn't run, or `SIG_SALT` was typed as `SIG SALT`. |
| Preprocessor restart-loops with a permission error | Step 4c wasn't run, so it can't write to `~/vault`. |
| Provider settings revert after a restart | Normal. `.env` wins over the UI, on purpose. Change `.env`. |
| Chat fails, retrieval works | No LLM at port 8000. Not your setup's fault. |
