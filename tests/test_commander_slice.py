"""Unit tests for the Commander/Failsafe parameter slice extension."""

from __future__ import annotations

from px4dep.commander_slice import (
    MAX_CONTEXT_PATHS_PER_PARAM,
    build_commander_slice,
)

CHECK_FILE = "src/modules/commander/HealthAndArmingChecks/checks/check.cpp"


def _parameter(name: str, in_build: bool = True) -> dict:
    return {
        "id": f"p-{name}",
        "kind": "parameter",
        "name": name,
        "qualified_name": name,
        "in_build": in_build,
    }


def _wrapper(
    field_name: str,
    parameter_name: str,
    parent_class: str,
    *,
    file: str = CHECK_FILE,
    line: int = 1,
) -> dict:
    return {
        "id": f"w-{parent_class}-{field_name}",
        "kind": "param_wrapper",
        "name": field_name,
        "field_name": field_name,
        "parameter_name": parameter_name,
        "parent_class": parent_class,
        "qualified_name": f"{parent_class}::{field_name}",
        "source": {"file": file, "line": line, "column": 1},
        "build_guards": [],
        "build_guard_status": "none",
    }


def _sink_node(qname: str, kind: str = "predicate") -> dict:
    return {
        "id": f"n-{qname}",
        "kind": kind,
        "name": qname.rsplit("::", 1)[-1].split("@", 1)[0],
        "qualified_name": qname,
    }


def _health_call(
    caller: str,
    caller_usr: str,
    method: str,
    *,
    args: list[str] | None = None,
    conditions: list[dict] | None = None,
    file: str = CHECK_FILE,
    line: int = 10,
    column: int = 2,
) -> tuple[dict, dict]:
    qname = f"health:{caller}::{method}@{file}:{line}:{column}"
    record = {
        "record": "health_call",
        "caller": caller,
        "caller_usr": caller_usr,
        "method": method,
        "args": [{"text": text} for text in (args or [])],
        "control_conditions": conditions or [],
        "file": file,
        "line": line,
        "column": column,
    }
    return record, _sink_node(qname)


def _condition(text: str, *, line: int = 9) -> dict:
    return {
        "kind": "if",
        "text": text,
        "file": CHECK_FILE,
        "line": line,
        "column": 1,
    }


def _read_use(
    parameter_name: str,
    wrapper_qname: str,
    caller: str,
    caller_usr: str,
    *,
    file: str = CHECK_FILE,
    line: int = 5,
) -> dict:
    return {
        "record": "parameter_use",
        "use_kind": "get",
        "parameter_name": parameter_name,
        "qualified_name": wrapper_qname,
        "caller": caller,
        "caller_usr": caller_usr,
        "file": file,
        "line": line,
        "column": 1,
    }


def _call(caller: str, caller_usr: str, callee: str, callee_usr: str) -> dict:
    return {
        "record": "call",
        "caller": caller,
        "caller_usr": caller_usr,
        "callee": callee,
        "callee_usr": callee_usr,
    }


def _build(raw, health_nodes, wrappers, parameters):
    return build_commander_slice(
        None,
        raw,
        health_nodes,
        wrappers,
        parameters,
        {},
        commit="testcommit",
        variant="test_variant",
    )


def test_direct_wrapper_argument_is_a_data_path():
    caller = "ImuConsistencyChecks::checkAndReport"
    record, node = _health_call(
        caller, "fn-chk", "armingCheckFailure", args=["_param_com_arm_imu_acc.get()"]
    )
    wrapper = _wrapper("_param_com_arm_imu_acc", "COM_ARM_IMU_ACC", "ImuConsistencyChecks")

    edges, paths, metrics, unresolved = _build(
        [record], [node], [wrapper], [_parameter("COM_ARM_IMU_ACC")]
    )

    assert unresolved == []
    assert len(paths) == 1
    path = paths[0]
    assert path["category"] == "data"
    assert path["sink_kind"] == "health_predicate"
    assert path["confidence"] == "medium"
    assert path["context_only"] is False
    assert path["steps"][0]["expression"] == "_param_com_arm_imu_acc.get()"
    assert metrics["data_paths"] == 1
    assert metrics["direct_parameters"] == 1
    assert edges[0]["relation"] == "data_depends_on"


def test_enclosing_condition_is_a_control_path():
    caller = "PowerChecks::checkAndReport"
    record, node = _health_call(
        caller,
        "fn-chk",
        "armingCheckFailure",
        conditions=[_condition("_param_com_power_count.get() > 0")],
    )
    wrapper = _wrapper("_param_com_power_count", "COM_POWER_COUNT", "PowerChecks")

    edges, paths, metrics, _ = _build(
        [record], [node], [wrapper], [_parameter("COM_POWER_COUNT")]
    )

    assert len(paths) == 1
    path = paths[0]
    assert path["category"] == "control"
    assert path["confidence"] == "low"
    assert path["context_only"] is False
    assert path["steps"][0]["condition"] == "_param_com_power_count.get() > 0"
    assert edges[0]["relation"] == "control_depends_on"
    assert metrics["control_parameters"] == 1


