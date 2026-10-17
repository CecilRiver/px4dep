"""Unit tests for the M4 cross-module path stitcher."""

from __future__ import annotations

from pathlib import Path

from px4dep.cross_module import (
    build_call_edges,
    build_cross_module,
    write_cross_module_csv,
)
from px4dep_testkit import make_config


def _function(
    node_id: str, qname: str, usr: str, file: str
) -> dict[str, object]:
    return {
        "id": node_id,
        "kind": "function",
        "qualified_name": qname,
        "name": qname.rsplit("::", 1)[-1],
        "usr": usr,
        "source": {"file": file, "line": 1},
    }


def _node(
    node_id: str, kind: str, qname: str, **extra: object
) -> dict[str, object]:
    node = {
        "id": node_id,
        "kind": kind,
        "qualified_name": qname,
        "name": qname.rsplit("::", 1)[-1],
        "source": {"file": extra.pop("file", "fixture"), "line": 1},
    }
    node.update(extra)
    return node


def _edge(
    edge_id: str,
    relation: str,
    src: str,
    dst: str,
    **evidence: object,
) -> dict[str, object]:
    ev = {"rule": "fixture", "file": "fixture", "line": 1}
    ev.update(evidence)
    return {
        "id": edge_id,
        "kind": "edge",
        "relation": relation,
        "src": src,
        "dst": dst,
        "confidence": "high",
        "build_variant": "test_variant",
        "evidence": ev,
    }


def _fixture() -> tuple[list[dict], list[dict], list[dict]]:
    ekf_usr = "c:@EKF2@Publish"
    consumer_usr = "c:@Check@checkAndReport"
    helper_usr = "c:@Check@checkStatus"
    flag_writer_usr = "c:@Check@setFlags"
    nodes = [
        _function(
            "fn-ekf", "EKF2::PublishStatus", ekf_usr, "src/modules/ekf2/EKF2.cpp"
        ),
        _function(
            "fn-consumer",
            "EstimatorChecks::checkAndReport",
            consumer_usr,
            "src/modules/commander/HealthAndArmingChecks/checks/estimatorCheck.cpp",
        ),
        _function(
            "fn-helper",
            "EstimatorChecks::checkEstimatorStatus",
            helper_usr,
            "src/modules/commander/HealthAndArmingChecks/checks/estimatorCheck.cpp",
        ),
        _function(
            "fn-flags",
            "EstimatorChecks::setModeRequirementFlags",
            flag_writer_usr,
            "src/modules/commander/HealthAndArmingChecks/checks/estimatorCheck.cpp",
        ),
        _node(
            "cf-source",
            "config_field",
            "estimator::parameters::ekf2_gps_check",
        ),
        _node(
            "field",
            "topic_field",
            "estimator_status_s::gps_check_fail_flags",
            message_type="estimator_status_s",
        ),
        _node("msg", "message_type", "estimator_status_s"),
        _node("topic", "topic", "estimator_status", message_type="estimator_status_s"),
        _node(
            "pub-endpoint",
            "uorb_endpoint",
            "EKF2::_estimator_status_pub",
            role="publication",
            topics=["estimator_status"],
        ),
        _node(
            "sub-endpoint",
            "uorb_endpoint",
            "EstimatorChecks::_estimator_status_sub",
            role="subscription",
            topics=["estimator_status"],
        ),
        _node(
            "health-predicate",
            "predicate",
            "health:EstimatorChecks::checkEstimatorStatus::armingCheckFailure@x:1",
            predicate_layer="arming_check",
            effect="arming_error",
            severity="error",
        ),
        _node("flag", "health_flag", "failsafe_flags_s::position_accuracy_low"),
        _node(
            "protection-predicate",
            "predicate",
            "protection:Failsafe::checkStateAndMode::CHECK_FAILSAFE@f:1",
            predicate_layer="protection_condition",
        ),
        _node("decision", "protection_decision", "FailsafeBase::Action::RTL"),
        _node("mode", "mode_capability", "mode_req:mode_req_gnss"),
    ]
    edges = [
        _edge("e-defines", "defines", "msg", "field"),
        _edge("e-instantiates", "instantiates", "topic", "msg"),
        _edge("e-publishes", "publishes", "pub-endpoint", "topic"),
        _edge(
            "e-producer-use",
            "uses_uorb",
            "fn-ekf",
            "pub-endpoint",
            operation="publish",
        ),
        _edge("e-subscribes", "subscribes", "sub-endpoint", "topic"),
        _edge(
            "e-consumer-use",
            "uses_uorb",
            "fn-consumer",
            "sub-endpoint",
            operation="copy",
        ),
        _edge("e-calls-helper", "calls", "fn-consumer", "fn-helper"),
        _edge("e-calls-flags", "calls", "fn-consumer", "fn-flags"),
        _edge(
            "e-health", "reports_health", "fn-helper", "health-predicate"
        ),
        _edge(
            "e-arming",
            "restricts_arming",
            "health-predicate",
            "mode",
        ),
        _edge("e-flag-write", "writes_flag", "fn-flags", "flag"),
        _edge("e-flag-read", "reads_flag", "protection-predicate", "flag"),
        _edge("e-action", "selects_action", "protection-predicate", "decision"),
    ]
    slice_path = [
        {
            "id": "slice-1",
            "kind": "slice_path",
            "sink_id": "field",
            "sink_kind": "criteria_state",
            "sink": "estimator_status_s::gps_check_fail_flags",
            "source_id": "cf-source",
            "source": "estimator::parameters::ekf2_gps_check",
            "parameter": "EKF2_GPS_CHECK",
            "category": "data",
            "confidence": "medium",
            "sink_writers": [{"usr": ekf_usr, "qualified_name": "EKF2::PublishStatus"}],
            "steps": [
                {
                    "kind": "rhs_member",
                    "relation": "data_depends_on",
                    "from": "estimator_status_s::gps_check_fail_flags",
                    "to": "parameter[estimator::parameters::ekf2_gps_check]",
                    "caller": "EKF2::PublishStatus",
                    "file": "src/modules/ekf2/EKF2.cpp",
                    "line": 9,
                }
            ],
        }
    ]
    return nodes, edges, slice_path


