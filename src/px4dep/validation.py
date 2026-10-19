"""Run validation: schemas, stable ids, dangling edges and input hashes."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .config import Config
from .llvm_fallback import m6_output_paths
from .manifest import Run
from .util import read_json, read_jsonl, repo_rel, sha256_file, sha256_files, write_json


def _load_schema(project_root: Path, name: str) -> dict[str, Any]:
    return read_json(project_root / "schemas" / name, {})


def _schema_errors(records: list[dict[str, Any]], schema: dict[str, Any]) -> list[dict[str, Any]]:
    if not schema:
        return [{"error": "schema_missing"}]
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return [{"error": "jsonschema_not_installed"}]
    validator = Draft202012Validator(schema)
    errors: list[dict[str, Any]] = []
    for idx, record in enumerate(records):
        for error in validator.iter_errors(record):
            errors.append(
                {
                    "index": idx,
                    "id": record.get("id", ""),
                    "path": "/".join(str(p) for p in error.absolute_path),
                    "message": error.message,
                }
            )
            if len(errors) >= 100:
                return errors
    return errors


def _manifest_hash_checks(cfg: Config, run: Run, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    inputs = manifest.get("inputs", {})
    expected = {
        "compile_commands": cfg.compile_commands,
        "parameters_json": cfg.parameters_json,
        "parameters_xml": cfg.parameters_xml,
    }
    for name, path in expected.items():
        recorded = (inputs.get(name) or {}).get("sha256", "")
        actual = sha256_file(path) if path.exists() else ""
        checks.append(
            {
                "check": f"input_hash:{name}",
                "ok": bool(recorded) and recorded == actual,
                "recorded": recorded,
                "actual": actual,
            }
        )

    msg_recorded = (inputs.get("msg_dir") or {}).get("aggregate_sha256", "")
    msg_actual = sha256_files(cfg.msg_files, cfg.repo) if cfg.msg_files else ""
    checks.append(
        {
            "check": "input_hash:msg_files",
            "ok": bool(msg_recorded) and msg_recorded == msg_actual,
            "recorded": msg_recorded,
            "actual": msg_actual,
        }
    )

    yaml_files = sorted({p for pattern in cfg.param_yaml_globs for p in cfg.repo.glob(pattern) if p.is_file()})
    yaml_recorded = (inputs.get("parameter_yaml") or {}).get("aggregate_sha256", "")
    yaml_actual = sha256_files(yaml_files, cfg.repo) if yaml_files else ""
    checks.append(
        {
            "check": "input_hash:parameter_yaml",
            "ok": bool(yaml_recorded) and yaml_recorded == yaml_actual,
            "recorded": yaml_recorded,
            "actual": yaml_actual,
        }
    )

    sink_recorded = (inputs.get("sink_catalog") or {}).get("sha256", "")
    sink_actual = sha256_file(cfg.sink_catalog) if cfg.sink_catalog.exists() else ""
    checks.append(
        {
            "check": "input_hash:sink_catalog",
            "ok": bool(sink_recorded) and sink_recorded == sink_actual,
            "recorded": sink_recorded,
            "actual": sink_actual,
        }
    )

    for entry in inputs.get("translation_units", []):
        path = cfg.repo / entry.get("path", "")
        actual = sha256_file(path) if path.exists() else ""
        checks.append(
            {
                "check": f"input_hash:tu:{entry.get('path', '')}",
                "ok": bool(entry.get("sha256")) and entry.get("sha256") == actual,
                "recorded": entry.get("sha256", ""),
                "actual": actual,
            }
        )

    ulog_entry = inputs.get("ulog")
    if ulog_entry:
        ulog_path = Path(ulog_entry.get("path", ""))
        actual = sha256_file(ulog_path) if ulog_path.exists() else ""
        checks.append(
            {
                "check": "input_hash:ulog",
                "ok": bool(ulog_entry.get("sha256")) and ulog_entry.get("sha256") == actual,
                "recorded": ulog_entry.get("sha256", ""),
                "actual": actual,
            }
        )
    return checks


def validate_run(cfg: Config, run: Run) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}

    manifest = read_json(run.manifest_json_path, {}) or {}
    hash_checks = _manifest_hash_checks(cfg, run, manifest)
    checks["input_hashes"] = hash_checks
    if not all(c["ok"] for c in hash_checks):
        warnings.append({"kind": "input_hash_mismatch", "detail": "run inputs changed since manifest"})

    parameter_schema = _load_schema(cfg.project_root, "parameter.schema.json")
    entity_schema = _load_schema(cfg.project_root, "entity.schema.json")
    edge_schema = _load_schema(cfg.project_root, "edge.schema.json")
    evidence_schema = _load_schema(cfg.project_root, "evidence.schema.json")

    parameters = read_jsonl(run.path("normalized", "parameters.jsonl"))
    checks["parameters_schema_errors"] = _schema_errors(parameters, parameter_schema)

    cpp_entities = read_jsonl(run.path("normalized", "cpp_entities.jsonl"))
    checks["cpp_entities_schema_errors"] = _schema_errors(cpp_entities, entity_schema)

    topics = read_jsonl(run.path("normalized", "topics.jsonl"))
    checks["topics_schema_errors"] = _schema_errors(topics, entity_schema)

    nodes = read_jsonl(run.path("graph", "nodes.jsonl"))
    node_errors: list[dict[str, Any]] = []
    for node in nodes:
        schema = parameter_schema if node.get("kind") == "parameter" else entity_schema
        node_errors.extend(_schema_errors([node], schema))
    checks["graph_nodes_schema_errors"] = node_errors

    edges = read_jsonl(run.path("graph", "edges.jsonl"))
    checks["graph_edges_schema_errors"] = _schema_errors(edges, edge_schema)

    evidence = read_jsonl(run.path("evidence", "source.jsonl"))
    checks["evidence_schema_errors"] = _schema_errors(evidence, evidence_schema)

    node_ids = [n.get("id", "") for n in nodes]
    duplicate_node_ids = [nid for nid, count in Counter(node_ids).items() if count > 1 and nid]
    checks["duplicate_node_ids"] = duplicate_node_ids

    edge_ids = [e.get("id", "") for e in edges]
    duplicate_edge_ids = [eid for eid, count in Counter(edge_ids).items() if count > 1 and eid]
    checks["duplicate_edge_ids"] = duplicate_edge_ids

    id_set = set(node_ids)
    dangling = [
        {"id": e.get("id", ""), "relation": e.get("relation", ""), "src": e.get("src", ""), "dst": e.get("dst", "")}
        for e in edges
        if e.get("src") not in id_set or e.get("dst") not in id_set
    ]
    checks["dangling_edges"] = dangling

    runtime_records = read_jsonl(run.path("evidence", "runtime.jsonl"))
    runtime_ids = [record.get("id", "") for record in runtime_records]
    checks["duplicate_runtime_ids"] = [
        rid for rid, count in Counter(runtime_ids).items() if count > 1 and rid
    ]
    m4_ids = {record.get("id", "") for record in read_jsonl(run.path("graph", "end_to_end_paths.jsonl"))}
    checks["runtime_paths_without_m4_path"] = sorted(
        {
            record.get("path_id", "")
            for record in runtime_records
            if record.get("kind") == "runtime_path"
            and record.get("path_id") not in m4_ids
        }
    )

    candidates = read_jsonl(run.path("evidence", "candidates.jsonl"))
    candidate_ids = [record.get("id", "") for record in candidates]
    checks["duplicate_candidate_ids"] = [
        cid for cid, count in Counter(candidate_ids).items() if count > 1 and cid
    ]
    build_parameter_names = {
        node.get("name", "")
        for node in nodes
        if node.get("kind") == "parameter" and node.get("in_build")
    }
    checks["candidates_without_build_parameter"] = sorted(
        record.get("parameter", "")
        for record in candidates
        if record.get("parameter", "") not in build_parameter_names
    )
    checks["candidates_without_explanation"] = sorted(
        record.get("parameter", "")
        for record in candidates
        if not (record.get("explanation") or {}).get("text")
    )

    commander_paths = read_jsonl(run.path("graph", "commander_paths.jsonl"))
    commander_path_ids = [record.get("id", "") for record in commander_paths]
    checks["duplicate_commander_path_ids"] = [
        pid for pid, count in Counter(commander_path_ids).items() if count > 1 and pid
    ]
    checks["commander_paths_without_build_parameter"] = sorted(
        {
            record.get("parameter", "")
            for record in commander_paths
            if record.get("parameter", "") not in build_parameter_names
        }
    )
    checks["commander_paths_without_steps"] = sorted(
        record.get("id", "")
        for record in commander_paths
        if not record.get("steps")
    )
    checks["commander_context_paths_not_labelled"] = sorted(
        record.get("id", "")
        for record in commander_paths
        if record.get("category") == "context"
        and (record.get("context_only") is not True or record.get("confidence") != "low")
    )

    break_chains = read_jsonl(run.path("evidence", "break_chains.jsonl"))
    break_ids = [record.get("id", "") for record in break_chains]
    checks["duplicate_break_chain_ids"] = [
        bid for bid, count in Counter(break_ids).items() if count > 1 and bid
    ]
    graph_unresolved = read_jsonl(run.path("unresolved", "graph.jsonl"))
    non_m4 = [r for r in break_chains if r.get("stage") != "M4"]
    checks["break_chain_inventory_preserved"] = bool(break_chains) and len(non_m4) == len(
        graph_unresolved
    )
    checks["break_chains_without_root_cause"] = sorted(
        record.get("id", "")
        for record in break_chains
        if not record.get("root_cause") or not record.get("source_file")
    )
    checks["break_chains_selected_type_mismatch"] = sorted(
        record.get("id", "")
        for record in break_chains
        if record.get("unresolved_kind") == "m4_writer_publish_topic_unresolved"
        and not record.get("llvm_applicable")
    )

    llvm_records = read_jsonl(run.path("evidence", "llvm.jsonl"))
    llvm_ids = [record.get("id", "") for record in llvm_records]
    checks["duplicate_llvm_ids"] = [
        lid for lid, count in Counter(llvm_ids).items() if count > 1 and lid
    ]
    checks["llvm_bindings_without_ast_cross_check"] = sorted(
        record.get("topic", "")
        for record in llvm_records
        if not (record.get("ast_cross_check") or {}).get("passed")
    )
    checks["llvm_bindings_without_ir_evidence"] = sorted(
        record.get("topic", "")
        for record in llvm_records
        if not (record.get("binding") or {}).get("ekf_member_field_index")
        or not (record.get("binding") or {}).get("ekf2_publication_field_index")
        or not str((record.get("binding") or {}).get("topic_global", "")).startswith(
            "__orb_"
        )
    )
    checks["llvm_bindings_with_end_to_end_claim"] = sorted(
        record.get("topic", "")
        for record in llvm_records
        if record.get("end_to_end_claim")
    )

    resolutions = read_jsonl(run.path("evidence", "break_chain_resolution.jsonl"))
    resolution_ids = [record.get("id", "") for record in resolutions]
    checks["duplicate_resolution_ids"] = [
        rid for rid, count in Counter(resolution_ids).items() if count > 1 and rid
    ]
    checks["resolutions_with_out_of_range_topic_count"] = sorted(
        record.get("id", "")
        for record in resolutions
        if record.get("topic_count") != len(record.get("topic_candidates") or [])
    )
    checks["resolutions_without_root_cause_after"] = sorted(
        record.get("id", "")
        for record in resolutions
        if not record.get("root_cause_after")
    )
    checks["resolutions_creating_end_to_end"] = sorted(
        record.get("id", "")
        for record in resolutions
        if record.get("end_to_end_created")
    )
    checks["llvm_only_end_to_end_paths"] = sorted(
        set(checks["llvm_bindings_with_end_to_end_claim"])
        | set(checks["resolutions_creating_end_to_end"])
    )

    llvm_residual = read_jsonl(run.path("unresolved", "llvm.jsonl"))
    residual_ids = [record.get("id", "") for record in llvm_residual]
    checks["duplicate_llvm_residual_ids"] = [
        rid for rid, count in Counter(residual_ids).items() if count > 1 and rid
    ]
    checks["llvm_residual_without_cause"] = sorted(
        record.get("id", "")
        for record in llvm_residual
        if not (record.get("root_cause_after") or record.get("reason"))
    )
    break_report = read_json(run.path("reports", "break_chains.json"), {}) or {}
    checks["break_chain_selection_missing"] = (
        (break_report.get("selection") or {}).get("selected_kind", "")
        != "m4_writer_publish_topic_unresolved"
    )

    missing_outputs = [
        name
        for name in (
            "normalized/parameters.jsonl",
            "normalized/cpp_entities.jsonl",
            "normalized/topics.jsonl",
            "graph/nodes.jsonl",
            "graph/edges.jsonl",
            "graph/paths.jsonl",
            "evidence/source.jsonl",
            "normalized/health_chain.jsonl",
            "normalized/health_chain_edges.jsonl",
            "unresolved/health_chain.jsonl",
            "reports/health_chain_metrics.json",
            "reports/health_chain.csv",
            "normalized/ekf_slice.jsonl",
            "normalized/ekf_slice_edges.jsonl",
            "unresolved/ekf_slice.jsonl",
            "reports/ekf_slice_metrics.json",
            "reports/ekf_slice.csv",
            "graph/commander_paths.jsonl",
            "normalized/commander_slice_edges.jsonl",
            "unresolved/commander_slice.jsonl",
            "reports/commander_slice_metrics.json",
            "reports/commander_slice.csv",
            "graph/end_to_end_paths.jsonl",
            "normalized/cross_module_edges.jsonl",
            "unresolved/cross_module.jsonl",
            "reports/cross_module_metrics.json",
            "reports/cross_module.csv",
            "evidence/runtime.jsonl",
            "unresolved/ulog.jsonl",
            "reports/runtime_metrics.json",
            "evidence/candidates.jsonl",
            "reports/candidate_metrics.json",
            "reports/candidates.csv",
            "reports/traceability.csv",
            "reports/metrics.json",
            "reports/summary.md",
            *m6_output_paths(),
        )
        if not run.path(name).exists()
    ]
    checks["missing_outputs"] = missing_outputs

    schema_error_count = sum(
        len(checks[key])
        for key in (
            "parameters_schema_errors",
            "cpp_entities_schema_errors",
            "topics_schema_errors",
            "graph_nodes_schema_errors",
            "graph_edges_schema_errors",
            "evidence_schema_errors",
        )
    )
    ok = (
        schema_error_count == 0
        and not duplicate_node_ids
        and not duplicate_edge_ids
        and not dangling
        and not checks["duplicate_runtime_ids"]
        and not checks["runtime_paths_without_m4_path"]
        and not checks["duplicate_candidate_ids"]
        and not checks["candidates_without_build_parameter"]
        and not checks["candidates_without_explanation"]
        and not checks["duplicate_commander_path_ids"]
        and not checks["commander_paths_without_build_parameter"]
        and not checks["commander_paths_without_steps"]
        and not checks["commander_context_paths_not_labelled"]
        and not checks["duplicate_break_chain_ids"]
        and checks["break_chain_inventory_preserved"]
        and not checks["break_chains_without_root_cause"]
        and not checks["break_chains_selected_type_mismatch"]
        and not checks["duplicate_llvm_ids"]
        and not checks["llvm_bindings_without_ast_cross_check"]
        and not checks["llvm_bindings_without_ir_evidence"]
        and not checks["duplicate_resolution_ids"]
        and not checks["resolutions_with_out_of_range_topic_count"]
        and not checks["resolutions_without_root_cause_after"]
        and not checks["llvm_only_end_to_end_paths"]
        and not checks["duplicate_llvm_residual_ids"]
        and not checks["llvm_residual_without_cause"]
        and not checks["break_chain_selection_missing"]
        and not missing_outputs
    )
    if schema_error_count:
        errors.append({"kind": "schema_errors", "count": schema_error_count})
    if duplicate_node_ids:
        errors.append({"kind": "duplicate_node_ids", "count": len(duplicate_node_ids)})
    if duplicate_edge_ids:
        errors.append({"kind": "duplicate_edge_ids", "count": len(duplicate_edge_ids)})
    if dangling:
        errors.append({"kind": "dangling_edges", "count": len(dangling)})
    for key, kind in (
        ("duplicate_runtime_ids", "duplicate_runtime_ids"),
        ("runtime_paths_without_m4_path", "runtime_paths_without_m4_path"),
        ("duplicate_candidate_ids", "duplicate_candidate_ids"),
        ("candidates_without_build_parameter", "candidates_without_build_parameter"),
        ("candidates_without_explanation", "candidates_without_explanation"),
        ("duplicate_commander_path_ids", "duplicate_commander_path_ids"),
        (
            "commander_paths_without_build_parameter",
            "commander_paths_without_build_parameter",
        ),
        ("commander_paths_without_steps", "commander_paths_without_steps"),
        (
            "commander_context_paths_not_labelled",
            "commander_context_paths_not_labelled",
        ),
        ("duplicate_break_chain_ids", "duplicate_break_chain_ids"),
        ("break_chains_without_root_cause", "break_chains_without_root_cause"),
        ("break_chains_selected_type_mismatch", "break_chains_selected_type_mismatch"),
        ("duplicate_llvm_ids", "duplicate_llvm_ids"),
        ("llvm_bindings_without_ast_cross_check", "llvm_bindings_without_ast_cross_check"),
        ("llvm_bindings_without_ir_evidence", "llvm_bindings_without_ir_evidence"),
        ("duplicate_resolution_ids", "duplicate_resolution_ids"),
        (
            "resolutions_with_out_of_range_topic_count",
            "resolutions_with_out_of_range_topic_count",
        ),
        ("resolutions_without_root_cause_after", "resolutions_without_root_cause_after"),
        ("llvm_only_end_to_end_paths", "llvm_only_end_to_end_paths"),
        ("duplicate_llvm_residual_ids", "duplicate_llvm_residual_ids"),
        ("llvm_residual_without_cause", "llvm_residual_without_cause"),
    ):
        if checks[key]:
            errors.append({"kind": kind, "count": len(checks[key])})
    if not checks["break_chain_inventory_preserved"]:
        errors.append({"kind": "break_chain_inventory_not_preserved"})
    if checks["break_chain_selection_missing"]:
        errors.append({"kind": "break_chain_selection_missing"})
    if missing_outputs:
        errors.append({"kind": "missing_outputs", "files": missing_outputs})

    result = {
        "run_id": run.run_id,
        "ok": ok,
        "sources": {
            "manifest": repo_rel(run.manifest_path, cfg.project_root),
            "config": repo_rel(cfg.path, cfg.project_root),
        },
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
    }
    write_json(run.path("reports", "validation.json"), result)
    return result