def test_cross_function_context_is_labelled_and_has_no_edge():
    sink_caller = "EstimatorChecks::checkAndReport"
    helper = "EstimatorChecks::checkPosVelValidity"
    record, node = _health_call(sink_caller, "fn-sink", "armingCheckFailure")
    wrapper = _wrapper("_param_com_vel_fs_evh", "COM_VEL_FS_EVH", "EstimatorChecks")
    raw = [
        record,
        _read_use("COM_VEL_FS_EVH", wrapper["qualified_name"], helper, "fn-helper"),
        _call(sink_caller, "fn-sink", helper, "fn-helper"),
    ]

    edges, paths, metrics, _ = _build(
        raw, [node], [wrapper], [_parameter("COM_VEL_FS_EVH")]
    )

    assert edges == []
    assert len(paths) == 1
    path = paths[0]
    assert path["category"] == "context"
    assert path["context_only"] is True
    assert path["confidence"] == "low"
    assert path["depth"] == 1
    assert path["cross_function"] is True
    assert path["read_sites"]
    assert metrics["context_only_paths"] == 1
    assert metrics["context_paths_without_read_site"] == 0


def test_exact_token_does_not_match_a_longer_field_name():
    caller = "WindChecks::checkAndReport"
    record, node = _health_call(
        caller, "fn-chk", "armingCheckFailure", args=["_param_com_wind_max_act.get()"]
    )
    wrapper = _wrapper("_param_com_wind_max", "COM_WIND_MAX", "WindChecks")

    _, paths, metrics, unresolved = _build(
        [record], [node], [wrapper], [_parameter("COM_WIND_MAX")]
    )

    assert paths == []
    assert metrics["parameters_with_wrapper"] == 1
    assert metrics["parameters_without_path_count"] == 1
    assert [item["kind"] for item in unresolved] == ["commander_parameter_without_path"]


def test_duplicate_field_name_disambiguates_by_caller_class():
    caller = "AlphaChecks::checkAndReport"
    record, node = _health_call(
        caller, "fn-chk", "armingCheckFailure", args=["_param_shared.get()"]
    )
    alpha = _wrapper("_param_shared", "PARAM_ALPHA", "AlphaChecks")
    beta = _wrapper("_param_shared", "PARAM_BETA", "BetaChecks")

    _, paths, _, unresolved = _build(
        [record],
        [node],
        [alpha, beta],
        [_parameter("PARAM_ALPHA"), _parameter("PARAM_BETA")],
    )

    assert [path["parameter"] for path in paths] == ["PARAM_ALPHA"]
    assert paths[0]["source"] == "AlphaChecks::_param_shared"
    assert not [item for item in unresolved if item["kind"] == "commander_wrapper_field_ambiguous"]


def test_parameter_absent_from_build_is_unresolved():
    caller = "SystemChecks::checkAndReport"
    record, node = _health_call(
        caller, "fn-chk", "armingCheckFailure", args=["_param_sys_has_gps.get()"]
    )
    wrapper = _wrapper("_param_sys_has_gps", "SYS_HAS_GPS", "SystemChecks")

    _, paths, _, unresolved = _build(
        [record], [node], [wrapper], [_parameter("SYS_HAS_GPS", in_build=False)]
    )

    assert paths == []
    kinds = [item["kind"] for item in unresolved]
    assert "commander_parameter_not_in_build" in kinds


def test_context_paths_are_capped_per_parameter():
    caller = "ManyChecks::checkAndReport"
    records = []
    nodes = []
    for index in range(MAX_CONTEXT_PATHS_PER_PARAM + 3):
        record, node = _health_call(
            caller,
            "fn-chk",
            f"armingCheckFailure{index}",
            line=10 + index,
        )
        records.append(record)
        nodes.append(node)
    wrapper = _wrapper("_param_many", "PARAM_MANY", "ManyChecks")
    records.append(
        _read_use("PARAM_MANY", wrapper["qualified_name"], caller, "fn-chk")
    )

    _, paths, metrics, _ = _build(
        records, nodes, [wrapper], [_parameter("PARAM_MANY")]
    )

    assert len(paths) == MAX_CONTEXT_PATHS_PER_PARAM
    assert all(path["context_only"] for path in paths)
    assert metrics["context_candidate_paths"] == MAX_CONTEXT_PATHS_PER_PARAM + 3
    assert metrics["context_paths_suppressed"] == 3


def test_later_flag_write_site_with_wrapper_still_matches():
    """A shared FailsafeFlags node must not hide reads from later write sites."""
    field = "local_velocity_invalid"
    qname = f"failsafe_flags_s::{field}"
    node = _sink_node(qname, kind="health_flag")
    init = {
        "record": "flag_access",
        "access": "write",
        "caller": "HealthAndArmingChecks::HealthAndArmingChecks",
        "caller_usr": "fn-init",
        "field": field,
        "receiver_expr": "_failsafe_flags",
        "rhs_text": "true",
        "file": "src/modules/commander/HealthAndArmingChecks/HealthAndArmingChecks.cpp",
        "line": 46,
        "column": 18,
    }
    estimator = {
        "record": "flag_access",
        "access": "write",
        "caller": "EstimatorChecks::setModeRequirementFlags",
        "caller_usr": "fn-est",
        "field": field,
        "receiver_expr": "failsafe_flags",
        "rhs_text": "!checkPosVelValidity(_param_com_vel_fs_evh.get())",
        "file": "src/modules/commander/HealthAndArmingChecks/checks/estimatorCheck.cpp",
        "line": 816,
        "column": 17,
    }
    wrapper = _wrapper(
        "_param_com_vel_fs_evh",
        "COM_VEL_FS_EVH",
        "EstimatorChecks",
        file=estimator["file"],
    )

    edges, paths, _, _ = _build(
        [init, estimator], [node], [wrapper], [_parameter("COM_VEL_FS_EVH")]
    )

    assert len(paths) == 1
    path = paths[0]
    assert path["category"] == "data"
    assert path["sink_kind"] == "failsafe_flag_write"
    assert path["steps"][0]["line"] == 816
    assert edges[0]["relation"] == "data_depends_on"
