"""Unit tests for M5 ULog reachability evidence (no real ULog required)."""

from __future__ import annotations

from pathlib import Path

from px4dep.ulog_evidence import (
    NOT_LOGGED,
    NOT_OBSERVED,
    OBSERVED,
    DatasetView,
    LogIndex,
    build_runtime,
)


def _topic(node_id: str, name: str, message_type: str) -> dict:
    return {
        "id": node_id,
        "kind": "topic",
        "name": name,
        "qualified_name": name,
        "message_type": message_type,
    }


def _field(
    node_id: str, message_type: str, name: str, **extra: object
) -> dict:
    node = {
        "id": node_id,
        "kind": "topic_field",
        "name": name,
        "qualified_name": f"{message_type}::{name}",
        "message_type": message_type,
        "is_constant": extra.pop("is_constant", False),
    }
    node.update(extra)
    return node


def _path(path_id: str = "path-1", sink: str = "estimator_status_s::gps_check_fail_flags") -> dict:
    return {
        "id": path_id,
        "kind": "end_to_end_path",
        "parameter": "EKF2_GPS_CHECK",
        "topic": "estimator_status",
        "sink": sink,
        "producer_function": "EKF2::PublishStatus",
        "consumer_function": "EstimatorChecks::checkAndReport",
        "confidence": "medium",
        "boundary_confidence": "high",
        "consumer_evidence": {
            "health_predicates": [
                {"function": "EstimatorChecks::checkGps", "target": "health:checkGps"}
            ],
            "flags_written": [
                {"target": "failsafe_flags_s::position_accuracy_low"},
            ],
            "flags_read": [],
        },
    }


def _nodes() -> list[dict]:
    return [
        _topic("t-status", "estimator_status", "estimator_status_s"),
        _field("f-gps", "estimator_status_s", "gps_check_fail_flags"),
        _field("f-missing", "estimator_status_s", "missing_field"),
        _field("f-const", "estimator_status_s", "const_field", is_constant=True),
        _topic("t-innov", "estimator_innovations", "estimator_innovations_s"),
        _field("f-innov", "estimator_innovations_s", "innovation"),
        _topic("t-att", "vehicle_attitude", "vehicle_attitude_s"),
        _field("f-roll", "vehicle_attitude_s", "roll"),
        {
            "id": "p-1",
            "kind": "parameter",
            "name": "EKF2_GPS_CHECK",
            "in_build": True,
        },
    ]


def _log(**overrides: object) -> LogIndex:
    defaults = dict(
        path=Path("fixture.ulg"),
        sha256="sha256-fixture",
        size_bytes=100,
        scenario="normal_sitl",
        ver_sw="abc123def",
        start_timestamp=1_000_000,
        last_timestamp=11_000_000,
        datasets={
            "estimator_status": [
                DatasetView(
                    name="estimator_status",
                    multi_id=0,
                    columns={
                        "timestamp": [1_000_000, 2_000_000, 3_000_000],
                        "gps_check_fail_flags": [0, 1, 2],
                        "const_field": [7, 7, 7],
                    },
                )
            ],
            "failsafe_flags": [
                DatasetView(
                    name="failsafe_flags",
                    multi_id=0,
                    columns={
                        "timestamp": [1_000_000, 4_000_000],
                        "position_accuracy_low": [0, 1],
                    },
                )
            ],
        },
        message_formats={
            "estimator_status",
            "estimator_innovations",
            "failsafe_flags",
            "vehicle_attitude",
        },
        initial_parameters={"EKF2_GPS_CHECK": 7},
        changed_parameters=[],
    )
    defaults.update(overrides)
    return LogIndex(**defaults)


def test_observed_topic_and_field_are_presence_only():
    records, metrics, unresolved = build_runtime(
        _nodes(), [_path()], _log(), commit="abc123"
    )
    assert metrics["log_present"] is True
    assert metrics["commit_match"] is True
    assert metrics["paths_field_observed"] == 1
    assert metrics["paths_topic_observed"] == 1
    assert metrics["unreachable_claims"] == 0
    assert metrics["not_observed_is_not_unreachable"] is True

    path = next(record for record in records if record["kind"] == "runtime_path")
    assert path["topic_level_reachable"] is True
    assert path["field_level_reachable"] is True
    assert path["runtime_support"] == "field_observed"
    assert path["interpretation"] == "presence_only_not_causality"
    assert path["not_observed_means_unreachable"] is False

    flag = next(record for record in records if record["kind"] == "runtime_flag")
    assert flag["status"] == OBSERVED
    assert flag["flag"] == "failsafe_flags_s::position_accuracy_low"
    assert unresolved == []


