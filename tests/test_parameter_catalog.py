"""Unit tests for the parameter catalog and YAML/template handling."""

from __future__ import annotations

import json

from px4dep_testkit import FIXTURES
from px4dep.parameter_catalog import (
    _expand_template_sources,
    _template_pattern,
    build_catalog,
    index_yaml_sources,
    normalize_build_parameter,
)


def _raw_records():
    return [
        {
            "record": "param_wrapper",
            "parameter_name": "EKF2_GPS_CTRL",
            "qualified_name": "Module::_gps_ctrl",
            "field_name": "_gps_ctrl",
            "file": "src/module.cpp",
            "line": 10,
            "column": 4,
            "usr": "c:@S@Module@FI@_gps_ctrl",
            "wrapper_kind": "ParamInt",
        },
        {
            "record": "call",
            "api": "parameter",
            "method": "param_find",
            "args": [{"text": '"EKF2_BARO_CTRL"'}],
            "file": "src/module.cpp",
            "line": 20,
            "column": 2,
            "caller": "Module::init",
        },
        {
            "record": "call",
            "api": "parameter",
            "method": "param_find_no_notification",
            "args": [{"text": '"EKF2_GPS_CTRL"'}],
            "file": "src/module.cpp",
            "line": 21,
            "column": 2,
            "caller": "Module::init",
        },
        # param_get/param_set take an opaque handle and must not be treated as
        # name-based registrations.
        {
            "record": "call",
            "api": "parameter",
            "method": "param_get",
            "args": [{"text": "handle"}],
            "file": "src/module.cpp",
            "line": 22,
            "column": 2,
            "caller": "Module::init",
        },
    ]


def test_normalize_build_parameter_values_and_missing_fields():
    raw = json.loads((FIXTURES / "parameters.json").read_text(encoding="utf-8"))
    by_name = {p["name"]: p for p in raw["parameters"]}

    gps = normalize_build_parameter(by_name["EKF2_GPS_CTRL"])
    assert gps["enum_values"] == {
        "1": "GPS position",
        "2": "GPS velocity",
    }
    assert gps["reboot_required"] is False

    battery = normalize_build_parameter(by_name["BAT1_A_PER_V"])
    assert battery["units"] == "A/V"
    assert battery["reboot_required"] is True
    assert battery["increment"] == 0.01

    empty = normalize_build_parameter({})
    assert empty["name"] == ""
    assert empty["enum_values"] == {}
    assert empty["reboot_required"] is False
    assert empty["default"] is None


def test_yaml_index_keeps_definition_and_line(parameter_config):
    index, duplicates = index_yaml_sources(parameter_config)
    assert duplicates == []
    assert set(index) == {"EKF2_BARO_CTRL", "BAT${i}_A_PER_V", "YAML_ONLY_PARAM"}
    assert index["EKF2_BARO_CTRL"]["file"] == "param_yaml/module.yaml"
    assert index["EKF2_BARO_CTRL"]["line"] > 0
    assert index["EKF2_BARO_CTRL"]["yaml"]["type"] == "boolean"
    assert index["BAT${i}_A_PER_V"]["yaml"]["unit"] == "A/V"


def test_template_pattern_only_matches_templates():
    assert _template_pattern("EKF2_BARO_CTRL") is None
    pattern = _template_pattern("BAT${i}_A_PER_V")
    assert pattern is not None
    assert pattern.fullmatch("BAT1_A_PER_V")
    assert pattern.fullmatch("BAT12_A_PER_V")
    assert not pattern.fullmatch("BAT_A_PER_V")


def test_template_expansion_and_ambiguity():
    sources = {
        "BAT${i}_A_PER_V": {"file": "a.yaml", "line": 1},
        "BAT${j}_A_PER_V": {"file": "b.yaml", "line": 2},
        "OTHER${i}_X": {"file": "c.yaml", "line": 3},
    }
    expanded, ambiguities = _expand_template_sources(
        sources, ["BAT1_A_PER_V", "OTHER2_X", "PLAIN"]
    )
    assert set(expanded) == {"BAT1_A_PER_V", "OTHER2_X"}
    assert expanded["BAT1_A_PER_V"]["template_name"] == "BAT${i}_A_PER_V"
    assert len(ambiguities) == 1
    assert ambiguities[0]["kind"] == "parameter_template_source_ambiguous"
    assert ambiguities[0]["parameter_name"] == "BAT1_A_PER_V"


def test_build_catalog_links_yaml_build_metadata_wrappers_and_registrations(
    parameter_config,
):
    records, metrics, unresolved = build_catalog(parameter_config, _raw_records())
    by_name = {r["name"]: r for r in records}

    assert metrics["build_parameter_count"] == 4
    assert metrics["source_yaml_parameter_count"] == 3
    assert metrics["template_expanded_parameters"] == 1
    assert metrics["source_only_parameter_count"] == 1
    assert metrics["wrapper_count"] == 1
    assert metrics["build_parameters_with_wrapper"] == 1
    assert metrics["parameters_with_param_find"] == 2
    assert metrics["parameter_parse_rate"] == 1.0
    assert unresolved == []

    baro = by_name["EKF2_BARO_CTRL"]
    assert baro["in_build"] is True
    assert baro["source_kind"] == "yaml"
    assert baro["status"] == "resolved"
    assert len(baro["registration_points"]) == 1
    assert baro["registration_points"][0]["method"] == "param_find"

    battery = by_name["BAT1_A_PER_V"]
    assert battery["source"]["template_name"] == "BAT${i}_A_PER_V"
    assert battery["source_kind"] == "yaml"
    assert battery["increment"] == 0.01

    gps = by_name["EKF2_GPS_CTRL"]
    assert gps["wrapper_count"] == 1
    assert gps["status"] == "bound_no_source"
    # param_get on a handle must not be recorded as a name registration.
    assert gps["registration_points"][0]["method"] == "param_find_no_notification"
    assert len(gps["registration_points"]) == 1

    build_only = by_name["BUILD_ONLY_PARAM"]
    assert build_only["source"] is None
    assert build_only["source_kind"] == "build_metadata_only"
    assert build_only["status"] == "in_build"

    yaml_only = by_name["YAML_ONLY_PARAM"]
    assert yaml_only["in_build"] is False
    assert yaml_only["status"] == "excluded_by_build"
    assert yaml_only["source"]["yaml"]["type"] == "int32"


def test_catalog_ids_are_stable(parameter_config):
    first, _, _ = build_catalog(parameter_config, _raw_records())
    second, _, _ = build_catalog(parameter_config, _raw_records())
    assert [(r["name"], r["id"]) for r in first] == [
        (r["name"], r["id"]) for r in second
    ]
