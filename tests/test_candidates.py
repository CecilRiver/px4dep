"""Unit tests for M5 graded review candidates."""

from __future__ import annotations

from pathlib import Path

from px4dep.candidates import build_candidates, write_candidates
from px4dep.config import Config
from px4dep.manifest import Run
from px4dep_testkit import make_config


def _parameter(name: str) -> dict:
    return {
        "id": f"p-{name}",
        "kind": "parameter",
        "name": name,
        "qualified_name": name,
        "in_build": True,
    }


def _slice_path(
    path_id: str,
    parameter: str,
    *,
    category: str = "data",
    sink_kind: str = "criteria_state",
    sink: str = "estimator_status_s::gps_check_fail_flags",
    depth: int = 1,
) -> dict:
    return {
        "id": path_id,
        "kind": "slice_path",
        "parameter": parameter,
        "source": f"estimator::parameters::{parameter.lower()}",
        "source_id": f"cf-{parameter}",
        "sink": sink,
        "sink_id": f"f-{sink}",
        "sink_kind": sink_kind,
        "category": category,
        "confidence": "medium" if category == "data" else "low",
        "depth": depth,
        "cross_function": False,
        "steps": [
            {
                "kind": "rhs_member",
                "relation": "data_depends_on" if category == "data" else "control_depends_on",
                "from": sink,
                "to": f"parameter[{parameter}]",
                "file": "fixture.cpp",
                "line": 1,
            }
        ],
    }


def _m4_path(path_id: str, parameter: str) -> dict:
    return {
        "id": path_id,
        "kind": "end_to_end_path",
        "parameter": parameter,
        "sink": "estimator_status_s::gps_check_fail_flags",
        "message_type": "estimator_status_s",
        "topic": "estimator_status",
        "producer_function": "EKF2::PublishStatus",
        "consumer_function": "EstimatorChecks::checkAndReport",
        "category": "data",
        "confidence": "medium",
        "boundary_confidence": "high",
        "steps": [
            {"stage": "parameter_to_field", "relation": "data_depends_on", "from": "a", "to": "b", "source": "ekf_slice"},
            {"stage": "field_to_message", "relation": "defines", "from": "b", "to": "c", "evidence": {"edge_id": "e1"}},
            {"stage": "message_to_topic", "relation": "instantiates", "from": "c", "to": "d", "evidence": {"edge_id": "e2"}},
            {"stage": "topic_to_producer", "relation": "publishes", "from": "d", "to": "e", "evidence": {"edge_id": "e3"}},
            {"stage": "topic_to_producer", "relation": "uses_uorb", "from": "e", "to": "f", "evidence": {"edge_id": "e4"}},
            {"stage": "topic_to_consumer", "relation": "subscribes", "from": "d", "to": "g", "evidence": {"edge_id": "e5"}},
            {"stage": "topic_to_consumer", "relation": "uses_uorb", "from": "g", "to": "h", "evidence": {"edge_id": "e6"}},
        ],
        "consumer_evidence": {
            "health_predicates": [
                {"function": "EstimatorChecks::checkGps", "target": "health:checkGps"}
            ],
            "flags_written": [
                {"target": "failsafe_flags_s::position_accuracy_low"}
            ],
            "flags_read": [],
            "mode_restrictions": [{"relation": "restricts_arming"}],
            "protection_chains": [],
        },
    }


def _runtime(path_id: str, status: str = "field_observed") -> dict:
    topic_status = "observed"
    field_status = "observed" if status == "field_observed" else "not_observed"
    return {
        "kind": "runtime_path",
        "path_id": path_id,
        "runtime_support": status,
        "boundary": {
            "topic": {"topic": "estimator_status", "status": topic_status, "samples": 10, "instances": [0]},
            "field": {
                "topic": "estimator_status",
                "field": "gps_check_fail_flags",
                "status": field_status,
                "samples_non_nan": 10 if field_status == "observed" else 0,
            },
        },
    }


def _runtime_log() -> dict:
    return {
        "kind": "runtime_log",
        "initial_parameter_names": ["EKF2_GPS_CHECK", "EKF2_BARO_NOISE", "EKF2_UNRESOLVED"],
        "changed_parameter_names": [],
    }


