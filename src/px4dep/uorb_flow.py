"""Link uORB declarations, initializers and publish/subscribe call sites."""

from __future__ import annotations

from typing import Any, Iterable

from .config import Config
from .util import stable_id, write_json, write_jsonl


def endpoint_id(commit: str, variant: str, usr: str, qualified_name: str) -> str:
    if usr:
        return stable_id(commit, variant, "uorb_endpoint", usr, "", "")
    return stable_id(commit, variant, "uorb_endpoint", qualified_name, "", "")


def _source(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": record.get("file", ""),
        "line": record.get("line", 0),
        "column": record.get("column", 0),
        "expansion_file": record.get("expansion_file", ""),
        "expansion_line": record.get("expansion_line", 0),
        "spelling_file": record.get("spelling_file", ""),
        "spelling_line": record.get("spelling_line", 0),
    }


def build_uorb_flow(
    cfg: Config,
    raw_records: Iterable[dict[str, Any]],
    topics: Iterable[dict[str, Any]],
    message_types: Iterable[dict[str, Any]],
    function_ids: dict[str, str],
    commit: str = "",
    variant: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    topic_by_name = {t["name"]: t for t in topics}

    declarations: dict[str, dict[str, Any]] = {}
    initializers: dict[str, dict[str, Any]] = {}
    calls: list[dict[str, Any]] = []
    for record in raw_records:
        kind = record.get("record")
        if kind == "uorb_decl":
            declarations[record.get("usr", "")] = record
        elif kind == "uorb_init":
            initializers[record.get("receiver_usr", "")] = record
        elif kind == "uorb_call":
            calls.append(record)

    endpoints: dict[str, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []

    def add_endpoint(usr: str, record: dict[str, Any], declaration_status: str) -> dict[str, Any]:
        qname = record.get("qualified_name") or record.get("receiver") or usr
        eid = endpoint_id(commit, variant, usr, qname)
        node = endpoints.get(eid)
        if node is None:
            topics_list = list(record.get("topics") or [])
            node = {
                "id": eid,
                "kind": "uorb_endpoint",
                "qualified_name": qname,
                "name": record.get("variable") or record.get("receiver") or "",
                "usr": usr,
                "role": record.get("role", ""),
                "uorb_class": record.get("uorb_class", ""),
                "message_type": record.get("message_type", ""),
                "instance_mode": record.get("instance_mode", ""),
                "topics": topics_list,
                "topic_expr": "",
                "declaration_status": declaration_status,
                "build_variant": variant,
                "source": _source(record),
                "tu": record.get("tu", ""),
            }
            endpoints[eid] = node
        else:
            for topic in record.get("topics") or []:
                if topic not in node["topics"]:
                    node["topics"].append(topic)
            if not node.get("message_type") and record.get("message_type"):
                node["message_type"] = record["message_type"]
            if not node.get("role") and record.get("role"):
                node["role"] = record.get("role")
        return node

    for usr, record in declarations.items():
        add_endpoint(usr, record, "declared")

    for usr, record in initializers.items():
        node = add_endpoint(usr, record, "declared" if usr in declarations else "initializer_only")
        node["topic_expr"] = record.get("topic_expr", "")
        if not node.get("uorb_class") and record.get("uorb_class"):
            node["uorb_class"] = record["uorb_class"]
        if not node.get("role") and record.get("role"):
            node["role"] = record["role"]

    for record in calls:
        usr = record.get("receiver_usr", "")
        if usr not in declarations and usr not in initializers:
            add_endpoint(usr, record, "call_only")

    edges: list[dict[str, Any]] = []
    topic_edge_count = 0
    for node in endpoints.values():
        eid = node["id"]
        role = node.get("role", "")
        relation = "publishes" if role == "publication" else "subscribes"
        if node.get("instance_mode") == "multi" and role == "publication":
            relation = "publishes"
        if not node.get("topics"):
            unresolved.append(
                {
                    "kind": "uorb_endpoint_without_topic",
                    "endpoint": node["qualified_name"],
                    "usr": node.get("usr", ""),
                    "file": node["source"]["file"],
                    "line": node["source"]["line"],
                }
            )
            continue
        if not node.get("message_type"):
            first_topic = node["topics"][0]
            topic_node = topic_by_name.get(first_topic)
            if topic_node:
                node["message_type"] = topic_node.get("message_type", "")
        for topic in node["topics"]:
            topic_node = topic_by_name.get(topic)
            if not topic_node:
                unresolved.append(
                    {
                        "kind": "uorb_topic_not_found",
                        "topic": topic,
                        "endpoint": node["qualified_name"],
                        "usr": node.get("usr", ""),
                        "file": node["source"]["file"],
                        "line": node["source"]["line"],
                    }
                )
                continue
            confidence = "high" if len(node["topics"]) == 1 else "medium"
            topic_edge_count += 1
            edges.append(
                {
                    "id": stable_id(commit, variant, "edge", relation, eid, topic_node["id"]),
                    "kind": "edge",
                    "relation": relation,
                    "src": eid,
                    "dst": topic_node["id"],
                    "build_variant": variant,
                    "evidence": {
                        "rule": "uorb_declaration_topic",
                        "file": node["source"]["file"],
                        "line": node["source"]["line"],
                        "topic_expr": node.get("topic_expr", ""),
                    },
                    "confidence": confidence,
                }
            )

    operation_edges = 0
    call_counts: dict[str, int] = {}
    for record in calls:
        operation = record.get("operation", "")
        call_counts[operation] = call_counts.get(operation, 0) + 1
        usr = record.get("receiver_usr", "")
        qname = record.get("receiver", "") or usr
        eid = endpoint_id(commit, variant, usr, qname)
        endpoint = endpoints.get(eid)
        if endpoint is None:
            unresolved.append(
                {
                    "kind": "uorb_call_unresolved_receiver",
                    "operation": operation,
                    "receiver": record.get("receiver", ""),
                    "receiver_type": record.get("receiver_type", ""),
                    "caller": record.get("caller", ""),
                    "file": record.get("file", ""),
                    "line": record.get("line", 0),
                }
            )
            continue
        caller_id = function_ids.get(record.get("caller_usr", ""))
        if not caller_id:
            unresolved.append(
                {
                    "kind": "uorb_call_caller_not_indexed",
                    "operation": operation,
                    "receiver": record.get("receiver", ""),
                    "caller": record.get("caller", ""),
                    "caller_usr": record.get("caller_usr", ""),
                    "file": record.get("file", ""),
                    "line": record.get("line", 0),
                }
            )
            continue
        relation = "selects_instance" if operation in {"ChangeInstance", "change_instance"} else "uses_uorb"
        operation_edges += 1
        edges.append(
            {
                "id": stable_id(
                    commit, variant, "edge", relation, caller_id, eid,
                    record.get("file", ""), record.get("line", 0), record.get("column", 0),
                ),
                "kind": "edge",
                "relation": relation,
                "src": caller_id,
                "dst": eid,
                "build_variant": variant,
                "evidence": {
                    "rule": "uorb_member_call",
                    "operation": operation,
                    "file": record.get("file", ""),
                    "line": record.get("line", 0),
                    "argument": record.get("argument", ""),
                },
                "confidence": "high" if operation != "ChangeInstance" else "medium",
            }
        )

    nodes = list(endpoints.values())
    metrics = {
        "uorb_declarations": len(declarations),
        "uorb_initializers": len(initializers),
        "uorb_calls": len(calls),
        "uorb_endpoints": len(nodes),
        "endpoints_declared": sum(1 for n in nodes if n["declaration_status"] == "declared"),
        "endpoints_initializer_only": sum(1 for n in nodes if n["declaration_status"] == "initializer_only"),
        "endpoints_call_only": sum(1 for n in nodes if n["declaration_status"] == "call_only"),
        "endpoint_topic_edges": topic_edge_count,
        "function_endpoint_edges": operation_edges,
        "calls_by_operation": call_counts,
        "unresolved": len(unresolved),
    }
    return nodes, edges, metrics, unresolved


def write_uorb_flow(
    run,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    metrics: dict[str, Any],
    unresolved: list[dict[str, Any]],
) -> None:
    write_jsonl(run.path("normalized", "uorb_endpoints.jsonl"), nodes)
    write_jsonl(run.path("normalized", "uorb_flow_edges.jsonl"), edges)
    write_jsonl(run.path("unresolved", "uorb_flow.jsonl"), unresolved)
    write_json(run.path("reports", "uorb_flow_metrics.json"), metrics)
