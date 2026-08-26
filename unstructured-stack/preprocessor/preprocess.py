#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.32,<3"]
# ///
"""Vault document preprocessor.

Watches an inbox folder, pushes each new/changed file through the self-hosted
Unstructured API, and writes two artifacts per source document into the vault:

    /vault/corporate/documents/<relpath>.md            clean Markdown, YAML frontmatter,
                                                       chunk boundaries marked inline
    /vault/corporate/chunks/<relpath>.chunks.jsonl     one JSON object per chunk, full
                                                       metadata (page/slide, table HTML,
                                                       source sha256, pipeline settings)

The Markdown file is what AnythingLLM ingests. The JSONL is the machine-readable
handoff for any other consumer and the basis for verifying chunking.

Runnable standalone for debugging, with no venv to manage:

    uv run preprocess.py --one-shot --inbox ./data/inbox --output /tmp/out \
        --api-url http://localhost:8003
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

LOG = logging.getLogger("preprocess")

# Extensions the Unstructured API can partition. Anything else in the inbox is ignored
# (and logged once) rather than being sent to the API to fail.
SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".docx", ".doc", ".odt", ".rtf",
    ".pptx", ".ppt", ".odp",
    ".xlsx", ".xls", ".ods", ".csv", ".tsv",
    ".html", ".htm", ".xml",
    ".txt", ".md", ".rst", ".org",
    ".eml", ".msg",
    ".epub",
    ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".heic",
}

# Files still being copied in should not be picked up mid-write.
QUIET_PERIOD_SECONDS = 5

_SLUG_STRIP = re.compile(r"[^a-z0-9._-]+")
_SLUG_DASHES = re.compile(r"-{2,}")


# --------------------------------------------------------------------------- config


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        LOG.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default


@dataclass
class Config:
    api_url: str = "http://unstructured-api:8000"
    inbox_dir: Path = Path("/data/inbox")
    archive_dir: Path = Path("/data/archive")
    failed_dir: Path = Path("/data/failed")
    state_dir: Path = Path("/data/state")
    output_dir: Path = Path("/vault/corporate")

    watch_interval: int = 20
    one_shot: bool = False
    archive_processed: bool = True
    reprocess_changed: bool = True

    partition_strategy: str = "auto"
    infer_table_structure: bool = False
    ocr_languages: str = "eng"

    chunking_strategy: str = "by_title"
    chunk_max_characters: int = 900
    chunk_new_after_n_chars: int = 750
    chunk_overlap: int = 120
    chunk_combine_under_n_chars: int = 250

    request_timeout: int = 1800

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            api_url=_env("UNSTRUCTURED_API_URL", cls.api_url).rstrip("/"),
            inbox_dir=Path(_env("INBOX_DIR", str(cls.inbox_dir))),
            archive_dir=Path(_env("ARCHIVE_DIR", str(cls.archive_dir))),
            failed_dir=Path(_env("FAILED_DIR", str(cls.failed_dir))),
            state_dir=Path(_env("STATE_DIR", str(cls.state_dir))),
            output_dir=Path(_env("OUTPUT_DIR", str(cls.output_dir))),
            watch_interval=_env_int("WATCH_INTERVAL_SECONDS", cls.watch_interval),
            one_shot=_env_bool("ONE_SHOT", cls.one_shot),
            archive_processed=_env_bool("ARCHIVE_PROCESSED", cls.archive_processed),
            reprocess_changed=_env_bool("REPROCESS_CHANGED", cls.reprocess_changed),
            partition_strategy=_env("PARTITION_STRATEGY", cls.partition_strategy),
            infer_table_structure=_env_bool("INFER_TABLE_STRUCTURE", cls.infer_table_structure),
            ocr_languages=_env("OCR_LANGUAGES", cls.ocr_languages),
            chunking_strategy=_env("CHUNKING_STRATEGY", cls.chunking_strategy),
            chunk_max_characters=_env_int("CHUNK_MAX_CHARACTERS", cls.chunk_max_characters),
            chunk_new_after_n_chars=_env_int("CHUNK_NEW_AFTER_N_CHARS", cls.chunk_new_after_n_chars),
            chunk_overlap=_env_int("CHUNK_OVERLAP", cls.chunk_overlap),
            chunk_combine_under_n_chars=_env_int(
                "CHUNK_COMBINE_UNDER_N_CHARS", cls.chunk_combine_under_n_chars
            ),
            request_timeout=_env_int("REQUEST_TIMEOUT_SECONDS", cls.request_timeout),
        )

    @property
    def documents_dir(self) -> Path:
        return self.output_dir / "documents"

    @property
    def chunks_dir(self) -> Path:
        return self.output_dir / "chunks"

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "ledger.json"

    @property
    def manifest_path(self) -> Path:
        # Deliberately in stack-local state, not in the vault: /vault/corporate holds only
        # documents/ and chunks/, so the ingestion side never has to filter stray files.
        return self.state_dir / "manifest.jsonl"

    def chunking_enabled(self) -> bool:
        return self.chunking_strategy.lower() not in {"", "none", "off"}


# --------------------------------------------------------------------------- helpers


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def slugify(value: str) -> str:
    out = _SLUG_STRIP.sub("-", value.lower()).strip("-._")
    out = _SLUG_DASHES.sub("-", out)
    return out or "document"


def yaml_scalar(value: object) -> str:
    """Render a scalar as YAML. JSON string quoting is valid YAML double-quoted style."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def unique_ordered(values: list) -> list:
    seen, out = set(), []
    for value in values:
        if value is not None and value not in seen:
            seen.add(value)
            out.append(value)
    return out


