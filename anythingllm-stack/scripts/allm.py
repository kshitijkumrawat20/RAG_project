#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.32,<3"]
# ///
"""AnythingLLM knowledge-base operations CLI.

Run with uv — no venv to create, no pip:

    uv run scripts/allm.py auth
    uv run scripts/allm.py bootstrap
    uv run scripts/allm.py ingest
    uv run scripts/allm.py query "What is our travel expense policy?"
    uv run scripts/allm.py docs
    uv run scripts/allm.py matrix --fixtures ./fixtures

Reads configuration from the environment, or from ../.env when run from this directory
(`--env-file` to point elsewhere). Required: ANYTHINGLLM_API_KEY.

Vault scoping: `ingest` only ever reads $VAULT_CORPORATE_DIR/documents/**/*.md. The path
is validated before any read — it must be a directory literally named `corporate`, and any
path containing `shared`, `tickets`, `learnings` or `skills` is rejected outright. Nothing
walks the vault root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

# Vault subfolders owned by other systems. Refuse to touch them, ever.
FORBIDDEN_VAULT_SEGMENTS = {"shared", "tickets", "learnings", "skills"}

STATE_FILENAME = ".ingest-ledger.json"


class Fail(RuntimeError):
    """User-facing error: printed without a traceback."""


# --------------------------------------------------------------------------- config


def load_env_file(path: Path) -> None:
    """Minimal dotenv loader. Existing environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value[:1] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass
class Config:
    base_url: str
    api_key: str
    workspace_name: str
    workspace_slug: str
    corporate_dir: str

    @classmethod
    def load(cls) -> "Config":
        api_key = os.environ.get("ANYTHINGLLM_API_KEY", "").strip()
        if not api_key:
            raise Fail(
                "ANYTHINGLLM_API_KEY is not set.\n"
                "  Generate one in the UI: Settings -> Tools -> Developer API -> "
                "Generate New API Key,\n  then put it in anythingllm-stack/.env."
            )
        return cls(
            base_url=os.environ.get("ANYTHINGLLM_BASE_URL", "http://localhost:3001").rstrip("/"),
            api_key=api_key,
            workspace_name=os.environ.get("WORKSPACE_NAME", "Corporate Knowledge Base"),
            workspace_slug=os.environ.get("WORKSPACE_SLUG", "corporate-knowledge-base"),
            corporate_dir=os.environ.get("VAULT_CORPORATE_DIR", "/vault/corporate"),
        )


# --------------------------------------------------------------------------- API client


class Client:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {cfg.api_key}", "Accept": "application/json"}
        )

    def _url(self, path: str) -> str:
        return f"{self.cfg.base_url}/api/v1/{path.lstrip('/')}"

    def request(self, method: str, path: str, *, timeout: int = 120, **kwargs) -> requests.Response:
        try:
            response = self.session.request(method, self._url(path), timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            raise Fail(f"cannot reach AnythingLLM at {self.cfg.base_url}: {exc}") from exc
        if response.status_code == 403:
            raise Fail("403 Forbidden — ANYTHINGLLM_API_KEY is wrong or was revoked.")
        return response

    def json(self, method: str, path: str, *, timeout: int = 120, **kwargs) -> dict:
        response = self.request(method, path, timeout=timeout, **kwargs)
        if response.status_code >= 400:
            raise Fail(f"{method} {path} -> HTTP {response.status_code}: {response.text[:800]}")
        try:
            return response.json() or {}
        except ValueError as exc:
            raise Fail(f"{method} {path} returned non-JSON: {response.text[:300]}") from exc


# --------------------------------------------------------------------------- vault paths


def resolve_corporate_dir(raw: str) -> Path:
    """Validate that `raw` points at the vault's corporate/ folder and nothing else."""
    root = Path(raw).expanduser()
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise Fail(f"VAULT_CORPORATE_DIR {raw!r} is not reachable: {exc}") from exc

    lowered = {part.lower() for part in root.parts}
    trespass = lowered & FORBIDDEN_VAULT_SEGMENTS
    if trespass:
        raise Fail(
            f"refusing to read {root}: path crosses vault subfolder(s) "
            f"{sorted(trespass)} which belong to other systems."
        )
    if root.name.lower() != "corporate":
        raise Fail(
            f"refusing to read {root}: VAULT_CORPORATE_DIR must point at the vault's "
            "`corporate` directory itself, not its parent."
        )
    if not root.is_dir():
        raise Fail(f"{root} is not a directory.")
    return root


def iter_documents(corporate_dir: Path) -> list[Path]:
    """Every preprocessed Markdown file, and only those."""
    documents_dir = corporate_dir / "documents"
    if not documents_dir.is_dir():
        raise Fail(
            f"{documents_dir} does not exist. Run a document through the unstructured-stack "
            "first — it creates documents/ and chunks/ under corporate/."
        )
    found = []
    for path in sorted(documents_dir.rglob("*.md")):
        if not path.is_file() or path.name.startswith("."):
            continue
        # Defence against a symlink pointing out of the vault.
        if not path.resolve().is_relative_to(corporate_dir):
            print(f"  ! skipping {path} — resolves outside {corporate_dir}", file=sys.stderr)
            continue
        found.append(path)
    return found


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Pull the preprocessor's YAML frontmatter out of a Markdown file."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip()
        if value[:1] == '"' and value[-1:] == '"':
            try:
                value = json.loads(value)
            except ValueError:
                value = value[1:-1]
        meta[key.strip()] = value
    return meta, text[end + 5 :]


# --------------------------------------------------------------------------- ledger


class IngestLedger:
    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, dict] = {}
        if path.is_file():
            try:
                self.entries = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.entries = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=2, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------- commands


