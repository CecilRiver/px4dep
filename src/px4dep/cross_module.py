"""M4: stitch EKF2 slices to Commander through explicit uORB field hops.

Every join in this module is made through a graph edge (or an M3 path step)
whose endpoints are node ids.  Display names are only copied into reports;
they are never used to decide that two entities are connected.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .util import stable_id, write_json, write_jsonl

EKF2_PREFIX = "src/modules/ekf2/"
COMMANDER_PREFIX = "src/modules/commander/"
MAX_CONSUMER_DEPTH = 6
MAX_CONSUMER_FUNCTIONS = 64
MAX_EVIDENCE_PER_PATH = 256
CALL_RELATION = "calls"
SIGNAL_RELATIONS = {
    "reports_health",
    "writes_flag",
    "reads_flag",
    "restricts_arming",
    "restricts_run",
}


def _source_file(node: dict[str, Any]) -> str:
    return (node.get("source") or {}).get("file", "")


def _in_module(node: dict[str, Any], prefix: str) -> bool:
    return _source_file(node).startswith(prefix)


def _display(node: dict[str, Any]) -> str:
    return node.get("qualified_name") or node.get("name", "")


def build_call_edges(
    cfg: Config,
    raw_records: Iterable[dict[str, Any]],
    function_ids: dict[str, str],
    commit: str = "",
    variant: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Expose Clang call sites between indexed functions as ``calls`` edges.

    Only calls whose caller and callee both have function nodes are emitted.
    The edge therefore never invents a function that was not indexed.
    """
    edges: dict[str, dict[str, Any]] = {}
    records = 0
    unindexed = 0
    same_function = 0
    for record in raw_records:
        if record.get("record") != "call":
            continue
        records += 1
        caller_usr = record.get("caller_usr", "")
        callee_usr = record.get("callee_usr", "")
        caller = function_ids.get(caller_usr)
        callee = function_ids.get(callee_usr)
        if not caller or not callee:
            unindexed += 1
            continue
        if caller == callee:
            same_function += 1
        file = record.get("file", "")
        line = int(record.get("line") or 0)
        column = int(record.get("column") or 0)
        edge_id = stable_id(
            commit, variant, "edge", CALL_RELATION, caller, callee, file, line, column
        )
        if edge_id in edges:
            continue
        edges[edge_id] = {
            "id": edge_id,
            "kind": "edge",
            "relation": CALL_RELATION,
            "src": caller,
            "dst": callee,
            "build_variant": variant,
            "build_guards": [],
            "build_guard_status": "none",
            "confidence": "high",
            "evidence": {
                "rule": "clang_call_site",
                "file": file,
                "line": line,
                "column": column,
                "caller": record.get("caller", ""),
                "callee": record.get("callee", ""),
                "caller_usr": caller_usr,
                "callee_usr": callee_usr,
            },
        }
    metrics = {
        "call_records": records,
        "call_edges": len(edges),
        "call_records_unindexed": unindexed,
        "call_records_same_function": same_function,
    }
    return list(edges.values()), metrics


