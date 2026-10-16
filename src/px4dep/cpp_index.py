"""Build/run the Clang LibTooling indexer and normalize its JSONL output."""

from __future__ import annotations

import json
import os
import posixpath
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .util import sha256_file, write_json, write_jsonl


PATH_FIELDS = ("file", "expansion_file", "spelling_file", "callee_file", "tu")


def _under_repo(path: str, cfg: Config) -> str:
    if not path:
        return path
    if os.path.isabs(path):
        try:
            return Path(path).resolve().relative_to(cfg.repo.resolve()).as_posix()
        except ValueError:
            return Path(path).as_posix()
    return path


def normalize_repo_path(path: str, cfg: Config) -> str:
    """Normalize repo-relative paths; PresumedLoc can contain ``..`` segments."""
    if not path:
        return path
    path = _under_repo(path, cfg)
    if path.startswith("<") or path.startswith("/usr/"):
        return path
    normalized = posixpath.normpath(path)
    if normalized == ".":
        return ""
    return normalized


def normalize_record(record: dict[str, Any], cfg: Config) -> dict[str, Any]:
    out = dict(record)
    for field in PATH_FIELDS:
        if field in out and isinstance(out[field], str):
            out[field] = normalize_repo_path(out[field], cfg)
    out.setdefault("build_variant", cfg.build_variant)
    return out


def _dedup_key(record: dict[str, Any]) -> tuple[Any, ...]:
    kind = record.get("record")
    if kind == "entity":
        return (kind, record.get("kind"), record.get("usr") or record.get("qualified_name"),
                record.get("file"), record.get("line"))
    if kind == "param_wrapper":
        return (kind, record.get("usr"), record.get("parameter_name"))
    if kind == "parameter_use":
        return (kind, record.get("caller_usr"), record.get("qualified_name"),
                record.get("file"), record.get("line"), record.get("column"))
    if kind == "binding":
        return (kind, record.get("src_usr"), record.get("dst_usr"),
                record.get("file"), record.get("line"))
    if kind == "uorb_decl":
        return (kind, record.get("usr"), record.get("file"), record.get("line"))
    if kind == "uorb_init":
        return (kind, record.get("receiver_usr"), record.get("topic_expr"),
                record.get("file"), record.get("line"))
    if kind == "uorb_call":
        return (kind, record.get("caller_usr"), record.get("receiver_usr"),
                record.get("operation"), record.get("file"), record.get("line"))
    if kind == "call":
        return (kind, record.get("caller_usr"), record.get("callee_usr"),
                record.get("api"), record.get("file"), record.get("line"))
    if kind == "health_call":
        return (kind, record.get("caller_usr"), record.get("method"),
                record.get("file"), record.get("line"), record.get("column"))
    if kind == "mode_check":
        return (kind, record.get("caller_usr"), record.get("method"),
                record.get("mode_expr"), record.get("file"), record.get("line"),
                record.get("column"))
    if kind == "failsafe_check":
        return (kind, record.get("caller_usr"), record.get("macro"),
                record.get("flag"), record.get("file"), record.get("line"),
                record.get("column"))
    if kind == "fallback_check":
        return (kind, record.get("caller_usr"), record.get("mode_expr"),
                record.get("file"), record.get("line"), record.get("column"))
    if kind == "flag_access":
        return (kind, record.get("caller_usr"), record.get("field"),
                record.get("access"), record.get("file"), record.get("line"),
                record.get("column"))
    if kind == "action_ref":
        return (kind, record.get("caller_usr"), record.get("action"),
                record.get("context"), record.get("file"), record.get("line"),
                record.get("column"))
    if kind == "enum_value":
        return (kind, record.get("enum_name"), record.get("value_name"),
                record.get("file"), record.get("line"))
    if kind == "member_access":
        return (kind, record.get("caller_usr"), record.get("qualified_name"),
                record.get("access"), record.get("receiver_expr"),
                record.get("file"), record.get("line"), record.get("column"))
    if kind == "local_write":
        return (kind, record.get("caller_usr"), record.get("variable"),
                record.get("write_kind"), record.get("file"), record.get("line"),
                record.get("column"))
    if kind == "call_arg":
        return (kind, record.get("caller_usr"), record.get("callee_usr"),
                record.get("arg_index"), record.get("file"), record.get("line"),
                record.get("column"))
    if kind == "call_result":
        return (kind, record.get("caller_usr"), record.get("callee_usr"),
                record.get("target_kind"), record.get("target_name"),
                record.get("target_field"), record.get("file"), record.get("line"),
                record.get("column"))
    if kind == "return_ref":
        return (kind, record.get("caller_usr"), record.get("file"),
                record.get("line"), record.get("column"))
    return (kind, json.dumps(record, sort_keys=True, ensure_ascii=False))


