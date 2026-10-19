"""Unit tests for the M6 break-chain and targeted LLVM join logic."""

from __future__ import annotations

from pathlib import Path

from px4dep.config import Config
from px4dep.llvm_fallback import (
    accessor_member_map,
    build_break_distribution,
    build_writer_member_map,
    collect_break_chains,
    filter_compile_command,
)
from px4dep.manifest import Run
from px4dep.util import write_jsonl


def _run(tmp_path: Path) -> Run:
    config = Config(path=tmp_path / "config.yaml", project_root=tmp_path, raw={})
    run = Run(cfg=config, run_id="test", root=tmp_path / "run")
    run.ensure_dirs()
    return run


def test_filter_compile_command_keeps_preprocessor_and_language_flags():
    command = (
        "/usr/bin/clang++ -Dnoreturn_function=__attribute__((noreturn)) "
        "-I/inc -include prefix.h -std=gnu++17 -O2 -g -Werror "
        "-fdiagnostics-color -fno-strict-aliasing -c -o EKF2.o "
        "src/modules/ekf2/EKF2.cpp"
    )
    kept = filter_compile_command(command, "src/modules/ekf2/EKF2.cpp")
    assert "-Dnoreturn_function=__attribute__((noreturn))" in kept
    assert "-I/inc" in kept
    assert "-include" in kept and "prefix.h" in kept
    assert "-std=gnu++17" in kept
    assert "-g" in kept
    assert "-fno-strict-aliasing" in kept
    assert "-O2" not in kept
    assert "-Werror" not in kept
    assert "-fdiagnostics-color" not in kept
    assert "-c" not in kept
    assert "-o" not in kept
    assert "EKF2.o" not in kept
    assert "src/modules/ekf2/EKF2.cpp" not in kept


def test_filter_compile_command_drops_separate_optimisation_argument():
    kept = filter_compile_command("clang++ -O 2 -Iinc -c file.cpp", "file.cpp")
    assert "2" not in kept
    assert kept == ["-Iinc"]


def test_accessor_member_map_uses_this_member_only():
    raw = [
        {
            "record": "member_access",
            "caller": "Ekf::aid_src_baro_hgt",
            "qualified_name": "Ekf::_aid_src_baro_hgt",
            "receiver_kind": "this",
            "receiver_expr": "_aid_src_baro_hgt",
            "field": "_aid_src_baro_hgt",
            "type": "struct estimator_aid_source1d_s",
            "file": "src/modules/ekf2/EKF/ekf.h",
            "line": 101,
        },
        {
            "record": "member_access",
            "caller": "Ekf::aid_src_other",
            "qualified_name": "Other::field",
            "receiver_kind": "local",
            "receiver_expr": "other",
        },
    ]
    result = accessor_member_map(raw)
    assert result["Ekf::aid_src_baro_hgt"]["member"] == "Ekf::_aid_src_baro_hgt"
    assert "Ekf::aid_src_other" not in result


def test_writer_member_fixed_point_propagates_through_local_and_parameter():
    raw = [
        {
            "record": "member_access",
            "caller": "Ekf::aid_src_baro_hgt",
            "caller_usr": "u_accessor",
            "qualified_name": "Ekf::_aid_src_baro_hgt",
            "receiver_kind": "this",
            "receiver_expr": "_aid_src_baro_hgt",
        },
        {
            "record": "local_write",
            "caller": "Ekf::fuseBaro",
            "caller_usr": "u_fuse",
            "variable": "source",
            "rhs_members": [{"qualified_name": "Ekf::_aid_src_baro_hgt"}],
            "rhs_locals": [],
        },
        {
            "record": "local_write",
            "caller": "Ekf::fuseBaro",
            "caller_usr": "u_fuse",
            "variable": "alias",
            "rhs_members": [],
            "rhs_locals": ["source"],
        },
        {
            "record": "local_write",
            "caller": "Ekf::controlBaro",
            "caller_usr": "u_control",
            "variable": "status",
            "rhs_members": [],
            "rhs_locals": [],
        },
        {
            "record": "call_arg",
            "caller": "Ekf::fuseBaro",
            "caller_usr": "u_fuse",
            "callee": "Ekf::updateStatus",
            "callee_usr": "u_update",
            "param_name": "status",
            "param_type": "struct estimator_aid_source1d_s &",
            "arg_text": "alias",
            "locals": ["alias"],
            "members": [],
        },
    ]
    result = build_writer_member_map(raw)
    assert result["by_function_usr"]["u_fuse"] == ["Ekf::_aid_src_baro_hgt"]
    assert result["by_function_usr"]["u_update"] == ["Ekf::_aid_src_baro_hgt"]
    assert result["iterations"] >= 2


def _write_break_inputs(run: Run) -> None:
    write_jsonl(
        run.path("graph", "paths.jsonl"),
        [
            {
                "id": "path1",
                "parameter": "EKF2_FOO",
                "source": "estimator::parameters::ekf2_foo",
                "sink": "estimator_aid_source1d_s::innovation",
                "sink_writers": [
                    {"usr": "u_fuse", "qualified_name": "Ekf::fuseFoo"}
                ],
                "steps": [
                    {
                        "from": "estimator_aid_source1d_s::innovation",
                        "to": "parameter[estimator::parameters::ekf2_foo]",
                        "caller": "Ekf::fuseFoo",
                    }
                ],
                "category": "data",
            }
        ],
    )
    write_jsonl(
        run.path("unresolved", "cross_module.jsonl"),
        [
            {
                "kind": "m4_writer_publish_topic_unresolved",
                "parameter": "EKF2_FOO",
                "source": "estimator::parameters::ekf2_foo",
                "sink": "estimator_aid_source1d_s::innovation",
                "message_type": "estimator_aid_source1d_s",
                "writers": "u_fuse",
            }
        ],
    )
    write_jsonl(
        run.path("unresolved", "calls.jsonl"),
        [{"kind": "param_find_unresolved", "callee": "foo"}],
    )


def test_collect_break_chains_preserves_original_and_links_paths(tmp_path: Path):
    run = _run(tmp_path)
    _write_break_inputs(run)
    records = collect_break_chains(run)
    by_kind = {record["unresolved_kind"]: record for record in records}
    assert set(by_kind) == {"m4_writer_publish_topic_unresolved", "param_find_unresolved"}
    m4 = by_kind["m4_writer_publish_topic_unresolved"]
    assert m4["stage"] == "M4"
    assert m4["llvm_applicable"] is True
    assert m4["path_count"] == 1
    assert m4["m3_path_ids"] == ["path1"]
    assert m4["record"]["writers"] == "u_fuse"
    assert by_kind["param_find_unresolved"]["llvm_applicable"] is False
    assert by_kind["param_find_unresolved"]["root_cause"] == "runtime_parameter_name"


def test_break_distribution_selects_only_ir_applicable_type(tmp_path: Path):
    run = _run(tmp_path)
    _write_break_inputs(run)
    records = collect_break_chains(run)
    distribution = build_break_distribution(records, run)
    selection = distribution["selection"]
    assert selection["selected_kind"] == "m4_writer_publish_topic_unresolved"
    assert selection["selected_score"] == 1 + 5 * 1 + 3 * 1
    m4 = next(
        item
        for item in distribution["by_kind"]
        if item["unresolved_kind"] == "m4_writer_publish_topic_unresolved"
    )
    assert m4["distinct_parameters"] == 1
    assert m4["path_linked_records"] == 1
    other = next(
        item
        for item in distribution["by_kind"]
        if item["unresolved_kind"] == "param_find_unresolved"
    )
    assert other["llvm_applicable"] is False