class _Index:
    def __init__(
        self, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
    ) -> None:
        self.node_by_id = {n["id"]: n for n in nodes}
        self.function_by_usr = {
            n.get("usr", ""): n["id"]
            for n in nodes
            if n.get("kind") == "function" and n.get("usr")
        }
        self.rel: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in edges:
            self.rel[edge.get("relation", "")].append(edge)

        self.field_to_message: dict[str, str] = {}
        for edge in self.rel["defines"]:
            if self._kind(edge["dst"]) == "topic_field":
                self.field_to_message[edge["dst"]] = edge["src"]
        self.message_to_topics: dict[str, list[str]] = defaultdict(list)
        for edge in self.rel["instantiates"]:
            if self._kind(edge["src"]) == "topic":
                self.message_to_topics[edge["dst"]].append(edge["src"])
        self.topic_to_message: dict[str, str] = {
            edge["src"]: edge["dst"] for edge in self.rel["instantiates"]
        }
        self.publishes_by_endpoint: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in self.rel["publishes"]:
            self.publishes_by_endpoint[edge["src"]].append(edge)
        self.subscribes_by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in self.rel["subscribes"]:
            self.subscribes_by_topic[edge["dst"]].append(edge)
        self.users_by_endpoint: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.uses_by_function: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in self.rel["uses_uorb"]:
            self.users_by_endpoint[edge["dst"]].append(edge)
            self.uses_by_function[edge["src"]].append(edge)
        self.calls_by_src: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in self.rel[CALL_RELATION]:
            self.calls_by_src[edge["src"]].append(edge)
        self.signals_by_src: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for relation in SIGNAL_RELATIONS:
            for edge in self.rel[relation]:
                self.signals_by_src[edge["src"]].append(edge)
        self.actions_by_predicate: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in self.rel["selects_action"]:
            self.actions_by_predicate[edge["src"]].append(edge)
        self.readers_by_flag: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in self.rel["reads_flag"]:
            self.readers_by_flag[edge["dst"]].append(edge)
        self.mode_links_by_predicate: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for relation in ("prevents_mode", "checks_mode"):
            for edge in self.rel[relation]:
                self.mode_links_by_predicate[edge["src"]].append(edge)
        self.restricts_by_predicate: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for relation in ("restricts_arming", "restricts_run"):
            for edge in self.rel[relation]:
                if self._kind(edge["src"]) == "predicate":
                    self.restricts_by_predicate[edge["src"]].append(edge)
        self.mode_predicates_by_function: dict[str, list[str]] = defaultdict(list)
        for relation in ("has_mode_check", "has_protection_condition"):
            for edge in self.rel[relation]:
                if self._kind(edge["src"]) == "function":
                    self.mode_predicates_by_function[edge["src"]].append(edge["dst"])

    def _kind(self, node_id: str) -> str:
        return self.node_by_id.get(node_id, {}).get("kind", "")

    def node(self, node_id: str) -> dict[str, Any]:
        return self.node_by_id.get(node_id, {})

    def qname(self, node_id: str) -> str:
        return _display(self.node(node_id))


def _edge_evidence(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "edge_id": edge["id"],
        "relation": edge.get("relation", ""),
        "confidence": edge.get("confidence", ""),
        "rule": (edge.get("evidence") or {}).get("rule", ""),
        "file": (edge.get("evidence") or {}).get("file", ""),
        "line": (edge.get("evidence") or {}).get("line", 0),
        "operation": (edge.get("evidence") or {}).get("operation", ""),
    }


def _hop(
    index: _Index,
    edge: dict[str, Any],
    *,
    stage: str,
    kind: str,
    from_id: str,
    to_id: str,
    direction: str,
) -> dict[str, Any]:
    hop = {
        "stage": stage,
        "kind": kind,
        "relation": edge.get("relation", ""),
        "from_id": from_id,
        "from": index.qname(from_id),
        "to_id": to_id,
        "to": index.qname(to_id),
        "direction": direction,
        "confidence": edge.get("confidence", ""),
        "evidence": _edge_evidence(edge),
    }
    return hop


def _call_chain_hop(index: _Index, edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "relation": CALL_RELATION,
        "from_id": edge["src"],
        "from": index.qname(edge["src"]),
        "to_id": edge["dst"],
        "to": index.qname(edge["dst"]),
        "confidence": edge.get("confidence", ""),
        "evidence": _edge_evidence(edge),
    }


