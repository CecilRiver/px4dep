"""Unit tests for the M2 health -> protection structural connector."""

from __future__ import annotations

from pathlib import Path

from px4dep.health_chain import build_health_chain, write_health_chain_csv
from px4dep_testkit import make_config


def _source(record: str, line: int, column: int = 1) -> dict[str, object]:
    return {
        "file": f"src/modules/commander/{record}.cpp",
        "line": line,
        "column": column,
        "tu": f"src/modules/commander/{record}.cpp",
    }


def _schema_nodes() -> list[dict[str, object]]:
    return [
        {
            "id": "msg-failsafe",
            "kind": "message_type",
            "qualified_name": "failsafe_flags_s",
            "name": "FailsafeFlags",
            "build_variant": "test",
            "source": {"file": "msg/FailsafeFlags.msg", "line": 1, "column": 1},
        },
        {
            "id": "topic-failsafe",
            "kind": "topic",
            "qualified_name": "failsafe_flags",
            "name": "failsafe_flags",
            "message_type": "failsafe_flags_s",
            "build_variant": "test",
            "source": {"file": "msg/FailsafeFlags.msg", "line": 15, "column": 1},
        },
        {
            "id": "field-flight-time",
            "kind": "topic_field",
            "qualified_name": "failsafe_flags_s::flight_time_limit_exceeded",
            "name": "flight_time_limit_exceeded",
            "message_type": "failsafe_flags_s",
            "build_variant": "test",
            "source": {"file": "msg/FailsafeFlags.msg", "line": 75, "column": 2},
        },
        {
            "id": "field-position-accuracy",
            "kind": "topic_field",
            "qualified_name": "failsafe_flags_s::position_accuracy_low",
            "name": "position_accuracy_low",
            "message_type": "failsafe_flags_s",
            "build_variant": "test",
            "source": {"file": "msg/FailsafeFlags.msg", "line": 20, "column": 2},
        },
        {
            "id": "field-battery-low",
            "kind": "topic_field",
            "qualified_name": "failsafe_flags_s::battery_low",
            "name": "battery_low",
            "message_type": "failsafe_flags_s",
            "build_variant": "test",
            "source": {"file": "msg/FailsafeFlags.msg", "line": 24, "column": 2},
        },
    ]


def _records() -> list[dict[str, object]]:
    health = {
        "record": "health_call",
        "method": "healthFailure",
        "receiver_type": "Report",
        "receiver_expr": "reporter",
        "caller_usr": "usr-health",
        "caller": "EstimatorChecks::lowPositionAccuracy",
        "args": [
            {"index": 0, "text": "NavModes::All"},
            {"index": 1, "text": "health_component_t::estimator"},
            {"index": 2, "text": "events::Log::Warning"},
            {"index": 3, "text": "events::ID(\"position accuracy low\")"},
        ],
        "mode_groups": ["All"],
        "nav_states": [],
        "mode_flags": [],
        "mode_dynamic": False,
        **_source("estimatorCheck", 716, 27),
    }
    write = {
        "record": "flag_access",
        "field": "position_accuracy_low",
        "qualified_name": "failsafe_flags_s::position_accuracy_low",
        "access": "write",
        "write_op": "=",
        "receiver_expr": "reporter.failsafeFlags()",
        "caller_usr": "usr-health",
        "caller": "EstimatorChecks::lowPositionAccuracy",
        **_source("estimatorCheck", 703, 30),
    }
    clear_run = {
        "record": "health_call",
        "method": "clearCanRunBits",
        "receiver_type": "Report",
        "receiver_expr": "reporter",
        "caller_usr": "usr-run",
        "caller": "ModeChecks::run",
        "args": [{"index": 0, "text": "NavModes::All"}],
        "mode_groups": ["All"],
        "nav_states": [],
        "mode_flags": [],
        "mode_dynamic": False,
        **_source("modeCheck", 90, 3),
    }
    failsafe = {
        "record": "failsafe_check",
        "macro": "CHECK_FAILSAFE",
        "flag": "flight_time_limit_exceeded",
        "condition_flags": ["flight_time_limit_exceeded"],
        "actions": ["RTL"],
        "action_dynamic": False,
        "options": "Action::RTL",
        "last_condition": "",
        "current_condition": "",
        "receiver_type": "FailsafeBase",
        "caller_usr": "usr-failsafe",
        "caller": "Failsafe::check",
        **_source("failsafe", 120, 9),
    }
    failsafe_dynamic = {
        "record": "failsafe_check",
        "macro": "CHECK_FAILSAFE",
        "flag": "battery_low",
        "condition_flags": ["battery_low"],
        "actions": [],
        "action_dynamic": True,
        "options": "fromBatteryParam()",
        "receiver_type": "FailsafeBase",
        "caller_usr": "usr-dynamic",
        "caller": "Failsafe::checkBattery",
        **_source("failsafe", 150, 9),
    }
    mode_check = {
        "record": "mode_check",
        "method": "modeCanRun",
        "mode_expr": "user_intended_mode",
        "mode_groups": [],
        "nav_states": [],
        "mode_flags": [],
        "mode_dynamic": True,
        "negated": True,
        "receiver_type": "FailsafeBase",
        "caller_usr": "usr-mode",
        "caller": "Failsafe::checkModeFallback",
        **_source("failsafe", 200, 15),
    }
    return [
        health,
        write,
        clear_run,
        failsafe,
        failsafe_dynamic,
        mode_check,
        {
            "record": "enum_value",
            "enum_name": "FailsafeBase::Action",
            "value_name": "Warn",
            "qualified_name": "FailsafeBase::Action::Warn",
            "value": 1,
            **_source("failsafe", 40, 3),
        },
        {
            "record": "enum_value",
            "enum_name": "FailsafeBase::Action",
            "value_name": "RTL",
            "qualified_name": "FailsafeBase::Action::RTL",
            "value": 6,
            **_source("failsafe", 45, 3),
        },
        {
            "record": "action_ref",
            "action": "RTL",
            "qualified_name": "FailsafeBase::Action::RTL",
            "context": "return",
            "in_return": True,
            "caller_usr": "usr-failsafe",
            "caller": "Failsafe::check",
            **_source("failsafe", 130, 16),
        },
    ]