def _build(tmp_path: Path, nodes=None, edges=None, paths=None):
    cfg = make_config(tmp_path)
    default_nodes, default_edges, default_paths = _fixture()
    return build_cross_module(
        cfg,
        default_nodes if nodes is None else nodes,
        default_edges if edges is None else edges,
        default_paths if paths is None else paths,
        commit="testcommit",
        variant="test_variant",
    )


def test_stitches_parameter_to_commander_through_field_and_topic(tmp_path: Path):
    paths, metrics, unresolved = _build(tmp_path)
    assert unresolved == []
    assert metrics["paths"] == 1
    assert metrics["name_based_joins"] == 0
    assert metrics["paths_without_topic_field_hop"] == 0
    assert metrics["paths_without_topic_hop"] == 0
    assert metrics["paths_with_health_predicate"] == 1
    assert metrics["paths_with_flag_write"] == 1
    assert metrics["paths_with_protection_chain"] == 1

    path = paths[0]
    assert path["parameter"] == "EKF2_GPS_CHECK"
    assert path["topic"] == "estimator_status"
    assert path["producer_function"] == "EKF2::PublishStatus"
    assert path["consumer_function"] == "EstimatorChecks::checkAndReport"
    stages = [step["stage"] for step in path["steps"]]
    assert stages == [
        "parameter_to_field",
        "field_to_message",
        "message_to_topic",
        "topic_to_producer",
        "topic_to_producer",
        "topic_to_consumer",
        "topic_to_consumer",
    ]
    boundary = [step for step in path["steps"] if step.get("source") != "ekf_slice"]
    assert all(step["evidence"]["edge_id"] for step in boundary)

    evidence = path["consumer_evidence"]
    assert evidence["health_predicates"][0]["function"] == (
        "EstimatorChecks::checkEstimatorStatus"
    )
    assert len(evidence["health_predicates"][0]["call_chain"]) == 1
    assert evidence["flags_written"][0]["target"] == (
        "failsafe_flags_s::position_accuracy_low"
    )
    assert evidence["protection_chains"][0]["actions"][0]["decision"] == (
        "FailsafeBase::Action::RTL"
    )
    assert evidence["mode_restrictions"][0]["relation"] == "restricts_arming"


def test_field_name_alone_does_not_connect_without_defines_edge(tmp_path: Path):
    nodes, edges, paths = _fixture()
    edges = [edge for edge in edges if edge["id"] != "e-defines"]
    result, metrics, unresolved = _build(
        tmp_path, nodes=nodes, edges=edges, paths=paths
    )
    assert result == []
    assert metrics["paths"] == 0
    assert any(
        item["kind"] == "m4_sink_without_message_definition"
        for item in unresolved
    )


def test_missing_publish_edge_is_reported_not_guessed(tmp_path: Path):
    nodes, edges, paths = _fixture()
    edges = [edge for edge in edges if edge["id"] != "e-publishes"]
    result, metrics, unresolved = _build(
        tmp_path, nodes=nodes, edges=edges, paths=paths
    )
    assert result == []
    assert metrics["paths"] == 0
    assert any(
        item["kind"] == "m4_writer_publish_topic_unresolved"
        for item in unresolved
    )


def test_missing_commander_subscription_is_reported(tmp_path: Path):
    nodes, edges, paths = _fixture()
    edges = [edge for edge in edges if edge["id"] != "e-consumer-use"]
    result, metrics, unresolved = _build(
        tmp_path, nodes=nodes, edges=edges, paths=paths
    )
    assert result == []
    assert metrics["paths"] == 0
    assert any(item["kind"] == "m4_no_commander_consumer" for item in unresolved)


def test_paths_are_stable_and_csv_has_one_row_each(tmp_path: Path):
    first = _build(tmp_path)
    second = _build(tmp_path)
    assert [path["id"] for path in first[0]] == [path["id"] for path in second[0]]
    out = tmp_path / "cross_module.csv"
    write_cross_module_csv(out, first[0])
    rows = out.read_text(encoding="utf-8").splitlines()
    assert len(rows) == len(first[0]) + 1
    assert rows[0].startswith("path_id,parameter,source_field,sink_field")


def test_call_edges_require_both_functions_indexed(tmp_path: Path):
    cfg = make_config(tmp_path)
    raw = [
        {
            "record": "call",
            "caller_usr": "usr-a",
            "callee_usr": "usr-b",
            "caller": "A::run",
            "callee": "B::step",
            "file": "src/modules/commander/a.cpp",
            "line": 10,
            "column": 2,
        },
        {
            "record": "call",
            "caller_usr": "usr-a",
            "callee_usr": "usr-missing",
            "caller": "A::run",
            "callee": "Missing::step",
            "file": "src/modules/commander/a.cpp",
            "line": 11,
            "column": 2,
        },
    ]
    edges, metrics = build_call_edges(
        cfg, raw, {"usr-a": "fn-a", "usr-b": "fn-b"}, "c", "v"
    )
    assert len(edges) == 1
    assert edges[0]["relation"] == "calls"
    assert edges[0]["src"] == "fn-a" and edges[0]["dst"] == "fn-b"
    assert metrics["call_records_unindexed"] == 1
