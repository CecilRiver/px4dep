"""Unit tests for Param* wrapper nodes and constructor binding edges."""

from __future__ import annotations

from px4dep.param_bindings import build_wrappers, wrapper_id
from px4dep_testkit import make_config


def _parameters():
    return [
        {"id": "pid-ekf2-x", "kind": "parameter", "name": "EKF2_X"},
        {"id": "pid-ekf2-y", "kind": "parameter", "name": "EKF2_Y"},
    ]


def _wrapper(name: str = "EKF2_X", usr: str = "c:@S@M@FI@_p"):
    return {
        "record": "param_wrapper",
        "parameter_name": name,
        "qualified_name": "M::_p",
        "field_name": "_p",
        "file": "src/m.cpp",
        "line": 5,
        "column": 3,
        "usr": usr,
        "wrapper_kind": "ParamExtFloat",
        "type": "ParamExtFloat<float>",
        "parent_class": "M",
        "tu": "src/m.cpp",
    }


def _binding(name: str = "EKF2_X", resolved: bool = True, src_usr: str = "c:@S@M@FI@_p"):
    return {
        "record": "binding",
        "parameter_name": name,
        "src_usr": src_usr,
        "src_name": "_p",
        "dst_usr": "c:@N@estimator@S@parameters@FI@x",
        "dst_name": "x",
        "resolved": resolved,
        "target_expr": "_params->x",
        "index_expr": "",
        "file": "src/m.cpp",
        "line": 5,
        "column": 3,
        "tu": "src/m.cpp",
    }


def test_wrapper_and_binding_edges(tmp_path):
    cfg = make_config(tmp_path)
    nodes, edges, metrics, unresolved = build_wrappers(
        cfg,
        [_wrapper(), _binding()],
        _parameters(),
        commit="commit",
        variant="test_variant",
    )

    assert len(nodes) == 1
    wrapper = nodes[0]
    assert wrapper["id"] == wrapper_id("commit", "test_variant", _wrapper())
    assert wrapper["kind"] == "param_wrapper"
    assert wrapper["parameter_name"] == "EKF2_X"
    assert wrapper["parameter_id"] == "pid-ekf2-x"
    assert unresolved == []

    relations = sorted(e["relation"] for e in edges)
    assert relations == ["binds_to", "configures", "wraps"]
    for edge in edges:
        assert edge["build_variant"] == "test_variant"
        assert edge["evidence"]

    assert metrics["param_wrappers"] == 1
    assert metrics["bindings"] == 1
    assert metrics["bindings_resolved"] == 1
    assert metrics["distinct_parameters_bound"] == 1
    assert metrics["wrapper_binding_rate"] == 1.0


def test_wrapper_for_parameter_missing_from_build_is_unresolved(tmp_path):
    cfg = make_config(tmp_path)
    nodes, edges, metrics, unresolved = build_wrappers(
        cfg, [_wrapper("EKF2_GHOST")], _parameters(), "commit", "test_variant"
    )
    assert len(nodes) == 1
    assert edges == []
    assert [u["kind"] for u in unresolved] == ["parameter_wrapper_unresolved"]
    assert metrics["wrappers_without_parameter"] == 1


def test_unresolved_constructor_target_emits_no_binding_edge(tmp_path):
    cfg = make_config(tmp_path)
    _nodes, edges, metrics, unresolved = build_wrappers(
        cfg,
        [_wrapper(), _binding(resolved=False)],
        _parameters(),
        "commit",
        "test_variant",
    )
    # The wrapper itself is still a node and still wraps its parameter.
    assert [e["relation"] for e in edges] == ["wraps"]
    assert [u["kind"] for u in unresolved] == ["parameter_binding_unresolved"]
    assert metrics["bindings_unresolved"] == 1
    assert metrics["wrapper_binding_rate"] == 0.0


def test_binding_parameter_missing_from_build_emits_no_edge(tmp_path):
    cfg = make_config(tmp_path)
    _nodes, edges, metrics, unresolved = build_wrappers(
        cfg, [_wrapper("EKF2_GHOST"), _binding("EKF2_GHOST")], _parameters(), "c", "v"
    )
    assert edges == []
    kinds = sorted(u["kind"] for u in unresolved)
    assert kinds == ["parameter_binding_parameter_missing", "parameter_wrapper_unresolved"]
    assert metrics["bindings_resolved"] == 1
