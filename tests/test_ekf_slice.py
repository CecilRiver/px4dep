"""Unit tests for the M3 EKF2 field-sensitive backward slice."""

from __future__ import annotations

from pathlib import Path

from px4dep.ekf_slice import build_ekf_slice, write_ekf_slice_csv
from px4dep_testkit import make_config


def _ref(
    qname: str,
    name: str,
    parent: str,
    receiver: str,
    kind: str = "member_field",
) -> dict[str, object]:
    return {
        "qualified_name": qname,
        "name": name,
        "parent_class": parent,
        "receiver_expr": receiver,
        "receiver_kind": kind,
    }


def _param_ref(name: str = "ekf2_gps_ctrl") -> dict[str, object]:
    return _ref(
        f"estimator::parameters::{name}",
        name,
        "estimator::parameters",
        "_params",
    )


def _member(
    qname: str,
    name: str,
    parent: str,
    receiver: str,
    caller_usr: str,
    caller: str,
    line: int,
    *,
    access: str = "write",
    rhs_members: list[dict[str, object]] | None = None,
    rhs_locals: list[str] | None = None,
    conditions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "record": "member_access",
        "qualified_name": qname,
        "field": name,
        "parent_class": parent,
        "receiver_expr": receiver,
        "receiver_kind": "member_field",
        "receiver_root": "root",
        "access": access,
        "write_op": "=" if access == "write" else "",
        "rhs_members": rhs_members or [],
        "rhs_locals": rhs_locals or [],
        "control_conditions": conditions or [],
        "caller_usr": caller_usr,
        "caller": caller,
        "file": f"src/modules/ekf2/EKF/{caller.split('::')[-1]}.cpp",
        "line": line,
        "column": 1,
        "tu": "src/modules/ekf2/EKF/test.cpp",
    }


def _condition(text: str, members: list[dict[str, object]], line: int) -> dict[str, object]:
    return {
        "kind": "if",
        "text": text,
        "members": members,
        "locals": [],
        "line": line,
        "column": 1,
    }


def _cpp_nodes(extra: list[dict[str, object]] | None = None) -> list[dict[str, object]]:
    nodes: list[dict[str, object]] = [
        {
            "id": "cf-gps-ctrl",
            "kind": "config_field",
            "qualified_name": "estimator::parameters::ekf2_gps_ctrl",
            "name": "ekf2_gps_ctrl",
        }
    ]
    nodes.extend(extra or [])
    return nodes


def _build(tmp_path: Path, raw, cpp_nodes=None, schema_nodes=None, binding_edges=None):
    cfg = make_config(tmp_path)
    return build_ekf_slice(
        cfg,
        raw,
        cpp_nodes if cpp_nodes is not None else _cpp_nodes(),
        schema_nodes or [],
        {"fn-a": "function-a", "fn-b": "function-b", "fn-pub": "function-pub"},
        binding_edges or [],
        [],
        commit="testcommit",
        variant="test_variant",
    )


def test_data_and_control_dependencies_are_separate(tmp_path: Path):
    raw = [
        _member(
            "estimator::parameters::ekf2_gps_ctrl",
            "ekf2_gps_ctrl",
            "estimator::parameters",
            "_params",
            "fn-a",
            "Ekf::update",
            10,
            access="read",
        ),
        _member(
            "estimator::filter_control_status_u::(anonymous struct)::gps_hgt",
            "gps_hgt",
            "estimator::filter_control_status_u::(anonymous struct)",
            "_control_status.flags",
            "fn-a",
            "Ekf::update",
            12,
            rhs_members=[_param_ref()],
        ),
        _member(
            "estimator::filter_control_status_u::(anonymous struct)::baro_hgt",
            "baro_hgt",
            "estimator::filter_control_status_u::(anonymous struct)",
            "_control_status.flags",
            "fn-a",
            "Ekf::update",
            15,
            conditions=[_condition("_params.ekf2_gps_ctrl & 4", [_param_ref()], 14)],
        ),
    ]
    _, edges, paths, metrics, _ = _build(tmp_path, raw)
    relations = {e["relation"] for e in edges}
    assert relations == {"data_depends_on", "control_depends_on"}
    assert metrics["data_edges"] == 1
    assert metrics["control_edges"] == 1
    assert metrics["sinks_control_flag"] == 2
    assert metrics["sink_writes_control_flag"] == 2

    control_path = next(p for p in paths if p["category"] == "control")
    assert control_path["steps"][0]["condition"] == "_params.ekf2_gps_ctrl & 4"
    assert control_path["steps"][0]["mask_symbols"] == ["_params.ekf2_gps_ctrl"]