def _build(tmp_path: Path):
    cfg = make_config(tmp_path, sink_catalog=str(tmp_path / "sink_catalog.yaml"))
    (tmp_path / "sink_catalog.yaml").write_text(
        "version: 1\nsinks:\n"
        "  - matcher: 'macro:CHECK_FAILSAFE'\n"
        "    layer: protection_condition\n"
        "    effect: protection_check\n",
        encoding="utf-8",
    )
    functions = {
        "usr-health": "fn-health",
        "usr-run": "fn-run",
        "usr-failsafe": "fn-failsafe",
        "usr-dynamic": "fn-dynamic",
        "usr-mode": "fn-mode",
    }
    return build_health_chain(
        cfg,
        _records(),  # type: ignore[arg-type]
        functions,
        _schema_nodes(),  # type: ignore[arg-type]
        commit="testcommit",
        variant="test_variant",
    )


def test_layers_and_severity_are_kept_distinct(tmp_path: Path):
    nodes, edges, metrics, unresolved = _build(tmp_path)
    by_id = {n["id"]: n for n in nodes}

    health = [
        n for n in nodes
        if n["kind"] == "predicate"
        and n["predicate_layer"] == "health_check"
        and n.get("method") == "healthFailure"
    ]
    assert len(health) == 1
    assert health[0]["severity"] == "warning"
    assert health[0]["effect"] == "health_warning"
    assert health[0]["component"] == "estimator"

    protection = [
        n for n in nodes
        if n["kind"] == "predicate" and n["predicate_layer"] == "protection_condition"
    ]
    assert len(protection) == 2
    assert metrics["failsafe_checks"] == 2
    assert metrics["failsafe_checks_dynamic_action"] == 1

    restrict_arming = [
        e for e in edges if e["relation"] == "restricts_arming"
    ]
    restrict_run = [e for e in edges if e["relation"] == "restricts_run"]
    assert restrict_arming
    assert restrict_run
    assert any(by_id[e["dst"]]["qualified_name"] == "NavModes::All" for e in restrict_arming)

    # A warning report is not the same node or edge class as a mode prohibition.
    assert {by_id[e["src"]]["predicate_layer"] for e in restrict_arming} == {"health_check"}
    assert {by_id[e["src"]]["predicate_layer"] for e in restrict_run} == {"mode_control"}


def test_flags_are_written_read_and_mapped(tmp_path: Path):
    nodes, edges, metrics, _ = _build(tmp_path)
    by_id = {n["id"]: n for n in nodes}

    writes = [e for e in edges if e["relation"] == "writes_flag"]
    reads = [e for e in edges if e["relation"] == "reads_flag"]
    maps = [e for e in edges if e["relation"] == "maps_to_field"]
    assert writes
    assert reads
    assert maps
    assert by_id[writes[0]["dst"]]["name"] == "position_accuracy_low"
    schema_ids = {n["id"] for n in _schema_nodes()}
    assert maps[0]["dst"] in schema_ids
    assert metrics["health_flags"] >= 3
    assert metrics["health_flags_mapped"] == metrics["health_flags"]
    assert metrics["health_flags_unmapped"] == 0

    reports_flag = [e for e in edges if e["relation"] == "reports_flag"]
    assert reports_flag
    assert all(e["confidence"] == "low" for e in reports_flag)
    assert all(e["evidence"]["rule"] == "same_function_scope" for e in reports_flag)


def test_action_decision_is_not_a_warning(tmp_path: Path):
    nodes, edges, _, _ = _build(tmp_path)
    by_id = {n["id"]: n for n in nodes}
    decisions = {n["name"]: n for n in nodes if n["kind"] == "protection_decision"}

    assert "RTL" in decisions
    assert "Warn" in decisions
    assert "Count" not in decisions
    assert decisions["RTL"]["action_class"] == "recovery"
    assert decisions["Warn"]["action_class"] == "warning"

    selects = [e for e in edges if e["relation"] == "selects_action"]
    returns = [e for e in edges if e["relation"] == "returns_action"]
    assert any(by_id[e["dst"]]["name"] == "RTL" for e in selects)
    assert any(by_id[e["dst"]]["name"] == "RTL" for e in returns)


def test_dynamic_inputs_are_unresolved_not_guessed(tmp_path: Path):
    _, _, _, unresolved = _build(tmp_path)
    kinds = {u["kind"] for u in unresolved}
    assert "protection_action_dynamic" in kinds
    assert "mode_set_dynamic" in kinds


def test_health_chain_ids_are_stable_across_runs(tmp_path: Path):
    first_nodes, first_edges, first_metrics, _ = _build(tmp_path)
    second_nodes, second_edges, second_metrics, _ = _build(tmp_path)
    assert {n["id"] for n in first_nodes} == {n["id"] for n in second_nodes}
    assert {e["id"] for e in first_edges} == {e["id"] for e in second_edges}
    assert first_metrics == second_metrics


def test_health_chain_csv_has_one_row_per_predicate(tmp_path: Path):
    nodes, _, _, _ = _build(tmp_path)
    path = tmp_path / "health_chain.csv"
    predicates = [n for n in nodes if n["kind"] == "predicate"]
    write_health_chain_csv(path, predicates)
    rows = path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == len(predicates) + 1
    assert "predicate,layer,effect" in rows[0]
