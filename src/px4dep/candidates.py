"""M5 graded review candidates.

Candidates are review items, not vulnerability or safety findings.  A candidate
must be a real parameter in the current build and must have at least one
program-semantic path: an EKF2 field-sensitive slice, a Commander/Failsafe
wrapper read, or (for the end-to-end tier) a complete explicit uORB path into
the Commander health layer.  Commander ``context`` paths are function-level
proximity only and are never treated as data or control dependencies.  Runtime
evidence can raise or lower review priority, but an unobserved topic is never
treated as evidence of unreachability.

The CSV is a review table derived from ``evidence/candidates.jsonl``; the
JSONL is the authoritative record and includes the explanation path.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Sequence

from .manifest import Run
from .util import stable_id, write_json, write_jsonl


CANDIDATE_COLUMNS = [
    "candidate_id",
    "parameter",
    "status",
    "review_priority",
    "score",
    "end_to_end",
    "path_class",
    "confidence",
    "sink_kinds",
    "path_count",
    "end_to_end_paths",
    "health_layer",
    "failsafe_flags",
    "mode_restrictions",
    "protection_actions",
    "min_depth",
    "runtime_support",
    "runtime_topics",
    "runtime_fields",
    "runtime_flags",
    "unresolved_paths",
    "log_parameter_present",
    "log_parameter_changed",
    "explanation",
    "analysis_scope",
    "commander_paths",
    "context_only",
]

MESSAGE_SINK_KINDS = {"criteria_state", "topic_field", "slice_sink"}

DISCLAIMER = (
    "Review item only: not a vulnerability, safety, or airworthiness finding. "
    "Static paths and runtime presence do not establish causality."
)


def _best_slice_path(paths: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return min(
        paths,
        key=lambda item: (
            0 if item.get("category") == "data" else (
                1 if item.get("category") == "control" else 2
            ),
            int(item.get("depth", 0)),
            item.get("id", ""),
        ),
    )


def _best_m4_path(paths: Sequence[dict[str, Any]]) -> dict[str, Any]:
    def key(item: dict[str, Any]) -> tuple[Any, ...]:
        evidence = item.get("consumer_evidence") or {}
        return (
            0 if evidence.get("health_predicates") else 1,
            0 if item.get("category") == "data" else 1,
            item.get("id", ""),
        )

    return min(paths, key=key)


def _slice_explanation(path: dict[str, Any]) -> dict[str, Any]:
    steps = [
        {
            "kind": step.get("kind", ""),
            "relation": step.get("relation", ""),
            "from": step.get("from", ""),
            "to": step.get("to", ""),
            "caller": step.get("caller", ""),
            "file": step.get("file", ""),
            "line": step.get("line"),
            "expression": step.get("expression") or step.get("condition") or "",
        }
        for step in (path.get("steps") or [])[:20]
    ]
    return {
        "type": "slice",
        "path_id": path.get("id", ""),
        "text": f"{path.get('parameter', '')} -> {path.get('sink', '')}",
        "steps": steps,
        "category": path.get("category", ""),
        "confidence": path.get("confidence", ""),
    }


def _m4_explanation(path: dict[str, Any]) -> dict[str, Any]:
    evidence = path.get("consumer_evidence") or {}
    stages = [
        {
            "stage": step.get("stage", ""),
            "relation": step.get("relation", ""),
            "from": step.get("from", ""),
            "to": step.get("to", ""),
            "edge_id": (step.get("evidence") or {}).get("edge_id", ""),
        }
        for step in (path.get("steps") or [])[:20]
    ]
    chain = [
        path.get("parameter", ""),
        path.get("sink", ""),
        path.get("message_type", ""),
        path.get("topic", ""),
        path.get("producer_function", ""),
        path.get("consumer_function", ""),
    ]
    predicates = []
    for item in evidence.get("health_predicates") or []:
        function = item.get("function", "")
        if function:
            label = f"health:{function}"
            if label not in predicates:
                predicates.append(label)
        if len(predicates) >= 3:
            break
    flags = []
    for item in evidence.get("flags_written") or []:
        target = item.get("target", "")
        if target:
            label = f"flag:{target}"
            if label not in flags:
                flags.append(label)
        if len(flags) >= 3:
            break
    chain.extend(predicates)
    chain.extend(flags)
    return {
        "type": "end_to_end",
        "path_id": path.get("id", ""),
        "text": " -> ".join(part for part in chain if part),
        "stages": stages,
        "category": path.get("category", ""),
        "confidence": path.get("confidence", ""),
        "health_predicates": predicates,
        "flags_written": flags,
        "mode_restrictions": [
            item.get("mode", "") or item.get("relation", "")
            for item in (evidence.get("mode_restrictions") or [])
            if item.get("mode") or item.get("relation")
        ],
    }


def _layers(
    slice_paths: Sequence[dict[str, Any]],
    m4_paths: Sequence[dict[str, Any]],
) -> list[str]:
    layers = ["parameter_source"]
    ekf_paths = [path for path in slice_paths if not path.get("scope")]
    commander_paths = [path for path in slice_paths if path.get("scope") == "commander"]
    if ekf_paths:
        layers.append("ekf2_field")
    if commander_paths:
        layers.append("commander_parameter")
    sink_kinds = {path.get("sink_kind", "") for path in slice_paths}
    for kind in sorted(sink_kinds):
        if kind == "control_flag":
            layers.append("estimator_control_flag")
        elif kind == "fault_flag":
            layers.append("estimator_fault_flag")
        elif kind in MESSAGE_SINK_KINDS:
            layers.append("message_field")
        elif kind == "health_predicate":
            layers.append("health_predicate")
        elif kind in {"failsafe_flag_write", "failsafe_flag_read"}:
            layers.append("failsafe_flag")
        elif kind == "mode_restriction":
            layers.append("mode_restriction")
        elif kind == "protection_check":
            layers.append("protection_condition")
        elif kind in {"protection_action", "protection_action_option"}:
            layers.append("protection_action")
    if m4_paths:
        layers.append("uorb_topic")
        layers.append("commander_consumer")
    evidence = [path.get("consumer_evidence") or {} for path in m4_paths]
    if any(item.get("health_predicates") for item in evidence):
        layers.append("health_predicate")
    if any(item.get("flags_written") for item in evidence):
        layers.append("failsafe_flag_write")
    if any(item.get("mode_restrictions") for item in evidence):
        layers.append("mode_restriction")
    if any(
        chain.get("actions")
        for item in evidence
        for chain in (item.get("protection_chains") or [])
    ):
        layers.append("protection_action")
    return list(dict.fromkeys(layers))


def _aggregate_runtime(
    m4_paths: Sequence[dict[str, Any]],
    runtime_by_path: dict[str, dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], int]:
    records = [
        runtime_by_path[path["id"]]
        for path in m4_paths
        if path.get("id") in runtime_by_path
    ]
    if not m4_paths:
        return "no_explicit_topic", [], [], 0
    if not records:
        return "no_runtime_record", [], [], 0
    if any(record.get("runtime_support") == "field_observed" for record in records):
        support = "field_observed"
    elif any(record.get("runtime_support") == "topic_observed" for record in records):
        support = "topic_observed"
    else:
        support = "boundary_not_observed"

    topics: dict[str, dict[str, Any]] = {}
    fields: dict[str, dict[str, Any]] = {}
    for record in records:
        boundary = record.get("boundary") or {}
        topic = boundary.get("topic") or {}
        if topic.get("topic"):
            topics.setdefault(
                topic["topic"],
                {
                    "topic": topic["topic"],
                    "status": topic.get("status", ""),
                    "samples": topic.get("samples", 0),
                    "instances": topic.get("instances", []),
                },
            )
        field = boundary.get("field") or {}
        if field.get("field"):
            fields.setdefault(
                f"{field.get('topic', '')}::{field['field']}",
                {
                    "topic": field.get("topic", ""),
                    "field": field["field"],
                    "status": field.get("status", ""),
                    "samples_non_nan": field.get("samples_non_nan", 0),
                },
            )
    observed = sum(
        1 for record in records if record.get("runtime_support") == "field_observed"
    )
    return support, sorted(topics.values(), key=lambda item: item["topic"]), sorted(
        fields.values(), key=lambda item: (item["topic"], item["field"])
    ), observed


def build_candidates(
    nodes: Sequence[dict[str, Any]],
    slice_paths: Sequence[dict[str, Any]],
    m4_paths: Sequence[dict[str, Any]],
    runtime_records: Sequence[dict[str, Any]],
    cross_unresolved: Sequence[dict[str, Any]],
    commit: str = "",
    variant: str = "",
    commander_paths: Sequence[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    parameter_nodes = {
        node.get("name", ""): node
        for node in nodes
        if node.get("kind") == "parameter" and node.get("name")
    }
    runtime_by_path = {
        record["path_id"]: record
        for record in runtime_records
        if record.get("kind") == "runtime_path" and record.get("path_id")
    }
    runtime_log = next(
        (
            record
            for record in runtime_records
            if record.get("kind") == "runtime_log"
        ),
        {},
    )
    initial_parameters = set(runtime_log.get("initial_parameter_names") or [])
    changed_parameters = set(runtime_log.get("changed_parameter_names") or [])

    slices_by_parameter: dict[str, list[dict[str, Any]]] = defaultdict(list)
    empty_parameter_paths = 0
    all_slice_paths = [*slice_paths, *(commander_paths or [])]
    for path in all_slice_paths:
        parameter = path.get("parameter", "")
        if not parameter:
            empty_parameter_paths += 1
            continue
        slices_by_parameter[parameter].append(path)

    m4_by_parameter: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in m4_paths:
        m4_by_parameter[path.get("parameter", "")].append(path)

    unresolved_by_parameter: Counter[str] = Counter()
    for record in cross_unresolved:
        parameter = record.get("parameter", "")
        if parameter:
            unresolved_by_parameter[parameter] += 1

    candidates: list[dict[str, Any]] = []
    metrics: Counter[str] = Counter()
    metrics["name_only_candidates"] = 0
    metrics["empty_parameter_paths"] = empty_parameter_paths

    for parameter, paths in sorted(slices_by_parameter.items()):
        parameter_node = parameter_nodes.get(parameter)
        if parameter_node is None or not parameter_node.get("in_build", False):
            metrics["excluded_not_in_build"] += 1
            continue
        metrics["parameters_seen"] += 1
        paths = sorted(paths, key=lambda item: item.get("id", ""))
        m4 = sorted(m4_by_parameter.get(parameter, []), key=lambda item: item.get("id", ""))
        categories = sorted({path.get("category", "") for path in paths})
        sink_kinds = sorted({path.get("sink_kind", "") for path in paths})
        sinks = sorted({path.get("sink", "") for path in paths})
        data_paths = [path for path in paths if path.get("category") == "data"]
        control_paths = [path for path in paths if path.get("category") == "control"]
        context_paths = [path for path in paths if path.get("category") == "context"]
        commander_slice_paths = [
            path for path in paths if path.get("scope") == "commander"
        ]
        analysis_scope = []
        if any(path.get("scope") != "commander" for path in paths):
            analysis_scope.append("ekf2_slice")
        if commander_slice_paths:
            analysis_scope.append("commander_slice")
        confidence = "medium" if data_paths else "low"
        min_depth = min(int(path.get("depth", 0)) for path in paths)
        cross_function = any(path.get("cross_function") for path in paths)
        # Context-only means every program path is function-level proximity, not
        # a data or control dependency.
        context_only = bool(paths) and not data_paths and not control_paths

        evidence = [path.get("consumer_evidence") or {} for path in m4]
        health_layer = any(item.get("health_predicates") for item in evidence) or any(
            path.get("sink_kind") == "health_predicate" for path in paths
        )
        flag_write = any(item.get("flags_written") for item in evidence) or any(
            path.get("sink_kind") == "failsafe_flag_write" for path in paths
        )
        mode_restriction = any(
            item.get("mode_restrictions") for item in evidence
        ) or any(path.get("sink_kind") == "mode_restriction" for path in paths)
        protection_action = any(
            chain.get("actions")
            for item in evidence
            for chain in (item.get("protection_chains") or [])
        ) or any(
            path.get("sink_kind") in {"protection_action", "protection_action_option"}
            for path in paths
        )

        runtime_support, runtime_topics, runtime_fields, observed_paths = (
            _aggregate_runtime(m4, runtime_by_path)
        )
        runtime_flags = sorted(
            {
                record.get("flag", "")
                for record in runtime_records
                if record.get("kind") == "runtime_flag"
                and set(record.get("path_ids") or []) & {p["id"] for p in m4}
                and record.get("flag")
            }
        )
        unresolved_paths = unresolved_by_parameter.get(parameter, 0)

        best_slice = _best_slice_path(paths)
        best_m4 = _best_m4_path(m4) if m4 else None
        explanation = (
            _m4_explanation(best_m4) if best_m4 is not None else _slice_explanation(best_slice)
        )

        score = 0
        reasons: list[str] = []
        if data_paths:
            score += 4
            reasons.append("data_dependency_path")
        elif control_paths:
            score += 1
            reasons.append("control_dependency_only")
        else:
            reasons.append("function_context_only")
        if len(sink_kinds) > 1:
            score += min(len(sink_kinds) - 1, 3)
            reasons.append(f"multiple_sink_kinds={len(sink_kinds)}")
        if any(kind in MESSAGE_SINK_KINDS for kind in sink_kinds):
            score += 2
            reasons.append("message_or_criteria_sink")
        if health_layer:
            score += 3
            reasons.append("health_predicate_reached")
        if flag_write:
            score += 2
            reasons.append("failsafe_flag_written")
        if mode_restriction:
            score += 2
            reasons.append("mode_restriction_reached")
        if protection_action:
            score += 3
            reasons.append("protection_action_reached")
        if runtime_support == "field_observed":
            score += 3
            reasons.append("runtime_field_observed")
        elif runtime_support == "topic_observed":
            score += 2
            reasons.append("runtime_topic_observed")
        if unresolved_paths:
            score -= 2
            reasons.append(f"unresolved_static_paths={unresolved_paths}")
        if min_depth > 4:
            score -= 1
            reasons.append("deep_static_path")

        priority = "high" if score >= 10 else "medium" if score >= 5 else "low"
        status = (
            "needs_review"
            if unresolved_paths or context_only
            else "eligible"
        )
        notes: list[str] = []
        if m4:
            notes.append("M4 显式 topic 路径存在；运行时证据仅表示边界出现，不表示因果。")
        else:
            notes.append(
                "M4 未把该参数的汇点绑定到显式 topic；"
                "runtime_support=no_explicit_topic 表示运行期证据不适用，不代表不可达。"
            )
        if unresolved_paths:
            notes.append("存在未拼接的消息字段路径；保留为待核查，不丢弃。")
        if context_only:
            notes.append(
                "仅有同函数或调用邻近的 Commander 上下文路径，"
                "不构成数据依赖或控制依赖；保留为待核查，置信度 low。"
            )
        elif not data_paths:
            notes.append("只有控制依赖路径（条件分支），置信度为 low。")
        if commander_slice_paths:
            notes.append(
                "Commander/Failsafe 包装参数路径：参数读取位置与汇点均来自静态表达式，"
                "不表示参数取值或运行因果。"
            )
        if runtime_support == "boundary_not_observed":
            notes.append("该日志未观测到边界 topic/field；未观测不等于不可达。")
        if parameter in changed_parameters:
            notes.append("该参数在日志期间发生过参数变更。")
        if parameter not in initial_parameters and runtime_log:
            notes.append("该参数未出现在日志的初始参数快照中。")

        candidate = {
            "id": stable_id("candidate", commit, variant, parameter),
            "kind": "review_candidate",
            "parameter": parameter,
            "parameter_node_id": parameter_node["id"],
            "source": best_slice.get("source", ""),
            "source_id": best_slice.get("source_id", ""),
            "status": status,
            "review_priority": priority,
            "score": score,
            "score_reasons": reasons,
            "disclaimer": DISCLAIMER,
            "path_class": categories,
            "confidence": confidence,
            "cross_function": cross_function,
            "analysis_scope": analysis_scope,
            "sink_kinds": sink_kinds,
            "sinks": sinks,
            "path_count": len(paths),
            "slice_path_ids": [path["id"] for path in paths][:20],
            "commander_path_count": len(commander_slice_paths),
            "commander_path_ids": [
                path["id"] for path in commander_slice_paths
            ][:20],
            "commander_sink_kinds": sorted(
                {path.get("sink_kind", "") for path in commander_slice_paths}
            ),
            "context_path_count": len(context_paths),
            "context_only": context_only,
            "end_to_end": bool(m4),
            "end_to_end_path_count": len(m4),
            "end_to_end_path_ids": [path["id"] for path in m4],
            "layers": _layers(paths, m4),
            "health_layer": health_layer,
            "failsafe_flag_write": flag_write,
            "mode_restriction": mode_restriction,
            "protection_action": protection_action,
            "min_depth": min_depth,
            "runtime_support": runtime_support,
            "runtime_observed_paths": observed_paths,
            "runtime_topics": runtime_topics,
            "runtime_fields": runtime_fields,
            "runtime_flags": runtime_flags,
            "runtime_note": "presence only; not causality, correlation or safety",
            "unresolved_paths": unresolved_paths,
            "log_parameter_present": (
                parameter in initial_parameters if runtime_log else None
            ),
            "log_parameter_changed": (
                parameter in changed_parameters if runtime_log else None
            ),
            "explanation": explanation,
            "alternative_path_ids": [
                path["id"]
                for path in (m4 + paths)
                if path.get("id") != explanation.get("path_id")
            ][:20],
            "notes": notes,
        }
        candidates.append(candidate)

        metrics["candidates"] += 1
        metrics[f"priority_{priority}"] += 1
        metrics[f"status_{status}"] += 1
        metrics["end_to_end"] += int(bool(m4))
        metrics["with_data_path"] += int(bool(data_paths))
        metrics["control_only"] += int(bool(control_paths) and not data_paths)
        metrics["context_only"] += int(context_only)
        metrics["commander_candidates"] += int(bool(commander_slice_paths))
        metrics["commander_data_paths"] += sum(
            1 for path in commander_slice_paths if path.get("category") == "data"
        )
        metrics["commander_control_paths"] += sum(
            1 for path in commander_slice_paths if path.get("category") == "control"
        )
        metrics["commander_context_paths"] += len(context_paths)
        metrics[f"runtime_{runtime_support}"] += 1
        metrics["with_health_layer"] += int(health_layer)
        metrics["with_flag_write"] += int(flag_write)
        metrics["with_mode_restriction"] += int(mode_restriction)
        metrics["with_protection_action"] += int(protection_action)
        metrics["program_semantic_paths"] += len(paths)
        metrics["log_parameter_present"] += int(
            bool(runtime_log) and parameter in initial_parameters
        )
        metrics["log_parameter_changed"] += int(parameter in changed_parameters)

    metrics["runtime_log_present"] = int(bool(runtime_log))
    metrics.setdefault("candidates", 0)
    metrics.setdefault("end_to_end", 0)
    candidates.sort(key=lambda item: (-item["score"], item["parameter"]))
    metrics["unresolved_linked"] = int(sum(unresolved_by_parameter.values()))
    return candidates, dict(metrics)


def write_candidates(
    run: Run,
    candidates: Sequence[dict[str, Any]],
    metrics: dict[str, Any],
) -> None:
    write_jsonl(run.path("evidence", "candidates.jsonl"), candidates)
    write_json(run.path("reports", "candidate_metrics.json"), metrics)

    rows: list[dict[str, Any]] = []
    for item in candidates:
        rows.append(
            {
                "candidate_id": item["id"],
                "parameter": item["parameter"],
                "status": item["status"],
                "review_priority": item["review_priority"],
                "score": item["score"],
                "end_to_end": str(item["end_to_end"]).lower(),
                "path_class": ";".join(item["path_class"]),
                "confidence": item["confidence"],
                "sink_kinds": ";".join(item["sink_kinds"]),
                "path_count": item["path_count"],
                "end_to_end_paths": item["end_to_end_path_count"],
                "health_layer": str(item["health_layer"]).lower(),
                "failsafe_flags": str(item["failsafe_flag_write"]).lower(),
                "mode_restrictions": str(item["mode_restriction"]).lower(),
                "protection_actions": str(item["protection_action"]).lower(),
                "min_depth": item["min_depth"],
                "runtime_support": item["runtime_support"],
                "runtime_topics": ";".join(
                    f"{topic['topic']}={topic['status']}"
                    for topic in item["runtime_topics"]
                ),
                "runtime_fields": ";".join(
                    f"{field['field']}={field['status']}"
                    for field in item["runtime_fields"]
                ),
                "runtime_flags": ";".join(item["runtime_flags"]),
                "unresolved_paths": item["unresolved_paths"],
                "log_parameter_present": item["log_parameter_present"],
                "log_parameter_changed": item["log_parameter_changed"],
                "explanation": item["explanation"]["text"],
                "analysis_scope": ";".join(item["analysis_scope"]),
                "commander_paths": item["commander_path_count"],
                "context_only": str(item["context_only"]).lower(),
            }
        )
    path = run.path("reports", "candidates.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CANDIDATE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