def cmd_auth(client: Client, _args) -> int:
    payload = client.json("GET", "auth")
    print(f"API key valid: {payload}")
    print(f"Base URL:      {client.cfg.base_url}")

    for label, path in (("workspaces", "workspaces"), ("openai-compat models", "openai/models")):
        response = client.request("GET", path, timeout=30)
        state = "reachable" if response.status_code < 400 else f"HTTP {response.status_code}"
        print(f"{label:<22} {state}")
    return 0


def _find_workspace(client: Client, slug: str) -> dict | None:
    payload = client.json("GET", "workspaces")
    for workspace in payload.get("workspaces", []):
        if workspace.get("slug") == slug:
            return workspace
    return None


def cmd_bootstrap(client: Client, args) -> int:
    cfg = client.cfg
    existing = _find_workspace(client, cfg.workspace_slug)
    if existing:
        print(f"workspace already exists: {existing['slug']} (id={existing.get('id')})")
    else:
        payload = client.json("POST", "workspace/new", json={"name": cfg.workspace_name})
        workspace = payload.get("workspace") or {}
        slug = workspace.get("slug")
        if not slug:
            raise Fail(f"workspace creation returned no slug: {payload}")
        if slug != cfg.workspace_slug:
            print(
                f"  ! AnythingLLM slugified {cfg.workspace_name!r} to {slug!r}, which differs\n"
                f"    from WORKSPACE_SLUG={cfg.workspace_slug!r}. Update .env to {slug!r}."
            )
        print(f"created workspace: {slug} (id={workspace.get('id')})")
        cfg.workspace_slug = slug

    settings = {
        "openAiTemp": args.temperature,
        "openAiHistory": args.history,
        "similarityThreshold": args.similarity_threshold,
        "topN": args.top_n,
        "chatMode": "query",
        "queryRefusalResponse": (
            "I could not find that in the corporate knowledge base."
        ),
    }
    response = client.request(
        "POST", f"workspace/{cfg.workspace_slug}/update", json=settings, timeout=60
    )
    if response.status_code < 400:
        print(f"applied retrieval settings: topN={args.top_n} "
              f"similarityThreshold={args.similarity_threshold} chatMode=query temp={args.temperature}")
    else:
        print(f"  ! could not apply retrieval settings (HTTP {response.status_code}); "
              f"set them in the workspace UI instead")
    print(f"\nworkspace endpoint: {cfg.base_url}/api/v1/workspace/{cfg.workspace_slug}/chat")
    return 0


