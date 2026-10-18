"""M5 ULog reachability evidence.

This module answers only one runtime question: were the topics and fields on a
static path present in one authorized, normal-scenario log?  It deliberately
does not compute correlations, causality, parameter effects, or "unreachable"
conclusions.  An absent topic is reported as ``not_observed`` (or
``not_logged``) and is never converted into a negative reachability claim.

The pyulog dependency is imported lazily so the graph-linking logic can be
unit-tested with a synthetic :class:`LogIndex`.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .manifest import Run, record_ulog_input
from .util import sha256_file, stable_id, write_json, write_jsonl


OBSERVED = "observed"
NOT_OBSERVED = "not_observed"
NOT_LOGGED = "not_logged"
NOT_APPLICABLE = "not_applicable_constant"

INTERPRETATION = "presence_only_not_causality"


@dataclass(frozen=True)
class DatasetView:
    """One ULog dataset instance (a topic may have several instances)."""

    name: str
    multi_id: int
    columns: Mapping[str, Sequence[Any]]

    def sample_count(self) -> int:
        if "timestamp" in self.columns:
            return len(self.columns["timestamp"])
        return max((len(values) for values in self.columns.values()), default=0)

    def matching_columns(self, field_name: str) -> list[str]:
        prefixes = (field_name + "[", field_name + ".")
        return [
            name
            for name in self.columns
            if name == field_name or name.startswith(prefixes)
        ]

    def finite_samples(self, field_name: str) -> int:
        total = 0
        for column in self.matching_columns(field_name):
            total += _finite_count(self.columns[column])
        return total


def _finite_count(values: Sequence[Any]) -> int:
    try:
        import numpy as np

        array = np.asarray(values)
        if array.size == 0:
            return 0
        if array.dtype.kind in "fc":
            return int(np.isfinite(array).sum())
        return int(array.size)
    except Exception:
        count = 0
        for value in values:
            try:
                if math.isfinite(float(value)):
                    count += 1
            except (TypeError, ValueError):
                continue
        return count


@dataclass
class LogIndex:
    """Normalized view of the parts of a ULog needed for reachability."""

    path: Path
    sha256: str
    size_bytes: int
    scenario: str
    ver_sw: str
    start_timestamp: int
    last_timestamp: int
    datasets: dict[str, list[DatasetView]] = field(default_factory=dict)
    message_formats: set[str] = field(default_factory=set)
    initial_parameters: dict[str, Any] = field(default_factory=dict)
    changed_parameters: list[tuple[int, str, Any]] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return max(0.0, (self.last_timestamp - self.start_timestamp) / 1e6)

    def dataset_instances(self, topic: str) -> list[DatasetView]:
        return list(self.datasets.get(topic, []))

    def topic_samples(self, topic: str) -> int:
        return sum(view.sample_count() for view in self.dataset_instances(topic))

    def topic_status(self, topic: str) -> str:
        if any(view.sample_count() > 0 for view in self.dataset_instances(topic)):
            return OBSERVED
        if topic in self.message_formats:
            return NOT_OBSERVED
        return NOT_LOGGED

    def topic_instances(self, topic: str) -> list[int]:
        return sorted(
            view.multi_id
            for view in self.dataset_instances(topic)
            if view.sample_count() > 0
        )

    def topic_time_range(self, topic: str) -> tuple[float, float] | None:
        stamps: list[float] = []
        for view in self.dataset_instances(topic):
            values = view.columns.get("timestamp")
            if values is None or len(values) == 0:
                continue
            stamps.append(float(min(values)))
            stamps.append(float(max(values)))
        if not stamps:
            return None
        return (
            round((min(stamps) - self.start_timestamp) / 1e6, 3),
            round((max(stamps) - self.start_timestamp) / 1e6, 3),
        )

    def field_status(self, topic: str, field_name: str) -> tuple[str, int, list[int]]:
        topic_status = self.topic_status(topic)
        if topic_status != OBSERVED:
            return topic_status, 0, []
        total = 0
        instances: list[int] = []
        for view in self.dataset_instances(topic):
            count = view.finite_samples(field_name)
            if count:
                total += count
                instances.append(view.multi_id)
        if total:
            return OBSERVED, total, sorted(set(instances))
        return NOT_OBSERVED, 0, []

    def parameter_names(self) -> set[str]:
        return set(self.initial_parameters or {})

    def changed_parameter_names(self) -> set[str]:
        return {str(item[1]) for item in self.changed_parameters if len(item) >= 2}


def load_ulog(path: str | Path, scenario: str = "") -> LogIndex:
    """Read a ULog with pyulog and build a :class:`LogIndex`."""
    from pyulog import ULog

    log_path = Path(path).expanduser().resolve()
    ulog = ULog(str(log_path))
    datasets: dict[str, list[DatasetView]] = defaultdict(list)
    for data in ulog.data_list:
        datasets[data.name].append(
            DatasetView(name=data.name, multi_id=int(data.multi_id), columns=data.data)
        )
    return LogIndex(
        path=log_path,
        sha256=sha256_file(log_path),
        size_bytes=log_path.stat().st_size,
        scenario=scenario,
        ver_sw=str((ulog.msg_info_dict or {}).get("ver_sw", "")),
        start_timestamp=int(ulog.start_timestamp or 0),
        last_timestamp=int(ulog.last_timestamp or 0),
        datasets=dict(datasets),
        message_formats=set((ulog.message_formats or {}).keys()),
        initial_parameters=dict(ulog.initial_parameters or {}),
        changed_parameters=list(ulog.changed_parameters or []),
    )


def _topic_node_fields(nodes: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    fields_by_message: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        if node.get("kind") == "topic_field":
            fields_by_message[node.get("message_type", "")].append(node)
    return fields_by_message


def _topic_record(log: LogIndex, node: dict[str, Any]) -> dict[str, Any]:
    topic = node.get("name", "")
    status = log.topic_status(topic)
    time_range = log.topic_time_range(topic)
    return {
        "id": stable_id("ulog_topic", log.sha256, topic),
        "kind": "runtime_topic",
        "topic": topic,
        "node_id": node["id"],
        "message_type": node.get("message_type", ""),
        "status": status,
        "message_format_present": topic in log.message_formats,
        "instances": log.topic_instances(topic),
        "samples": log.topic_samples(topic),
        "start_s": time_range[0] if time_range else None,
        "end_s": time_range[1] if time_range else None,
        "interpretation": INTERPRETATION,
        "not_observed_means_unreachable": False,
    }


def _field_record(
    log: LogIndex, topic: str, field_node: dict[str, Any]
) -> dict[str, Any]:
    field_name = field_node.get("name", "")
    if field_node.get("is_constant"):
        status, samples, instances = NOT_APPLICABLE, 0, []
    else:
        status, samples, instances = log.field_status(topic, field_name)
    return {
        "id": stable_id("ulog_field", log.sha256, topic, field_node["id"]),
        "kind": "runtime_field",
        "topic": topic,
        "field": field_name,
        "field_node_id": field_node["id"],
        "message_type": field_node.get("message_type", ""),
        "status": status,
        "is_constant": bool(field_node.get("is_constant")),
        "samples_non_nan": samples,
        "instances": instances,
        "interpretation": INTERPRETATION,
        "not_observed_means_unreachable": False,
    }


def _boundary_topic(log: LogIndex, topic: str) -> dict[str, Any]:
    status = log.topic_status(topic)
    time_range = log.topic_time_range(topic)
    return {
        "topic": topic,
        "status": status,
        "message_format_present": topic in log.message_formats,
        "instances": log.topic_instances(topic),
        "samples": log.topic_samples(topic),
        "start_s": time_range[0] if time_range else None,
        "end_s": time_range[1] if time_range else None,
    }


def _boundary_field(log: LogIndex, topic: str, field_name: str) -> dict[str, Any]:
    status, samples, instances = log.field_status(topic, field_name)
    return {
        "topic": topic,
        "field": field_name,
        "status": status,
        "samples_non_nan": samples,
        "instances": instances,
    }


def _runtime_support(topic_status: str, field_status: str) -> str:
    if topic_status == OBSERVED and field_status == OBSERVED:
        return "field_observed"
    if topic_status == OBSERVED:
        return "topic_observed"
    return "boundary_not_observed"


def _path_runtime_record(log: LogIndex, path: dict[str, Any]) -> dict[str, Any]:
    topic = path.get("topic", "")
    sink = path.get("sink", "")
    field_name = sink.split("::")[-1] if sink else ""
    topic_evidence = _boundary_topic(log, topic)
    field_evidence = _boundary_field(log, topic, field_name)
    support = _runtime_support(topic_evidence["status"], field_evidence["status"])
    return {
        "id": stable_id("ulog_path", log.sha256, path["id"]),
        "kind": "runtime_path",
        "path_id": path["id"],
        "parameter": path.get("parameter", ""),
        "topic": topic,
        "sink": sink,
        "sink_field": field_name,
        "producer_function": path.get("producer_function", ""),
        "consumer_function": path.get("consumer_function", ""),
        "topic_level_reachable": topic_evidence["status"] == OBSERVED,
        "field_level_reachable": field_evidence["status"] == OBSERVED,
        "runtime_support": support,
        "boundary": {
            "topic": topic_evidence,
            "field": field_evidence,
        },
        "static_path_confidence": path.get("confidence", ""),
        "boundary_confidence": path.get("boundary_confidence", ""),
        "interpretation": INTERPRETATION,
        "not_observed_means_unreachable": False,
    }


def _flag_evidence(
    log: LogIndex, paths: Sequence[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Check failsafe flag fields named by the Commander consumer evidence."""
    by_flag: dict[str, dict[str, Any]] = {}
    for path in paths:
        evidence = path.get("consumer_evidence") or {}
        for role, key in (("written", "flags_written"), ("read", "flags_read")):
            for item in evidence.get(key) or []:
                target = item.get("target", "")
                if not target.startswith("failsafe_flags_s::"):
                    continue
                record = by_flag.setdefault(
                    target,
                    {
                        "flag": target,
                        "field": target.split("::")[-1],
                        "roles": set(),
                        "path_ids": set(),
                        "functions": set(),
                    },
                )
                record["roles"].add(role)
                record["path_ids"].add(path["id"])
                if item.get("function"):
                    record["functions"].add(item["function"])

    records: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for target, item in sorted(by_flag.items()):
        status, samples, instances = log.field_status("failsafe_flags", item["field"])
        records.append(
            {
                "id": stable_id("ulog_flag", log.sha256, target),
                "kind": "runtime_flag",
                "topic": "failsafe_flags",
                "flag": target,
                "field": item["field"],
                "status": status,
                "samples_non_nan": samples,
                "instances": instances,
                "roles": sorted(item["roles"]),
                "path_ids": sorted(item["path_ids"]),
                "functions": sorted(item["functions"]),
                "interpretation": "presence_only_not_this_path_caused_it",
                "not_observed_means_unreachable": False,
            }
        )
        if status != OBSERVED:
            kind = (
                "ulog_flag_not_logged"
                if status == NOT_LOGGED
                else "ulog_flag_not_observed"
            )
            unresolved.append(
                {
                    "kind": kind,
                    "flag": target,
                    "field": item["field"],
                    "topic": "failsafe_flags",
                    "path_ids": sorted(item["path_ids"]),
                }
            )
    return records, unresolved