def _collect_consumer_evidence(
    index: _Index, start_id: str
) -> tuple[dict[str, Any], Counter[str]]:
    """Breadth-first walk over explicit ``calls`` edges inside Commander."""
    evidence: dict[str, Any] = {
        "health_predicates": [],
        "flags_written": [],
        "flags_read": [],
        "mode_restrictions": [],
        "flag_read_links": [],
        "protection_chains": [],
        "reachable_functions": 0,
        "truncated": False,
    }
    diagnostics: Counter[str] = Counter()
    queue: deque[tuple[str, list[dict[str, Any]]]] = deque([(start_id, [])])
    visited: set[str] = {start_id}
    evidence_count = 0

    while queue:
        function_id, chain = queue.popleft()
        evidence["reachable_functions"] += 1
        if evidence_count >= MAX_EVIDENCE_PER_PATH:
            evidence["truncated"] = True
            diagnostics["evidence_limit"] += 1
            break

        for edge in sorted(
            index.signals_by_src.get(function_id, []),
            key=lambda item: (item.get("relation", ""), item["dst"]),
        ):
            relation = edge.get("relation")
            target = index.node(edge["dst"])
            record = {
                "relation": relation,
                "function_id": function_id,
                "function": index.qname(function_id),
                "target_id": edge["dst"],
                "target": index.qname(edge["dst"]),
                "target_kind": target.get("kind", ""),
                "predicate_layer": target.get("predicate_layer", ""),
                "effect": target.get("effect", ""),
                "severity": target.get("severity", ""),
                "call_chain": list(chain),
                "evidence": _edge_evidence(edge),
            }
            if relation == "reports_health":
                evidence["health_predicates"].append(record)
                evidence_count += 1
                for mode_edge in index.mode_links_by_predicate.get(edge["dst"], []) + index.restricts_by_predicate.get(edge["dst"], []):
                    evidence["mode_restrictions"].append(
                        {
                            "predicate_id": edge["dst"],
                            "predicate": index.qname(edge["dst"]),
                            "mode_id": mode_edge["dst"],
                            "mode": index.qname(mode_edge["dst"]),
                            "relation": mode_edge.get("relation", ""),
                            "call_chain": list(chain),
                            "evidence": _edge_evidence(mode_edge),
                        }
                    )
                    evidence_count += 1
            elif relation == "writes_flag":
                evidence["flags_written"].append(record)
                evidence_count += 1
            elif relation == "reads_flag":
                evidence["flags_read"].append(record)
                evidence_count += 1
            elif relation in {"restricts_arming", "restricts_run"}:
                evidence["mode_restrictions"].append(
                    {
                        "function_id": function_id,
                        "function": index.qname(function_id),
                        "mode_id": edge["dst"],
                        "mode": index.qname(edge["dst"]),
                        "relation": relation,
                        "call_chain": list(chain),
                        "evidence": _edge_evidence(edge),
                    }
                )
                evidence_count += 1

        if len(chain) >= MAX_CONSUMER_DEPTH:
            diagnostics["depth_limit"] += 1
            continue
        for edge in sorted(
            index.calls_by_src.get(function_id, []),
            key=lambda item: item["dst"],
        ):
            callee = edge["dst"]
            if callee in visited:
                continue
            visited.add(callee)
            queue.append((callee, chain + [_call_chain_hop(index, edge)]))

    # Flag writes are joined to later protection checks by health-flag node id,
    # not by the flag's display name.
    for written in evidence["flags_written"]:
        flag_id = written["target_id"]
        for read_edge in index.readers_by_flag.get(flag_id, []):
            reader = index.node(read_edge["src"])
            actions = index.actions_by_predicate.get(read_edge["src"], [])
            mode_links = index.mode_links_by_predicate.get(read_edge["src"], [])
            link = {
                "flag_id": flag_id,
                "flag": index.qname(flag_id),
                "writer_function_id": written["function_id"],
                "writer_function": written["function"],
                "write_evidence": written["evidence"],
                "reader_id": read_edge["src"],
                "reader": index.qname(read_edge["src"]),
                "reader_kind": reader.get("kind", ""),
                "reader_layer": reader.get("predicate_layer", ""),
                "read_evidence": _edge_evidence(read_edge),
                "actions": [
                    {
                        "decision_id": action["dst"],
                        "decision": index.qname(action["dst"]),
                        "evidence": _edge_evidence(action),
                    }
                    for action in actions
                ],
                "modes": [
                    {
                        "mode_id": mode_edge["dst"],
                        "mode": index.qname(mode_edge["dst"]),
                        "relation": mode_edge.get("relation", ""),
                        "evidence": _edge_evidence(mode_edge),
                    }
                    for mode_edge in mode_links
                ],
            }
            evidence["flag_read_links"].append(link)
            if link["actions"]:
                evidence["protection_chains"].append(link)
            evidence_count += 1
            if evidence_count >= MAX_EVIDENCE_PER_PATH:
                evidence["truncated"] = True
                break
        # A function reader can be wrapped by a mode-check predicate; expose
        # that predicate's explicit mode links as well.
        for read_edge in index.readers_by_flag.get(flag_id, []):
            if index._kind(read_edge["src"]) != "function":
                continue
            for predicate_id in index.mode_predicates_by_function.get(
                read_edge["src"], []
            ):
                for mode_edge in index.mode_links_by_predicate.get(predicate_id, []):
                    evidence["mode_restrictions"].append(
                        {
                            "function_id": read_edge["src"],
                            "function": index.qname(read_edge["src"]),
                            "predicate_id": predicate_id,
                            "predicate": index.qname(predicate_id),
                            "mode_id": mode_edge["dst"],
                            "mode": index.qname(mode_edge["dst"]),
                            "relation": mode_edge.get("relation", ""),
                            "call_chain": [],
                            "evidence": _edge_evidence(mode_edge),
                        }
                    )
                    evidence_count += 1
    return evidence, diagnostics


