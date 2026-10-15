"""Parameter wrapper / configuration-field binding edges."""

from __future__ import annotations

from typing import Any, Iterable

from .config import Config
from .guards import GuardIndex
from .util import stable_id, write_json, write_jsonl


def wrapper_id(commit: str, variant: str, record: dict[str, Any]) -> str:
    return stable_id(
        commit,
        variant,
        "param_wrapper",
        record.get("qualified_name", ""),
        record.get("file", ""),
        record.get("line", 0),
    )


def parameter_id(commit: str, variant: str, name: str) -> str:
    return stable_id(commit, variant, "parameter", name, "", "")


def _source(record: dict[str, Any], cfg: Config) -> dict[str, Any]:
    return {
        "file": record.get("file", ""),
        "line": record.get("line", 0),
        "column": record.get("column", 0),
        "expansion_file": record.get("expansion_file", ""),
        "expansion_line": record.get("expansion_line", 0),
        "spelling_file": record.get("spelling_file", ""),
        "spelling_line": record.get("spelling_line", 0),
    }


def build_wrappers(
    cfg: Config,
    raw_records: Iterable[dict[str, Any]],
    parameter_records: Iterable[dict[str, Any]],
    commit: str = "",
    variant: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Return (nodes, edges, metrics, unresolved) for wrappers and bindings."""
    guards = GuardIndex(cfg)
    params = {r["name"]: r for r in parameter_records if r.get("kind") == "parameter"}
    param_ids = {name: r["id"] for name, r in params.items()}

    wrappers = [r for r in raw_records if r.get("record") == "param_wrapper"]
    bindings = [r for r in raw_records if r.get("record") == "binding"]
    wrapper_by_usr = {r.get("usr", ""): r for r in wrappers}

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    bound_parameter_names: set[str] = set()

    for record in wrappers:
        name = record.get("parameter_name", "")
        wid = wrapper_id(commit, variant, record)
        pid = param_ids.get(name)
        guard_list = guards.for_record(record)
        guard_status = guards.status_for_record(record)
        node = {
            "id": wid,
            "kind": "param_wrapper",
            "qualified_name": record.get("qualified_name", ""),
            "name": record.get("field_name", ""),
            "field_name": record.get("field_name", ""),
            "parameter_name": name,
            "parameter_id": pid or "",
            "wrapper_kind": record.get("wrapper_kind", ""),
            "wrapper_type": record.get("type", ""),
            "parent_class": record.get("parent_class", ""),
            "usr": record.get("usr", ""),
            "build_variant": variant,
            "build_guards": guard_list,
            "build_guard_status": guard_status,
            "source": _source(record, cfg),
            "tu": record.get("tu", ""),
        }
        nodes.append(node)

        if pid:
            edges.append(
                {
                    "id": stable_id(commit, variant, "edge", "wraps", wid, pid),
                    "kind": "edge",
                    "relation": "wraps",
                    "src": wid,
                    "dst": pid,
                    "build_variant": variant,
                    "build_guards": guard_list,
                    "build_guard_status": guard_status,
                    "evidence": {
                        "rule": "param_wrapper_field_type",
                        "file": record.get("file", ""),
                        "line": record.get("line", 0),
                        "spelling_file": record.get("spelling_file", ""),
                        "spelling_line": record.get("spelling_line", 0),
                    },
                    "confidence": "high",
                }
            )
        else:
            unresolved.append(
                {
                    "kind": "parameter_wrapper_unresolved",
                    "parameter_name": name,
                    "wrapper_usr": record.get("usr", ""),
                    "wrapper": record.get("qualified_name", ""),
                    "file": record.get("file", ""),
                    "line": record.get("line", 0),
                    "reason": "parameter_not_in_build_catalog",
                }
            )

    for record in bindings:
        name = record.get("parameter_name", "")
        pid = param_ids.get(name)
        dst_usr = record.get("dst_usr", "")
        config_field_id = stable_id(commit, variant, "config_field", dst_usr, "", "")
        wid = ""
        src_usr = record.get("src_usr", "")
        wrapper = wrapper_by_usr.get(src_usr)
        if wrapper:
            wid = wrapper_id(commit, variant, wrapper)
        guard_list = guards.for_record(record)
        guard_status = guards.status_for_record(record)

        if not record.get("resolved") or not dst_usr:
            unresolved.append(
                {
                    "kind": "parameter_binding_unresolved",
                    "parameter_name": name,
                    "src_usr": src_usr,
                    "src_name": record.get("src_name", ""),
                    "target_expr": record.get("target_expr", ""),
                    "file": record.get("file", ""),
                    "line": record.get("line", 0),
                    "reason": "constructor_target_not_resolved",
                }
            )
            continue
        if not wid:
            unresolved.append(
                {
                    "kind": "parameter_binding_without_wrapper",
                    "parameter_name": name,
                    "src_usr": src_usr,
                    "src_name": record.get("src_name", ""),
                    "file": record.get("file", ""),
                    "line": record.get("line", 0),
                }
            )
        if not pid:
            unresolved.append(
                {
                    "kind": "parameter_binding_parameter_missing",
                    "parameter_name": name,
                    "dst_usr": dst_usr,
                    "file": record.get("file", ""),
                    "line": record.get("line", 0),
                }
            )
            continue

        bound_parameter_names.add(name)
        if wid:
            edges.append(
                {
                    "id": stable_id(commit, variant, "edge", "binds_to", wid, config_field_id),
                    "kind": "edge",
                    "relation": "binds_to",
                    "src": wid,
                    "dst": config_field_id,
                    "build_variant": variant,
                    "build_guards": guard_list,
                    "build_guard_status": guard_status,
                    "evidence": {
                        "rule": "constructor_member_initializer",
                        "file": record.get("file", ""),
                        "line": record.get("line", 0),
                        "target_expr": record.get("target_expr", ""),
                        "index_expr": record.get("index_expr", ""),
                    },
                    "confidence": "high",
                }
            )
        edges.append(
            {
                "id": stable_id(commit, variant, "edge", "configures", pid, config_field_id),
                "kind": "edge",
                "relation": "configures",
                "src": pid,
                "dst": config_field_id,
                "build_variant": variant,
                "build_guards": guard_list,
                "build_guard_status": guard_status,
                "evidence": {
                    "rule": "parameter_wrapper_binding",
                    "file": record.get("file", ""),
                    "line": record.get("line", 0),
                    "wrapper_usr": src_usr,
                    "target_expr": record.get("target_expr", ""),
                },
                "confidence": "high",
            }
        )

    metrics = {
        "param_wrappers": len(wrappers),
        "bindings": len(bindings),
        "bindings_resolved": sum(1 for r in bindings if r.get("resolved")),
        "bindings_unresolved": sum(1 for r in bindings if not r.get("resolved")),
        "wrappers_with_parameter": sum(1 for r in wrappers if r.get("parameter_name") in params),
        "wrappers_without_parameter": sum(1 for r in wrappers if r.get("parameter_name") not in params),
        "distinct_parameters_bound": len(bound_parameter_names),
        "config_field_edges": sum(1 for e in edges if e["relation"] == "configures"),
        "unresolved_records": len(unresolved),
        "wrapper_binding_rate": round(
            sum(1 for r in bindings if r.get("resolved")) / max(1, len(bindings)), 6
        ),
    }
    return nodes, edges, metrics, unresolved


def build_parameter_usage(
    cfg: Config,
    raw_records: Iterable[dict[str, Any]],
    parameter_records: Iterable[dict[str, Any]],
    function_ids: dict[str, str],
    commit: str = "",
    variant: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Link param_find/param_get/param_set calls from functions to parameters."""
    params = {r["name"]: r for r in parameter_records if r.get("kind") == "parameter"}
    edges: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    for record in raw_records:
        if record.get("record") != "call" or record.get("api") != "parameter":
            continue
        method = record.get("method", "")
        caller = record.get("caller_usr", "")
        caller_id = function_ids.get(caller)
        if not caller_id:
            continue
        relation = {
            "param_find": "lookup_parameter",
            "param_find_no_notification": "lookup_parameter",
            "param_get": "reads_parameter",
            "param_set": "writes_parameter",
        }.get(method)
        if not relation:
            continue

        args = record.get("args") or []
        if method.startswith("param_find"):
            name = str(args[0].get("text", "")).strip().strip('"') if args else ""
            if name in params:
                pid = params[name]["id"]
                counts[relation] = counts.get(relation, 0) + 1
                edges.append(
                    {
                        "id": stable_id(
                            commit, variant, "edge", relation, caller_id, pid,
                            record.get("file", ""), record.get("line", 0), record.get("column", 0),
                        ),
                        "kind": "edge",
                        "relation": relation,
                        "src": caller_id,
                        "dst": pid,
                        "build_variant": variant,
                        "evidence": {
                            "rule": "parameter_api_call",
                            "method": method,
                            "file": record.get("file", ""),
                            "line": record.get("line", 0),
                        },
                        "confidence": "high",
                    }
                )
            else:
                unresolved.append(
                    {
                        "kind": "param_find_unresolved",
                        "parameter_name": name,
                        "caller": record.get("caller", ""),
                        "file": record.get("file", ""),
                        "line": record.get("line", 0),
                    }
                )
        else:
            # param_get/param_set need a handle->name dataflow pass; M1 records
            # the call site but cannot yet name the parameter.
            unresolved.append(
                {
                    "kind": "parameter_handle_not_resolved",
                    "method": method,
                    "caller": record.get("caller", ""),
                    "file": record.get("file", ""),
                    "line": record.get("line", 0),
                    "reason": "requires_handle_dataflow",
                }
            )

    metrics = {
        "parameter_usage_edges": len(edges),
        "by_relation": counts,
        "unresolved": len(unresolved),
    }
    return edges, metrics, unresolved


def write_bindings(
    run,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    metrics: dict[str, Any],
    usage_metrics: dict[str, Any],
    unresolved: list[dict[str, Any]],
) -> None:
    write_jsonl(run.path("normalized", "param_wrappers.jsonl"), nodes)
    write_jsonl(run.path("normalized", "param_binding_edges.jsonl"), edges)
    write_jsonl(run.path("unresolved", "param_bindings.jsonl"), unresolved)
    write_json(
        run.path("reports", "param_binding_metrics.json"),
        {**metrics, "usage": usage_metrics},
    )
