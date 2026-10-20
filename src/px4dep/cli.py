"""px4dep command line interface (M1/M2/M3/M4/M5/M6)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .candidates import build_candidates, write_candidates
from .config import Config, load_config
from .cpp_index import load_raw, run_tool
from .graph_builder import build_graph
from .llvm_fallback import run_llvm_fallback
from .manifest import Run, create_run, resolve_run
from .parameter_catalog import build_catalog, write_catalog
from .report import build_report
from .uorb_schema import build_schema, write_schema
from .ulog_evidence import build_runtime, load_ulog, write_runtime
from .util import read_jsonl
from .validation import validate_run


def _identity(run: Run) -> tuple[str, str]:
    manifest = run.load_manifest()
    px4 = manifest.get("px4") or {}
    return px4.get("commit", ""), px4.get("build_variant", run.cfg.build_variant)


def _resolve_or_create(cfg: Config, run_id: str | None) -> Run:
    if run_id is None:
        return resolve_run(cfg, "latest")
    if run_id == "new":
        return create_run(cfg)
    return resolve_run(cfg, run_id)


def cmd_manifest(cfg: Config, args: argparse.Namespace) -> int:
    run = create_run(cfg)
    manifest = run.load_manifest()
    print(f"run_id: {run.run_id}")
    print(f"status: {manifest.get('status')}")
    print(f"manifest: {run.manifest_path}")
    for warning in manifest.get("warnings", []):
        print(f"warning: {warning}")
    for error in manifest.get("errors", []):
        print(f"error: {error}")
    return 0


def cmd_params(cfg: Config, args: argparse.Namespace) -> int:
    run = _resolve_or_create(cfg, args.run)
    raw = load_raw(cfg, run)
    if not raw:
        print("error: no raw C++ records; run `px4dep index-cpp` first", file=sys.stderr)
        return 2
    records, metrics, unresolved = build_catalog(cfg, raw)
    write_catalog(run, records, metrics, unresolved)
    print(f"parameters: {metrics['build_parameter_count']} build, {metrics['source_yaml_parameter_count']} YAML")
    print(f"wrappers: {metrics['build_parameters_with_wrapper']} parameters have wrappers")
    print(f"unresolved: {len(unresolved)}")
    return 0


def cmd_index_cpp(cfg: Config, args: argparse.Namespace) -> int:
    run = _resolve_or_create(cfg, args.run)
    metrics = run_tool(cfg, run, rebuild=args.rebuild)
    print(f"cpp records: {metrics.get('normalized_records', 0)}")
    print(f"TUs: {len(metrics.get('tus_run', []))} run, {len(metrics.get('tus_skipped', []))} skipped")
    print(f"parse errors: {metrics.get('record_parse_errors', 0)}")
    return 0


def cmd_index_uorb(cfg: Config, args: argparse.Namespace) -> int:
    run = _resolve_or_create(cfg, args.run)
    commit, variant = _identity(run)
    nodes, edges, metrics, unresolved = build_schema(cfg, commit, variant)
    write_schema(run, nodes, edges, metrics, unresolved)
    print(f"msg files: {metrics['msg_files']}; topics: {metrics['topics']}; fields: {metrics['fields']}")
    print(f"parse errors: {metrics['msg_parse_errors']}; unresolved: {len(unresolved)}")
    return 0


def _load_schema_records(run: Run, cfg: Config, commit: str, variant: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    nodes = (
        read_jsonl(run.path("normalized", "uorb_messages.jsonl"))
        + read_jsonl(run.path("normalized", "uorb_topics.jsonl"))
        + read_jsonl(run.path("normalized", "uorb_fields.jsonl"))
    )
    edges = read_jsonl(run.path("normalized", "uorb_schema_edges.jsonl"))
    if not nodes:
        nodes, edges, metrics, unresolved = build_schema(cfg, commit, variant)
        write_schema(run, nodes, edges, metrics, unresolved)
    unresolved = read_jsonl(run.path("unresolved", "uorb_schema.jsonl"))
    return nodes, edges, unresolved


def cmd_build_graph(cfg: Config, args: argparse.Namespace) -> int:
    run = _resolve_or_create(cfg, args.run)
    commit, variant = _identity(run)
    raw = load_raw(cfg, run)
    if not raw:
        print("error: no raw C++ records; run `px4dep index-cpp` first", file=sys.stderr)
        return 2
    parameters = read_jsonl(run.path("normalized", "parameters.jsonl"))
    if not parameters:
        parameters, metrics, unresolved = build_catalog(cfg, raw)
        write_catalog(run, parameters, metrics, unresolved)
    schema_nodes, schema_edges, schema_unresolved = _load_schema_records(run, cfg, commit, variant)
    metrics = build_graph(
        cfg,
        run,
        raw,
        parameters,
        schema_nodes,
        schema_edges,
        commit,
        variant,
        schema_unresolved,
    )
    print(f"graph: {metrics['nodes']} nodes, {metrics['edges']} edges")
    health = metrics.get("health_chain") or {}
    print(
        "health chain: "
        f"{health.get('health_predicates', 0)} health predicates, "
        f"{health.get('failsafe_checks', 0)} failsafe checks, "
        f"{health.get('protection_decisions', 0)} action decisions"
    )
    slice_metrics = metrics.get("ekf_slice") or {}
    print(
        "ekf slice: "
        f"{slice_metrics.get('sink_nodes', 0)} sinks, "
        f"{slice_metrics.get('data_edges', 0)} data edges, "
        f"{slice_metrics.get('control_edges', 0)} control edges, "
        f"{slice_metrics.get('paths', 0)} paths"
    )
    cross = metrics.get("cross_module") or {}
    print(
        "cross module: "
        f"{cross.get('paths', 0)} paths, "
        f"{cross.get('distinct_topics', 0)} topics, "
        f"{cross.get('paths_with_health_predicate', 0)} with health evidence, "
        f"{cross.get('name_based_joins', 0)} name-based joins"
    )
    commander = metrics.get("commander_slice") or {}
    print(
        "commander slice: "
        f"{commander.get('parameters_with_path', 0)} parameters, "
        f"{commander.get('data_paths', 0)} data / "
        f"{commander.get('control_paths', 0)} control / "
        f"{commander.get('context_paths', 0)} context paths"
    )
    print(f"unresolved: {metrics['unresolved']}")
    return 0


def _ingest_runtime(
    cfg: Config,
    run: Run,
    commit: str,
    ulog_arg: str | None = None,
    scenario_arg: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    nodes = read_jsonl(run.path("graph", "nodes.jsonl"))
    m4_paths = read_jsonl(run.path("graph", "end_to_end_paths.jsonl"))
    ulog_path = (
        Path(ulog_arg).expanduser().resolve() if ulog_arg else cfg.ulog_path
    )
    scenario = scenario_arg or cfg.ulog_scenario
    log = None
    error = None
    if ulog_path is not None and ulog_path.exists():
        try:
            log = load_ulog(ulog_path, scenario)
        except Exception as exc:  # pyulog raises several parser-specific types
            error = f"{type(exc).__name__}: {exc}"
    records, metrics, unresolved = build_runtime(nodes, m4_paths, log, commit)
    if error:
        metrics["log_error"] = error
        unresolved.append({"kind": "ulog_parse_error", "error": error})
        metrics["unresolved"] = len(unresolved)
    write_runtime(
        run,
        records,
        metrics,
        unresolved,
        ulog=ulog_path if log is not None else None,
        scenario=scenario,
        ver_sw=log.ver_sw if log is not None else "",
    )
    prefix = str(ulog_path) if ulog_path else "(none)"
    print(f"ulog: {prefix}")
    print(
        f"runtime: {metrics.get('paths_field_observed', 0)}/"
        f"{metrics.get('paths', 0)} paths field-observed, "
        f"{metrics.get('graph_topics_observed', 0)}/"
        f"{metrics.get('graph_topics', 0)} graph topics observed, "
        f"{metrics.get('unresolved', 0)} unresolved"
    )
    if error:
        print(f"warning: could not parse ULog: {error}")
    return metrics, error


def cmd_ingest_ulog(cfg: Config, args: argparse.Namespace) -> int:
    run = _resolve_or_create(cfg, args.run)
    if not run.path("graph", "end_to_end_paths.jsonl").exists():
        print("error: no M4 paths; run `px4dep build-graph` first", file=sys.stderr)
        return 2
    commit, _variant = _identity(run)
    metrics, error = _ingest_runtime(
        cfg, run, commit, ulog_arg=args.ulog, scenario_arg=args.scenario
    )
    return 2 if error else 0


def cmd_candidates(cfg: Config, args: argparse.Namespace) -> int:
    run = _resolve_or_create(cfg, args.run)
    commit, variant = _identity(run)
    nodes = read_jsonl(run.path("graph", "nodes.jsonl"))
    slice_paths = read_jsonl(run.path("graph", "paths.jsonl"))
    commander_paths = read_jsonl(run.path("graph", "commander_paths.jsonl"))
    m4_paths = read_jsonl(run.path("graph", "end_to_end_paths.jsonl"))
    if not nodes or not (slice_paths or commander_paths):
        print("error: graph paths missing; run `px4dep build-graph` first", file=sys.stderr)
        return 2
    runtime = read_jsonl(run.path("evidence", "runtime.jsonl"))
    cross_unresolved = read_jsonl(run.path("unresolved", "cross_module.jsonl"))
    candidates, metrics = build_candidates(
        nodes,
        slice_paths,
        m4_paths,
        runtime,
        cross_unresolved,
        commit=commit,
        variant=variant,
        commander_paths=commander_paths,
    )
    write_candidates(run, candidates, metrics)
    print(
        f"candidates: {metrics.get('candidates', 0)} "
        f"({metrics.get('end_to_end', 0)} end-to-end, "
        f"{metrics.get('with_data_path', 0)} with data path, "
        f"{metrics.get('commander_candidates', 0)} commander, "
        f"{metrics.get('context_only', 0)} context-only, "
        f"{metrics.get('runtime_field_observed', 0)} runtime-observed)"
    )
    return 0


def cmd_llvm_fallback(cfg: Config, args: argparse.Namespace) -> int:
    run = _resolve_or_create(cfg, args.run)
    required = (
        run.path("unresolved", "cross_module.jsonl"),
        run.path("graph", "paths.jsonl"),
        run.path("raw", "cpp_records.jsonl"),
        run.path("unresolved", "health_chain.jsonl"),
        run.path("unresolved", "ekf_slice.jsonl"),
    )
    if not all(path.exists() for path in required):
        print(
            "error: M1-M5 outputs missing; run `px4dep run-all` or the earlier "
            "stages first",
            file=sys.stderr,
        )
        return 2
    metrics = run_llvm_fallback(cfg, run, rebuild=args.rebuild)
    distribution = metrics.get("break_chain_distribution") or {}
    selection = distribution.get("selection") or {}
    linking = metrics.get("linking") or {}
    print(
        f"break chains: {distribution.get('total', 0)} "
        f"(selected `{selection.get('selected_kind', '')}`, "
        f"score {selection.get('selected_score', 0)})"
    )
    print(
        f"llvm: {metrics.get('bindings', 0)} bindings, "
        f"{metrics.get('bindings_cross_checked', 0)} AST cross-checked, "
        f"{len(metrics.get('binding_gaps') or [])} gaps"
    )
    print(
        f"narrowing: {linking.get('unique_topic_records', 0)} unique / "
        f"{linking.get('multiple_topic_records', 0)} multiple / "
        f"{linking.get('no_topic_records', 0)} none; "
        f"{linking.get('records_with_commander_consumer', 0)} with Commander consumer"
    )
    print("end-to-end paths created by M6: 0")
    return 0


def cmd_validate(cfg: Config, args: argparse.Namespace) -> int:
    run = _resolve_or_create(cfg, args.run)
    result = validate_run(cfg, run)
    print(f"validation: {'PASS' if result['ok'] else 'FAIL'}")
    for error in result["errors"]:
        print(f"error: {error}")
    for warning in result["warnings"]:
        print(f"warning: {warning}")
    return 0 if result["ok"] else 1


def cmd_report(cfg: Config, args: argparse.Namespace) -> int:
    run = _resolve_or_create(cfg, args.run)
    metrics = build_report(cfg, run)
    print(f"summary: {run.path('reports', 'summary.md')}")
    print(f"M1 gate: {'PASS' if metrics.get('m1_gate_passed') else 'FAIL'}")
    print(f"M2 gate: {'PASS' if metrics.get('m2_gate_passed') else 'FAIL'}")
    print(f"M3 gate: {'PASS' if metrics.get('m3_gate_passed') else 'FAIL'}")
    print(
        "Commander slice gate: "
        f"{'PASS' if metrics.get('commander_slice_gate_passed') else 'FAIL'}"
    )
    print(f"M4 gate: {'PASS' if metrics.get('m4_gate_passed') else 'FAIL'}")
    print(f"M5 gate: {'PASS' if metrics.get('m5_gate_passed') else 'FAIL'}")
    print(f"M6 gate: {'PASS' if metrics.get('m6_gate_passed') else 'FAIL'}")
    return 0 if all(
        metrics.get(f"m{index}_gate_passed") for index in (1, 2, 3, 4, 5, 6)
    ) else 1


def cmd_run_all(cfg: Config, args: argparse.Namespace) -> int:
    run = create_run(cfg)
    print(f"run_id: {run.run_id}")
    commit, variant = _identity(run)

    print("[1/9] clang index ...")
    cpp_metrics = run_tool(cfg, run, rebuild=args.rebuild)
    print(f"      {cpp_metrics.get('normalized_records', 0)} records")

    print("[2/9] uORB schema ...")
    schema_nodes, schema_edges, schema_metrics, schema_unresolved = build_schema(cfg, commit, variant)
    write_schema(run, schema_nodes, schema_edges, schema_metrics, schema_unresolved)
    print(f"      {schema_metrics['topics']} topics from {schema_metrics['msg_files']} msg files")

    print("[3/9] parameter catalog ...")
    raw = load_raw(cfg, run)
    parameter_records, parameter_metrics, parameter_unresolved = build_catalog(cfg, raw)
    write_catalog(run, parameter_records, parameter_metrics, parameter_unresolved)
    print(f"      {parameter_metrics['build_parameter_count']} parameters in build")

    print("[4/9] evidence graph ...")
    graph_metrics = build_graph(
        cfg,
        run,
        raw,
        parameter_records,
        schema_nodes,
        schema_edges,
        commit,
        variant,
    )
    print(f"      {graph_metrics['nodes']} nodes, {graph_metrics['edges']} edges")
    health = graph_metrics.get("health_chain") or {}
    print(
        "      health chain: "
        f"{health.get('health_predicates', 0)} health, "
        f"{health.get('mode_checks', 0)} mode checks, "
        f"{health.get('failsafe_checks', 0)} failsafe checks, "
        f"{health.get('protection_decisions', 0)} decisions"
    )
    slice_metrics = graph_metrics.get("ekf_slice") or {}
    print(
        "      ekf slice: "
        f"{slice_metrics.get('sink_nodes', 0)} sinks, "
        f"{slice_metrics.get('parameter_fields_bound', 0)} parameter fields bound, "
        f"{slice_metrics.get('data_edges', 0)} data / "
        f"{slice_metrics.get('control_edges', 0)} control edges, "
        f"{slice_metrics.get('paths', 0)} paths"
    )
    commander_metrics = graph_metrics.get("commander_slice") or {}
    print(
        "      commander slice: "
        f"{commander_metrics.get('parameters_with_path', 0)} parameters with paths, "
        f"{commander_metrics.get('data_paths', 0)} data / "
        f"{commander_metrics.get('control_paths', 0)} control / "
        f"{commander_metrics.get('context_paths', 0)} context, "
        f"{commander_metrics.get('unresolved', 0)} unresolved"
    )
    cross_metrics = graph_metrics.get("cross_module") or {}
    print(
        "      cross module: "
        f"{cross_metrics.get('paths', 0)} paths, "
        f"{cross_metrics.get('distinct_consumers', 0)} consumers, "
        f"{cross_metrics.get('paths_with_health_predicate', 0)} health-evidence paths"
    )

    print("[5/9] ULog runtime evidence ...")
    runtime_metrics, runtime_error = _ingest_runtime(
        cfg, run, commit, ulog_arg=args.ulog, scenario_arg=None
    )

    print("[6/9] review candidates ...")
    graph_nodes = read_jsonl(run.path("graph", "nodes.jsonl"))
    slice_paths = read_jsonl(run.path("graph", "paths.jsonl"))
    commander_paths = read_jsonl(run.path("graph", "commander_paths.jsonl"))
    m4_paths = read_jsonl(run.path("graph", "end_to_end_paths.jsonl"))
    cross_unresolved = read_jsonl(run.path("unresolved", "cross_module.jsonl"))
    candidates, candidate_metrics = build_candidates(
        graph_nodes,
        slice_paths,
        m4_paths,
        read_jsonl(run.path("evidence", "runtime.jsonl")),
        cross_unresolved,
        commit=commit,
        variant=variant,
        commander_paths=commander_paths,
    )
    write_candidates(run, candidates, candidate_metrics)
    print(
        f"      {candidate_metrics.get('candidates', 0)} candidates "
        f"({candidate_metrics.get('end_to_end', 0)} end-to-end, "
        f"{candidate_metrics.get('commander_candidates', 0)} commander, "
        f"{candidate_metrics.get('context_only', 0)} context-only)"
    )

    print("[7/9] break chains + targeted LLVM pass ...")
    llvm_metrics = run_llvm_fallback(cfg, run, rebuild=args.rebuild)
    llvm_linking = llvm_metrics.get("linking") or {}
    print(
        f"      {llvm_metrics.get('bindings', 0)} LLVM bindings; "
        f"{llvm_linking.get('unique_topic_records', 0)} unique / "
        f"{llvm_linking.get('multiple_topic_records', 0)} multiple / "
        f"{llvm_linking.get('no_topic_records', 0)} no-topic records"
    )

    print("[8/9] report ...")
    report_metrics = build_report(cfg, run)
    print(f"      M1 gate: {'PASS' if report_metrics.get('m1_gate_passed') else 'FAIL'}")
    print(f"      M2 gate: {'PASS' if report_metrics.get('m2_gate_passed') else 'FAIL'}")
    print(f"      M3 gate: {'PASS' if report_metrics.get('m3_gate_passed') else 'FAIL'}")
    print(f"      M4 gate: {'PASS' if report_metrics.get('m4_gate_passed') else 'FAIL'}")
    print(f"      M5 gate: {'PASS' if report_metrics.get('m5_gate_passed') else 'FAIL'}")
    print(f"      M6 gate: {'PASS' if report_metrics.get('m6_gate_passed') else 'FAIL'}")

    print("[9/9] validation ...")
    validation = validate_run(cfg, run)
    print(f"      {'PASS' if validation['ok'] else 'FAIL'}")
    print(f"summary: {run.path('reports', 'summary.md')}")
    return 0 if (
        validation["ok"]
        and report_metrics.get("m1_gate_passed")
        and report_metrics.get("m2_gate_passed")
        and report_metrics.get("m3_gate_passed")
        and report_metrics.get("m4_gate_passed")
        and report_metrics.get("m5_gate_passed")
        and report_metrics.get("m6_gate_passed")
    ) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="px4dep", description="PX4 configuration dependency analyzer (M1-M6)")
    parser.add_argument("--config", default="configs/px4_sitl_default.yaml", help="experiment config YAML")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("manifest", help="create a run and record input hashes")
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("params", help="build the parameter catalog")
    p.add_argument("--run", default="latest")
    p.set_defaults(func=cmd_params)

    p = sub.add_parser("index-cpp", help="run the Clang indexer")
    p.add_argument("--run", default="latest")
    p.add_argument("--rebuild", action="store_true", help="rebuild the Clang tool first")
    p.set_defaults(func=cmd_index_cpp)

    p = sub.add_parser("index-uorb", help="parse .msg schemas")
    p.add_argument("--run", default="latest")
    p.set_defaults(func=cmd_index_uorb)

    p = sub.add_parser("build-graph", help="merge normalized records into the evidence graph")
    p.add_argument("--run", default="latest")
    p.set_defaults(func=cmd_build_graph)

    p = sub.add_parser("ingest-ulog", help="attach normal-scenario ULog reachability evidence")
    p.add_argument("--run", default="latest")
    p.add_argument("--ulog", default=None, help="authorized normal-scenario .ulg (overrides config)")
    p.add_argument("--scenario", default=None, help="operator-declared scenario label")
    p.set_defaults(func=cmd_ingest_ulog)

    p = sub.add_parser("candidates", help="build graded review candidates from static and runtime evidence")
    p.add_argument("--run", default="latest")
    p.set_defaults(func=cmd_candidates)

    p = sub.add_parser(
        "llvm-fallback",
        help="count M1-M5 break chains and run the targeted LLVM pass for the selected type",
    )
    p.add_argument("--run", default="latest")
    p.add_argument(
        "--rebuild", action="store_true", help="rebuild the LLVM pass plugin first"
    )
    p.set_defaults(func=cmd_llvm_fallback)

    p = sub.add_parser("validate", help="validate schemas, ids, input hashes and dangling edges")
    p.add_argument("--run", default="latest")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("report", help="write summary.md, metrics.json and CSV review tables")
    p.add_argument("--run", default="latest")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("run-all", help="run every M1-M6 stage in a new run")
    p.add_argument("--rebuild", action="store_true", help="rebuild the Clang and LLVM tools first")
    p.add_argument("--ulog", default=None, help="authorized normal-scenario .ulg (overrides config)")
    p.set_defaults(func=cmd_run_all)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    try:
        return args.func(cfg, args)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
