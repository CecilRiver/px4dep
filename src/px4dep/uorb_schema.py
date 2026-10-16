"""Parse PX4 uORB .msg schemas into message/topic/field entities.

The generated C++ struct name is not the file stem: PX4 converts the message
name to snake_case and appends ``_s`` (``EstimatorAidSource1d`` ->
``estimator_aid_source1d_s``).  Topics are the ``# TOPICS`` aliases, or the
snake_case message name when the file declares no aliases.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .util import repo_rel, stable_id, write_json, write_jsonl


PRIMITIVE_TYPES = {
    "uint8", "uint16", "uint32", "uint64",
    "int8", "int16", "int32", "int64",
    "float32", "float64",
    "bool", "char",
}

_TOPIC_RE = re.compile(r"^\s*#\s*TOPICS\b(.*)$")
_FIELD_RE = re.compile(
    r"^(?P<type>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\[(?P<array>\d+)\])?"
    r"\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)$"
)
_CONST_RE = re.compile(
    r"^(?P<type>[A-Za-z_][A-Za-z0-9_]*)"
    r"\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"\s*=\s*(?P<value>.+)$"
)


def camel_to_snake(name: str) -> str:
    """PX4 message-name conversion: ``EstimatorAidSource1d`` -> ``estimator_aid_source1d``."""
    step1 = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    step2 = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", step1)
    return step2.lower()


def message_cpp_type(stem: str) -> str:
    return camel_to_snake(stem) + "_s"


def _strip_comment(line: str) -> tuple[str, str]:
    code, sep, comment = line.partition("#")
    return code.strip(), comment.strip() if sep else ""


def parse_msg_file(path: Path, repo_root: Path | None = None) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    fields: list[dict[str, Any]] = []
    constants: list[dict[str, Any]] = []
    topics: list[str] = []
    topic_lines: list[int] = []
    parse_errors: list[dict[str, Any]] = []

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        topic_match = _TOPIC_RE.match(raw_line)
        if topic_match:
            for topic in topic_match.group(1).split():
                if topic and topic not in topics:
                    topics.append(topic)
                    topic_lines.append(lineno)
            continue

        code, comment = _strip_comment(raw_line)
        if not code:
            continue

        const = _CONST_RE.match(code)
        if const:
            constants.append(
                {
                    "name": const.group("name"),
                    "type": const.group("type"),
                    "value": const.group("value").strip(),
                    "line": lineno,
                    "comment": comment,
                }
            )
            continue

        field = _FIELD_RE.match(code)
        if field:
            base_type = field.group("type")
            array = field.group("array")
            fields.append(
                {
                    "name": field.group("name"),
                    "type": base_type,
                    "array_size": int(array) if array else None,
                    "is_array": bool(array),
                    "is_nested": base_type not in PRIMITIVE_TYPES,
                    "line": lineno,
                    "column": raw_line.index(field.group("name")) + 1,
                    "comment": comment,
                }
            )
            continue

        parse_errors.append({"line": lineno, "text": raw_line, "reason": "unparsed_msg_line"})

    stem = path.stem
    cpp_type = message_cpp_type(stem)
    if not topics:
        topics = [camel_to_snake(stem)]
        topic_lines = [1]

    return {
        "name": stem,
        "cpp_type": cpp_type,
        "cpp_alias": f"px4::msg::{stem}",
        "file": repo_rel(path, repo_root) if repo_root else path.as_posix(),
        "topics": topics,
        "topic_lines": topic_lines,
        "fields": fields,
        "constants": constants,
        "parse_errors": parse_errors,
    }


def discover_msg_files(cfg: Config) -> list[Path]:
    return cfg.msg_files


def build_schema(
    cfg: Config,
    commit: str = "",
    variant: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Return (nodes, edges, metrics, unresolved)."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    parsed: list[dict[str, Any]] = []
    for path in discover_msg_files(cfg):
        record = parse_msg_file(path, cfg.repo)
        record["path"] = path
        parsed.append(record)

    by_cpp_type = {m["cpp_type"]: m for m in parsed}
    by_name = {m["name"]: m for m in parsed}
    topic_owner: dict[str, str] = {}
    for message in parsed:
        for topic in message["topics"]:
            if topic in topic_owner:
                unresolved.append(
                    {
                        "kind": "duplicate_topic_alias",
                        "topic": topic,
                        "first_message": topic_owner[topic],
                        "second_message": message["cpp_type"],
                        "file": message["file"],
                    }
                )
                continue
            topic_owner[topic] = message["cpp_type"]

    for message in parsed:
        cpp_type = message["cpp_type"]
        message_id = stable_id(commit, variant, "message_type", cpp_type, "", "")
        nodes.append(
            {
                "id": message_id,
                "kind": "message_type",
                "qualified_name": cpp_type,
                "name": message["name"],
                "cpp_type": cpp_type,
                "cpp_alias": message["cpp_alias"],
                "topics": message["topics"],
                "field_count": len(message["fields"]),
                "constant_count": len(message["constants"]),
                "build_variant": variant,
                "source": {
                    "file": message["file"],
                    "line": 1,
                    "column": 1,
                    "spelling_file": message["file"],
                    "spelling_line": 1,
                    "spelling_column": 1,
                },
            }
        )

        for topic, topic_line in zip(message["topics"], message["topic_lines"]):
            topic_id = stable_id(commit, variant, "topic", topic, "", "")
            nodes.append(
                {
                    "id": topic_id,
                    "kind": "topic",
                    "qualified_name": topic,
                    "name": topic,
                    "message_type": cpp_type,
                    "aliases": [topic],
                    "primary_alias": message["topics"][0],
                    "build_variant": variant,
                    "source": {
                        "file": message["file"],
                        "line": topic_line,
                        "column": 1,
                        "spelling_file": message["file"],
                        "spelling_line": topic_line,
                        "spelling_column": 1,
                    },
                }
            )
            edges.append(
                {
                    "id": stable_id(commit, variant, "edge", "instantiates", topic_id, message_id),
                    "kind": "edge",
                    "relation": "instantiates",
                    "src": topic_id,
                    "dst": message_id,
                    "build_variant": variant,
                    "evidence": {
                        "rule": "uorb_topic_alias",
                        "file": message["file"],
                        "line": topic_line,
                    },
                    "confidence": "high",
                }
            )

        for field in message["fields"]:
            field_qname = f"{cpp_type}::{field['name']}"
            field_id = stable_id(
                commit, variant, "topic_field", field_qname, message["file"], field["line"]
            )
            field_type = (
                message_cpp_type(field["type"]) if field["is_nested"] else field["type"]
            )
            nodes.append(
                {
                    "id": field_id,
                    "kind": "topic_field",
                    "qualified_name": field_qname,
                    "name": field["name"],
                    "message_type": cpp_type,
                    "field_type": field["type"],
                    "field_cpp_type": field_type,
                    "is_nested": field["is_nested"],
                    "is_array": field["is_array"],
                    "array_size": field["array_size"],
                    "is_constant": False,
                    "build_variant": variant,
                    "source": {
                        "file": message["file"],
                        "line": field["line"],
                        "column": field["column"],
                        "spelling_file": message["file"],
                        "spelling_line": field["line"],
                        "spelling_column": field["column"],
                    },
                }
            )
            edges.append(
                {
                    "id": stable_id(commit, variant, "edge", "defines", message_id, field_id),
                    "kind": "edge",
                    "relation": "defines",
                    "src": message_id,
                    "dst": field_id,
                    "build_variant": variant,
                    "evidence": {
                        "rule": "msg_field_declaration",
                        "file": message["file"],
                        "line": field["line"],
                    },
                    "confidence": "high",
                }
            )
            if field["is_nested"]:
                nested = by_cpp_type.get(field_type)
                if nested:
                    nested_id = stable_id(commit, variant, "message_type", field_type, "", "")
                    edges.append(
                        {
                            "id": stable_id(
                                commit, variant, "edge", "field_type", field_id, nested_id
                            ),
                            "kind": "edge",
                            "relation": "field_type",
                            "src": field_id,
                            "dst": nested_id,
                            "build_variant": variant,
                            "evidence": {
                                "rule": "msg_nested_type",
                                "file": message["file"],
                                "line": field["line"],
                            },
                            "confidence": "high",
                        }
                    )
                else:
                    unresolved.append(
                        {
                            "kind": "nested_message_type_not_found",
                            "message_type": cpp_type,
                            "field": field["name"],
                            "field_type": field_type,
                            "file": message["file"],
                            "line": field["line"],
                        }
                    )

        for constant in message["constants"]:
            constant_qname = f"{cpp_type}::{constant['name']}"
            constant_id = stable_id(
                commit, variant, "topic_field", constant_qname, message["file"], constant["line"]
            )
            nodes.append(
                {
                    "id": constant_id,
                    "kind": "topic_field",
                    "qualified_name": constant_qname,
                    "name": constant["name"],
                    "message_type": cpp_type,
                    "field_type": constant["type"],
                    "field_cpp_type": constant["type"],
                    "is_nested": False,
                    "is_array": False,
                    "array_size": None,
                    "is_constant": True,
                    "constant_value": constant["value"],
                    "build_variant": variant,
                    "source": {
                        "file": message["file"],
                        "line": constant["line"],
                        "column": 1,
                        "spelling_file": message["file"],
                        "spelling_line": constant["line"],
                        "spelling_column": 1,
                    },
                }
            )
            edges.append(
                {
                    "id": stable_id(commit, variant, "edge", "defines", message_id, constant_id),
                    "kind": "edge",
                    "relation": "defines",
                    "src": message_id,
                    "dst": constant_id,
                    "build_variant": variant,
                    "evidence": {
                        "rule": "msg_constant_declaration",
                        "file": message["file"],
                        "line": constant["line"],
                    },
                    "confidence": "high",
                }
            )

        for error in message["parse_errors"]:
            unresolved.append(
                {
                    "kind": "msg_parse_error",
                    "message_type": cpp_type,
                    "file": message["file"],
                    **error,
                }
            )

    metrics = {
        "msg_files": len(parsed),
        "message_types": sum(1 for n in nodes if n["kind"] == "message_type"),
        "topics": sum(1 for n in nodes if n["kind"] == "topic"),
        "fields": sum(1 for n in nodes if n["kind"] == "topic_field" and not n["is_constant"]),
        "constants": sum(1 for n in nodes if n["kind"] == "topic_field" and n["is_constant"]),
        "nested_field_edges": sum(1 for e in edges if e["relation"] == "field_type"),
        "msg_parse_errors": sum(1 for u in unresolved if u["kind"] == "msg_parse_error"),
        "duplicate_topic_aliases": sum(1 for u in unresolved if u["kind"] == "duplicate_topic_alias"),
        "per_file": {
            m["cpp_type"]: {
                "file": m["file"],
                "topics": len(m["topics"]),
                "fields": len(m["fields"]),
                "constants": len(m["constants"]),
            }
            for m in parsed
        },
    }
    return nodes, edges, metrics, unresolved


def write_schema(
    run,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    metrics: dict[str, Any],
    unresolved: list[dict[str, Any]],
) -> None:
    write_jsonl(run.path("normalized", "uorb_messages.jsonl"), (n for n in nodes if n["kind"] == "message_type"))
    topic_nodes = [n for n in nodes if n["kind"] == "topic"]
    write_jsonl(run.path("normalized", "uorb_topics.jsonl"), topic_nodes)
    write_jsonl(run.path("normalized", "topics.jsonl"), topic_nodes)
    write_jsonl(run.path("normalized", "uorb_fields.jsonl"), (n for n in nodes if n["kind"] == "topic_field"))
    write_jsonl(run.path("normalized", "uorb_schema_edges.jsonl"), edges)
    write_jsonl(run.path("unresolved", "uorb_schema.jsonl"), unresolved)
    write_json(run.path("reports", "uorb_metrics.json"), metrics)