def normalize_records(raw: Iterable[dict[str, Any]], cfg: Config) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    duplicates = 0
    for record in raw:
        record = normalize_record(record, cfg)
        key = _dedup_key(record)
        if key in seen:
            duplicates += 1
            existing = seen[key]
            # Merge topic lists; the same declaration can be seen in several TUs.
            if record.get("record") in {"uorb_decl", "uorb_init"}:
                topics = list(existing.get("topics") or [])
                for topic in record.get("topics") or []:
                    if topic not in topics:
                        topics.append(topic)
                existing["topics"] = topics
                tus = existing.get("tus") or [existing.get("tu", "")]
                tus.append(record.get("tu", ""))
                existing["tus"] = sorted({t for t in tus if t})
            continue
        seen[key] = record
    records = list(seen.values())
    metrics = {
        "raw_records": len(list(raw)) if isinstance(raw, list) else None,
        "duplicate_records_removed": duplicates,
        "normalized_records": len(records),
        "by_record": dict(Counter(r.get("record", "") for r in records)),
        "by_tu": dict(Counter(r.get("tu", "") for r in records)),
    }
    return records, metrics


def ensure_tool(cfg: Config, run=None, rebuild: bool = False) -> dict[str, Any]:
    tool = cfg.clang_tool
    if tool.exists() and not rebuild:
        return {"built": False, "path": str(tool), "sha256": sha256_file(tool)}
    if not cfg.clang_build_script.exists():
        raise FileNotFoundError(f"clang tool build script not found: {cfg.clang_build_script}")

    log_lines: list[str] = []
    proc = subprocess.run(
        ["bash", str(cfg.clang_build_script)],
        cwd=str(cfg.project_root),
        check=False,
        capture_output=True,
        text=True,
    )
    log_lines.extend(proc.stdout.splitlines())
    log_lines.extend(proc.stderr.splitlines())
    if run is not None:
        (run.path("logs", "clang_build.log")).write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    if proc.returncode != 0 or not tool.exists():
        tail = "\n".join(log_lines[-40:])
        raise RuntimeError(f"clang tool build failed (exit {proc.returncode})\n{tail}")
    return {"built": True, "path": str(tool), "sha256": sha256_file(tool), "log_tail": log_lines[-10:]}


def _compile_files(cfg: Config) -> set[str]:
    if not cfg.compile_commands.exists():
        return set()
    try:
        data = json.loads(cfg.compile_commands.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {normalize_repo_path(entry.get("file", ""), cfg) for entry in data}


def run_tool(cfg: Config, run, rebuild: bool = False) -> dict[str, Any]:
    """Run the tool for every configured TU and write normalized raw records."""
    tool_info = ensure_tool(cfg, run, rebuild=rebuild)
    raw_path = run.path("raw", "cpp_records.jsonl")
    log_path = run.path("logs", "clang_tool.log")

    compile_files = _compile_files(cfg)
    tus: list[str] = []
    skipped: list[dict[str, Any]] = []
    for tu in cfg.tus:
        path = cfg.repo / tu
        if not path.exists():
            skipped.append({"tu": tu, "reason": "file_not_found"})
            continue
        if compile_files and tu not in compile_files:
            skipped.append({"tu": tu, "reason": "not_in_compile_commands"})
            continue
        tus.append(tu)
    if not tus:
        raise RuntimeError("no configured translation units are present in compile_commands.json")

    if raw_path.exists():
        raw_path.unlink()
    command = [
        str(cfg.clang_tool),
        "-p", str(cfg.build_dir),
        "--output", str(raw_path),
        "--repo-root", str(cfg.repo),
        "--tool-version", "0.1.0",
    ]
    for scope in cfg.scope_dirs:
        command.extend(["--scope", scope])
    command.extend(str(cfg.repo / tu) for tu in tus)

    proc = subprocess.run(command, cwd=str(cfg.repo), check=False, capture_output=True, text=True)
    log_text = "$ " + " ".join(command) + "\n\n" + proc.stdout + proc.stderr
    log_path.write_text(log_text, encoding="utf-8")
    if proc.returncode != 0:
        tail = "\n".join(log_text.splitlines()[-40:])
        raise RuntimeError(f"clang tool failed (exit {proc.returncode})\n{tail}")

    raw_records: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    if raw_path.exists():
        with raw_path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw_records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    parse_errors.append({"line": lineno, "error": str(exc), "text": line[:500]})

    records, metrics = normalize_records(raw_records, cfg)
    metrics.update(
        {
            "tool": tool_info,
            "command": command,
            "tus_run": tus,
            "tus_skipped": skipped,
            "raw_output": str(raw_path),
            "raw_output_sha256": sha256_file(raw_path) if raw_path.exists() else "",
            "record_parse_errors": len(parse_errors),
        }
    )
    write_jsonl(raw_path, records)
    write_jsonl(run.path("unresolved", "cpp_records.jsonl"), parse_errors)
    write_json(run.path("reports", "cpp_metrics.json"), metrics)
    return metrics


def load_raw(cfg: Config, run) -> list[dict[str, Any]]:
    path = run.path("raw", "cpp_records.jsonl")
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