# --------------------------------------------------------------------------- ledger


class Ledger:
    """sha256 record of what has already been processed, so restarts are cheap."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, dict] = {}
        if path.exists():
            try:
                self.entries = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                LOG.warning("ledger at %s unreadable (%s); starting fresh", path, exc)
                self.entries = {}

    def is_current(self, relpath: str, sha: str, reprocess_changed: bool) -> bool:
        entry = self.entries.get(relpath)
        if entry is None:
            return False
        # A previously failed file is always retried: the operator may have changed
        # PARTITION_STRATEGY and moved it back from failed/ into the inbox.
        if entry.get("status") != "ok":
            return False
        if not reprocess_changed:
            return True
        return entry.get("source_sha256") == sha

    def record(self, relpath: str, entry: dict) -> None:
        self.entries[relpath] = entry
        self.flush()

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.entries, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


# --------------------------------------------------------------------------- API call


class PartitionError(RuntimeError):
    pass


def partition(cfg: Config, session: requests.Session, path: Path) -> list[dict]:
    """POST a file to the Unstructured API and return its element/chunk list."""
    fields: list[tuple[str, str]] = [
        ("strategy", cfg.partition_strategy),
        ("output_format", "application/json"),
        ("coordinates", "false"),
        ("unique_element_ids", "true"),
        ("pdf_infer_table_structure", "true" if cfg.infer_table_structure else "false"),
    ]
    for language in (lang.strip() for lang in cfg.ocr_languages.split(",") if lang.strip()):
        fields.append(("languages", language))

    if cfg.chunking_enabled():
        fields += [
            ("chunking_strategy", cfg.chunking_strategy),
            ("max_characters", str(cfg.chunk_max_characters)),
            ("new_after_n_chars", str(cfg.chunk_new_after_n_chars)),
            ("overlap", str(cfg.chunk_overlap)),
            ("overlap_all", "false"),
            ("combine_under_n_chars", str(cfg.chunk_combine_under_n_chars)),
            # Chunks would otherwise carry every source element base64-gzipped in
            # metadata.orig_elements, roughly doubling payload size for no downstream use.
            ("include_orig_elements", "false"),
        ]

    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        with path.open("rb") as fh:
            response = session.post(
                f"{cfg.api_url}/general/v0/general",
                files=[("files", (path.name, fh, mime))],
                data=fields,
                timeout=cfg.request_timeout,
            )
    except requests.RequestException as exc:
        raise PartitionError(f"request to Unstructured API failed: {exc}") from exc

    if response.status_code != 200:
        raise PartitionError(
            f"HTTP {response.status_code} from Unstructured API: {response.text[:2000]}"
        )

    try:
        elements = response.json()
    except ValueError as exc:
        raise PartitionError(f"non-JSON response from Unstructured API: {exc}") from exc

    if not isinstance(elements, list):
        raise PartitionError(f"expected a JSON list of elements, got {type(elements).__name__}")
    return elements


# --------------------------------------------------------------------------- rendering


@dataclass
class SourceInfo:
    filename: str
    relpath: str
    sha256: str
    filetype: str
    size_bytes: int
    modified_at: str
    slug: str


def has_content(element: dict) -> bool:
    if (element.get("text") or "").strip():
        return True
    return bool((element.get("metadata") or {}).get("text_as_html"))


def build_chunk_records(cfg: Config, elements: list[dict], src: SourceInfo) -> list[dict]:
    """Build one record per element. Callers must drop empty elements first, so that
    chunk_index/chunk_count are contiguous and match what is written to disk."""
    total = len(elements)
    pipeline = {
        "partition_strategy": cfg.partition_strategy,
        "infer_table_structure": cfg.infer_table_structure,
        "ocr_languages": cfg.ocr_languages,
        "chunking_strategy": cfg.chunking_strategy if cfg.chunking_enabled() else "none",
        "max_characters": cfg.chunk_max_characters,
        "new_after_n_chars": cfg.chunk_new_after_n_chars,
        "overlap": cfg.chunk_overlap,
        "combine_under_n_chars": cfg.chunk_combine_under_n_chars,
    }
    produced_at = now_iso()

    records = []
    for index, element in enumerate(elements):
        meta = element.get("metadata") or {}
        text = (element.get("text") or "").strip()
        records.append(
            {
                "chunk_id": f"{src.slug}::{index:04d}",
                "chunk_index": index,
                "chunk_count": total,
                "element_id": element.get("element_id"),
                "type": element.get("type"),
                "text": text,
                "char_count": len(text),
                # Present for Table/TableChunk when INFER_TABLE_STRUCTURE=true.
                "text_as_html": meta.get("text_as_html"),
                "locator": {
                    # PDF page number, or PPTX slide number. Absent for paragraph-stream
                    # formats (DOCX/HTML/MD) — chunk_index is the locator there.
                    "page_number": meta.get("page_number"),
                    "section": meta.get("section"),
                    "url": meta.get("url"),
                    "parent_id": meta.get("parent_id"),
                },
                "languages": meta.get("languages"),
                "source": {
                    "filename": src.filename,
                    "relpath": src.relpath,
                    "sha256": src.sha256,
                    "filetype": meta.get("filetype") or src.filetype,
                    "size_bytes": src.size_bytes,
                    "modified_at": src.modified_at,
                },
                "pipeline": pipeline,
                "produced_at": produced_at,
            }
        )
    return records


def _as_heading(text: str) -> str:
    """Promote a chunk's leading line to a Markdown H2 when it reads like a section title.

    by_title chunking starts each chunk at a section boundary, so the first line is
    usually that section's title. Marking it up makes the output readable Markdown; the
    text itself is unchanged, and the raw form is always preserved in the JSONL.
    """
    first, sep, rest = text.partition("\n")
    candidate = first.strip()
    if (
        candidate
        and sep
        and rest.strip()
        and len(candidate) <= 90
        and not candidate.startswith("#")
        and not candidate.startswith("|")
        and candidate[-1] not in ".,;:"
    ):
        return f"## {candidate}\n\n{rest.strip()}"
    return text


def render_markdown(cfg: Config, records: list[dict], src: SourceInfo, doc_rel: Path, chunks_rel: Path) -> str:
    pages = unique_ordered([r["locator"]["page_number"] for r in records])
    frontmatter = {
        "title": Path(src.filename).stem.replace("_", " ").replace("-", " ").strip(),
        "source_filename": src.filename,
        "source_relpath": src.relpath,
        "source_sha256": src.sha256,
        "source_filetype": src.filetype,
        "source_modified_at": src.modified_at,
        "page_count": len(pages) or None,
        "chunk_count": len(records),
        "chunk_strategy": cfg.chunking_strategy if cfg.chunking_enabled() else "none",
        "chunk_max_characters": cfg.chunk_max_characters,
        "chunk_overlap": cfg.chunk_overlap,
        "partition_strategy": cfg.partition_strategy,
        "processed_at": now_iso(),
        "producer": "unstructured-api",
        "vault_document": f"/vault/corporate/{doc_rel.as_posix()}",
        "vault_chunks": f"/vault/corporate/{chunks_rel.as_posix()}",
    }

    lines = ["---"]
    lines += [f"{key}: {yaml_scalar(value)}" for key, value in frontmatter.items()]
    lines += ["---", ""]

    for record in records:
        page = record["locator"]["page_number"]
        locator = f" | page {page}" if page is not None else ""
        lines.append(
            f"<!-- chunk {record['chunk_index'] + 1}/{record['chunk_count']}"
            f"{locator} | {record['char_count']} chars -->"
        )
        lines.append("")
        is_table = record["type"] in {"Table", "TableChunk"} and record["text_as_html"]
        lines.append(record["text_as_html"] if is_table else _as_heading(record["text"]))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- pipeline


def resolve_output_paths(cfg: Config, ledger: Ledger, relpath: Path, sha: str) -> tuple[Path, Path, str]:
    """Pick collision-free vault paths for a source file's Markdown and JSONL output."""
    parts = [slugify(part) for part in relpath.parent.parts if part not in {".", ""}]
    stem = slugify(relpath.stem)
    slug = "/".join(parts + [stem])

    doc_rel = Path("documents", *parts, f"{stem}.md")
    if (cfg.output_dir / doc_rel).exists():
        owner = ledger.entries.get(relpath.as_posix(), {})
        if owner.get("document") != doc_rel.as_posix():
            # A different source file already claims this slug — disambiguate by content.
            stem = f"{stem}-{sha[:8]}"
            slug = "/".join(parts + [stem])
            doc_rel = Path("documents", *parts, f"{stem}.md")

    chunks_rel = Path("chunks", *parts, f"{stem}.chunks.jsonl")
    return doc_rel, chunks_rel, slug


