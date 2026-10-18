"""Unit tests for graph entity construction and merge validation."""

from __future__ import annotations

from px4dep.graph_builder import build_cpp_entities, merge_and_validate
from px4dep_testkit import make_config


def _node(node_id: str, kind: str = "function", file: str = "src/a.cpp"):
    return {
        "id": node_id,
        "kind": kind,
        "qualified_name": node_id,
        "source": {"file": file, "line": 1},
    }


def _edge(edge_id: str, src: str, dst: str):
    return {
        "id": edge_id,
        "kind": "edge",
        "relation": "calls",
        "src": src,
        "dst": dst,
    }


def test_merge_and_validate_deduplicates_and_drops_dangling_edges(tmp_path):
    cfg = make_config(tmp_path, exclude_dirs=["src/excluded"])
    nodes = [
        _node("n1"),
        _node("n1"),  # duplicate id
        _node("excluded", file="src/excluded/x.cpp"),
        _node("n2"),
    ]
    edges = [
        _edge("e1", "n1", "n2"),
        _edge("e1", "n1", "n2"),  # duplicate edge id
        _edge("dangling", "n1", "missing"),
    ]

    final_nodes, final_edges, metrics, unresolved = merge_and_validate(
        cfg, nodes, edges
    )

    assert {n["id"] for n in final_nodes} == {"n1", "n2"}
    assert [e["id"] for e in final_edges] == ["e1"]
    assert metrics["duplicate_node_ids"] == 1
    assert metrics["duplicate_edge_ids"] == 1
    assert metrics["edges_dropped_missing_endpoint"] == 1
    kinds = [u["kind"] for u in unresolved]
    assert kinds == ["edge_endpoint_missing"]


def test_merge_and_validate_reports_missing_ids(tmp_path):
    cfg = make_config(tmp_path)
    nodes = [{"kind": "function", "qualified_name": "x"}]
    edges = [{"kind": "edge", "relation": "calls", "src": "a", "dst": "b"}]
    final_nodes, final_edges, _metrics, unresolved = merge_and_validate(
        cfg, nodes, edges
    )
    assert final_nodes == []
    assert final_edges == []
    assert sorted(u["kind"] for u in unresolved) == [
        "edge_without_id",
        "node_without_id",
    ]


def test_build_cpp_entities_distinguishes_config_fields(tmp_path):
    cfg = make_config(tmp_path)
    records = [
        {
            "record": "entity",
            "kind": "field",
            "usr": "c:@N@estimator@S@parameters@FI@ekf2_predict_us",
            "qualified_name": "estimator::parameters::ekf2_predict_us",
            "parent_class": "estimator::parameters",
            "file": "src/EKF/common.h",
            "line": 10,
            "column": 2,
            "type": "float",
            "is_mutable": True,
            "tu": "src/modules/ekf2/EKF2.cpp",
        },
        {
            "record": "entity",
            "kind": "field",
            "usr": "c:@S@Filter@FI@_dt",
            "qualified_name": "Filter::_dt",
            "parent_class": "Filter",
            "file": "src/EKF/filter.cpp",
            "line": 20,
            "column": 2,
            "type": "float",
            "is_mutable": True,
            "tu": "src/modules/ekf2/EKF2.cpp",
        },
        {
            "record": "entity",
            "kind": "function",
            "usr": "c:@F@EKF2::update",
            "qualified_name": "EKF2::update",
            "is_method": True,
            "is_static": False,
            "file": "src/EKF2.cpp",
            "line": 30,
            "column": 1,
            "type": "void (void)",
            "tu": "src/modules/ekf2/EKF2.cpp",
        },
        {"record": "uorb_init", "usn": "ignored"},
    ]

    nodes, metrics, unresolved, function_ids, config_field_ids = build_cpp_entities(
        cfg, records, commit="commit", variant="test_variant"
    )
    by_name = {n["qualified_name"]: n for n in nodes}

    assert metrics["cpp_entities"] == 3
    assert metrics["functions"] == 1
    assert metrics["fields"] == 1
    assert metrics["config_fields"] == 1
    assert unresolved == []
    assert by_name["estimator::parameters::ekf2_predict_us"]["kind"] == "config_field"
    assert by_name["Filter::_dt"]["kind"] == "field"
    assert by_name["EKF2::update"]["kind"] == "function"
    assert function_ids["c:@F@EKF2::update"] == by_name["EKF2::update"]["id"]
    assert (
        config_field_ids["c:@N@estimator@S@parameters@FI@ekf2_predict_us"]
        == by_name["estimator::parameters::ekf2_predict_us"]["id"]
    )


def test_cpp_entity_ids_are_stable(tmp_path):
    cfg = make_config(tmp_path)
    record = {
        "record": "entity",
        "kind": "function",
        "usr": "c:@F@f",
        "qualified_name": "f",
        "file": "src/a.cpp",
        "line": 1,
        "column": 1,
        "type": "void (void)",
    }
    first, _, _, _, _ = build_cpp_entities(cfg, [record], "commit", "variant")
    second, _, _, _, _ = build_cpp_entities(cfg, [record], "commit", "variant")
    assert first[0]["id"] == second[0]["id"]
