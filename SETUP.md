# Setup, the simple version

The main [README.md](README.md) explains *why* everything is set up the way it is. This file
just tells you what to type.

**Everything happens inside this repo.** There is no copying folders around. Start by going
there and staying there:

```bash
cd ~/RAG_project
```

Every path below is relative to that. If your shell prompt doesn't say `RAG_project`, run
that command again before continuing.

---

## Before you start

Three things must work on the machine:

```bash
docker ps
```

```bash
uv --version
```

```bash
openssl version
```

`docker ps` should print a table, even an empty one. If `uv` is missing:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Step 1 — Run the setup script

This one command does all the fiddly parts: creates the Docker network, creates the vault
folder, creates both `.env` files, fills in the paths, generates the three secrets, creates
the runtime folders, and detects your local LLM if it's running.

```bash
bash scripts/setup.sh
```

It's safe to run again any time. It never overwrites an existing `.env` and never
regenerates a secret that's already set.

**Check:** the last section says `Done.` with 0 warnings, or with one warning about port
8000 (that one is fine — see the note at the bottom of this file).

Want the vault somewhere other than `~/vault`? Pass it in:

```bash
bash scripts/setup.sh --vault /srv/vault
```

<details>
<summary>What it actually changed, if you want to look</summary>

```bash
grep -E '^(VAULT_HOST_PATH|PREPROCESSOR_UID|PREPROCESSOR_GID)=' unstructured-stack/.env
```

```bash
grep -E '^(VAULT_HOST_PATH|VAULT_CORPORATE_DIR|JWT_SECRET|SIG_KEY|SIG_SALT|LLM_MODEL)=' anythingllm-stack/.env
```

Every one of those should have a real value after the `=`. Nothing should still say
`CHANGE-ME` or `/srv/vault` (unless you asked for `/srv/vault`).

</details>

---

## Step 2 — Start the preprocessing stack

```bash
cd unstructured-stack && docker compose up -d --build && cd ..
```

The first run downloads a large image and builds the worker. Expect several minutes.

**Check:** wait a minute, then

```bash
docker compose -f unstructured-stack/docker-compose.yml ps
```

You want `unstructured-api` **healthy** and `unstructured-preprocessor` **running**. If the
preprocessor keeps restarting, it will tell you why:

```bash
docker logs --tail 50 unstructured-preprocessor
```

---

## Step 3 — Push one document through it

Drop any PDF, Word file, PowerPoint or spreadsheet into the inbox. The worker notices it
within 20 seconds by itself — you don't run anything.

```bash
cp /path/to/your/document.pdf unstructured-stack/data/inbox/
```

Watch it work (`Ctrl+C` to stop watching — that doesn't stop the worker):

```bash
docker logs -f unstructured-preprocessor
```

**Check:** one Markdown file and one chunks file appeared in the vault, both named after
your document:

```bash
ls ~/vault/corporate/documents ~/vault/corporate/chunks
```

Look inside the Markdown one:

```bash
head -30 ~/vault/corporate/documents/*.md
```

You should see a block between `---` lines at the top — that's the metadata: where the file
came from, how many chunks, its checksum — then the text, with `<!-- chunk 1/N -->` markers
between sections.

If your file landed in `unstructured-stack/data/failed/` instead, there's a `.error.txt`
next to it explaining why.

---

## Step 4 — Start AnythingLLM

```bash
cd anythingllm-stack && docker compose up -d && cd ..
```

**Check:** after a minute or two,

```bash
curl -s http://localhost:3001/api/ping
```

should reply. If the container exited straight away, `docker logs anythingllm` names the
reason.

---

## Step 5 — Get an API key from the UI

This is the only step that needs a browser. Open **http://localhost:3001** — on a cloud dev
machine, use your IDE's port forwarding for port 3001.

Click past the setup wizard's provider questions. The config file already set all of that,
and anything you choose in the wizard gets overwritten on the next restart anyway.

Then: **Settings** (the gear, bottom-left) → **Tools** → **Developer API** → **Generate New
API Key**. Copy it, and paste it in place of `PASTE-YOUR-KEY-HERE` below:

```bash
cd anythingllm-stack && uv run scripts/allm.py set-key PASTE-YOUR-KEY-HERE && cd ..
```

**Check:**

```bash
cd anythingllm-stack && uv run scripts/allm.py auth && cd ..
```

It should say the key was accepted. No restart needed — this key is only used by the helper
scripts, not by the container.

---

## Step 6 — Create the workspace and load the documents

```bash
cd anythingllm-stack && uv run scripts/allm.py bootstrap && cd ..
```

```bash
cd anythingllm-stack && uv run scripts/allm.py ingest && cd ..
```

**Check:** `ingest` lists each file it uploaded and embedded. Run it a second time and it
should skip everything as unchanged — that's the ledger doing its job.

---

## Step 7 — Ask it a question

```bash
cd anythingllm-stack && uv run scripts/allm.py query "What is in this knowledge base?" && cd ..
```

**Check:** you get an answer *and* a list of sources under it.

- **Answer but no sources** → retrieval matched nothing. Re-run `ingest`.
- **Sources but a connection error instead of an answer** → the LLM on port 8000 isn't
  reachable. Retrieval works fine; you just don't have the chat model on this machine.

---

## Step 8 — Run the full check

```bash
bash scripts/verify.sh
```

Note `bash`, not `source`. Sourcing it dumps its shell settings into your terminal.

It prints PASS or FAIL for every requirement, with a count at the end. Each FAIL says what
to do about it.

---

## If there's no LLM on this machine

The chat model is not part of either stack — it's an existing container on the target
server, on port 8000. Check whether you have one:

```bash
curl -s http://localhost:8000/v1/models
```

If that returns nothing, `scripts/setup.sh` already put a placeholder in for you and warned
you about it. Everything still works except the final written answer:

| Works | Doesn't work |
| --- | --- |
| Preprocessing (Steps 2–3) | Step 7's written answer |
| Upload, chunking, embedding (Step 6) | verify.sh section 4 |
| Vector search — `sources` in Step 7 | verify.sh section 8's chat check |

So two FAILs in `verify.sh` are expected here and are not your setup's fault. Everything
else should pass. When you deploy on the real server, re-run `bash scripts/setup.sh` — it
will find the LLM and fill in the model id automatically.

---

## Quick reference

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

## Common mistakes

| Symptom | Cause |
| --- | --- |
| `cd: no such file or directory: ~/anythingllm-stack` | It's `RAG_project/anythingllm-stack`. `cd ~/RAG_project` first. |
| `command not found: cp.env.example.env` | `cp` `.env.example` `.env` are three words — you need both spaces. Or just run `scripts/setup.sh`. |
| `command not found: .env` | `$EDITOR` isn't set. Use `nano .env`, or let `setup.sh` do the editing. |
| `sed: -e expression #1: unknown option to 's'` | Mismatched delimiters — `s\|…\|…\|` needs the same character all three times. Again: use `setup.sh`. |
| `network ai-stack-net not found` | `setup.sh` couldn't reach Docker. Check `docker ps`, then re-run it. |
| `set JWT_SECRET in .env` | Secrets missing. Re-run `setup.sh`. |
| Preprocessor restart-loops with a permission error | It isn't running as you. Re-run `setup.sh`, then `docker compose up -d` again. |
| Provider settings revert after a restart | Normal and deliberate — `.env` wins over the UI. Change `.env`. |
| Chat fails but retrieval works | No LLM at port 8000. See the section above. |