def process_file(cfg: Config, session: requests.Session, ledger: Ledger, path: Path) -> bool:
    relpath = path.relative_to(cfg.inbox_dir)
    rel_key = relpath.as_posix()
    stat = path.stat()
    sha = sha256_file(path)

    if ledger.is_current(rel_key, sha, cfg.reprocess_changed):
        return False

    LOG.info("processing %s (%.1f KiB)", rel_key, stat.st_size / 1024)
    started = time.monotonic()
    doc_rel, chunks_rel, slug = resolve_output_paths(cfg, ledger, relpath, sha)

    src = SourceInfo(
        filename=path.name,
        relpath=rel_key,
        sha256=sha,
        filetype=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        slug=slug,
    )

    try:
        elements = [element for element in partition(cfg, session, path) if has_content(element)]
        if not elements:
            raise PartitionError(
                "partitioning produced no text. For a PDF this usually means it is a scan "
                "with no text layer — retry with PARTITION_STRATEGY=hi_res or ocr_only."
            )
        records = build_chunk_records(cfg, elements, src)

        doc_path = cfg.output_dir / doc_rel
        chunks_path = cfg.output_dir / chunks_rel
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        chunks_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to a temp file then rename, so an ingestion run never sees a partial doc.
        _atomic_write(doc_path, render_markdown(cfg, records, src, doc_rel, chunks_rel))
        _atomic_write(chunks_path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    except PartitionError as exc:
        LOG.error("failed %s: %s", rel_key, exc)
        _quarantine(cfg, path, relpath, exc)
        ledger.record(
            rel_key,
            {"source_sha256": sha, "status": "failed", "error": str(exc), "at": now_iso()},
        )
        return False

    elapsed = time.monotonic() - started
    chars = sum(r["char_count"] for r in records)
    LOG.info(
        "wrote %s (%d chunks, %d chars) in %.1fs",
        doc_rel.as_posix(),
        len(records),
        chars,
        elapsed,
    )

    entry = {
        "source_sha256": sha,
        "status": "ok",
        "document": doc_rel.as_posix(),
        "chunks": chunks_rel.as_posix(),
        "chunk_count": len(records),
        "char_count": chars,
        "duration_seconds": round(elapsed, 2),
        "at": now_iso(),
    }
    ledger.record(rel_key, entry)
    _append_manifest(cfg, {"source": rel_key, **entry})

    if cfg.archive_processed:
        _relocate(path, cfg.archive_dir / relpath)
    return True


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _relocate(path: Path, target: Path) -> Path:
    """Move a file, side-stepping name collisions. Returns where it actually landed."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target = target.with_name(f"{target.stem}-{int(time.time())}{target.suffix}")
    shutil.move(str(path), str(target))
    return target


def _quarantine(cfg: Config, path: Path, relpath: Path, exc: Exception) -> None:
    landed = _relocate(path, cfg.failed_dir / relpath)
    note = landed.with_suffix(landed.suffix + ".error.txt")
    note.write_text(f"{now_iso()}\n{exc}\n", encoding="utf-8")


def _append_manifest(cfg: Config, entry: dict) -> None:
    cfg.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with cfg.manifest_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def scan_inbox(cfg: Config, warned: set[str]) -> list[Path]:
    if not cfg.inbox_dir.exists():
        return []
    ready, cutoff = [], time.time() - QUIET_PERIOD_SECONDS
    for path in sorted(cfg.inbox_dir.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            key = path.relative_to(cfg.inbox_dir).as_posix()
            if key not in warned:
                LOG.warning("skipping unsupported file type %s", key)
                warned.add(key)
            continue
        if path.stat().st_mtime > cutoff:
            continue  # still being written; pick it up next pass
        ready.append(path)
    return ready


def wait_for_api(cfg: Config, session: requests.Session, attempts: int = 30) -> None:
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(f"{cfg.api_url}/healthcheck", timeout=10)
            if response.status_code == 200:
                LOG.info("unstructured-api reachable at %s", cfg.api_url)
                return
        except requests.RequestException:
            pass
        LOG.info("waiting for unstructured-api at %s (%d/%d)", cfg.api_url, attempt, attempts)
        time.sleep(min(2 * attempt, 15))
    LOG.warning("unstructured-api not confirmed healthy; continuing anyway")


# --------------------------------------------------------------------------- entrypoint


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-url", help="override UNSTRUCTURED_API_URL")
    parser.add_argument("--inbox", help="override INBOX_DIR")
    parser.add_argument("--output", help="override OUTPUT_DIR (the /vault/corporate root)")
    parser.add_argument("--one-shot", action="store_true", help="process the backlog once and exit")
    parser.add_argument("--no-archive", action="store_true", help="leave processed originals in the inbox")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, _env("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    cfg = Config.from_env()
    if args.api_url:
        cfg.api_url = args.api_url.rstrip("/")
    if args.inbox:
        cfg.inbox_dir = Path(args.inbox)
    if args.output:
        cfg.output_dir = Path(args.output)
    if args.one_shot:
        cfg.one_shot = True
    if args.no_archive:
        cfg.archive_processed = False

    for directory in (cfg.inbox_dir, cfg.archive_dir, cfg.failed_dir, cfg.state_dir,
                      cfg.documents_dir, cfg.chunks_dir):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            LOG.error("cannot create %s: %s", directory, exc)
            return 1

    LOG.info(
        "inbox=%s output=%s strategy=%s chunking=%s max_chars=%d overlap=%d",
        cfg.inbox_dir, cfg.output_dir, cfg.partition_strategy,
        cfg.chunking_strategy, cfg.chunk_max_characters, cfg.chunk_overlap,
    )

    stopping = False

    def _stop(signum, _frame):
        nonlocal stopping
        LOG.info("received signal %s; finishing current file then exiting", signum)
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    ledger = Ledger(cfg.ledger_path)
    warned: set[str] = set()

    with requests.Session() as session:
        wait_for_api(cfg, session)
        while not stopping:
            processed = 0
            for path in scan_inbox(cfg, warned):
                if stopping:
                    break
                try:
                    if process_file(cfg, session, ledger, path):
                        processed += 1
                except OSError as exc:
                    LOG.error("I/O error on %s: %s", path, exc)
            if cfg.one_shot:
                LOG.info("one-shot run complete: %d file(s) processed", processed)
                return 0
            if processed:
                LOG.info("idle: %d file(s) processed this pass", processed)
            for _ in range(cfg.watch_interval):
                if stopping:
                    break
                time.sleep(1)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