def _build(
    *,
    slice_paths=None,
    m4_paths=None,
    runtime=None,
    unresolved=None,
):
    nodes = [
        _parameter("EKF2_GPS_CHECK"),
        _parameter("EKF2_BARO_NOISE"),
        _parameter("EKF2_UNRESOLVED"),
        _parameter("EKF2_NOT_BUILT"),
    ]
    slices = slice_paths if slice_paths is not None else [
        _slice_path("s1", "EKF2_GPS_CHECK"),
        _slice_path("s2", "EKF2_BARO_NOISE", category="control", sink_kind="control_flag"),
        _slice_path("s3", "EKF2_UNRESOLVED", sink_kind="control_flag"),
        _slice_path("s4", ""),
    ]
    m4 = m4_paths if m4_paths is not None else [_m4_path("m1", "EKF2_GPS_CHECK")]
    records = runtime if runtime is not None else [_runtime_log(), _runtime("m1")]
    records_unresolved = (
        unresolved
        if unresolved is not None
        else [{"kind": "m4_writer_publish_topic_unresolved", "parameter": "EKF2_UNRESOLVED"}]
    )
    return build_candidates(
        nodes,
        slices,
        m4,
        records,
        records_unresolved,
        commit="testcommit",
        variant="test_variant",
    )


def test_end_to_end_candidate_has_explanation_and_runtime_support():
    candidates, metrics = _build()
    by_name = {candidate["parameter"]: candidate for candidate in candidates}
    candidate = by_name["EKF2_GPS_CHECK"]
    assert candidate["end_to_end"] is True
    assert candidate["runtime_support"] == "field_observed"
    assert candidate["review_priority"] == "high"
    assert candidate["status"] == "eligible"
    assert candidate["health_layer"] is True
    assert candidate["failsafe_flag_write"] is True
    assert candidate["mode_restriction"] is True
    assert candidate["explanation"]["type"] == "end_to_end"
    assert "EKF2_GPS_CHECK" in candidate["explanation"]["text"]
    assert "EstimatorChecks::checkAndReport" in candidate["explanation"]["text"]
    assert candidate["alternative_path_ids"]
    assert "not a vulnerability" in candidate["disclaimer"]
    assert metrics["end_to_end"] == 1
    assert metrics["name_only_candidates"] == 0


def test_control_only_and_unresolved_are_downgraded():
    candidates, _metrics = _build()
    by_name = {candidate["parameter"]: candidate for candidate in candidates}
    control = by_name["EKF2_BARO_NOISE"]
    assert control["confidence"] == "low"
    assert control["review_priority"] == "low"
    assert control["runtime_support"] == "no_explicit_topic"
    assert "控制依赖" in " ".join(control["notes"])

    unresolved = by_name["EKF2_UNRESOLVED"]
    assert unresolved["status"] == "needs_review"
    assert unresolved["unresolved_paths"] == 1
    assert any("待核查" in note for note in unresolved["notes"])


def test_empty_parameter_and_unknown_parameter_are_not_candidates():
    candidates, metrics = _build()
    names = {candidate["parameter"] for candidate in candidates}
    assert "" not in names
    assert metrics["empty_parameter_paths"] == 1


def test_not_observed_runtime_does_not_become_unreachable():
    candidates, _metrics = _build(
        runtime=[_runtime_log(), _runtime("m1", status="boundary_not_observed")]
    )
    candidate = next(c for c in candidates if c["parameter"] == "EKF2_GPS_CHECK")
    assert candidate["runtime_support"] == "boundary_not_observed"
    assert any("未观测不等于不可达" in note for note in candidate["notes"])
    assert "unreachable" not in candidate


def test_missing_runtime_record_is_not_a_negative_claim():
    candidates, _metrics = _build(runtime=[_runtime_log()])
    candidate = next(c for c in candidates if c["parameter"] == "EKF2_GPS_CHECK")
    assert candidate["runtime_support"] == "no_runtime_record"
    assert candidate["end_to_end"] is True


def test_candidates_and_csv_are_stable(tmp_path: Path):
    first, _ = _build()
    second, _ = _build()
    assert [c["id"] for c in first] == [c["id"] for c in second]
    cfg: Config = make_config(tmp_path)
    run = Run(cfg=cfg, run_id="fixture", root=tmp_path / "run")
    run.ensure_dirs()
    write_candidates(run, first, {"candidates": len(first)})
    rows = run.path("reports", "candidates.csv").read_text(encoding="utf-8").splitlines()
    assert len(rows) == len(first) + 1
    assert rows[0].startswith("candidate_id,parameter,status,review_priority,score")
    assert all("EKF2_GPS_CHECK" in line for line in rows[1:2])