def cmd_ingest(client: Client, args) -> int:
    cfg = client.cfg
    corporate_dir = resolve_corporate_dir(args.corporate_dir or cfg.corporate_dir)
    print(f"vault scope:  {corporate_dir}/documents  (read-only, nothing else is touched)")

    if not _find_workspace(client, cfg.workspace_slug):
        raise Fail(f"workspace {cfg.workspace_slug!r} does not exist — run `bootstrap` first.")

    documents = iter_documents(corporate_dir)
    if not documents:
        print("nothing to ingest.")
        return 0

    ledger = IngestLedger(Path(args.state_file))
    uploaded: list[str] = []
    stale: list[str] = []
    skipped = 0

    for path in documents:
        rel = path.relative_to(corporate_dir).as_posix()
        raw = path.read_text(encoding="utf-8", errors="replace")
        digest = sha256_text(raw)
        previous = ledger.entries.get(rel)

        if previous and previous.get("sha256") == digest and not args.force:
            skipped += 1
            continue

        meta, body = parse_frontmatter(raw)
        title = meta.get("source_filename") or path.stem
        pieces = _payload_pieces(args.mode, title, body, meta, rel)

        if previous and previous.get("locations"):
            stale.extend(previous["locations"])

        locations = []
        for piece_title, piece_text, description in pieces:
            payload = client.json(
                "POST",
                "document/raw-text",
                timeout=300,
                json={
                    "textContent": piece_text,
                    "metadata": {
                        "title": piece_title,
                        "docSource": f"/vault/corporate/{rel}",
                        "description": description,
                        "docAuthor": "unstructured-stack",
                    },
                },
            )
            for document in payload.get("documents", []):
                if document.get("location"):
                    locations.append(document["location"])

        if not locations:
            print(f"  ! {rel}: upload returned no document location; skipping")
            continue

        uploaded.extend(locations)
        ledger.entries[rel] = {
            "sha256": digest,
            "locations": locations,
            "title": title,
            "mode": args.mode,
            "chunk_count": meta.get("chunk_count"),
        }
        print(f"  + {rel} -> {len(locations)} document(s)")

    if uploaded or stale:
        client.json(
            "POST",
            f"workspace/{cfg.workspace_slug}/update-embeddings",
            timeout=1800,
            json={"adds": uploaded, "deletes": stale},
        )
        print(f"\nembedded {len(uploaded)} document(s) into {cfg.workspace_slug}"
              f"{f', removed {len(stale)} superseded' if stale else ''}")
        if stale and args.prune:
            client.request(
                "DELETE", "system/remove-documents", timeout=300, json={"names": stale}
            )
            print(f"pruned {len(stale)} superseded document(s) from storage")
    else:
        print("\nnothing new to embed.")

    if skipped:
        print(f"unchanged, skipped: {skipped} (use --force to re-embed)")
    ledger.save()
    print(f"ledger: {ledger.path}")
    return 0


def _payload_pieces(mode: str, title: str, body: str, meta: dict, rel: str) -> list[tuple[str, str, str]]:
    """One payload per document, or one per preprocessed chunk."""
    summary = (
        f"Preprocessed from {title} "
        f"({meta.get('chunk_count', '?')} chunks, {meta.get('partition_strategy', '?')} strategy)"
    )
    if mode == "document":
        return [(title, body.strip(), summary)]

    pieces = []
    current: list[str] = []
    header = None
    for line in body.splitlines():
        if line.startswith("<!-- chunk "):
            if header and current:
                pieces.append((header, "\n".join(current).strip()))
            header = line.strip("<!-> ").strip()
            current = []
        elif header:
            current.append(line)
    if header and current:
        pieces.append((header, "\n".join(current).strip()))

    if not pieces:
        return [(title, body.strip(), summary)]
    return [
        (f"{title} [{marker}]", text, f"{summary}; {marker}")
        for marker, text in pieces
        if text
    ]


def cmd_query(client: Client, args) -> int:
    cfg = client.cfg
    payload = client.json(
        "POST",
        f"workspace/{cfg.workspace_slug}/chat",
        timeout=600,
        json={"message": args.question, "mode": args.mode},
    )
    if payload.get("error"):
        raise Fail(f"chat returned an error: {payload['error']}")

    print("=" * 72)
    print(payload.get("textResponse", "<no textResponse field>").strip())
    print("=" * 72)

    sources = payload.get("sources") or []
    print(f"\nsources: {len(sources)}")
    for source in sources:
        score = source.get("score")
        score_text = f"{score:.4f}" if isinstance(score, (int, float)) else "n/a"
        print(f"  - {source.get('title', '?')}  score={score_text}")
        print(f"    docSource: {source.get('docSource', '?')}")
        snippet = " ".join((source.get("text") or "").split())[:160]
        if snippet:
            print(f"    text: {snippet}...")

    if not sources:
        print("  (no sources — retrieval found nothing above the similarity threshold)")
        return 1
    return 0


def cmd_docs(client: Client, _args) -> int:
    payload = client.json("GET", "documents")
    items = payload.get("localFiles", {}).get("items", [])
    total = 0
    for folder in items:
        children = folder.get("items", [])
        total += len(children)
        print(f"{folder.get('name', '?')}/  ({len(children)} document(s))")
        for child in children[:200]:
            print(f"  - {child.get('title', child.get('name', '?'))}  "
                  f"words={child.get('wordCount', '?')}  src={child.get('docSource', '?')}")
    print(f"\ntotal documents in storage: {total}")
    return 0


