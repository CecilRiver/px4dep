"""Merge normalized records into a validated evidence graph."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable

from .config import Config
from .commander_slice import build_commander_slice, write_commander_slice
from .cross_module import (
    build_call_edges,
    build_cross_module,
    write_cross_module,
)
from .ekf_slice import build_ekf_slice, write_ekf_slice_csv
from .guards import GuardIndex
from .health_chain import build_health_chain, write_health_chain_csv
from .param_bindings import (
    build_parameter_usage,
    build_wrappers,
    parameter_id,
    wrapper_id,
)
from .uorb_flow import build_uorb_flow, endpoint_id
from .util import stable_id, write_json, write_jsonl


CPP_KINDS = {"function", "field", "config_field"}
HEALTH_KINDS = {"predicate", "health_flag", "mode_capability", "protection_decision", "health_component"}
SLICE_KINDS = {"estimator_flag", "slice_sink"}
SLICE_RELATIONS = {"data_depends_on", "control_depends_on"}
HEALTH_RELATIONS = {
    "reports_health",
    "reports_flag",
    "reports_component",
    "writes_flag",
    "reads_flag",
    "maps_to_field",
    "requires_mode",
    "restricts_arming",
    "restricts_run",
    "checks_mode",
    "prevents_mode",
    "queries_mode",
    "has_mode_check",
    "has_protection_condition",
    "selects_action",
    "returns_action",
    "assigns_action",
}


def _normalized(path: str) -> str:
    return path.strip("/")


def _excluded(cfg: Config, path: str) -> bool:
    if not path:
        return False
    path = _normalized(path)
    for excluded in cfg.exclude_dirs:
        excluded = _normalized(excluded)
        if path == excluded or path.startswith(excluded + "/"):
            return True
    return False


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


def build_cpp_entities(
    cfg: Config,
    raw_records: Iterable[dict[str, Any]],
    commit: str = "",
    variant: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]], dict[str, str], dict[str, str]]:
    """Build function/field/config_field nodes from Clang entity records.

    Returns (nodes, metrics, unresolved, function_ids, config_field_ids).
    """
    guards = GuardIndex(cfg)
    nodes: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    function_ids: dict[str, str] = {}
    config_field_ids: dict[str, str] = {}
    seen: set[str] = set()

    for record in raw_records:
        if record.get("record") != "entity":
            continue
        kind = record.get("kind")
        if kind not in {"function", "field"}:
            continue
        file = record.get("file", "")
        if _excluded(cfg, file):
            continue
        usr = record.get("usr", "")
        qname = record.get("qualified_name", "")

        if kind == "function":
            node_kind = "function"
            node_id = stable_id(commit, variant, "function", usr or qname, "", "")
        else:
            parent = record.get("parent_class", "")
            is_config = parent.endswith("::parameters") or parent == "parameters" or (
                "::parameters::" in qname
            )
            node_kind = "config_field" if is_config else "field"
            node_id = stable_id(commit, variant, node_kind, usr or qname, "", "")

        if node_id in seen:
            continue
        seen.add(node_id)

        node = {
            "id": node_id,
            "kind": node_kind,
            "qualified_name": qname,
            "name": qname.rsplit("::", 1)[-1],
            "usr": usr,
            "type": record.get("type", ""),
            "parent_class": record.get("parent_class", ""),
            "build_variant": variant,
            "build_guards": guards.for_record(record),
            "build_guard_status": guards.status_for_record(record),
            "source": _source(record),
            "tu": record.get("tu", ""),
        }
        if kind == "function":
            node["is_method"] = bool(record.get("is_method", False))
            node["is_static"] = bool(record.get("is_static", False))
            function_ids[usr or qname] = node_id
        else:
            node["is_mutable"] = bool(record.get("is_mutable", False))
            if node_kind == "config_field":
                config_field_ids[usr or qname] = node_id
        nodes.append(node)

    metrics = {
        "cpp_entities": len(nodes),
        "functions": sum(1 for n in nodes if n["kind"] == "function"),
        "fields": sum(1 for n in nodes if n["kind"] == "field"),
        "config_fields": sum(1 for n in nodes if n["kind"] == "config_field"),
        "unresolved": len(unresolved),
    }
    return nodes, metrics, unresolved, function_ids, config_field_ids


def attach_entity_guards(cfg: Config, nodes: Iterable[dict[str, Any]]) -> None:
    guards = GuardIndex(cfg)
    for node in nodes:
        if node.get("build_guards") is not None:
            continue
        source = node.get("source") or {}
        file = source.get("spelling_file") or source.get("file") or ""
        line = int(source.get("spelling_line") or source.get("line") or 0)
        if not file or line <= 0:
            node["build_guards"] = []
            node["build_guard_status"] = "none"
            continue
        guard_list, status = guards.guards_for(file, line)
        node["build_guards"] = guard_list
        node["build_guard_status"] = status


def merge_and_validate(
    cfg: Config,
    nodes: Iterable[dict[str, Any]],
    edges: Iterable[dict[str, Any]],
    extra_unresolved: Iterable[dict[str, Any]] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    unresolved = list(extra_unresolved)
    nodes = [n for n in nodes if not _excluded(cfg, (n.get("source") or {}).get("file", ""))]

    by_id: dict[str, dict[str, Any]] = {}
    duplicates = 0
    for node in nodes:
        nid = node.get("id", "")
        if not nid:
            unresolved.append({"kind": "node_without_id", "node": node})
            continue
        if nid in by_id:
            duplicates += 1
            continue
        by_id[nid] = node

    edge_by_id: dict[str, dict[str, Any]] = {}
    edge_duplicates = 0
    missing_endpoints = 0
    for edge in edges:
        eid = edge.get("id", "")
        if not eid:
            unresolved.append({"kind": "edge_without_id", "edge": edge})
            continue
        if eid in edge_by_id:
            edge_duplicates += 1
            continue
        if edge.get("src") not in by_id or edge.get("dst") not in by_id:
            missing_endpoints += 1
            unresolved.append(
                {
                    "kind": "edge_endpoint_missing",
                    "edge_id": eid,
                    "relation": edge.get("relation", ""),
                    "src": edge.get("src", ""),
                    "dst": edge.get("dst", ""),
                    "evidence": edge.get("evidence", {}),
                }
            )
            continue
        edge_by_id[eid] = edge

    final_nodes = list(by_id.values())
    final_edges = list(edge_by_id.values())
    kinds = Counter(n["kind"] for n in final_nodes)
    relations = Counter(e["relation"] for e in final_edges)
    metrics = {
        "nodes": len(final_nodes),
        "edges": len(final_edges),
        "nodes_by_kind": dict(kinds),
        "edges_by_relation": dict(relations),
        "duplicate_node_ids": duplicates,
        "duplicate_edge_ids": edge_duplicates,
        "edges_dropped_missing_endpoint": missing_endpoints,
        "unresolved": len(unresolved),
    }
    return final_nodes, final_edges, metrics, unresolved


def _write_graphml(path, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    """Minimal GraphML export; JSONL stays the authoritative store."""
    import xml.etree.ElementTree as ET

    root = ET.Element("graphml")
    for key, attr_type in (
        ("kind", "string"),
        ("qualified_name", "string"),
        ("source_file", "string"),
        ("source_line", "int"),
        ("relation", "string"),
        ("confidence", "string"),
    ):
        ET.SubElement(root, "key", id=key, **{"for": "all"}, attr_name=key, attr_type=attr_type)
    graph = ET.SubElement(root, "graph", id="px4dep", edgedefault="directed")
    for node in nodes:
        source = node.get("source") or {}
        element = ET.SubElement(graph, "node", id=node["id"])
        ET.SubElement(element, "data", key="kind").text = node.get("kind", "")
        ET.SubElement(element, "data", key="qualified_name").text = node.get("qualified_name", "")
        ET.SubElement(element, "data", key="source_file").text = source.get("file", "")
        ET.SubElement(element, "data", key="source_line").text = str(source.get("line", 0))
    for edge in edges:
        element = ET.SubElement(graph, "edge", id=edge["id"], source=edge["src"], target=edge["dst"])
        ET.SubElement(element, "data", key="relation").text = edge.get("relation", "")
        ET.SubElement(element, "data", key="confidence").text = edge.get("confidence", "")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def build_graph(
    cfg: Config,
    run,
    raw_records: list[dict[str, Any]],
    parameter_records: list[dict[str, Any]],
    schema_nodes: list[dict[str, Any]],
    schema_edges: list[dict[str, Any]],
    commit: str = "",
    variant: str = "",
    schema_unresolved: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cpp_nodes, cpp_metrics, cpp_unresolved, function_ids, config_field_ids = build_cpp_entities(
        cfg, raw_records, commit, variant
    )

    wrapper_nodes, binding_edges, binding_metrics, binding_unresolved = build_wrappers(
        cfg, raw_records, parameter_records, commit, variant
    )
    usage_edges, usage_metrics, usage_unresolved = build_parameter_usage(
        cfg, raw_records, parameter_records, function_ids, commit, variant
    )

    topics = [n for n in schema_nodes if n["kind"] == "topic"]
    messages = [n for n in schema_nodes if n["kind"] == "message_type"]
    endpoint_nodes, flow_edges, flow_metrics, flow_unresolved = build_uorb_flow(
        cfg, raw_records, topics, messages, function_ids, commit, variant
    )
    health_nodes, health_edges, health_metrics, health_unresolved = build_health_chain(
        cfg, raw_records, function_ids, schema_nodes, commit, variant
    )
    commander_edges, commander_paths, commander_metrics, commander_unresolved = (
        build_commander_slice(
            cfg,
            raw_records,
            health_nodes,
            wrapper_nodes,
            parameter_records,
            function_ids,
            commit,
            variant,
        )
    )
    slice_nodes, slice_edges, slice_paths, slice_metrics, slice_unresolved = build_ekf_slice(
        cfg,
        raw_records,
        cpp_nodes,
        schema_nodes,
        function_ids,
        binding_edges,
        parameter_records,
        commit,
        variant,
    )
    call_edges, call_metrics = build_call_edges(
        cfg, raw_records, function_ids, commit, variant
    )

    all_nodes: list[dict[str, Any]] = list(schema_nodes)
    all_nodes.extend(parameter_records)
    all_nodes.extend(cpp_nodes)
    all_nodes.extend(wrapper_nodes)
    all_nodes.extend(endpoint_nodes)
    all_nodes.extend(health_nodes)
    all_nodes.extend(slice_nodes)

    all_edges: list[dict[str, Any]] = list(schema_edges)
    all_edges.extend(binding_edges)
    all_edges.extend(usage_edges)
    all_edges.extend(flow_edges)
    all_edges.extend(health_edges)
    all_edges.extend(commander_edges)
    all_edges.extend(slice_edges)
    all_edges.extend(call_edges)

    attach_entity_guards(cfg, all_nodes)

    # Binding targets and uORB endpoint declarations may live in headers whose
    # entity node was not emitted (e.g. an array member).  Do not invent graph
    # nodes; report the dangling edges instead.
    final_nodes, final_edges, graph_metrics, graph_unresolved = merge_and_validate(
        cfg,
        all_nodes,
        all_edges,
        cpp_unresolved
        + binding_unresolved
        + usage_unresolved
        + flow_unresolved
        + health_unresolved
        + slice_unresolved
        + list(schema_unresolved or []),
    )

    cross_paths, cross_metrics, cross_unresolved = build_cross_module(
        cfg, final_nodes, final_edges, slice_paths, commit, variant
    )

    graph_metrics.update(
        {
            "cpp": cpp_metrics,
            "parameter_bindings": binding_metrics,
            "parameter_usage": usage_metrics,
            "uorb_flow": flow_metrics,
            "health_chain": health_metrics,
            "commander_slice": commander_metrics,
            "ekf_slice": slice_metrics,
            "call_graph": call_metrics,
            "cross_module": cross_metrics,
            "distinct_function_nodes": len(function_ids),
            "distinct_config_fields": len(config_field_ids),
            "input_records": len(raw_records),
        }
    )

    write_jsonl(run.path("graph", "nodes.jsonl"), final_nodes)
    write_jsonl(run.path("graph", "edges.jsonl"), final_edges)
    write_jsonl(run.path("graph", "paths.jsonl"), slice_paths)
    _write_graphml(run.path("graph", "dependency.graphml"), final_nodes, final_edges)

    write_jsonl(
        run.path("normalized", "cpp_entities.jsonl"),
        (n for n in final_nodes if n["kind"] in CPP_KINDS),
    )
    write_jsonl(
        run.path("normalized", "param_wrappers.jsonl"),
        (n for n in final_nodes if n["kind"] == "param_wrapper"),
    )
    write_jsonl(
        run.path("normalized", "uorb_endpoints.jsonl"),
        (n for n in final_nodes if n["kind"] == "uorb_endpoint"),
    )
    write_jsonl(
        run.path("normalized", "topics.jsonl"),
        (n for n in final_nodes if n["kind"] == "topic"),
    )
    health_final_nodes = [n for n in final_nodes if n["kind"] in HEALTH_KINDS]
    health_final_edges = [e for e in final_edges if e["relation"] in HEALTH_RELATIONS]
    write_jsonl(run.path("normalized", "health_chain.jsonl"), health_final_nodes)
    write_jsonl(run.path("normalized", "health_chain_edges.jsonl"), health_final_edges)
    write_jsonl(
        run.path("unresolved", "health_chain.jsonl"),
        health_unresolved,
    )
    write_json(run.path("reports", "health_chain_metrics.json"), health_metrics)
    write_health_chain_csv(
        run.path("reports", "health_chain.csv"),
        [n for n in health_final_nodes if n["kind"] == "predicate"],
    )

    slice_final_nodes = [n for n in final_nodes if n["kind"] in SLICE_KINDS]
    slice_final_edges = [
        e
        for e in final_edges
        if e["relation"] in SLICE_RELATIONS
        and (e.get("evidence") or {}).get("scope") != "commander"
    ]
    write_jsonl(run.path("normalized", "ekf_slice.jsonl"), slice_final_nodes)
    write_jsonl(run.path("normalized", "ekf_slice_edges.jsonl"), slice_final_edges)
    write_jsonl(run.path("unresolved", "ekf_slice.jsonl"), slice_unresolved)
    write_json(run.path("reports", "ekf_slice_metrics.json"), slice_metrics)
    write_ekf_slice_csv(
        run.path("reports", "ekf_slice.csv"), slice_final_edges, slice_paths
    )
    write_commander_slice(
        run,
        commander_edges,
        commander_paths,
        commander_metrics,
        commander_unresolved,
    )

    call_final_edges = [
        e for e in final_edges if e["relation"] == "calls"
    ]
    write_cross_module(
        run,
        call_final_edges,
        call_metrics,
        cross_paths,
        cross_metrics,
        cross_unresolved,
    )

    write_json(
        run.path("reports", "param_binding_metrics.json"),
        {**binding_metrics, "usage": usage_metrics},
    )
    write_json(run.path("reports", "uorb_flow_metrics.json"), flow_metrics)

    write_jsonl(
        run.path("evidence", "edge_evidence.jsonl"),
        (
            {
                "edge_id": e["id"],
                "relation": e["relation"],
                "src": e["src"],
                "dst": e["dst"],
                "confidence": e.get("confidence", ""),
                "evidence": e.get("evidence", {}),
                "build_guards": e.get("build_guards", []),
            }
            for e in final_edges
        ),
    )
    write_jsonl(
        run.path("evidence", "source.jsonl"),
        (
            {
                "kind": "node",
                "id": n["id"],
                "entity_kind": n["kind"],
                "qualified_name": n.get("qualified_name", ""),
                "source": n.get("source") or {},
                "build_guards": n.get("build_guards", []),
                "build_guard_status": n.get("build_guard_status", "none"),
            }
            for n in final_nodes
        ),
    )
    write_jsonl(
        run.path("evidence", "source.jsonl"),
        (
            {
                "kind": "edge",
                "id": n["id"],
                "relation": n.get("relation", ""),
                "confidence": n.get("confidence", ""),
                "evidence": n.get("evidence", {}),
                "build_guards": n.get("build_guards", []),
            }
            for n in final_edges
        ),
        append=True,
    )

    # Category-specific unresolved lists expected by the report layout.
    categories = {
        "calls.jsonl": {"uorb_call_unresolved_receiver", "uorb_call_caller_not_indexed",
                        "parameter_binding_unresolved", "parameter_binding_without_wrapper",
                        "parameter_binding_parameter_missing", "param_find_unresolved",
                        "parameter_handle_not_resolved"},
        "topics.jsonl": {"uorb_topic_not_found", "uorb_endpoint_without_topic",
                         "nested_message_type_not_found", "msg_parse_error"},
        "aliases.jsonl": {"duplicate_topic_alias", "parameter_wrapper_unresolved"},
        "truncations.jsonl": {"edge_endpoint_missing"},
    }
    for filename, kinds in categories.items():
        write_jsonl(
            run.path("unresolved", filename),
            (u for u in graph_unresolved if u.get("kind") in kinds),
        )

    write_jsonl(run.path("unresolved", "graph.jsonl"), graph_unresolved)
    write_json(run.path("reports", "graph_metrics.json"), graph_metrics)
    return graph_metrics