def test_topic_format_without_data_is_not_observed_not_unreachable():
    log = _log()
    log.datasets.pop("estimator_status")
    records, metrics, unresolved = build_runtime(
        _nodes(), [_path()], log, commit="abc123"
    )
    assert metrics["paths_topic_observed"] == 0
    assert metrics["paths_boundary_not_observed"] == 1
    assert metrics["unreachable_claims"] == 0
    path = next(record for record in records if record["kind"] == "runtime_path")
    assert path["runtime_support"] == "boundary_not_observed"
    assert path["boundary"]["topic"]["status"] == NOT_OBSERVED
    assert path["not_observed_means_unreachable"] is False
    assert any(
        item["kind"] == "ulog_path_topic_not_observed" for item in unresolved
    )


def test_topic_absent_from_formats_is_reported_as_not_logged():
    log = _log(message_formats={"failsafe_flags"})
    log.datasets.pop("estimator_status")
    records, metrics, unresolved = build_runtime(
        _nodes(), [_path()], log, commit="abc123"
    )
    path = next(record for record in records if record["kind"] == "runtime_path")
    assert path["boundary"]["topic"]["status"] == NOT_LOGGED
    assert any(item["kind"] == "ulog_path_topic_not_logged" for item in unresolved)
    assert metrics["graph_topics_not_logged"] >= 1


def test_multi_instance_topic_sums_samples_and_lists_instances():
    log = _log()
    log.datasets["estimator_status"] = [
        DatasetView(
            name="estimator_status",
            multi_id=0,
            columns={"timestamp": [1, 2], "gps_check_fail_flags": [0, 1]},
        ),
        DatasetView(
            name="estimator_status",
            multi_id=3,
            columns={"timestamp": [3, 4, 5], "gps_check_fail_flags": [0, 1, 2]},
        ),
    ]
    records, metrics, _unresolved = build_runtime(
        _nodes(), [_path()], log, commit="abc123"
    )
    topic = next(
        record
        for record in records
        if record["kind"] == "runtime_topic" and record["topic"] == "estimator_status"
    )
    assert topic["instances"] == [0, 3]
    assert topic["samples"] == 5
    assert metrics["paths_field_observed"] == 1


def test_missing_field_keeps_topic_reachable_but_marks_field_absent():
    records, metrics, unresolved = build_runtime(
        _nodes(),
        [_path(sink="estimator_status_s::missing_field")],
        _log(),
        commit="abc123",
    )
    path = next(record for record in records if record["kind"] == "runtime_path")
    assert path["topic_level_reachable"] is True
    assert path["field_level_reachable"] is False
    assert path["runtime_support"] == "topic_observed"
    assert metrics["paths_field_observed"] == 0
    assert any(
        item["kind"] == "ulog_path_field_not_observed" for item in unresolved
    )


def test_constant_field_is_not_applicable():
    records, _metrics, _unresolved = build_runtime(
        _nodes(), [_path()], _log(), commit="abc123"
    )
    const = next(
        record
        for record in records
        if record["kind"] == "runtime_field" and record["field"] == "const_field"
    )
    assert const["status"] == "not_applicable_constant"
    assert const["samples_non_nan"] == 0


def test_missing_log_does_not_fabricate_negative_reachability():
    records, metrics, unresolved = build_runtime(_nodes(), [_path()], None)
    assert records == []
    assert metrics["log_present"] is False
    assert metrics["unreachable_claims"] == 0
    assert unresolved == [{"kind": "ulog_not_supplied"}]


def test_flag_absent_from_log_is_reported_without_negative_claim():
    log = _log()
    log.datasets["failsafe_flags"] = [
        DatasetView(
            name="failsafe_flags",
            multi_id=0,
            columns={"timestamp": [1], "other_flag": [1]},
        )
    ]
    records, metrics, unresolved = build_runtime(
        _nodes(), [_path()], log, commit="abc123"
    )
    flag = next(record for record in records if record["kind"] == "runtime_flag")
    assert flag["status"] == NOT_OBSERVED
    assert metrics["flags_not_observed"] == 1
    assert metrics["unreachable_claims"] == 0
    assert any(item["kind"] == "ulog_flag_not_observed" for item in unresolved)
