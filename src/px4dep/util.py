"""Shared helpers for the PX4 dependency experiment."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str | os.PathLike[str]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_files(
    paths: Iterable[str | os.PathLike[str]],
    root: str | os.PathLike[str] | None = None,
) -> str:
    """Aggregate hash over sorted (path, content hash) pairs.

    When ``root`` is given, paths are reduced to root-relative POSIX names so
    the aggregate stays stable across machines.
    """
    h = hashlib.sha256()
    entries = []
    for path in paths:
        p = Path(path)
        name = repo_rel(p, root) if root is not None else str(p)
        entries.append((name, sha256_file(p)))
    for name, digest in sorted(entries):
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(digest.encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def repo_rel(path: str | os.PathLike[str], repo_root: str | os.PathLike[str]) -> str:
    p = Path(path).resolve()
    root = Path(repo_root).resolve()
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


def stable_id(*parts: Any) -> str:
    """Stable entity/edge id from semantic parts, never from display names alone."""
    payload = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def read_jsonl(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    p = Path(path)
    if not p.exists():
        return records
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def write_jsonl(
    path: str | os.PathLike[str],
    records: Iterable[dict[str, Any]],
    append: bool = False,
) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with p.open("a" if append else "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
            count += 1
    return count


def write_json(path: str | os.PathLike[str], obj: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def read_json(path: str | os.PathLike[str], default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def excerpt_hash(path: str | os.PathLike[str], line: int, radius: int = 0) -> str:
    """Hash a source excerpt without copying production code into the results."""
    p = Path(path)
    if not p.exists() or line <= 0:
        return ""
    lines: list[str] = []
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for idx, text in enumerate(fh, start=1):
                if line - radius <= idx <= line + radius:
                    lines.append(text)
                if idx > line + radius:
                    break
    except OSError:
        return ""
    return sha256_text("".join(lines))