def test_local_definition_propagates_to_sink(tmp_path: Path):
    raw = [
        {
            "record": "local_write",
            "variable": "gps_ctrl",
            "type": "int32_t",
            "is_parameter": False,
            "write_kind": "decl",
            "rhs_text": "_params.ekf2_gps_ctrl",
            "rhs_members": [_param_ref()],
            "rhs_locals": [],
            "control_conditions": [],
            "caller_usr": "fn-a",
            "caller": "Ekf::update",
            "file": "src/modules/ekf2/EKF/update.cpp",
            "line": 9,
            "column": 2,
            "tu": "t",
        },
        _member(
            "estimator::filter_control_status_u::(anonymous struct)::gps_hgt",
            "gps_hgt",
            "estimator::filter_control_status_u::(anonymous struct)",
            "_control_status.flags",
            "fn-a",
            "Ekf::update",
            12,
            rhs_locals=["gps_ctrl"],
        ),
    ]
    _, edges, paths, _, _ = _build(tmp_path, raw)
    assert [e["relation"] for e in edges] == ["data_depends_on"]
    assert paths[0]["steps"][0]["kind"] == "rhs_local"
    assert paths[0]["steps"][1]["kind"] == "local_definition"


def test_cross_function_memory_predecessor_is_visible(tmp_path: Path):
    raw = [
        _member(
            "estimator::StateSample::vel",
            "vel",
            "estimator::StateSample",
            "_state",
            "fn-a",
            "Ekf::predict",
            20,
            rhs_members=[_param_ref()],
        ),
        _member(
            "estimator::StateSample::vel",
            "vel",
            "estimator::StateSample",
            "_state",
            "fn-b",
            "Ekf::check",
            40,
            access="read",
        ),
        _member(
            "estimator::filter_control_status_u::(anonymous struct)::gps_hgt",
            "gps_hgt",
            "estimator::filter_control_status_u::(anonymous struct)",
            "_control_status.flags",
            "fn-b",
            "Ekf::check",
            42,
            rhs_members=[
                _ref(
                    "estimator::StateSample::vel",
                    "vel",
                    "estimator::StateSample",
                    "_state",
                )
            ],
        ),
    ]
    _, _, paths, metrics, _ = _build(tmp_path, raw)
    assert metrics["cross_function_paths"] == 1
    assert paths[0]["cross_function"] is True
    assert any(step["kind"] == "memory_write" for step in paths[0]["steps"])


def test_call_argument_carries_parameter_across_functions(tmp_path: Path):
    raw = [
        _member(
            "estimator::filter_control_status_u::(anonymous struct)::gps_hgt",
            "gps_hgt",
            "estimator::filter_control_status_u::(anonymous struct)",
            "_control_status.flags",
            "fn-b",
            "Ekf::apply",
            30,
            rhs_locals=["value"],
        ),
        {
            "record": "call_arg",
            "callee_usr": "fn-b",
            "callee": "Ekf::apply",
            "param_name": "value",
            "param_type": "int",
            "arg_index": 0,
            "arg_text": "_params.ekf2_gps_ctrl",
            "members": [_param_ref()],
            "locals": [],
            "control_conditions": [],
            "caller_usr": "fn-a",
            "caller": "Ekf::update",
            "file": "src/modules/ekf2/EKF/update.cpp",
            "line": 12,
            "column": 3,
            "tu": "t",
        },
    ]
    _, _, paths, metrics, _ = _build(tmp_path, raw)
    assert metrics["cross_function_paths"] == 1
    assert any(step["kind"] == "call_argument" for step in paths[0]["steps"])