def build_runtime(
    nodes: Sequence[dict[str, Any]],
    m4_paths: Sequence[dict[str, Any]],
    log: LogIndex | None,
    commit: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Link graph topics/fields/paths to one ULog's observed datasets."""
    metrics: Counter[str] = Counter()
    for key in (
        "records",
        "graph_topics",
        "graph_topics_observed",
        "graph_topics_not_observed",
        "graph_topics_not_logged",
        "graph_fields",
        "graph_fields_observed",
        "graph_fields_not_observed",
        "graph_fields_not_logged",
        "graph_fields_not_applicable",
        "paths",
        "paths_with_runtime_record",
        "paths_topic_observed",
        "paths_field_observed",
        "paths_boundary_not_observed",
        "flags",
        "flags_observed",
        "flags_not_observed",
        "flags_not_logged",
        "graph_parameters",
        "graph_parameters_in_log",
        "changed_parameters",
        "unreachable_claims",
    ):
        metrics[key] = 0
    metrics["interpretation"] = "reachability_only"
    metrics["not_observed_is_not_unreachable"] = True
    unresolved: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []

    if log is None:
        metrics["log_present"] = False
        unresolved.append({"kind": "ulog_not_supplied"})
        return records, dict(metrics), unresolved

    metrics["log_present"] = True
    metrics["log_sha256"] = log.sha256
    metrics["log_size_bytes"] = log.size_bytes
    metrics["ver_sw"] = log.ver_sw
    metrics["commit"] = commit
    metrics["commit_match"] = bool(
        log.ver_sw and commit and log.ver_sw.startswith(commit)
    )
    metrics["scenario_declared"] = log.scenario
    metrics["duration_s"] = round(log.duration_s, 3)
    metrics["changed_parameter_names"] = sorted(log.changed_parameter_names())
    metrics["changed_parameters"] = len(log.changed_parameters)
    metrics["initial_parameters"] = len(log.initial_parameters)
    metrics["logged_topics"] = len(
        {name for name, views in log.datasets.items() if any(v.sample_count() for v in views)}
    )
    metrics["message_formats"] = len(log.message_formats)

    graph_parameters = [
        node["name"]
        for node in nodes
        if node.get("kind") == "parameter" and node.get("name")
    ]
    metrics["graph_parameters"] = len(graph_parameters)
    metrics["graph_parameters_in_log"] = sum(
        1 for name in graph_parameters if name in log.initial_parameters
    )

    records.append(
        {
            "id": stable_id("ulog_log", log.sha256),
            "kind": "runtime_log",
            "present": True,
            "path": str(log.path),
            "sha256": log.sha256,
            "size_bytes": log.size_bytes,
            "scenario_declared": log.scenario,
            "ver_sw": log.ver_sw,
            "commit": commit,
            "commit_match": metrics["commit_match"],
            "duration_s": round(log.duration_s, 3),
            "logged_topics": metrics["logged_topics"],
            "message_formats": len(log.message_formats),
            "initial_parameter_count": len(log.initial_parameters),
            "changed_parameter_count": len(log.changed_parameters),
            "changed_parameter_names": sorted(log.changed_parameter_names()),
            "initial_parameter_names": sorted(log.initial_parameters),
            "graph_parameters_in_log": metrics["graph_parameters_in_log"],
            "follows_static_graph": False,
            "interpretation": "runtime_evidence_only",
            "usage": "reachability only; no causal, correlation or safety claim",
        }
    )

    fields_by_message = _topic_node_fields(nodes)

    for node in nodes:
        if node.get("kind") != "topic":
            continue
        metrics["graph_topics"] += 1
        record = _topic_record(log, node)
        records.append(record)
        if record["status"] == OBSERVED:
            metrics["graph_topics_observed"] += 1
        elif record["status"] == NOT_OBSERVED:
            metrics["graph_topics_not_observed"] += 1
        else:
            metrics["graph_topics_not_logged"] += 1

        for field_node in fields_by_message.get(node.get("message_type", ""), []):
            metrics["graph_fields"] += 1
            field_record = _field_record(log, node["name"], field_node)
            records.append(field_record)
            status = field_record["status"]
            if status == OBSERVED:
                metrics["graph_fields_observed"] += 1
            elif status == NOT_OBSERVED:
                metrics["graph_fields_not_observed"] += 1
            elif status == NOT_LOGGED:
                metrics["graph_fields_not_logged"] += 1
            else:
                metrics["graph_fields_not_applicable"] += 1

    for path in m4_paths:
        metrics["paths"] += 1
        record = _path_runtime_record(log, path)
        records.append(record)
        metrics["paths_with_runtime_record"] += 1
        if record["topic_level_reachable"]:
            metrics["paths_topic_observed"] += 1
        if record["field_level_reachable"]:
            metrics["paths_field_observed"] += 1
        if record["runtime_support"] == "boundary_not_observed":
            metrics["paths_boundary_not_observed"] += 1
            topic = record["boundary"]["topic"]
            kind = (
                "ulog_path_topic_not_logged"
                if topic["status"] == NOT_LOGGED
                else "ulog_path_topic_not_observed"
            )
            unresolved.append(
                {
                    "kind": kind,
                    "path_id": path["id"],
                    "parameter": path.get("parameter", ""),
                    "topic": topic["topic"],
                }
            )
        elif record["boundary"]["field"]["status"] != OBSERVED:
            unresolved.append(
                {
                    "kind": "ulog_path_field_not_observed",
                    "path_id": path["id"],
                    "parameter": path.get("parameter", ""),
                    "topic": record["topic"],
                    "field": record["sink_field"],
                }
            )

    flag_records, flag_unresolved = _flag_evidence(log, m4_paths)
    records.extend(flag_records)
    unresolved.extend(flag_unresolved)
    metrics["flags"] = len(flag_records)
    for record in flag_records:
        if record["status"] == OBSERVED:
            metrics["flags_observed"] += 1
        elif record["status"] == NOT_LOGGED:
            metrics["flags_not_logged"] += 1
        else:
            metrics["flags_not_observed"] += 1

    # This module never emits a negative reachability conclusion.  Keep the
    # count explicit so a future change that introduces one fails the M5 gate.
    metrics["unreachable_claims"] = sum(
        1 for record in records if record.get("not_observed_means_unreachable") is True
    )
    metrics["records"] = len(records)
    metrics["unresolved"] = len(unresolved)
    return records, dict(metrics), unresolved


def write_runtime(
    run: Run,
    records: Sequence[dict[str, Any]],
    metrics: dict[str, Any],
    unresolved: Sequence[dict[str, Any]],
    ulog: Path | None = None,
    scenario: str = "",
    ver_sw: str = "",
) -> None:
    write_jsonl(run.path("evidence", "runtime.jsonl"), records)
    write_jsonl(run.path("unresolved", "ulog.jsonl"), unresolved)
    write_json(run.path("reports", "runtime_metrics.json"), metrics)
    if ulog is not None:
        record_ulog_input(run, ulog, scenario, ver_sw)