def _consumer_options(
    index: _Index, topic_id: str
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Commander functions that subscribe to ``topic_id`` through an endpoint."""
    options: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
    for subscribe_edge in index.subscribes_by_topic.get(topic_id, []):
        endpoint_id = subscribe_edge["src"]
        for use_edge in index.users_by_endpoint.get(endpoint_id, []):
            function_id = use_edge["src"]
            function = index.node(function_id)
            if function.get("kind") != "function":
                continue
            if not _in_module(function, COMMANDER_PREFIX):
                continue
            operation = (use_edge.get("evidence") or {}).get("operation", "")
            if operation not in {"copy", "update"}:
                continue
            if function_id not in options:
                options[function_id] = (function_id, use_edge, subscribe_edge)
    ordered = sorted(
        options.values(), key=lambda item: index.qname(item[0])
    )
    return ordered[:MAX_CONSUMER_FUNCTIONS]


def _producer_options(
    index: _Index,
    writer_ids: Iterable[str],
    allowed_topics: set[str],
) -> list[tuple[str, dict[str, Any], dict[str, Any], str]]:
    """(function, uses_uorb edge, publishes edge, topic id) for EKF2 writers."""
    options: dict[tuple[str, str], tuple[str, dict[str, Any], dict[str, Any], str]] = {}
    for function_id in writer_ids:
        function = index.node(function_id)
        if function.get("kind") != "function" or not _in_module(function, EKF2_PREFIX):
            continue
        for use_edge in index.uses_by_function.get(function_id, []):
            operation = (use_edge.get("evidence") or {}).get("operation", "")
            if operation != "publish":
                continue
            endpoint_id = use_edge["dst"]
            for publish_edge in index.publishes_by_endpoint.get(endpoint_id, []):
                topic_id = publish_edge["dst"]
                if topic_id not in allowed_topics:
                    continue
                options[(function_id, topic_id)] = (
                    function_id,
                    use_edge,
                    publish_edge,
                    topic_id,
                )
    return sorted(
        options.values(), key=lambda item: (index.qname(item[0]), item[3])
    )


def _dedupe_evidence(items: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = json.dumps(
            [item.get(k, "") for k in keys], sort_keys=True, default=str
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def build_cross_module(
    cfg: Config,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    slice_paths: list[dict[str, Any]],
    commit: str = "",
    variant: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    index = _Index(nodes, edges)

    paths: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    seen_unresolved: set[tuple[str, ...]] = set()
    diagnostics: Counter[str] = Counter()

    def add_unresolved(kind: str, **detail: Any) -> None:
        key = (kind,) + tuple(f"{k}={detail[k]}" for k in sorted(detail))
        if key in seen_unresolved:
            return
        seen_unresolved.add(key)
        unresolved.append({"kind": kind, **detail})

    metrics: Counter[str] = Counter()
    for key in (
        "paths",
        "slice_paths",
        "slice_paths_message_field",
        "slice_paths_non_message_sink",
        "slice_paths_without_message_definition",
        "slice_paths_without_sink_writer",
        "slice_paths_without_indexed_writer",
        "slice_paths_without_publisher_topic",
        "topics_without_commander_consumer",
        "paths_with_health_predicate",
        "paths_with_flag_write",
        "paths_with_protection_chain",
        "paths_with_mode_restriction",
        "paths_with_truncated_consumer_evidence",
        "paths_without_edge_evidence",
        "paths_with_incomplete_boundary",
        "paths_without_topic_field_hop",
        "paths_without_topic_hop",
        "paths_without_ekf2_producer",
        "paths_without_commander_consumer",
        "paths_without_steps",
    ):
        metrics[key] = 0
    parameters: set[str] = set()
    topics: set[str] = set()
    producers: set[str] = set()
    consumers: set[str] = set()

    for slice_path in slice_paths:
        metrics["slice_paths"] += 1
        path_identity = {
            "parameter": slice_path.get("parameter", ""),
            "source": slice_path.get("source", ""),
        }
        sink = index.node(slice_path.get("sink_id", ""))
        if sink.get("kind") != "topic_field":
            metrics["slice_paths_non_message_sink"] += 1
            continue
        metrics["slice_paths_message_field"] += 1

        message_id = index.field_to_message.get(sink["id"])
        if not message_id or index._kind(message_id) != "message_type":
            metrics["slice_paths_without_message_definition"] += 1
            add_unresolved(
                "m4_sink_without_message_definition",
                sink=sink.get("qualified_name", ""),
                sink_id=sink["id"],
                **path_identity,
            )
            continue
        message_type = index.qname(message_id)

        writers = [
            item.get("usr", "")
            for item in (slice_path.get("sink_writers") or [])
            if item.get("usr")
        ]
        writer_ids = [
            index.function_by_usr[usr]
            for usr in writers
            if usr in index.function_by_usr
        ]
        if not writers:
            metrics["slice_paths_without_sink_writer"] += 1
            add_unresolved(
                "m4_sink_without_writer",
                sink=sink.get("qualified_name", ""),
                sink_id=sink["id"],
                **path_identity,
            )
            continue
        if not writer_ids:
            metrics["slice_paths_without_indexed_writer"] += 1
            add_unresolved(
                "m4_sink_writer_not_indexed",
                sink=sink.get("qualified_name", ""),
                sink_id=sink["id"],
                writers=";".join(writers),
                **path_identity,
            )
            continue

        allowed_topics = set(index.message_to_topics.get(message_id, []))
        producer_options = _producer_options(index, writer_ids, allowed_topics)
        if not producer_options:
            metrics["slice_paths_without_publisher_topic"] += 1
            add_unresolved(
                "m4_writer_publish_topic_unresolved",
                sink=sink.get("qualified_name", ""),
                sink_id=sink["id"],
                message_type=message_type,
                writers=";".join(sorted(writers)),
                **path_identity,
            )
            continue

        for producer_id, use_edge, publish_edge, topic_id in producer_options:
            topic = index.node(topic_id)
            consumer_options = _consumer_options(index, topic_id)
            if not consumer_options:
                metrics["topics_without_commander_consumer"] += 1
                add_unresolved(
                    "m4_no_commander_consumer",
                    topic=topic.get("name", ""),
                    topic_id=topic_id,
                    message_type=message_type,
                    **path_identity,
                )
                continue

            for consumer_id, consumer_use_edge, subscribe_edge in consumer_options:
                steps: list[dict[str, Any]] = []
                for step in slice_path.get("steps") or []:
                    steps.append(
                        {
                            "stage": "parameter_to_field",
                            "kind": step.get("kind", ""),
                            "relation": step.get("relation", ""),
                            "from": step.get("from", ""),
                            "to": step.get("to", ""),
                            "caller": step.get("caller", ""),
                            "file": step.get("file", ""),
                            "line": step.get("line", 0),
                            "source": "ekf_slice",
                            "confidence": slice_path.get("confidence", ""),
                        }
                    )

                definition_edge = next(
                    (
                        edge
                        for edge in index.rel["defines"]
                        if edge["src"] == message_id and edge["dst"] == sink["id"]
                    ),
                    None,
                )
                if definition_edge is None:
                    add_unresolved(
                        "m4_field_definition_edge_missing",
                        sink=sink.get("qualified_name", ""),
                        sink_id=sink["id"],
                        message_type=message_type,
                        topic=topic.get("name", ""),
                        **path_identity,
                    )
                    continue
                steps.append(
                    _hop(
                        index,
                        definition_edge,
                        stage="field_to_message",
                        kind="message_field_definition",
                        from_id=sink["id"],
                        to_id=message_id,
                        direction="field_defined_by_message",
                    )
                )

                instantiation_edge = next(
                    (
                        edge
                        for edge in index.rel["instantiates"]
                        if edge["src"] == topic_id and edge["dst"] == message_id
                    ),
                    None,
                )
                if instantiation_edge is None:
                    add_unresolved(
                        "m4_topic_instantiation_edge_missing",
                        topic=topic.get("name", ""),
                        topic_id=topic_id,
                        message_type=message_type,
                        sink=sink.get("qualified_name", ""),
                        sink_id=sink["id"],
                        **path_identity,
                    )
                    continue
                steps.append(
                    _hop(
                        index,
                        instantiation_edge,
                        stage="message_to_topic",
                        kind="topic_instantiation",
                        from_id=message_id,
                        to_id=topic_id,
                        direction="message_instantiated_by_topic",
                    )
                )
                steps.append(
                    _hop(
                        index,
                        publish_edge,
                        stage="topic_to_producer",
                        kind="topic_publication",
                        from_id=topic_id,
                        to_id=publish_edge["src"],
                        direction="topic_published_by_endpoint",
                    )
                )
                steps.append(
                    _hop(
                        index,
                        use_edge,
                        stage="topic_to_producer",
                        kind="producer_endpoint_use",
                        from_id=use_edge["dst"],
                        to_id=producer_id,
                        direction="endpoint_used_by_function",
                    )
                )
                steps.append(
                    _hop(
                        index,
                        subscribe_edge,
                        stage="topic_to_consumer",
                        kind="topic_subscription",
                        from_id=topic_id,
                        to_id=subscribe_edge["src"],
                        direction="topic_subscribed_by_endpoint",
                    )
                )
                steps.append(
                    _hop(
                        index,
                        consumer_use_edge,
                        stage="topic_to_consumer",
                        kind="consumer_endpoint_use",
                        from_id=consumer_use_edge["dst"],
                        to_id=consumer_id,
                        direction="endpoint_used_by_function",
                    )
                )

                consumer_evidence, consumer_diagnostics = _collect_consumer_evidence(
                    index, consumer_id
                )
                for key, value in consumer_diagnostics.items():
                    diagnostics[f"consumer_{key}"] += value
                consumer_evidence["health_predicates"] = _dedupe_evidence(
                    consumer_evidence["health_predicates"],
                    ("function_id", "target_id", "evidence", "call_chain"),
                )
                consumer_evidence["flags_written"] = _dedupe_evidence(
                    consumer_evidence["flags_written"],
                    ("function_id", "target_id", "evidence"),
                )
                consumer_evidence["flags_read"] = _dedupe_evidence(
                    consumer_evidence["flags_read"],
                    ("function_id", "target_id", "evidence"),
                )
                consumer_evidence["mode_restrictions"] = _dedupe_evidence(
                    consumer_evidence["mode_restrictions"],
                    ("mode_id", "relation", "predicate_id", "function_id", "evidence"),
                )
                consumer_evidence["flag_read_links"] = _dedupe_evidence(
                    consumer_evidence["flag_read_links"],
                    ("flag_id", "writer_function_id", "reader_id", "read_evidence"),
                )
                consumer_evidence["protection_chains"] = _dedupe_evidence(
                    consumer_evidence["protection_chains"],
                    ("flag_id", "writer_function_id", "reader_id", "actions"),
                )
                for item in consumer_evidence["health_predicates"]:
                    item["call_chain_id"] = stable_id(
                        commit,
                        variant,
                        "call_chain",
                        consumer_id,
                        item["function_id"],
                        *(hop["evidence"]["edge_id"] for hop in item["call_chain"]),
                    )

                path_id = stable_id(
                    commit,
                    variant,
                    "end_to_end_path",
                    slice_path.get("id", ""),
                    topic_id,
                    consumer_id,
                )
                has_health = bool(consumer_evidence["health_predicates"])
                has_flag_write = bool(consumer_evidence["flags_written"])
                has_protection = any(
                    chain.get("actions")
                    for chain in consumer_evidence["protection_chains"]
                )
                has_mode = bool(consumer_evidence["mode_restrictions"])
                if has_health:
                    metrics["paths_with_health_predicate"] += 1
                if has_flag_write:
                    metrics["paths_with_flag_write"] += 1
                if has_protection:
                    metrics["paths_with_protection_chain"] += 1
                if has_mode:
                    metrics["paths_with_mode_restriction"] += 1
                if consumer_evidence["truncated"]:
                    metrics["paths_with_truncated_consumer_evidence"] += 1

                boundary_steps = [s for s in steps if s.get("source") != "ekf_slice"]
                if not boundary_steps:
                    metrics["paths_without_edge_evidence"] += 1
                if len(boundary_steps) < 6:
                    metrics["paths_with_incomplete_boundary"] += 1
                if not any(s.get("stage") == "field_to_message" for s in steps):
                    metrics["paths_without_topic_field_hop"] += 1
                if not any(s.get("stage") == "message_to_topic" for s in steps):
                    metrics["paths_without_topic_hop"] += 1
                if not _in_module(
                    index.node(producer_id), EKF2_PREFIX
                ):
                    metrics["paths_without_ekf2_producer"] += 1
                if not _in_module(
                    index.node(consumer_id), COMMANDER_PREFIX
                ):
                    metrics["paths_without_commander_consumer"] += 1
                if not steps:
                    metrics["paths_without_steps"] += 1

                metrics["paths"] += 1
                parameters.add(slice_path.get("parameter", ""))
                topics.add(topic.get("name", ""))
                producers.add(index.qname(producer_id))
                consumers.add(index.qname(consumer_id))

                paths.append(
                    {
                        "id": path_id,
                        "kind": "end_to_end_path",
                        "rule": "cross_module_uorb_field_stitch",
                        "slice_path_id": slice_path.get("id", ""),
                        "parameter": slice_path.get("parameter", ""),
                        "source": slice_path.get("source", ""),
                        "source_id": slice_path.get("source_id", ""),
                        "category": slice_path.get("category", ""),
                        "sink": sink.get("qualified_name", ""),
                        "sink_id": sink["id"],
                        "sink_kind": slice_path.get("sink_kind", ""),
                        "message_type": message_type,
                        "message_type_id": message_id,
                        "topic": topic.get("name", ""),
                        "topic_id": topic_id,
                        "producer_function": index.qname(producer_id),
                        "producer_function_id": producer_id,
                        "consumer_function": index.qname(consumer_id),
                        "consumer_function_id": consumer_id,
                        "consumer_module": "commander",
                        "producer_module": "ekf2",
                        "boundary_confidence": "high",
                        "confidence": "medium"
                        if slice_path.get("category") == "data"
                        else "low",
                        "consumer_field_binding": "topic_level",
                        "name_based_joins": 0,
                        "steps": steps,
                        "consumer_evidence": consumer_evidence,
                        "build_variant": variant,
                    }
                )

    metrics["distinct_parameters"] = len({p for p in parameters if p})
    metrics["distinct_topics"] = len(topics)
    metrics["distinct_producers"] = len(producers)
    metrics["distinct_consumers"] = len(consumers)
    metrics["name_based_joins"] = 0
    metrics["unresolved"] = len(unresolved)
    metrics["unresolved_by_kind"] = dict(
        sorted(Counter(item["kind"] for item in unresolved).items())
    )
    metrics["diagnostics"] = dict(sorted(diagnostics.items()))
    metrics["paths_without_steps"] = metrics.get("paths_without_steps", 0)
    metrics["paths_without_topic_field_hop"] = metrics.get(
        "paths_without_topic_field_hop", 0
    )
    metrics["paths_without_topic_hop"] = metrics.get("paths_without_topic_hop", 0)
    metrics["paths_without_ekf2_producer"] = metrics.get(
        "paths_without_ekf2_producer", 0
    )
    metrics["paths_without_commander_consumer"] = metrics.get(
        "paths_without_commander_consumer", 0
    )
    metrics["paths_without_edge_evidence"] = metrics.get(
        "paths_without_edge_evidence", 0
    )
    metrics["paths_with_incomplete_boundary"] = metrics.get(
        "paths_with_incomplete_boundary", 0
    )
    return paths, dict(sorted(metrics.items())), unresolved


def write_cross_module(
    run,
    call_edges: list[dict[str, Any]],
    call_metrics: dict[str, Any],
    paths: list[dict[str, Any]],
    metrics: dict[str, Any],
    unresolved: list[dict[str, Any]],
) -> None:
    write_jsonl(run.path("normalized", "cross_module_edges.jsonl"), call_edges)
    write_jsonl(run.path("graph", "end_to_end_paths.jsonl"), paths)
    write_jsonl(run.path("unresolved", "cross_module.jsonl"), unresolved)
    write_json(
        run.path("reports", "cross_module_metrics.json"),
        {**metrics, "call_graph": call_metrics},
    )
    write_cross_module_csv(run.path("reports", "cross_module.csv"), paths)


def write_cross_module_csv(path: Path, paths: list[dict[str, Any]]) -> None:
    columns = [
        "path_id",
        "parameter",
        "source_field",
        "sink_field",
        "message_type",
        "topic",
        "producer_function",
        "consumer_function",
        "category",
        "health_predicates",
        "flags_written",
        "protection_actions",
        "mode_restrictions",
        "depth",
        "confidence",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for item in paths:
            evidence = item.get("consumer_evidence") or {}
            writer.writerow(
                {
                    "path_id": item.get("id", ""),
                    "parameter": item.get("parameter", ""),
                    "source_field": item.get("source", ""),
                    "sink_field": item.get("sink", ""),
                    "message_type": item.get("message_type", ""),
                    "topic": item.get("topic", ""),
                    "producer_function": item.get("producer_function", ""),
                    "consumer_function": item.get("consumer_function", ""),
                    "category": item.get("category", ""),
                    "health_predicates": len(
                        evidence.get("health_predicates") or []
                    ),
                    "flags_written": len(evidence.get("flags_written") or []),
                    "protection_actions": sum(
                        len(chain.get("actions") or [])
                        for chain in evidence.get("protection_chains") or []
                    ),
                    "mode_restrictions": len(
                        evidence.get("mode_restrictions") or []
                    ),
                    "depth": len(item.get("steps") or []),
                    "confidence": item.get("confidence", ""),
                }
            )