def test_published_message_field_uses_topic_field_sink(tmp_path: Path):
    schema = [
        {
            "id": "tf-q",
            "kind": "topic_field",
            "qualified_name": "vehicle_attitude_s::q",
            "name": "q",
            "message_type": "vehicle_attitude_s",
        }
    ]
    raw = [
        _member(
            "vehicle_attitude_s::q",
            "q",
            "vehicle_attitude_s",
            "attitude",
            "fn-pub",
            "EKF2::PublishAttitude",
            50,
            rhs_members=[_param_ref()],
        ),
        {
            "record": "uorb_call",
            "operation": "publish",
            "caller_usr": "fn-pub",
            "caller": "EKF2::PublishAttitude",
            "receiver": "_attitude_pub",
            "file": "src/modules/ekf2/EKF2.cpp",
            "line": 60,
            "column": 2,
            "tu": "t",
        },
    ]
    nodes, edges, _, metrics, _ = _build(tmp_path, raw, schema_nodes=schema)
    assert metrics["sinks_message_field"] == 1
    assert nodes == []  # existing topic_field is reused, not duplicated
    assert edges[0]["src"] == "tf-q"


def test_unbound_parameter_is_unresolved_not_guessed(tmp_path: Path):
    raw = [
        _member(
            "estimator::parameters::ekf2_unknown",
            "ekf2_unknown",
            "estimator::parameters",
            "_params",
            "fn-a",
            "Ekf::update",
            10,
            access="read",
        ),
        _member(
            "estimator::filter_control_status_u::(anonymous struct)::gps_hgt",
            "gps_hgt",
            "estimator::filter_control_status_u::(anonymous struct)",
            "_control_status.flags",
            "fn-a",
            "Ekf::update",
            12,
            rhs_members=[
                _ref(
                    "estimator::parameters::ekf2_unknown",
                    "ekf2_unknown",
                    "estimator::parameters",
                    "_params",
                )
            ],
        ),
    ]
    _, edges, _, metrics, unresolved = _build(tmp_path, raw)
    assert edges == []
    assert metrics["parameter_fields_unbound"] == 1
    assert metrics["parameter_binding_complete"] == 0
    assert any(u["kind"] == "slice_parameter_field_without_field_node" for u in unresolved)


def test_ids_are_stable_across_runs(tmp_path: Path):
    raw = [
        _member(
            "estimator::filter_control_status_u::(anonymous struct)::gps_hgt",
            "gps_hgt",
            "estimator::filter_control_status_u::(anonymous struct)",
            "_control_status.flags",
            "fn-a",
            "Ekf::update",
            12,
            rhs_members=[_param_ref()],
        )
    ]
    first = _build(tmp_path, raw)
    second = _build(tmp_path, raw)
    assert {e["id"] for e in first[1]} == {e["id"] for e in second[1]}
    assert {p["id"] for p in first[2]} == {p["id"] for p in second[2]}


def test_csv_has_one_row_per_edge(tmp_path: Path):
    raw = [
        _member(
            "estimator::filter_control_status_u::(anonymous struct)::gps_hgt",
            "gps_hgt",
            "estimator::filter_control_status_u::(anonymous struct)",
            "_control_status.flags",
            "fn-a",
            "Ekf::update",
            12,
            rhs_members=[_param_ref()],
        )
    ]
    _, edges, paths, _, _ = _build(tmp_path, raw)
    out = tmp_path / "ekf_slice.csv"
    write_ekf_slice_csv(out, edges, paths)
    rows = out.read_text(encoding="utf-8").splitlines()
    assert len(rows) == len(edges) + 1
    assert "sink,sink_kind,source,parameter,category" in rows[0]
