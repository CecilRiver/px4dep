"""Unit and golden tests for .msg parsing and schema construction."""

from __future__ import annotations

import json

import pytest

from px4dep_testkit import FIXTURES, GOLDEN
from px4dep.uorb_schema import build_schema, camel_to_snake, parse_msg_file


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("VehicleAttitude", "vehicle_attitude"),
        ("SensorAccel", "sensor_accel"),
        ("EstimatorAidSource1d", "estimator_aid_source1d"),
        ("EstimatorStatusFlags", "estimator_status_flags"),
        ("Quatf", "quatf"),
        ("VehicleCommandAck", "vehicle_command_ack"),
        ("EstimatorAidSource3d", "estimator_aid_source3d"),
    ],
)
def test_camel_to_snake(name: str, expected: str):
    assert camel_to_snake(name) == expected


def test_parse_msg_fields_constants_arrays_and_aliases():
    path = FIXTURES / "uorb" / "msg" / "EstimatorAidSource1d.msg"
    parsed = parse_msg_file(path, FIXTURES / "uorb")

    assert parsed["file"] == "msg/EstimatorAidSource1d.msg"
    assert parsed["cpp_type"] == "estimator_aid_source1d_s"
    assert parsed["topics"] == [
        "estimator_aid_src_baro_hgt",
        "estimator_aid_src_gnss_pos",
    ]
    assert parsed["topic_lines"] == [7, 7]
    assert parsed["parse_errors"] == []

    fields = {f["name"]: f for f in parsed["fields"]}
    assert set(fields) == {
        "timestamp",
        "timestamp_sample",
        "estimator_instance",
        "innovation_rejected",
        "test_ratio",
        "test_ratio_filtered",
    }
    assert fields["test_ratio_filtered"]["is_array"] is True
    assert fields["test_ratio_filtered"]["array_size"] == 2
    assert fields["test_ratio"]["is_array"] is False
    assert fields["innovation_rejected"]["type"] == "bool"
    # Constants are not fields.
    assert "MAX_INSTANCES" not in fields
    assert parsed["constants"] == [
        {"name": "MAX_INSTANCES", "type": "uint8", "value": "3", "line": 8, "comment": ""}
    ]


def test_parse_msg_without_topics_derives_topic_from_stem():
    parsed = parse_msg_file(FIXTURES / "uorb" / "msg" / "Quatf.msg", FIXTURES / "uorb")
    assert parsed["topics"] == ["quatf"]
    assert parsed["cpp_type"] == "quatf_s"


def test_parse_versioned_message():
    path = FIXTURES / "uorb" / "msg" / "versioned" / "SensorAccel.msg"
    parsed = parse_msg_file(path, FIXTURES / "uorb")
    assert parsed["file"] == "msg/versioned/SensorAccel.msg"
    assert parsed["topics"] == ["sensor_accel"]
    assert {c["name"] for c in parsed["constants"]} == {"VERSION", "CLIP_X"}


def _summarize(nodes, edges, unresolved):
    by_id = {n["id"]: n for n in nodes}

    def qname(node_id: str) -> str:
        return by_id[node_id]["qualified_name"]

    return {
        "messages": sorted(
            n["qualified_name"] for n in nodes if n["kind"] == "message_type"
        ),
        "topics": sorted(
            [n["qualified_name"], n["message_type"]]
            for n in nodes
            if n["kind"] == "topic"
        ),
        "topic_fields": sorted(
            n["qualified_name"] for n in nodes if n["kind"] == "topic_field"
        ),
        "constants": sorted(
            n["qualified_name"]
            for n in nodes
            if n["kind"] == "topic_field" and n["is_constant"]
        ),
        "arrays": sorted(
            n["qualified_name"]
            for n in nodes
            if n["kind"] == "topic_field" and n["is_array"]
        ),
        "nested": sorted(
            n["qualified_name"]
            for n in nodes
            if n["kind"] == "topic_field" and n["is_nested"]
        ),
        "edges": sorted(
            [e["relation"], qname(e["src"]), qname(e["dst"])] for e in edges
        ),
        "unresolved": sorted(u["kind"] for u in unresolved),
    }


def test_build_schema_matches_golden(uorb_config):
    nodes, edges, metrics, unresolved = build_schema(
        uorb_config, commit="fixture-commit", variant="test_variant"
    )
    summary = _summarize(nodes, edges, unresolved)
    golden = json.loads((GOLDEN / "uorb_schema.json").read_text(encoding="utf-8"))

    assert summary == golden
    assert metrics["msg_files"] == 4
    assert metrics["topics"] == 6
    assert metrics["message_types"] == 4
    assert metrics["fields"] == 18
    assert metrics["constants"] == 4
    assert metrics["msg_parse_errors"] == 0
    assert metrics["duplicate_topic_aliases"] == 0
    assert metrics["nested_field_edges"] == 1


def test_schema_ids_are_stable_across_rebuilds(uorb_config):
    first_nodes, first_edges, _, _ = build_schema(
        uorb_config, commit="fixture-commit", variant="test_variant"
    )
    second_nodes, second_edges, _, _ = build_schema(
        uorb_config, commit="fixture-commit", variant="test_variant"
    )

    def ids(nodes, edges):
        return [n["id"] for n in nodes], [e["id"] for e in edges]

    assert ids(first_nodes, first_edges) == ids(second_nodes, second_edges)


def test_duplicate_topic_alias_is_reported(uorb_config, tmp_path):
    duplicate = tmp_path / "msg" / "VehicleAttitudeAlias.msg"
    duplicate.parent.mkdir()
    duplicate.write_text(
        "uint64 timestamp\nfloat32[4] delta_q_reset\n# TOPICS vehicle_attitude\n",
        encoding="utf-8",
    )
    # Build a config whose msg dir also contains the fixture messages.
    for source in (FIXTURES / "uorb" / "msg").glob("*.msg"):
        (tmp_path / "msg" / source.name).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
    versioned = tmp_path / "msg" / "versioned"
    versioned.mkdir()
    for source in (FIXTURES / "uorb" / "msg" / "versioned").glob("*.msg"):
        (versioned / source.name).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )

    from px4dep_testkit import make_config

    cfg = make_config(tmp_path, msg_dirs=["msg", "msg/versioned"])
    _nodes, _edges, _metrics, unresolved = build_schema(
        cfg, commit="fixture-commit", variant="test_variant"
    )
    duplicates = [u for u in unresolved if u["kind"] == "duplicate_topic_alias"]
    assert len(duplicates) == 1
    assert duplicates[0]["topic"] == "vehicle_attitude"