def cmd_matrix(client: Client, args) -> int:
    """Empirically determine which file types AnythingLLM ingests without preprocessing."""
    fixtures = Path(args.fixtures).expanduser().resolve()
    if not fixtures.is_dir():
        raise Fail(
            f"{fixtures} is not a directory. Put one small sample file per extension in it "
            "(sample.pdf, scanned.pdf, sample.docx, sample.pptx, sample.html, ...)."
        )

    response = client.request("GET", "document/accepted-file-types", timeout=30)
    declared = response.json() if response.status_code < 400 else {}
    print("declared accepted file types (from the collector):")
    print(json.dumps(declared, indent=2)[:2000])

    results = []
    print(f"\nuploading {len(list(fixtures.iterdir()))} fixture(s) from {fixtures}\n")
    for path in sorted(fixtures.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as fh:
            response = client.request(
                "POST", "document/upload", timeout=900, files={"file": (path.name, fh, mime)}
            )
        entry = {
            "file": path.name,
            "extension": path.suffix.lower(),
            "http_status": response.status_code,
            "accepted": False,
            "word_count": None,
            "verdict": "",
        }
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        documents = payload.get("documents") or []
        if response.status_code < 400 and payload.get("success") and documents:
            words = documents[0].get("wordCount")
            entry["accepted"] = True
            entry["word_count"] = words
            entry["verdict"] = (
                "DIRECT" if isinstance(words, int) and words > 0 else "EMPTY -> needs preprocessing"
            )
        else:
            entry["verdict"] = f"REJECTED -> needs preprocessing ({payload.get('error') or 'no detail'})"
        results.append(entry)
        print(f"  {path.name:<28} {entry['verdict']}  words={entry['word_count']}")

    out = Path(args.out)
    out.write_text(json.dumps({"declared": declared, "results": results}, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")

    print("\n| File | Ext | Accepted | Words extracted | Verdict |")
    print("|---|---|---|---|---|")
    for entry in results:
        print(f"| {entry['file']} | {entry['extension']} | {entry['accepted']} | "
              f"{entry['word_count']} | {entry['verdict']} |")
    print("\nNote: these uploads land in AnythingLLM's storage but are NOT attached to the\n"
          "corporate workspace. Remove them from Documents in the UI when you are done.")
    return 0


# --------------------------------------------------------------------------- entrypoint


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--env-file",
        default=str(Path(__file__).resolve().parent.parent / ".env"),
        help="dotenv file to load (default: ../.env relative to this script)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="validate the API key and show reachable endpoints")

    bootstrap = sub.add_parser("bootstrap", help="create the workspace and set retrieval defaults")
    bootstrap.add_argument("--top-n", type=int, default=6, help="chunks retrieved per query")
    bootstrap.add_argument("--similarity-threshold", type=float, default=0.25)
    bootstrap.add_argument("--temperature", type=float, default=0.1)
    bootstrap.add_argument("--history", type=int, default=8)

    ingest = sub.add_parser("ingest", help="embed /vault/corporate/documents into the workspace")
    ingest.add_argument("--corporate-dir", help="override VAULT_CORPORATE_DIR")
    ingest.add_argument(
        "--mode",
        choices=("document", "chunks"),
        default="document",
        help="document: one AnythingLLM doc per source file, re-split by TEXT_SPLITTER_* "
             "(default). chunks: one doc per preprocessed chunk, preserving exact "
             "boundaries and per-chunk citations.",
    )
    ingest.add_argument("--force", action="store_true", help="re-embed even if unchanged")
    ingest.add_argument("--prune", action="store_true", help="delete superseded docs from storage")
    ingest.add_argument(
        "--state-file",
        default=str(Path(__file__).resolve().parent / STATE_FILENAME),
        help="ingest ledger location",
    )

    query = sub.add_parser("query", help="ask the workspace a question")
    query.add_argument("question")
    query.add_argument("--mode", choices=("query", "chat"), default="query")

    sub.add_parser("docs", help="list documents currently in AnythingLLM storage")

    matrix = sub.add_parser("matrix", help="test which raw file types ingest without preprocessing")
    matrix.add_argument("--fixtures", default="./fixtures")
    matrix.add_argument("--out", default="./file-type-matrix.json")

    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    load_env_file(Path(args.env_file))
    cfg = Config.load()
    client = Client(cfg)

    handlers = {
        "auth": cmd_auth,
        "bootstrap": cmd_bootstrap,
        "ingest": cmd_ingest,
        "query": cmd_query,
        "docs": cmd_docs,
        "matrix": cmd_matrix,
    }
    return handlers[args.command](client, args)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Fail as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
