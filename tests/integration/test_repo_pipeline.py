"""Repository integration tests for framework-level gold-standard relations.

These tests read the real PX4 tree and the most recent run; they are skipped
when the declared baseline is not available on this machine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from px4dep.cpp_index import load_raw
from px4dep.config import load_config
from px4dep.manifest import resolve_run
from px4dep.parameter_catalog import build_catalog
from px4dep.uorb_schema import build_schema
from px4dep.util import read_jsonl
from px4dep.validation import validate_run
from px4dep_testkit import PROJECT_ROOT

pytestmark = pytest.mark.integration

CONFIG_PATH = PROJECT_ROOT / "configs" / "px4_sitl_default.yaml"


@pytest.fixture(scope="module")
def cfg():
    if not CONFIG_PATH.exists():
        pytest.skip("baseline config is missing")
    config = load_config(CONFIG_PATH)
    if not (config.repo / ".git").exists() and not config.parameters_json.exists():
        pytest.skip("PX4 repository baseline is not available")
    if not config.parameters_json.exists():
        pytest.skip("PX4 build metadata is not available")
    return config


@pytest.fixture(scope="module")
def latest_run(cfg):
    try:
        return resolve_run(cfg, "latest")
    except FileNotFoundError:
        pytest.skip("no completed run found; run `px4dep run-all` first")


def test_real_msg_schema_contains_vehicle_attitude(cfg):
    nodes, edges, metrics, unresolved = build_schema(
        cfg, commit="integration", variant=cfg.build_variant
    )
    by_name = {n["name"]: n for n in nodes if n["kind"] == "topic"}
    assert "vehicle_attitude" in by_name
    assert by_name["vehicle_attitude"]["message_type"] == "vehicle_attitude_s"
    assert "estimator_status" in by_name
    assert "sensor_accel" in by_name

    fields = {
        n["qualified_name"]: n
        for n in nodes
        if n["kind"] == "topic_field" and n["message_type"] == "vehicle_attitude_s"
    }
    assert "vehicle_attitude_s::q" in fields
    assert fields["vehicle_attitude_s::q"]["is_array"] is True
    assert fields["vehicle_attitude_s::q"]["array_size"] == 4

    triplet_fields = {
        n["qualified_name"]: n
        for n in nodes
        if n["kind"] == "topic_field"
        and n["message_type"] == "position_setpoint_triplet_s"
    }
    assert triplet_fields["position_setpoint_triplet_s::previous"]["is_nested"] is True
    assert (
        triplet_fields["position_setpoint_triplet_s::previous"]["field_cpp_type"]
        == "position_setpoint_s"
    )

    by_id = {n["id"]: n for n in nodes}
    field_type_edges = [
        e for e in edges if e["relation"] == "field_type"
        and by_id[e["src"]]["qualified_name"]
        == "position_setpoint_triplet_s::previous"
    ]
    assert len(field_type_edges) == 1
    assert by_id[field_type_edges[0]["dst"]]["qualified_name"] == "position_setpoint_s"
    assert metrics["msg_parse_errors"] == 0
    assert not [u for u in unresolved if u["kind"] == "msg_parse_error"]


def test_latest_run_validates_and_passes_m1_gates(latest_run):
    validation = validate_run(latest_run.cfg, latest_run)
    assert validation["ok"], validation["errors"]

    metrics = json.loads(
        (latest_run.path("reports", "metrics.json")).read_text(encoding="utf-8")
    )
    assert metrics["m1_gate_passed"] is True
    assert all(metrics["m1_gates"].values())
    assert metrics["graph"]["nodes"] > 0
    assert metrics["graph"]["edges"] > 0


def test_latest_run_passes_m2_health_chain_gates(latest_run):
    metrics = json.loads(
        (latest_run.path("reports", "metrics.json")).read_text(encoding="utf-8")
    )
    assert metrics["m2_gate_passed"] is True
    assert all(metrics["m2_gates"].values())
    health = metrics["health_chain"]
    assert health["health_predicates"] > 0
    assert health["health_flags_mapped"] > 0

    nodes = {
        n["id"]: n
        for n in read_jsonl(latest_run.path("graph", "nodes.jsonl"))
    }
    edges = read_jsonl(latest_run.path("graph", "edges.jsonl"))
    kinds = {n["kind"] for n in nodes.values()}
    assert {
        "predicate",
        "health_flag",
        "mode_capability",
        "protection_decision",
        "health_component",
    } <= kinds

    relations = {e["relation"] for e in edges}
    assert {
        "reports_health",
        "restricts_arming",
        "restricts_run",
        "writes_flag",
        "reads_flag",
        "selects_action",
    } <= relations


def test_gold_standard_flag_to_protection_action(latest_run):
    nodes = {
        n["id"]: n
        for n in read_jsonl(latest_run.path("graph", "nodes.jsonl"))
    }
    edges = read_jsonl(latest_run.path("graph", "edges.jsonl"))

    flags = [
        n for n in nodes.values()
        if n["kind"] == "health_flag"
        and n["name"] == "flight_time_limit_exceeded"
    ]
    assert len(flags) == 1
    flag_id = flags[0]["id"]
    assert "failsafe_flags" in flags[0]["topics"]

    reads = [
        e for e in edges
        if e["relation"] == "reads_flag" and e["dst"] == flag_id
        and nodes[e["src"]]["kind"] == "predicate"
        and nodes[e["src"]]["predicate_layer"] == "protection_condition"
    ]
    assert reads

    selects = []
    for predicate_edge in reads:
        selects.extend(
            e for e in edges
            if e["relation"] == "selects_action" and e["src"] == predicate_edge["src"]
        )
    assert any(nodes[e["dst"]]["name"] == "RTL" for e in selects)


def test_gold_standard_mode_capability_and_query(latest_run):
    nodes = {
        n["id"]: n
        for n in read_jsonl(latest_run.path("graph", "nodes.jsonl"))
    }
    edges = read_jsonl(latest_run.path("graph", "edges.jsonl"))

    modes = [n for n in nodes.values() if n["kind"] == "mode_capability"]
    assert any(n.get("mode_domain") == "mode_group" for n in modes)
    assert any(n.get("mode_domain") == "nav_state" for n in modes)

    queries = [
        n for n in nodes.values()
        if n["kind"] == "predicate" and n.get("method") == "modeCanRun"
    ]
    assert queries
    checked = [
        e for e in edges
        if e["relation"] == "checks_mode" and e["src"] == queries[0]["id"]
    ]
    assert checked
    assert all(nodes[e["dst"]]["kind"] == "mode_capability" for e in checked)


def test_gold_standard_health_flag_write_read_and_message_mapping(latest_run):
    nodes = {
        n["id"]: n
        for n in read_jsonl(latest_run.path("graph", "nodes.jsonl"))
    }
    edges = read_jsonl(latest_run.path("graph", "edges.jsonl"))

    flag = next(
        n for n in nodes.values()
        if n["kind"] == "health_flag" and n["name"] == "position_accuracy_low"
    )
    writes = [
        e for e in edges if e["relation"] == "writes_flag" and e["dst"] == flag["id"]
    ]
    reads = [
        e for e in edges if e["relation"] == "reads_flag" and e["dst"] == flag["id"]
    ]
    maps = [
        e for e in edges if e["relation"] == "maps_to_field" and e["src"] == flag["id"]
    ]
    assert writes
    assert reads
    assert len(maps) == 1
    assert nodes[maps[0]["dst"]]["qualified_name"] == (
        "failsafe_flags_s::position_accuracy_low"
    )


def test_warning_mode_prohibition_flag_and_action_are_distinct(latest_run):
    nodes = {
        n["id"]: n
        for n in read_jsonl(latest_run.path("graph", "nodes.jsonl"))
    }
    edges = read_jsonl(latest_run.path("graph", "edges.jsonl"))

    warnings = [
        n for n in nodes.values()
        if n["kind"] == "predicate"
        and n.get("effect") in {"health_warning", "arming_warning"}
    ]
    assert warnings

    arming_prohibitions = [
        e for e in edges
        if e["relation"] == "restricts_arming"
        and nodes[e["src"]]["kind"] == "predicate"
    ]
    assert arming_prohibitions

    flags = [n for n in nodes.values() if n["kind"] == "health_flag"]
    decisions = [n for n in nodes.values() if n["kind"] == "protection_decision"]
    assert flags
    assert decisions

    selected = [e for e in edges if e["relation"] == "selects_action"]
    assert selected
    # Decisions are their own node class, never health warnings.
    assert all(nodes[e["dst"]]["kind"] == "protection_decision" for e in selected)
    assert not ({n["id"] for n in warnings} & {n["id"] for n in decisions})


def test_gold_standard_parameter_wrapper_binding_chain(latest_run):
    nodes = {
        n["id"]: n
        for n in read_jsonl(latest_run.path("graph", "nodes.jsonl"))
    }
    edges = read_jsonl(latest_run.path("graph", "edges.jsonl"))

    parameters = [
        n for n in nodes.values()
        if n["kind"] == "parameter" and n["name"] == "EKF2_SENS_EN"
    ]
    wrappers = [
        n for n in nodes.values()
        if n["kind"] == "param_wrapper" and n.get("parameter_name") == "EKF2_SENS_EN"
    ]
    config_fields = [
        n for n in nodes.values()
        if n["kind"] == "config_field"
        and n["qualified_name"] == "estimator::parameters::ekf2_sens_en"
    ]
    assert len(parameters) == 1
    assert wrappers
    assert len(config_fields) == 1

    parameter_id = parameters[0]["id"]
    config_field_id = config_fields[0]["id"]
    wrapper_ids = {w["id"] for w in wrappers}

    relations = {(e["relation"], e["src"], e["dst"]) for e in edges}
    assert any(
        relation == "wraps" and src in wrapper_ids and dst == parameter_id
        for relation, src, dst in relations
    )
    assert any(
        relation == "binds_to" and src in wrapper_ids and dst == config_field_id
        for relation, src, dst in relations
    )
    assert ("configures", parameter_id, config_field_id) in relations


def test_gold_standard_ekf2_publishes_vehicle_attitude(latest_run):
    nodes = {
        n["id"]: n
        for n in read_jsonl(latest_run.path("graph", "nodes.jsonl"))
    }
    edges = read_jsonl(latest_run.path("graph", "edges.jsonl"))
    topics = [
        n for n in nodes.values()
        if n["kind"] == "topic" and n["name"] == "vehicle_attitude"
    ]
    assert len(topics) == 1
    topic_id = topics[0]["id"]

    endpoints = [
        n for n in nodes.values()
        if n["kind"] == "uorb_endpoint" and n["qualified_name"] == "EKF2::_attitude_pub"
    ]
    assert len(endpoints) == 1
    endpoint_ids = {n["id"] for n in endpoints}
    assert any(
        e["relation"] == "publishes" and e["src"] in endpoint_ids and e["dst"] == topic_id
        for e in edges
    )


def test_repeated_parsing_produces_identical_ids(cfg, latest_run):
    first_schema = build_schema(cfg, "integration", cfg.build_variant)
    second_schema = build_schema(cfg, "integration", cfg.build_variant)
    assert [n["id"] for n in first_schema[0]] == [n["id"] for n in second_schema[0]]
    assert [e["id"] for e in first_schema[1]] == [e["id"] for e in second_schema[1]]

    raw = load_raw(cfg, latest_run)
    if not raw:
        pytest.skip("latest run has no C++ records")
    first_catalog, _, _ = build_catalog(cfg, raw)
    second_catalog, _, _ = build_catalog(cfg, raw)
    assert [(r["name"], r["id"]) for r in first_catalog] == [
        (r["name"], r["id"]) for r in second_catalog
    ]


def test_latest_run_passes_m3_ekf_slice_gates(latest_run):
    metrics = json.loads(
        (latest_run.path("reports", "metrics.json")).read_text(encoding="utf-8")
    )
    assert metrics["m3_gate_passed"] is True
    assert all(metrics["m3_gates"].values())
    ekf = metrics["ekf_slice"]
    assert ekf["parameter_fields_used"] > 0
    assert ekf["parameter_fields_unbound"] == 0
    assert ekf["data_edges"] > 0
    assert ekf["control_edges"] > 0
    assert ekf["paths_without_steps"] == 0
    assert ekf["cross_function_paths"] > 0
    assert {
        "sinks_control_flag",
        "sinks_fault_flag",
        "sinks_criteria_state",
        "sinks_message_field",
    } <= set(ekf)

    nodes = {
        n["id"]: n
        for n in read_jsonl(latest_run.path("graph", "nodes.jsonl"))
    }
    edges = read_jsonl(latest_run.path("graph", "edges.jsonl"))
    kinds = {n["kind"] for n in nodes.values()}
    assert "estimator_flag" in kinds
    assert kinds & {"estimator_flag", "slice_sink"}

    relations = {e["relation"] for e in edges}
    assert {"data_depends_on", "control_depends_on"} <= relations

    paths = read_jsonl(latest_run.path("graph", "paths.jsonl"))
    assert paths
    assert all(p["steps"] for p in paths)
    assert any(p["category"] == "data" for p in paths)
    assert any(p["category"] == "control" for p in paths)
    assert all(
        p["category"] in {"data", "control"} for p in paths
    )


def test_gold_standard_ekf2_parameter_to_control_flag(latest_run):
    nodes = {
        n["id"]: n
        for n in read_jsonl(latest_run.path("graph", "nodes.jsonl"))
    }
    edges = read_jsonl(latest_run.path("graph", "edges.jsonl"))
    paths = read_jsonl(latest_run.path("graph", "paths.jsonl"))

    path = next(
        p for p in paths
        if p["parameter"] == "EKF2_GND_MAX_HGT"
        and p["sink"].endswith("::gnd_effect")
        and p["category"] == "data"
    )
    assert path["sink_kind"] == "control_flag"
    assert path["source"] == "estimator::parameters::ekf2_gnd_max_hgt"
    assert path["depth"] == 1
    assert path["steps"][0]["kind"] == "rhs_member"
    assert path["steps"][0]["relation"] == "data_depends_on"

    sink = nodes[path["sink_id"]]
    source = nodes[path["source_id"]]
    assert sink["kind"] == "estimator_flag"
    assert sink["flag_domain"] == "control"
    assert source["kind"] == "config_field"

    matching = [
        e for e in edges
        if e["relation"] == "data_depends_on"
        and e["src"] == path["sink_id"]
        and e["dst"] == path["source_id"]
    ]
    assert len(matching) == 1
    assert matching[0]["evidence"]["parameter"] == "EKF2_GND_MAX_HGT"
    assert matching[0]["evidence"]["dependency_class"] == "data"
    assert matching[0]["confidence"] == "medium"


def test_latest_run_passes_m4_cross_module_gates(latest_run):
    metrics = json.loads(
        (latest_run.path("reports", "metrics.json")).read_text(encoding="utf-8")
    )
    assert metrics["m4_gate_passed"] is True
    assert all(metrics["m4_gates"].values())
    cross = metrics["cross_module"]
    assert cross["paths"] > 0
    assert cross["name_based_joins"] == 0
    assert cross["paths_without_topic_field_hop"] == 0
    assert cross["paths_without_topic_hop"] == 0
    assert cross["paths_without_ekf2_producer"] == 0
    assert cross["paths_without_commander_consumer"] == 0
    assert cross["paths_without_edge_evidence"] == 0
    assert cross["paths_with_health_predicate"] > 0
    assert cross["call_graph"]["call_edges"] > 0

    paths = read_jsonl(latest_run.path("graph", "end_to_end_paths.jsonl"))
    assert paths
    for path in paths:
        stages = {step.get("stage") for step in path["steps"]}
        assert {"parameter_to_field", "field_to_message", "message_to_topic"} <= stages
        assert "topic_to_producer" in stages
        assert "topic_to_consumer" in stages
        boundary = [
            step for step in path["steps"] if step.get("source") != "ekf_slice"
        ]
        assert len(boundary) == 6
        assert all(step["evidence"]["edge_id"] for step in boundary)
        assert path["consumer_module"] == "commander"
        assert path["producer_module"] == "ekf2"
        assert path["consumer_field_binding"] == "topic_level"


def test_gold_standard_ekf2_status_field_to_commander_health_check(latest_run):
    nodes = {
        n["id"]: n for n in read_jsonl(latest_run.path("graph", "nodes.jsonl"))
    }
    edges = read_jsonl(latest_run.path("graph", "edges.jsonl"))
    paths = read_jsonl(latest_run.path("graph", "end_to_end_paths.jsonl"))

    path = next(
        item
        for item in paths
        if item["parameter"] == "EKF2_GPS_CHECK"
        and item["sink"] == "estimator_status_s::gps_check_fail_flags"
        and item["consumer_function"] == "EstimatorChecks::checkAndReport"
    )
    assert path["topic"] == "estimator_status"
    assert path["producer_function"] == "EKF2::PublishStatus"
    assert path["category"] == "data"
    assert path["confidence"] == "medium"

    stage_steps: dict[str, list[dict]] = {}
    for step in path["steps"]:
        stage_steps.setdefault(step["stage"], []).append(step)
    assert stage_steps["field_to_message"][0]["relation"] == "defines"
    assert stage_steps["message_to_topic"][0]["relation"] == "instantiates"
    assert {step["relation"] for step in stage_steps["topic_to_producer"]} == {
        "uses_uorb",
        "publishes",
    }
    assert {step["relation"] for step in stage_steps["topic_to_consumer"]} == {
        "subscribes",
        "uses_uorb",
    }

    edge_by_id = {edge["id"]: edge for edge in edges}
    for stage in (
        "field_to_message",
        "message_to_topic",
        "topic_to_producer",
        "topic_to_consumer",
    ):
        for step in stage_steps[stage]:
            assert step["evidence"]["edge_id"] in edge_by_id
    uses = [
        edge_by_id[step["evidence"]["edge_id"]]
        for step in path["steps"]
        if step["kind"] in {"producer_endpoint_use", "consumer_endpoint_use"}
    ]
    assert {edge["relation"] for edge in uses} == {"uses_uorb"}
    assert any(
        edge["evidence"].get("operation") == "publish" for edge in uses
    )

    evidence = path["consumer_evidence"]
    helper_functions = {
        item["function"] for item in evidence["health_predicates"]
    }
    assert "EstimatorChecks::checkEstimatorStatus" in helper_functions
    assert any(
        item["target"] == "failsafe_flags_s::global_position_invalid"
        for item in evidence["flags_written"]
    )
    assert evidence["mode_restrictions"]

    sink = nodes[path["sink_id"]]
    topic = nodes[path["topic_id"]]
    assert sink["kind"] == "topic_field"
    assert sink["message_type"] == path["message_type"]
    assert topic["kind"] == "topic"
    assert topic["message_type"] == path["message_type"]


def test_aid_source_fields_are_not_fanned_out_by_message_type(latest_run):
    paths = read_jsonl(latest_run.path("graph", "end_to_end_paths.jsonl"))
    assert not [
        item
        for item in paths
        if item["message_type"].startswith("estimator_aid_source")
    ]
    unresolved = read_jsonl(latest_run.path("unresolved", "cross_module.jsonl"))
    assert any(
        item["kind"] == "m4_writer_publish_topic_unresolved"
        and item["message_type"].startswith("estimator_aid_source")
        for item in unresolved
    )


def test_latest_run_passes_m5_runtime_and_candidate_gates(latest_run):
    metrics = json.loads(
        (latest_run.path("reports", "metrics.json")).read_text(encoding="utf-8")
    )
    assert metrics["m5_gate_passed"] is True
    assert all(metrics["m5_gates"].values())

    runtime = metrics["runtime"]
    assert runtime["log_present"] is True
    assert runtime["commit_match"] is True
    assert runtime["interpretation"] == "reachability_only"
    assert runtime["unreachable_claims"] == 0
    assert runtime["paths"] > 0
    assert runtime["paths_with_runtime_record"] == runtime["paths"]

    candidates = metrics["candidates"]
    assert candidates["candidates"] > 0
    assert candidates["name_only_candidates"] == 0
    assert candidates["program_semantic_paths"] >= candidates["candidates"]


def test_gold_standard_ekf2_gps_check_runtime_field_is_observed(latest_run):
    paths = read_jsonl(latest_run.path("graph", "end_to_end_paths.jsonl"))
    runtime = read_jsonl(latest_run.path("evidence", "runtime.jsonl"))
    runtime_by_path = {
        record["path_id"]: record
        for record in runtime
        if record.get("kind") == "runtime_path"
    }
    golden = next(
        item
        for item in paths
        if item["parameter"] == "EKF2_GPS_CHECK"
        and item["sink"] == "estimator_status_s::gps_check_fail_flags"
        and item["consumer_function"] == "EstimatorChecks::checkAndReport"
    )
    record = runtime_by_path[golden["id"]]
    assert record["topic_level_reachable"] is True
    assert record["field_level_reachable"] is True
    assert record["runtime_support"] == "field_observed"
    assert record["boundary"]["field"]["samples_non_nan"] > 0
    assert record["interpretation"] == "presence_only_not_causality"
    assert record["not_observed_means_unreachable"] is False


def test_runtime_statuses_are_presence_only_not_negative_claims(latest_run):
    runtime = read_jsonl(latest_run.path("evidence", "runtime.jsonl"))
    assert runtime
    evidence_records = [
        record for record in runtime if record.get("kind") != "runtime_log"
    ]
    assert evidence_records
    assert all(
        record.get("not_observed_means_unreachable") is False
        for record in evidence_records
    )
    statuses = {record.get("status") for record in runtime if record.get("kind") in {
        "runtime_topic",
        "runtime_field",
        "runtime_flag",
    }}
    assert statuses <= {"observed", "not_observed", "not_logged", "not_applicable_constant"}

    metrics = json.loads(
        (latest_run.path("reports", "runtime_metrics.json")).read_text(encoding="utf-8")
    )
    assert metrics["not_observed_is_not_unreachable"] is True
    assert metrics["graph_topics_not_observed"] + metrics["graph_topics_not_logged"] > 0
    assert metrics["unreachable_claims"] == 0


def test_candidates_are_graded_review_items_with_explanations(latest_run):
    nodes = {
        n["id"]: n for n in read_jsonl(latest_run.path("graph", "nodes.jsonl"))
    }
    candidates = read_jsonl(latest_run.path("evidence", "candidates.jsonl"))
    assert candidates

    for candidate in candidates:
        parameter_node = nodes[candidate["parameter_node_id"]]
        assert parameter_node["kind"] == "parameter"
        assert parameter_node["in_build"] is True
        assert candidate["disclaimer"]
        assert candidate["explanation"]["text"]
        assert candidate["path_count"] > 0
        assert candidate["review_priority"] in {"high", "medium", "low"}
        assert candidate["status"] in {"eligible", "needs_review"}
        assert "runtime_support" in candidate

    by_name = {candidate["parameter"]: candidate for candidate in candidates}
    golden = by_name["EKF2_GPS_CHECK"]
    assert golden["end_to_end"] is True
    assert golden["health_layer"] is True
    assert golden["failsafe_flag_write"] is True
    assert golden["mode_restriction"] is True
    assert golden["runtime_support"] == "field_observed"
    assert golden["review_priority"] == "high"
    assert golden["status"] == "eligible"
    assert "EstimatorChecks::checkAndReport" in golden["explanation"]["text"]
    assert golden["log_parameter_present"] is True
    assert golden["log_parameter_changed"] is False


def test_candidate_csv_matches_candidate_jsonl(latest_run):
    candidates = read_jsonl(latest_run.path("evidence", "candidates.jsonl"))
    lines = (
        latest_run.path("reports", "candidates.csv")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(lines) == len(candidates) + 1
    assert lines[0].split(",")[:5] == [
        "candidate_id",
        "parameter",
        "status",
        "review_priority",
        "score",
    ]
    csv_ids = {line.split(",")[0] for line in lines[1:]}
    assert csv_ids == {candidate["id"] for candidate in candidates}


def test_gold_standard_control_condition_is_not_a_data_edge(latest_run):
    nodes = {
        n["id"]: n
        for n in read_jsonl(latest_run.path("graph", "nodes.jsonl"))
    }
    edges = read_jsonl(latest_run.path("graph", "edges.jsonl"))
    paths = read_jsonl(latest_run.path("graph", "paths.jsonl"))

    path = next(
        p for p in paths
        if p["parameter"] == "EKF2_HGT_REF"
        and p["sink"] == "estimator_aid_source1d_s::time_last_fuse"
        and p["category"] == "control"
    )
    assert path["sink_kind"] == "criteria_state"
    assert path["steps"][0]["kind"] == "control_condition"
    assert path["steps"][0]["relation"] == "control_depends_on"
    assert "ekf2_hgt_ref" in path["steps"][0]["condition"]

    pair_edges = [
        e for e in edges
        if e["src"] == path["sink_id"] and e["dst"] == path["source_id"]
    ]
    assert pair_edges
    assert {e["relation"] for e in pair_edges} == {"control_depends_on"}
    assert nodes[path["sink_id"]]["kind"] == "topic_field"


def test_latest_run_passes_m6_break_chain_and_llvm_gates(latest_run):
    metrics = json.loads(
        (latest_run.path("reports", "metrics.json")).read_text(encoding="utf-8")
    )
    assert metrics["m6_gate_passed"] is True
    assert all(metrics["m6_gates"].values())

    break_chains = read_jsonl(latest_run.path("evidence", "break_chains.jsonl"))
    assert break_chains
    assert {"M1", "M2", "M3", "M4"} <= {
        record["stage"] for record in break_chains
    }
    assert set(metrics["break_chains"]["by_stage"]) == {
        "M1",
        "M2",
        "M3",
        "M4",
        "M5",
    }
    assert metrics["break_chains"]["selection"]["selected_kind"] == (
        "m4_writer_publish_topic_unresolved"
    )

    llvm_records = read_jsonl(latest_run.path("evidence", "llvm.jsonl"))
    assert llvm_records
    assert all(record["ast_cross_check"]["passed"] for record in llvm_records)
    assert all(record["end_to_end_claim"] is False for record in llvm_records)
    assert metrics["llvm"]["pass"]["stats"]["unresolved"] == 0
    assert metrics["llvm"]["end_to_end_paths_created"] == 0

    resolutions = read_jsonl(
        latest_run.path("evidence", "break_chain_resolution.jsonl")
    )
    selected = metrics["break_chains"]["selection"]["selected_kind"]
    selected_records = [
        record
        for record in break_chains
        if record["unresolved_kind"] == selected
    ]
    assert len(resolutions) == len(selected_records)
    assert all(record["end_to_end_created"] is False for record in resolutions)
    assert all(record["root_cause_after"] for record in resolutions)

    residual = read_jsonl(latest_run.path("unresolved", "llvm.jsonl"))
    assert residual
    assert all(
        record.get("root_cause_after") or record.get("reason")
        for record in residual
    )
    assert metrics["llvm"]["downstream_coverage"]["aid_source_topics"] == 21
    assert (
        metrics["llvm"]["downstream_coverage"]["with_commander_subscriber"] == 0
    )
