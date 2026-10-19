"""Human-readable summary and CSV review tables for a run."""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import Config
from .llvm_fallback import m6_output_paths
from .manifest import Run
from .util import read_json, read_jsonl, write_json


TRACEABILITY_COLUMNS = [
    "parameter",
    "in_build",
    "source_kind",
    "wrapper_count",
    "bound_config_fields",
    "config_fields",
    "build_guards",
    "status",
]

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


def _unresolved_counts(run: Run) -> dict[str, int]:
    counts: Counter[str] = Counter()
    # graph.jsonl is the merged unresolved list.  Schema/parameter/Clang-parse
    # failures are not graph edges, so add their dedicated files as well.
    sources = [
        run.path("unresolved", "graph.jsonl"),
        run.path("unresolved", "uorb_schema.jsonl"),
        run.path("unresolved", "parameters.jsonl"),
        run.path("unresolved", "cpp_records.jsonl"),
        run.path("unresolved", "ulog.jsonl"),
    ]
    for path in sources:
        for record in read_jsonl(path):
            counts[record.get("kind", path.stem)] += 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _load_optional(run: Run, *parts: str) -> dict[str, Any]:
    return read_json(run.path(*parts), {}) or {}


def _traceability_rows(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {n["id"]: n for n in nodes}
    fields_by_param: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        if edge.get("relation") == "configures":
            field = by_id.get(edge["dst"], {})
            name = field.get("qualified_name", "")
            if name and name not in fields_by_param[edge["src"]]:
                fields_by_param[edge["src"]].append(name)

    rows = []
    for node in nodes:
        if node.get("kind") != "parameter":
            continue
        fields = sorted(fields_by_param.get(node["id"], []))
        rows.append(
            {
                "parameter": node.get("name", ""),
                "in_build": str(bool(node.get("in_build", False))).lower(),
                "source_kind": node.get("source_kind", ""),
                "wrapper_count": node.get("wrapper_count", 0),
                "bound_config_fields": len(fields),
                "config_fields": ";".join(fields),
                "build_guards": ";".join(node.get("build_guards") or []),
                "status": node.get("status", ""),
            }
        )
    rows.sort(key=lambda row: row["parameter"])
    return rows


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def build_report(cfg: Config, run: Run) -> dict[str, Any]:
    manifest = read_json(run.manifest_json_path, {}) or {}
    parameter_metrics = _load_optional(run, "reports", "parameter_metrics.json")
    cpp_metrics = _load_optional(run, "reports", "cpp_metrics.json")
    uorb_metrics = _load_optional(run, "reports", "uorb_metrics.json")
    flow_metrics = _load_optional(run, "reports", "uorb_flow_metrics.json")
    binding_metrics = _load_optional(run, "reports", "param_binding_metrics.json")
    graph_metrics = _load_optional(run, "reports", "graph_metrics.json")
    health_metrics = _load_optional(run, "reports", "health_chain_metrics.json")
    slice_metrics = _load_optional(run, "reports", "ekf_slice_metrics.json")
    commander_metrics = _load_optional(run, "reports", "commander_slice_metrics.json")
    cross_metrics = _load_optional(run, "reports", "cross_module_metrics.json")
    runtime_metrics = _load_optional(run, "reports", "runtime_metrics.json")
    candidate_metrics = _load_optional(run, "reports", "candidate_metrics.json")
    break_metrics = _load_optional(run, "reports", "break_chains.json")
    llvm_metrics = _load_optional(run, "reports", "llvm_metrics.json")

    nodes = read_jsonl(run.path("graph", "nodes.jsonl"))
    edges = read_jsonl(run.path("graph", "edges.jsonl"))
    runtime_records = read_jsonl(run.path("evidence", "runtime.jsonl"))
    candidates = read_jsonl(run.path("evidence", "candidates.jsonl"))
    llvm_records = read_jsonl(run.path("evidence", "llvm.jsonl"))
    resolutions = read_jsonl(run.path("evidence", "break_chain_resolution.jsonl"))
    llvm_residual = read_jsonl(run.path("unresolved", "llvm.jsonl"))
    unresolved_counts = _unresolved_counts(run)

    trace_rows = _traceability_rows(nodes, edges)
    _write_csv(run.path("reports", "traceability.csv"), TRACEABILITY_COLUMNS, trace_rows)

    required_outputs = [
        "manifest.yaml",
        "manifest.json",
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
        *m6_output_paths(),
    ]
    missing_outputs = [name for name in required_outputs if not run.path(name).exists()]

    relations = graph_metrics.get("edges_by_relation") or {}
    gates = {
        "manifest_status_ok": manifest.get("status") in {"ok", "ok_with_warnings"},
        "cpp_tool_ran": bool(cpp_metrics.get("tus_run")),
        "cpp_record_parse_errors_zero": cpp_metrics.get("record_parse_errors", 0) == 0,
        "parameter_parse_rate_full": parameter_metrics.get("parameter_parse_rate") == 1.0,
        "msg_parse_errors_zero": uorb_metrics.get("msg_parse_errors", 0) == 0,
        "no_dangling_edges": graph_metrics.get("edges_dropped_missing_endpoint", 0) == 0,
        "no_missing_required_outputs": not missing_outputs,
        "no_duplicate_node_ids": graph_metrics.get("duplicate_node_ids", 0) == 0,
    }
    m1_gate = all(gates.values())

    m2_gates = {
        "health_layer_present": health_metrics.get("health_predicates", 0) > 0
        and health_metrics.get("arming_check_predicates", 0) > 0,
        "warning_layer_present": bool(
            (health_metrics.get("effect_counts") or {}).get("health_warning")
        )
        or bool((health_metrics.get("effect_counts") or {}).get("arming_warning"))
        or bool((health_metrics.get("severity_counts") or {}).get("warning")),
        "mode_layer_present": (health_metrics.get("nodes_by_kind") or {}).get("mode_capability", 0) > 0
        and (health_metrics.get("mode_checks", 0) > 0
             or health_metrics.get("mode_availability_predicates", 0) > 0),
        "arming_prohibition_present": relations.get("restricts_arming", 0) > 0,
        "run_prohibition_present": relations.get("restricts_run", 0) > 0,
        "mode_query_present": relations.get("checks_mode", 0) > 0
        or relations.get("queries_mode", 0) > 0,
        "failsafe_flag_layer_present": health_metrics.get("health_flags", 0) > 0
        and health_metrics.get("flag_writes", 0) > 0
        and health_metrics.get("flag_reads", 0) > 0,
        "health_flags_mapped_to_message": health_metrics.get("health_flags_mapped", 0) > 0
        and health_metrics.get("health_flags_unmapped", 0) == 0,
        "protection_condition_present": health_metrics.get("failsafe_checks", 0) > 0,
        "protection_decision_present": health_metrics.get("protection_decisions", 0) > 0
        and relations.get("selects_action", 0) > 0,
        "action_return_or_assignment_present": relations.get("returns_action", 0) > 0
        or relations.get("assigns_action", 0) > 0,
        "layers_not_collapsed": (
            bool(health_metrics.get("health_predicates", 0))
            and bool(health_metrics.get("failsafe_checks", 0))
            and bool(relations.get("restricts_arming", 0))
            and bool(relations.get("selects_action", 0))
        ),
        "no_missing_m2_outputs": not missing_outputs,
        "no_dangling_edges_m2": graph_metrics.get("edges_dropped_missing_endpoint", 0) == 0,
    }
    m2_gate = all(m2_gates.values())

    m3_gates = {
        "parameter_field_binding_complete": slice_metrics.get(
            "parameter_binding_complete", 0
        )
        == 1
        and slice_metrics.get("parameter_fields_used", 0) > 0,
        "parameter_source_reached": slice_metrics.get("distinct_sources", 0) > 0,
        "data_dependency_edges_present": slice_metrics.get("data_edges", 0) > 0,
        "control_dependency_edges_present": slice_metrics.get("control_edges", 0) > 0,
        "data_and_control_separate": (
            relations.get("data_depends_on", 0) > 0
            and relations.get("control_depends_on", 0) > 0
            and slice_metrics.get("data_edges", 0) > 0
            and slice_metrics.get("control_edges", 0) > 0
        ),
        "control_flag_sink_present": slice_metrics.get("sinks_control_flag", 0) > 0,
        "fault_flag_sink_present": slice_metrics.get("sinks_fault_flag", 0) > 0,
        "message_field_sink_present": slice_metrics.get("sinks_message_field", 0) > 0,
        "criteria_state_sink_present": slice_metrics.get("sinks_criteria_state", 0) > 0,
        "cross_function_path_present": slice_metrics.get("cross_function_paths", 0) > 0,
        "slice_paths_have_steps": slice_metrics.get("paths", 0) > 0
        and slice_metrics.get("paths_without_steps", 0) == 0,
        "no_parameter_field_unbound": slice_metrics.get(
            "parameter_fields_unbound", 0
        )
        == 0,
        "no_missing_m3_outputs": not missing_outputs,
        "no_dangling_edges_m3": graph_metrics.get("edges_dropped_missing_endpoint", 0)
        == 0,
    }
    m3_gate = all(m3_gates.values())

    commander_gates = {
        "commander_slice_paths_present": commander_metrics.get("paths", 0) > 0,
        "commander_data_paths_present": commander_metrics.get("data_paths", 0) > 0,
        "commander_control_paths_present": commander_metrics.get("control_paths", 0) > 0,
        "commander_context_paths_labelled": (
            commander_metrics.get("context_only_paths", 0)
            == commander_metrics.get("context_paths", -1)
            and commander_metrics.get("context_paths_without_read_site", 1) == 0
        ),
        "commander_paths_have_expression": (
            commander_metrics.get("paths", 0) > 0
            and commander_metrics.get("paths_without_expression", 1) == 0
            and commander_metrics.get("paths_without_steps", 1) == 0
        ),
        "commander_sink_layers_present": {
            "health_predicate",
            "mode_restriction",
            "failsafe_flag_write",
            "protection_action_option",
        }
        <= set((commander_metrics.get("path_sink_kinds") or {}).keys()),
        "commander_parameters_are_build_parameters": commander_metrics.get(
            "parameters_with_path", 0
        )
        <= commander_metrics.get("parameters_with_wrapper", -1),
        "no_missing_commander_outputs": not [
            name
            for name in (
                "graph/commander_paths.jsonl",
                "normalized/commander_slice_edges.jsonl",
                "unresolved/commander_slice.jsonl",
                "reports/commander_slice_metrics.json",
                "reports/commander_slice.csv",
            )
            if not run.path(name).exists()
        ],
    }
    commander_gate = all(commander_gates.values())

    m4_gates = {
        "cross_module_paths_present": cross_metrics.get("paths", 0) > 0,
        "all_paths_cross_ekf2_commander": (
            cross_metrics.get("paths", 0) > 0
            and cross_metrics.get("paths_without_ekf2_producer", 1) == 0
            and cross_metrics.get("paths_without_commander_consumer", 1) == 0
        ),
        "all_paths_have_topic_field_hop": (
            cross_metrics.get("paths", 0) > 0
            and cross_metrics.get("paths_without_topic_field_hop", 1) == 0
        ),
        "all_paths_have_topic_hop": (
            cross_metrics.get("paths", 0) > 0
            and cross_metrics.get("paths_without_topic_hop", 1) == 0
        ),
        "all_paths_have_edge_evidence": (
            cross_metrics.get("paths", 0) > 0
            and cross_metrics.get("paths_without_edge_evidence", 1) == 0
        ),
        "all_paths_have_complete_boundary": (
            cross_metrics.get("paths", 0) > 0
            and cross_metrics.get("paths_with_incomplete_boundary", 1) == 0
        ),
        "no_name_based_joins": cross_metrics.get("name_based_joins", 1) == 0,
        "health_layer_reached": cross_metrics.get("paths_with_health_predicate", 0) > 0,
        "consumer_paths_have_steps": (
            cross_metrics.get("paths", 0) > 0
            and cross_metrics.get("paths_without_steps", 1) == 0
        ),
        "no_missing_m4_outputs": not missing_outputs,
        "no_dangling_edges_m4": graph_metrics.get("edges_dropped_missing_endpoint", 0)
        == 0,
    }
    m4_gate = all(m4_gates.values())

    build_parameter_ids = {
        node["id"]
        for node in nodes
        if node.get("kind") == "parameter" and node.get("in_build")
    }
    m5_gates = {
        "runtime_evidence_present": bool(runtime_metrics.get("log_present"))
        and runtime_metrics.get("records", 0) > 0,
        "log_matches_build_commit": bool(runtime_metrics.get("commit_match")),
        "all_m4_paths_have_runtime_record": (
            cross_metrics.get("paths", 0) > 0
            and runtime_metrics.get("paths_with_runtime_record", 0)
            == cross_metrics.get("paths", 0)
        ),
        "runtime_interpretation_reachability_only": runtime_metrics.get(
            "interpretation"
        )
        == "reachability_only",
        "not_observed_not_marked_unreachable": runtime_metrics.get(
            "unreachable_claims", 1
        )
        == 0
        and bool(runtime_metrics.get("not_observed_is_not_unreachable")),
        "candidates_present": candidate_metrics.get("candidates", 0) > 0,
        "all_candidates_are_build_parameters": bool(candidates)
        and all(
            candidate.get("parameter_node_id") in build_parameter_ids
            for candidate in candidates
        ),
        "all_candidates_have_program_path": bool(candidates)
        and candidate_metrics.get("program_semantic_paths", 0)
        >= candidate_metrics.get("candidates", 0),
        "no_name_only_candidates": candidate_metrics.get(
            "name_only_candidates", 1
        )
        == 0,
        "all_candidates_have_explanation": bool(candidates)
        and all(
            bool((candidate.get("explanation") or {}).get("text"))
            for candidate in candidates
        ),
        "all_candidates_have_runtime_column": bool(candidates)
        and all("runtime_support" in candidate for candidate in candidates),
        "no_missing_m5_outputs": not missing_outputs,
    }
    m5_gate = all(m5_gates.values())

    break_by_kind = break_metrics.get("by_kind") or []
    break_selection = break_metrics.get("selection") or {}
    break_coverage = break_metrics.get("coverage") or {}
    llvm_pass = llvm_metrics.get("pass") or {}
    llvm_pass_stats = llvm_pass.get("stats") or {}
    llvm_toolchain = llvm_metrics.get("toolchain") or {}
    llvm_linking = llvm_metrics.get("linking") or {}
    llvm_scope = llvm_metrics.get("scope") or {}
    selected_kind = break_selection.get("selected_kind", "")
    applicable_scores = [
        item.get("selection_score", 0)
        for item in break_by_kind
        if item.get("llvm_applicable")
    ]
    m6_gates = {
        "break_chain_distribution_present": break_metrics.get("total", 0) > 0
        and break_metrics.get("total", 0)
        == sum(item.get("count", 0) for item in break_by_kind),
        "distribution_covers_m1_to_m5": set(
            (break_metrics.get("by_stage") or {}).keys()
        )
        == {"M1", "M2", "M3", "M4", "M5"},
        "unresolved_inventory_preserved": bool(break_coverage)
        and break_coverage.get("non_m4_equals_graph_merged") is True
        and break_coverage.get("total", 0) > 0,
        "high_value_type_selected_from_distribution": selected_kind
        == "m4_writer_publish_topic_unresolved"
        and bool(applicable_scores)
        and break_selection.get("selected_score", -1) == max(applicable_scores),
        "llvm_toolchain_present": bool(llvm_toolchain.get("clang_path"))
        and bool(llvm_toolchain.get("opt_path"))
        and bool(llvm_toolchain.get("clang_version")),
        "llvm_pass_ran": llvm_pass_stats.get("publish_aid_source_calls", 0) > 0
        and llvm_pass_stats.get("bindings", -1) == len(llvm_records),
        "llvm_bindings_have_ir_evidence": bool(llvm_records)
        and all(
            (record.get("binding") or {}).get("ekf_member_field_index", 0) > 0
            and (record.get("binding") or {}).get("ekf2_publication_field_index", 0) > 0
            and str((record.get("binding") or {}).get("topic_global", "")).startswith(
                "__orb_"
            )
            for record in llvm_records
        ),
        "llvm_bindings_cross_checked_by_ast": bool(llvm_records)
        and all(
            (record.get("ast_cross_check") or {}).get("passed") is True
            for record in llvm_records
        )
        and llvm_metrics.get("bindings_cross_checked", 0) == len(llvm_records),
        "llvm_scope_gap_recorded": bool(llvm_linking.get("scope_gaps"))
        and bool(
            (llvm_metrics.get("coverage") or {}).get("uncovered_topics")
        ),
        "selected_type_narrowed": bool(resolutions)
        and llvm_linking.get("records", 0) == len(resolutions)
        and llvm_linking.get("unique_topic_records", 0)
        + llvm_linking.get("multiple_topic_records", 0)
        + llvm_linking.get("no_topic_records", 0)
        == len(resolutions)
        and llvm_linking.get("unique_topic_records", 0) > 0,
        "residual_root_cause_recorded": bool(llvm_residual)
        and all(record.get("root_cause_after") for record in resolutions)
        and bool(llvm_linking.get("residual_by_reason")),
        "no_llvm_only_end_to_end_paths": llvm_metrics.get(
            "end_to_end_paths_created", 1
        )
        == 0
        and all(not record.get("end_to_end_claim") for record in llvm_records)
        and all(not record.get("end_to_end_created") for record in resolutions)
        and llvm_scope.get("not_a_general_framework") is True,
        "no_missing_m6_outputs": not [
            name for name in m6_output_paths() if not run.path(name).exists()
        ],
    }
    m6_gate = all(m6_gates.values())

    metrics = {
        "run_id": run.run_id,
        "px4": manifest.get("px4", {}),
        "scope": manifest.get("scope", {}),
        "parameters": parameter_metrics,
        "cpp": cpp_metrics,
        "uorb_schema": {k: v for k, v in uorb_metrics.items() if k != "per_file"},
        "uorb_flow": flow_metrics,
        "parameter_bindings": binding_metrics,
        "graph": graph_metrics,
        "health_chain": health_metrics,
        "ekf_slice": slice_metrics,
        "commander_slice": commander_metrics,
        "cross_module": cross_metrics,
        "call_graph": cross_metrics.get("call_graph", {}),
        "runtime": runtime_metrics,
        "candidates": candidate_metrics,
        "break_chains": break_metrics,
        "llvm": llvm_metrics,
        "unresolved_by_kind": unresolved_counts,
        "outputs_missing": missing_outputs,
        "m1_gates": gates,
        "m1_gate_passed": m1_gate,
        "m2_gates": m2_gates,
        "m2_gate_passed": m2_gate,
        "m3_gates": m3_gates,
        "m3_gate_passed": m3_gate,
        "commander_slice_gates": commander_gates,
        "commander_slice_gate_passed": commander_gate,
        "m4_gates": m4_gates,
        "m4_gate_passed": m4_gate,
        "m5_gates": m5_gates,
        "m5_gate_passed": m5_gate,
        "m6_gates": m6_gates,
        "m6_gate_passed": m6_gate,
    }
    write_json(run.path("reports", "metrics.json"), metrics)

    lines: list[str] = []
    lines.append(f"# PX4 配置依赖实验报告 — run `{run.run_id}`\n")
    px4 = manifest.get("px4", {})
    lines.append("## 基线与可复现性\n")
    lines.append(f"- PX4 commit: `{px4.get('commit', '')}` ({px4.get('describe', '')})")
    lines.append(f"- 构建变体: `{px4.get('build_variant', '')}`，build dir: `{px4.get('build_dir', '')}`")
    lines.append(f"- 仓库 dirty: {px4.get('dirty', 'unknown')}；manifest 状态: `{manifest.get('status', '')}`")
    lines.append(f"- 分析范围: {', '.join((manifest.get('scope') or {}).get('dirs', []))}")
    lines.append(f"- 翻译单元: {len((manifest.get('scope') or {}).get('translation_units', []))} 个\n")

    lines.append("## M1 结果概览\n")
    lines.append("| 项目 | 数量 |")
    lines.append("|---|---:|")
    lines.append(f"| 构建参数 | {parameter_metrics.get('build_parameter_count', 0)} |")
    lines.append(f"| 参数源码 (YAML) 覆盖 | {parameter_metrics.get('build_parameters_with_yaml_source', 0)} |")
    lines.append(f"| 参数 wrapper | {parameter_metrics.get('wrapper_count', 0)} |")
    lines.append(f"| 参数绑定 | {binding_metrics.get('bindings', 0)} |")
    lines.append(f"| uORB message 类型 | {uorb_metrics.get('message_types', 0)} |")
    lines.append(f"| uORB topics | {uorb_metrics.get('topics', 0)} |")
    lines.append(f"| uORB endpoints | {flow_metrics.get('uorb_endpoints', 0)} |")
    lines.append(f"| 图节点 | {graph_metrics.get('nodes', 0)} |")
    lines.append(f"| 图边 | {graph_metrics.get('edges', 0)} |")
    lines.append(f"| 未解析记录 | {sum(unresolved_counts.values())} |\n")

    lines.append("### 节点类型\n")
    lines.append("| kind | count |")
    lines.append("|---|---:|")
    for kind, count in sorted((graph_metrics.get("nodes_by_kind") or {}).items()):
        lines.append(f"| {kind} | {count} |")
    lines.append("")

    lines.append("### 边类型\n")
    lines.append("| relation | count |")
    lines.append("|---|---:|")
    for relation, count in sorted((graph_metrics.get("edges_by_relation") or {}).items()):
        lines.append(f"| {relation} | {count} |")
    lines.append("")

    lines.append("## M2 健康—保护链结构\n")
    lines.append("M2 只建立框架层结构，不生成参数候选；四类结果分开统计。\n")
    severities = health_metrics.get("severity_counts") or {}
    actions = health_metrics.get("actions") or {}
    lines.append("| 层次 | 证据 | 数量 |")
    lines.append("|---|---|---:|")
    lines.append(
        f"| 告警 / 健康报告 | `healthFailure` / `armingCheckFailure` 调用点 | "
        f"{health_metrics.get('health_predicates', 0)} |"
    )
    lines.append(f"| 其中 warning | `effect` 或 `events::Log::Warning` | {severities.get('warning', 0)} |")
    lines.append(f"| 其中 error | `effect` 或 `events::Log::Error/Critical` | {severities.get('error', 0)} |")
    lines.append(
        f"| 模式禁止（arming） | `restricts_arming` 边 | {relations.get('restricts_arming', 0)} |"
    )
    lines.append(
        f"| 模式禁止（run） | `restricts_run` 边 | {relations.get('restricts_run', 0)} |"
    )
    lines.append(
        f"| failsafe flag 写入 | `writes_flag` 边 | {relations.get('writes_flag', 0)} |"
    )
    lines.append(
        f"| failsafe flag 读取 | `reads_flag` 边 | {relations.get('reads_flag', 0)} |"
    )
    lines.append(
        f"| flag 映射到 `.msg` 字段 | `maps_to_field` 边 | {relations.get('maps_to_field', 0)} |"
    )
    lines.append(f"| failsafe 条件 | `CHECK_FAILSAFE` / `checkFailsafe` | {health_metrics.get('failsafe_checks', 0)} |")
    lines.append(
        f"| 保护动作决策 | `FailsafeBase::Action` 节点 | {health_metrics.get('protection_decisions', 0)} |"
    )
    lines.append(f"| 动作选择边 | `selects_action` | {relations.get('selects_action', 0)} |")
    lines.append(
        f"| 动作值驱动（动态） | 无法静态解析的 options | "
        f"{health_metrics.get('failsafe_checks_dynamic_action', 0)} |\n"
    )

    lines.append("### 告警、模式禁止、flag 与动作决策的区分\n")
    lines.append("| 类别 | 图上的落点 | 证据来源 | 本 run 数量 |")
    lines.append("|---|---|---|---:|")
    lines.append(
        "| 告警 / 健康报告 | `predicate(effect=health_warning/arming_warning)` | "
        f"`Report::healthFailure/armingCheckFailure` | {severities.get('warning', 0)} |"
    )
    lines.append(
        "| 模式禁止 | `restricts_arming` / `restricts_run` / `prevents_mode` | "
        f"required_modes 与 `modeCanRun` 的否定判断 | "
        f"{relations.get('restricts_arming', 0) + relations.get('restricts_run', 0) + relations.get('prevents_mode', 0)} |"
    )
    lines.append(
        "| failsafe flag | `health_flag` 节点 + `writes_flag/reads_flag` | "
        f"`failsafe_flags_s` 成员访问与 `CHECK_FAILSAFE` | {health_metrics.get('health_flags', 0)} 个 flag |"
    )
    lines.append(
        "| 动作决策 | `protection_decision` + `selects_action` | "
        f"`FailsafeBase::Action` 枚举与动作参数 | {relations.get('selects_action', 0)} 条选择边 |\n"
    )

    if actions:
        lines.append("保护动作分布：\n")
        lines.append("| action | class | count |")
        lines.append("|---|---|---:|")
        action_class = {
            "None": "none",
            "Warn": "warning",
            "FallbackPosCtrl": "fallback",
            "FallbackAltCtrl": "fallback",
            "FallbackStab": "fallback",
            "Hold": "recovery",
            "RTL": "recovery",
            "Land": "recovery",
            "Descend": "recovery",
            "Disarm": "termination",
            "Terminate": "termination",
        }
        for action, count in sorted(actions.items()):
            lines.append(f"| `{action}` | {action_class.get(action, 'other')} | {count} |")
        lines.append("")
    else:
        lines.append("本 run 没有解析到保护动作枚举值。\n")

    lines.append("### M2 层间关系\n")
    lines.append("| relation | count |")
    lines.append("|---|---:|")
    for relation in sorted(HEALTH_RELATIONS):
        lines.append(f"| `{relation}` | {relations.get(relation, 0)} |")
    lines.append("")

    lines.append("## M3 EKF2 字段敏感反向切片\n")
    lines.append(
        "M3 从 EKF2 的消息字段、control/fault flags 和判据状态出发反向遍历；"
        "数值依赖（`data_depends_on`）与控制依赖（`control_depends_on`）"
        "保持为不同关系。\n"
    )
    lines.append("| 汇点类别 | 汇点节点数 | 写入次数 |")
    lines.append("|---|---:|---:|")
    for label, key in (
        ("control flag", "sinks_control_flag"),
        ("fault flag", "sinks_fault_flag"),
        ("criteria state / innovation", "sinks_criteria_state"),
        ("published message field", "sinks_message_field"),
    ):
        writes_key = key.replace("sinks_", "sink_writes_", 1)
        lines.append(
            f"| {label} | {slice_metrics.get(key, 0)} | "
            f"{slice_metrics.get(writes_key, 0)} |"
        )
    lines.append("")
    lines.append("| 指标 | 数量 |")
    lines.append("|---|---:|")
    lines.append(
        f"| `estimator::parameters` 字段（使用 / 已绑定 / 未绑定） | "
        f"{slice_metrics.get('parameter_fields_used', 0)} / "
        f"{slice_metrics.get('parameter_fields_bound', 0)} / "
        f"{slice_metrics.get('parameter_fields_unbound', 0)} |"
    )
    lines.append(f"| 数据依赖边 | {slice_metrics.get('data_edges', 0)} |")
    lines.append(f"| 控制依赖边 | {slice_metrics.get('control_edges', 0)} |")
    lines.append(f"| 反向路径 | {slice_metrics.get('paths', 0)} |")
    lines.append(
        f"| 其中跨函数路径 | {slice_metrics.get('cross_function_paths', 0)} |"
    )
    lines.append(f"| 到达的参数字段节点 | {slice_metrics.get('distinct_sources', 0)} |")
    lines.append(f"| 边界字段（无本地写入） | {slice_metrics.get('boundary_fields', 0)} |")
    lines.append(f"| 截断的汇点 | {slice_metrics.get('truncated_sinks', 0)} |")
    lines.append("")
    lines.append(
        "边和路径均记录 `dependency_class`、`flow_sensitive=false` 以及源码位置。"
        "共享类成员按 `receiver_expr.field` 做字段敏感匹配，但遍历是流不敏感的，"
        "因此同一字段的多个写入都是候选前驱；这体现在 `memory_predecessors=true` "
        "和路径的 `memory_write` 步骤中，而不是被当作唯一证明。\n"
    )

    lines.append("## M4 跨模块端到端路径\n")
    lines.append(
        "M4 只通过显式图边拼接 EKF2 与 Commander："
        "`topic_field` ←(`defines`) message_type ←(`instantiates`) topic "
        "←(`publishes`/`subscribes`) endpoint ←(`uses_uorb`) function。"
        "每一跳都引用 edge id；没有按名称相似度建立的连接。\n"
    )
    lines.append("| 指标 | 数量 |")
    lines.append("|---|---:|")
    lines.append(f"| M3 路径（汇点为消息字段） | {cross_metrics.get('slice_paths_message_field', 0)} |")
    lines.append(f"| 拼接后的端到端路径 | {cross_metrics.get('paths', 0)} |")
    lines.append(f"| 不同参数 / topic / 生产者 / 消费者 | "
                 f"{cross_metrics.get('distinct_parameters', 0)} / "
                 f"{cross_metrics.get('distinct_topics', 0)} / "
                 f"{cross_metrics.get('distinct_producers', 0)} / "
                 f"{cross_metrics.get('distinct_consumers', 0)} |")
    lines.append(f"| 名称跳连（name-based joins） | {cross_metrics.get('name_based_joins', 0)} |")
    lines.append(
        f"| M3 消息字段路径未能绑定发布 topic | "
        f"{cross_metrics.get('slice_paths_without_publisher_topic', 0)} |"
    )
    lines.append("")
    lines.append("| 消费者侧证据 | 路径数 |")
    lines.append("|---|---:|")
    lines.append(
        f"| 到达健康/arming 谓词 | "
        f"{cross_metrics.get('paths_with_health_predicate', 0)} |"
    )
    lines.append(
        f"| 写入 failsafe flag | "
        f"{cross_metrics.get('paths_with_flag_write', 0)} |"
    )
    lines.append(
        f"| 经 flag 到达保护动作选择 | "
        f"{cross_metrics.get('paths_with_protection_chain', 0)} |"
    )
    lines.append(
        f"| 到达模式可用性限制 | "
        f"{cross_metrics.get('paths_with_mode_restriction', 0)} |"
    )
    lines.append("")
    cross_paths = read_jsonl(run.path("graph", "end_to_end_paths.jsonl"))
    if cross_paths:
        lines.append("| parameter | sink field | topic | producer | consumer | class | health | flags |")
        lines.append("|---|---|---|---|---|---|---:|---:|")
        for path in cross_paths[:20]:
            evidence = path.get("consumer_evidence") or {}
            lines.append(
                f"| `{path.get('parameter', '')}` | "
                f"`{path.get('sink', '')}` | `{path.get('topic', '')}` | "
                f"`{path.get('producer_function', '')}` | "
                f"`{path.get('consumer_function', '')}` | "
                f"{path.get('category', '')} | "
                f"{len(evidence.get('health_predicates') or [])} | "
                f"{len(evidence.get('flags_written') or [])} |"
            )
        if len(cross_paths) > 20:
            lines.append(f"| … | 其余 {len(cross_paths) - 20} 条见 `reports/cross_module.csv` | | | | | | |")
        lines.append("")
    lines.append(
        "边界证据（字段、消息类型、topic、发布/订阅端点、生产者/消费者函数）为 high；"
        "整条路径的置信度继承 M3 的数据（medium）或控制（low）类别。"
        "uORB 订阅之后只声明 `consumer_field_binding=topic_level`："
        "本实验没有生成 Commander 侧的逐字段读取记录，因此不声称消费者读取了该具体字段。\n"
    )
    cross_unresolved = read_jsonl(run.path("unresolved", "cross_module.jsonl"))
    if cross_unresolved:
        counts = Counter(item.get("kind", "") for item in cross_unresolved)
        lines.append("M4 未拼接项：\n")
        lines.append("| kind | count |")
        lines.append("|---|---:|")
        for kind, count in sorted(counts.items()):
            lines.append(f"| `{kind}` | {count} |")
        lines.append("")
        lines.append(
            "主要未拼接原因是消息字段由 helper 写入、随后由另一个函数发布，"
            "静态证据无法把消息实例绑定到具体 topic；这些项保留在 "
            "`unresolved/cross_module.jsonl`，不做名称或类型猜测。\n"
        )

    lines.append("## M5 运行期可达性与候选\n")
    runtime_log = next(
        (record for record in runtime_records if record.get("kind") == "runtime_log"),
        {},
    )
    lines.append(
        "M5 只使用一份授权正常场景日志，检查静态路径的边界 topic/field "
        "是否出现。它不做相关性、因果或参数取值分析；日志中未观测到 "
        "不等于静态或运行期不可达。\n"
    )
    lines.append("### 日志与构建对应\n")
    lines.append("| 项目 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 日志 sha256 | `{str(runtime_log.get('sha256', ''))[:16]}…` |")
    lines.append(f"| ULog 记录固件版本 | `{runtime_log.get('ver_sw', '')}` |")
    lines.append(f"| 构建 commit | `{(manifest.get('px4') or {}).get('commit', '')}` |")
    lines.append(
        f"| commit 对应 | {'PASS' if runtime_metrics.get('commit_match') else 'FAIL'} |"
    )
    lines.append(
        f"| 场景声明（操作者提供，非日志推断） | `{runtime_log.get('scenario_declared', '')}` |"
    )
    lines.append(f"| 时长 | {runtime_log.get('duration_s', 0)} s |")
    lines.append(f"| 已观测 topic | {runtime_log.get('logged_topics', 0)} |")
    lines.append(
        f"| 日志消息格式 | {runtime_log.get('message_formats', 0)} |"
    )
    lines.append(
        f"| 初始参数 / 日志期间变更参数 | "
        f"{runtime_log.get('initial_parameter_count', 0)} / "
        f"{runtime_log.get('changed_parameter_count', 0)} |"
    )
    lines.append("")

    lines.append("### 静态边界的运行期观测\n")
    lines.append("| 指标 | 数量 |")
    lines.append("|---|---:|")
    lines.append(
        f"| 图 topic：observed / not_observed / not_logged | "
        f"{runtime_metrics.get('graph_topics_observed', 0)} / "
        f"{runtime_metrics.get('graph_topics_not_observed', 0)} / "
        f"{runtime_metrics.get('graph_topics_not_logged', 0)} |"
    )
    lines.append(
        f"| 图 field：observed / not_observed / not_logged | "
        f"{runtime_metrics.get('graph_fields_observed', 0)} / "
        f"{runtime_metrics.get('graph_fields_not_observed', 0)} / "
        f"{runtime_metrics.get('graph_fields_not_logged', 0)} |"
    )
    lines.append(
        f"| M4 路径边界：topic observed / field observed | "
        f"{runtime_metrics.get('paths_topic_observed', 0)} / "
        f"{runtime_metrics.get('paths_field_observed', 0)} "
        f"（共 {runtime_metrics.get('paths', 0)}） |"
    )
    lines.append(
        f"| failsafe flag：observed / not_observed / not_logged | "
        f"{runtime_metrics.get('flags_observed', 0)} / "
        f"{runtime_metrics.get('flags_not_observed', 0)} / "
        f"{runtime_metrics.get('flags_not_logged', 0)} |"
    )
    lines.append("")

    runtime_by_path = {
        record.get("path_id"): record
        for record in runtime_records
        if record.get("kind") == "runtime_path"
    }
    if cross_paths:
        lines.append("| parameter | topic | sink field | topic 观测 | field 观测 | 支持级别 |")
        lines.append("|---|---|---|---|---|---|")
        for path in cross_paths:
            record = runtime_by_path.get(path["id"], {})
            boundary = record.get("boundary") or {}
            topic_evidence = boundary.get("topic") or {}
            field_evidence = boundary.get("field") or {}
            lines.append(
                f"| `{path.get('parameter', '')}` | `{path.get('topic', '')}` | "
                f"`{path.get('sink', '')}` | {topic_evidence.get('status', '')} | "
                f"{field_evidence.get('status', '')} | "
                f"`{record.get('runtime_support', '')}` |"
            )
        lines.append("")

    lines.append("### 候选分级（审查排序，不是安全结论）\n")
    lines.append("| 指标 | 数量 |")
    lines.append("|---|---:|")
    lines.append(f"| 候选参数 | {candidate_metrics.get('candidates', 0)} |")
    lines.append(f"| 其中端到端路径 | {candidate_metrics.get('end_to_end', 0)} |")
    lines.append(f"| 有数据依赖路径 | {candidate_metrics.get('with_data_path', 0)} |")
    lines.append(f"| 仅控制依赖 | {candidate_metrics.get('control_only', 0)} |")
    lines.append(
        f"| 含 Commander/Failsafe 路径 | "
        f"{candidate_metrics.get('commander_candidates', 0)} |"
    )
    lines.append(
        f"| 仅函数上下文邻近 | {candidate_metrics.get('context_only', 0)} |"
    )
    lines.append(f"| 运行期 field 观测 | {candidate_metrics.get('runtime_field_observed', 0)} |")
    lines.append(f"| 运行期 topic 观测 | {candidate_metrics.get('runtime_topic_observed', 0)} |")
    lines.append(f"| 无显式 topic（运行期不适用） | {candidate_metrics.get('runtime_no_explicit_topic', 0)} |")
    lines.append(f"| 待核查（含未拼接路径） | {candidate_metrics.get('status_needs_review', 0)} |")
    lines.append("")
    if candidates:
        lines.append(
            "| parameter | 优先级 | 分数 | 状态 | 端到端 | 数据/控制 | 运行期支持 | 未拼接路径 |"
        )
        lines.append("|---|---|---:|---|---|---|---|---:|")
        for candidate in candidates[:20]:
            lines.append(
                f"| `{candidate.get('parameter', '')}` | "
                f"{candidate.get('review_priority', '')} | "
                f"{candidate.get('score', 0)} | "
                f"{candidate.get('status', '')} | "
                f"{'yes' if candidate.get('end_to_end') else 'no'} | "
                f"{'/'.join(candidate.get('path_class') or [])} | "
                f"`{candidate.get('runtime_support', '')}` | "
                f"{candidate.get('unresolved_paths', 0)} |"
            )
        if len(candidates) > 20:
            lines.append(
                f"| … | 其余 {len(candidates) - 20} 个见 `reports/candidates.csv` | | | | | | |"
            )
        lines.append("")
        lines.append(
            "每条候选在 `evidence/candidates.jsonl` 中保留最短解释路径、"
            "替代路径、汇点类别、运行期边界状态和降级原因。"
            "`review_priority` 仅用于人工审查排序，不是漏洞、适航或飞行安全结论；"
            "`runtime_support=no_explicit_topic` 表示 M4 未绑定显式 topic，"
            "运行期证据不适用，不代表不可达。\n"
        )
    else:
        lines.append("本 run 没有候选参数。\n")

    lines.append("## Commander/Failsafe 参数切片扩展\n")
    lines.append(
        "该扩展把 Commander/Failsafe 的 `px4::params::NAME` 包装成员读取作为切片源，"
        "汇点为健康/arming 谓词、failsafe flag 写入、模式限制和保护动作选择。"
        "`data` 路径要求参数包装 token 精确出现在汇点表达式内；"
        "`control` 路径要求它出现在汇点的外层条件内；"
        "`context` 路径只表示同函数或至多两跳反向调用邻近，"
        "不构成数据或控制依赖，全部标记为 `context_only=true`、`confidence=low`。"
        "参数名本身从不作为证据，所有路径都绑定具体表达式和源码位置。\n"
    )
    lines.append("| 指标 | 数量 |")
    lines.append("|---|---:|")
    lines.append(
        f"| 构建内参数总数 | {commander_metrics.get('parameters_in_build', 0)} |"
    )
    lines.append(
        f"| Commander 包装参数（构建内） | "
        f"{commander_metrics.get('parameters_with_wrapper', 0)} |"
    )
    lines.append(
        f"| 有静态路径的参数 | {commander_metrics.get('parameters_with_path', 0)} |"
    )
    lines.append(
        f"| 直接数据路径参数 / 控制路径参数 | "
        f"{commander_metrics.get('direct_parameters', 0)} / "
        f"{commander_metrics.get('control_parameters', 0)} |"
    )
    lines.append(
        f"| 有上下文路径的参数 | {commander_metrics.get('context_parameters', 0)} |"
    )
    lines.append(
        f"| 仅上下文路径的参数 | "
        f"{commander_metrics.get('context_only_parameters', 0)} |"
    )
    lines.append(
        f"| data / control / context 路径 | "
        f"{commander_metrics.get('data_paths', 0)} / "
        f"{commander_metrics.get('control_paths', 0)} / "
        f"{commander_metrics.get('context_paths', 0)} |"
    )
    lines.append(
        f"| 无路径包装参数 | "
        f"{commander_metrics.get('parameters_without_path_count', 0)} |"
    )
    lines.append(
        f"| 未解析记录 | {commander_metrics.get('unresolved', 0)} |"
    )
    lines.append("")
    sink_counts = commander_metrics.get("path_sink_kinds") or {}
    if sink_counts:
        lines.append("| 路径 sink kind | 路径数 |")
        lines.append("|---|---:|")
        for kind, count in sorted(sink_counts.items()):
            lines.append(f"| `{kind}` | {count} |")
        lines.append("")
    commander_paths = read_jsonl(run.path("graph", "commander_paths.jsonl"))
    if commander_paths:
        ranked = sorted(
            commander_paths,
            key=lambda item: (
                0 if item.get("category") == "data" else (
                    1 if item.get("category") == "control" else 2
                ),
                item.get("parameter", ""),
                item.get("sink", ""),
            ),
        )
        lines.append(
            "| parameter | wrapper | sink kind | 层级 | 类别 | context | 汇点位置 | 条件/表达式位置 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for path in ranked[:25]:
            step = (path.get("steps") or [{}])[0]
            sink_qname = path.get("sink", "")
            sink_site = sink_qname.rsplit("@", 1)[1] if "@" in sink_qname else "—"
            lines.append(
                f"| `{path.get('parameter', '')}` | "
                f"`{path.get('source', '')}` | "
                f"`{path.get('sink_kind', '')}` | "
                f"{path.get('sink_layer', '')} | "
                f"{path.get('category', '')} | "
                f"{'yes' if path.get('context_only') else 'no'} | "
                f"`{sink_site}` | "
                f"`{step.get('file', '')}:{step.get('line', 0)}` |"
            )
        if len(ranked) > 25:
            lines.append(
                f"| … | 其余 {len(ranked) - 25} 条见 "
                "`reports/commander_slice.csv` | | | | | | |"
            )
        lines.append("")
        lines.append(
            "context 路径单独保留在 `graph/commander_paths.jsonl` 与 "
            "`unresolved/commander_slice.jsonl`，不写入 `graph/edges.jsonl` 的"
            "依赖关系，避免把函数邻近误报成数据/控制依赖。\n"
        )
    else:
        lines.append("本 run 没有 Commander/Failsafe 参数切片路径。\n")

    lines.append("## M6 断链统计与定向 LLVM pass\n")
    lines.append(
        "M6 先统计 M1—M5 的全部未解析记录，再只对得分最高的可 IR 解析类型"
        "实现一个范围很窄的 LLVM 14 pass。它不是通用 LLVM 分析框架；"
        "LLVM 证据只用于把消息实例绑定到 topic，不单独生成端到端路径。\n"
    )
    lines.append("### 断链分布（每条原始记录都保留）\n")
    lines.append("| stage | unresolved kind | root cause | count | 参数数 | 关联 M3 路径 | IR 适用 |")
    lines.append("|---|---|---|---:|---:|---:|---|")
    for item in break_by_kind:
        lines.append(
            f"| {item.get('stage', '')} | `{item.get('unresolved_kind', '')}` | "
            f"`{item.get('root_cause', '')}` | {item.get('count', 0)} | "
            f"{item.get('distinct_parameters', 0)} | "
            f"{item.get('path_linked_records', 0)} | "
            f"{'yes' if item.get('llvm_applicable') else 'no'} |"
        )
    lines.append("")
    lines.append(
        f"合计 **{break_metrics.get('total', 0)}** 条。`unresolved/graph.jsonl` "
        "是 M1/M2/M3 的合并视图，未重复计数；仅用作覆盖校验："
        f"非 M4 记录 {break_coverage.get('non_m4_break_chains', 0)} 条，"
        f"合并视图 {break_coverage.get('graph_merged_unresolved_records', 0)} 条，"
        f"一致={break_coverage.get('non_m4_equals_graph_merged')}。\n"
    )
    lines.append("### 高价值类型选择\n")
    lines.append(
        "选择规则：在可适用定向 IR 的类型中，取 "
        "`count + 5×distinct_parameters + 3×path_linked_records` 最大者。\n"
    )
    lines.append("| 项目 | 值 |")
    lines.append("|---|---|")
    lines.append(
        f"| 选中类型 | `{break_selection.get('selected_kind', '')}` |"
    )
    lines.append(f"| 得分 | {break_selection.get('selected_score', 0)} |")
    runner_up = break_selection.get("runner_up_kind", "")
    lines.append(
        f"| 次优可适用类型 | "
        f"{('`' + runner_up + '`') if runner_up else '（无其他可适用类型）'} "
        f"({break_selection.get('runner_up_score', 0)}) |"
    )
    lines.append(
        f"| 选择理由 | {break_selection.get('why', '')} |"
    )
    lines.append("")
    lines.append("### 定向 LLVM pass 证据\n")
    lines.append(
        "pass 只处理 `EKF2::PublishAidSourceStatus<...>` 调用点，"
        "在 `-O0 -g` IR 上读取 EKF2 发布成员与 `Ekf::aid_src_*` 访问器的"
        "结构体字段索引，以及 `__orb_*` 元数据操作数。\n"
    )
    lines.append("| 项目 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| Clang | `{llvm_toolchain.get('clang_version', '')}` |")
    lines.append(f"| opt | `{llvm_toolchain.get('opt_version', '')}` |")
    lines.append(
        f"| pass 插件 sha256 | `{str((llvm_metrics.get('plugin') or {}).get('plugin_sha256', ''))[:16]}…` |"
    )
    lines.append(
        f"| IR sha256 | `{str((llvm_metrics.get('ir') or {}).get('ir_sha256', ''))[:16]}…` |"
    )
    lines.append(f"| 优化级别 | `{llvm_scope.get('optimization', '')}` |")
    lines.append(
        f"| 发布调用点 / 绑定 / 未解析 | "
        f"{llvm_pass_stats.get('publish_aid_source_calls', 0)} / "
        f"{llvm_pass_stats.get('bindings', 0)} / "
        f"{llvm_pass_stats.get('unresolved', 0)} |"
    )
    lines.append(
        f"| AST 交叉校验通过 | {llvm_metrics.get('bindings_cross_checked', 0)} / "
        f"{len(llvm_records)} |"
    )
    lines.append(
        f"| 作用域外的 aid-source 发布 topic | "
        f"{len((llvm_metrics.get('coverage') or {}).get('uncovered_topics') or [])} "
        f"（`{'; '.join((llvm_metrics.get('coverage') or {}).get('uncovered_topics') or [])}`） |"
    )
    lines.append("")
    if llvm_records:
        lines.append("| topic | message type | accessor | EKF 字段 | EKF2 发布字段 | AST 校验 |")
        lines.append("|---|---|---|---:|---:|---|")
        for record in llvm_records[:10]:
            binding = record.get("binding") or {}
            lines.append(
                f"| `{record.get('topic', '')}` | `{record.get('message_type', '')}` | "
                f"`{record.get('accessor_function', '')}` | "
                f"{binding.get('ekf_member_field_index', 0)} | "
                f"{binding.get('ekf2_publication_field_index', 0)} | "
                f"{'PASS' if (record.get('ast_cross_check') or {}).get('passed') else 'FAIL'} |"
            )
        if len(llvm_records) > 10:
            lines.append(
                f"| … | 其余 {len(llvm_records) - 10} 条见 `reports/llvm_bindings.csv` | | | | |"
            )
        lines.append("")
    lines.append("### 选中类型的收窄结果\n")
    lines.append(
        "对 218 条 M4 writer→topic 断链，先用 LLVM 绑定与 AST 写入者证据"
        "求候选 topic，再按 M3 路径优先取直接写入者（无直接写入者时退回"
        "该汇点的全部写入者）。多个候选不会被强行合并成一个。\n"
    )
    lines.append("| 指标 | 数量 |")
    lines.append("|---|---:|")
    lines.append(f"| 选中类型断链 | {llvm_linking.get('records', 0)} |")
    lines.append(
        f"| 唯一 topic / 多个 topic / 无 topic | "
        f"{llvm_linking.get('unique_topic_records', 0)} / "
        f"{llvm_linking.get('multiple_topic_records', 0)} / "
        f"{llvm_linking.get('no_topic_records', 0)} |"
    )
    lines.append(
        f"| 有候选 topic | {llvm_linking.get('records_with_topic_candidate', 0)} |"
    )
    lines.append(
        f"| 找到 Commander 消费者 | "
        f"{llvm_linking.get('records_with_commander_consumer', 0)} |"
    )
    downstream_coverage = llvm_metrics.get("downstream_coverage") or {}
    lines.append(
        f"| aid-source topic 中有 / 无 Commander 订阅者 | "
        f"{downstream_coverage.get('with_commander_subscriber', 0)} / "
        f"{downstream_coverage.get('without_commander_subscriber', 0)} "
        f"（共 {downstream_coverage.get('aid_source_topics', 0)}） |"
    )
    lines.append(
        f"| 写入者函数：可映射 / 不可映射 | "
        f"{llvm_linking.get('writer_functions_mapped', 0)} / "
        f"{llvm_linking.get('writer_functions_unmapped', 0)} |"
    )
    lines.append(
        f"| 路径归属：直接写入者 / 汇点写入者回退 | "
        f"{(llvm_linking.get('path_attribution_counts') or {}).get('direct_writer', 0)} / "
        f"{(llvm_linking.get('path_attribution_counts') or {}).get('sink_writers_fallback', 0)} |"
    )
    lines.append("")
    if resolutions:
        status_counts = Counter(record.get("status", "") for record in resolutions)
        lines.append("| status | 记录数 |")
        lines.append("|---|---:|")
        for status, count in sorted(status_counts.items()):
            lines.append(f"| `{status}` | {count} |")
        lines.append("")
    lines.append("### 剩余断链原因（M6 之后）\n")
    residual_reasons = llvm_linking.get("residual_by_reason") or {}
    if residual_reasons:
        lines.append("| reason | count |")
        lines.append("|---|---:|")
        for reason, count in residual_reasons.items():
            lines.append(f"| `{reason}` | {count} |")
        lines.append("")
    lines.append(
        "这些剩余项写入 `unresolved/llvm.jsonl`，原因分级为分析缺口，"
        "不是“不可达”。其中最主要的是已索引的 Commander 范围里没有"
        " `estimator_aid_src_*` 的订阅者；这是静态范围观察，"
        "不表示运行期没有消费者。\n"
    )
    lines.append("### M6 范围限制\n")
    for limitation in llvm_metrics.get("limitations") or []:
        lines.append(f"- {limitation}")
    lines.append("")

    lines.append("## 构建条件（build guards）\n")
    guard_counts = Counter()
    guard_unknown = 0
    for node in nodes:
        for guard in node.get("build_guards") or []:
            guard_counts[guard] += 1
        if node.get("build_guard_status") == "unknown":
            guard_unknown += 1
    if guard_counts:
        lines.append("| guard | 受影响节点 |")
        lines.append("|---|---:|")
        for guard, count in guard_counts.most_common(30):
            lines.append(f"| `{guard}` | {count} |")
        lines.append("")
    else:
        lines.append("本 run 的范围内没有节点受条件编译保护。\n")
    lines.append(f"guard 状态未知的节点: {guard_unknown}\n")

    lines.append("## 未解析项\n")
    if unresolved_counts:
        lines.append("| kind | count |")
        lines.append("|---|---:|")
        for kind, count in unresolved_counts.items():
            lines.append(f"| {kind} | {count} |")
        lines.append("")
        lines.append("详细记录见 `unresolved/`。\n")
    else:
        lines.append("无未解析记录。\n")

    lines.append("## M1 完成判据\n")
    lines.append("| gate | result |")
    lines.append("|---|---|")
    for name, value in gates.items():
        lines.append(f"| {name} | {'PASS' if value else 'FAIL'} |")
    if missing_outputs:
        lines.append("")
        lines.append("缺失输出: " + ", ".join(missing_outputs))
    lines.append("")
    lines.append(f"**M1 gate: {'PASS' if m1_gate else 'FAIL'}**\n")

    lines.append("## M2 完成判据\n")
    lines.append("| gate | result |")
    lines.append("|---|---|")
    for name, value in m2_gates.items():
        lines.append(f"| {name} | {'PASS' if value else 'FAIL'} |")
    lines.append("")
    lines.append(f"**M2 gate: {'PASS' if m2_gate else 'FAIL'}**\n")

    lines.append("## M3 完成判据\n")
    lines.append("| gate | result |")
    lines.append("|---|---|")
    for name, value in m3_gates.items():
        lines.append(f"| {name} | {'PASS' if value else 'FAIL'} |")
    lines.append("")
    lines.append(f"**M3 gate: {'PASS' if m3_gate else 'FAIL'}**\n")

    lines.append("## Commander/Failsafe 扩展完成判据\n")
    lines.append("| gate | result |")
    lines.append("|---|---|")
    for name, value in commander_gates.items():
        lines.append(f"| {name} | {'PASS' if value else 'FAIL'} |")
    lines.append("")
    lines.append(
        f"**Commander slice gate: "
        f"{'PASS' if commander_gate else 'FAIL'}**\n"
    )

    lines.append("## M4 完成判据\n")
    lines.append("| gate | result |")
    lines.append("|---|---|")
    for name, value in m4_gates.items():
        lines.append(f"| {name} | {'PASS' if value else 'FAIL'} |")
    lines.append("")
    lines.append(f"**M4 gate: {'PASS' if m4_gate else 'FAIL'}**\n")

    lines.append("## M5 完成判据\n")
    lines.append("| gate | result |")
    lines.append("|---|---|")
    for name, value in m5_gates.items():
        lines.append(f"| {name} | {'PASS' if value else 'FAIL'} |")
    lines.append("")
    lines.append(f"**M5 gate: {'PASS' if m5_gate else 'FAIL'}**\n")

    lines.append("## M6 完成判据\n")
    lines.append("| gate | result |")
    lines.append("|---|---|")
    for name, value in m6_gates.items():
        lines.append(f"| {name} | {'PASS' if value else 'FAIL'} |")
    lines.append("")
    lines.append(f"**M6 gate: {'PASS' if m6_gate else 'FAIL'}**\n")

    lines.append("## 说明\n")
    lines.append(
        "- `candidates.csv` 和 `evidence/candidates.jsonl` 列出审查候选；"
        "候选只表示需要进一步人工核查，不表示漏洞或危险参数。"
    )
    lines.append(
        "- `evidence/runtime.jsonl` 只记录正常场景日志中的 topic/field 出现情况；"
        "未观测到不会被写成不可达，且不提供因果或相关性证据。"
    )
    lines.append("- `traceability.csv` 仅陈述参数 → wrapper → 配置字段的静态证据，不代表运行期因果性。")
    lines.append(
        "- `ekf_slice.csv` 和 `graph/paths.jsonl` 逐条列出数据/控制依赖路径；"
        "路径中的 `memory_write` 是字段敏感但流不敏感的内存前驱。"
    )
    lines.append(
        "- `cross_module.csv` 和 `graph/end_to_end_paths.jsonl` 逐条列出 "
        "EKF2→uORB→Commander 路径；`calls` 边只连接已索引函数，"
        "消费者侧证据经调用边到达，不按函数名匹配。"
    )
    lines.append(
        "- `health_chain.csv` 逐条列出健康/模式/保护谓词；`reports_flag` 中 "
        "`rule=same_function_scope` 的边是低置信度的同函数结构关联，不是数据流。"
    )
    lines.append(
        "- 动态 mode 集合和动态 protection action 保留在 `unresolved/health_chain.jsonl`，"
        "不强行赋值为某个已知模式或动作。"
    )
    lines.append("- 所有路径均来自 Clang AST 或 `.msg` 语法；未使用名称相似度生成候选。")
    lines.append(
        "- `reports/break_chains.json` 保留 M1—M5 每一条未解析记录及其根因；"
        "`unresolved/graph.jsonl` 只作为合并视图做覆盖校验，不重复计数。"
    )
    lines.append(
        "- `evidence/llvm.jsonl` 是 LLVM IR 证据，`evidence/break_chain_resolution.jsonl` "
        "是 IR + AST 写入者 + M3 路径的连接结果；两者都不创建端到端路径，"
        "`end_to_end_created=false`。"
    )
    lines.append(
        "- M6 的 `unresolved/llvm.jsonl` 记录收窄后仍存在的分析缺口；"
        "“没有 Commander 消费者”是已索引静态范围的观察，不是运行期不可达结论。"
    )
    lines.append("- PX4 仓库保持只读，本报告未修改参数、未注入传感器数据。\n")

    run.path("reports", "summary.md").write_text("\n".join(lines), encoding="utf-8")
    return metrics
