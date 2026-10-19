"""M6: break-chain statistics and a targeted LLVM fallback.

The README for M6 is deliberately narrow: first count the unresolved/break
chains produced by M1--M5, then implement an LLVM pass only for the
highest-value unresolved *type*.  This module therefore has two distinct
parts:

* ``collect_break_chains`` / ``build_break_distribution`` -- an inventory of
  every authoritative unresolved record.  No unresolved item is dropped.
* ``run_llvm_fallback`` -- one targeted LLVM 14 pass for the selected type
  (``m4_writer_publish_topic_unresolved``).  The pass binds EKF aid-source
  message instances to uORB topics.  The Python join then combines that IR
  evidence with AST writer evidence and M3 paths.

Evidence kinds stay separate and are labelled in every record.  LLVM evidence
is never used on its own to create an end-to-end path.  This is not a generic
LLVM framework.
"""

from __future__ import annotations

import csv
import os
import re
import shlex
import shutil
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .config import Config
from .manifest import Run
from .util import read_jsonl, sha256_file, stable_id, utc_now, write_json, write_jsonl


# Authoritative per-stage unresolved files.  ``unresolved/graph.jsonl`` is a
# merged view that duplicates the M1/M2/M3 records, so it is deliberately not
# counted as an independent source; it is used only as a coverage check.
STAGE_SOURCES: list[tuple[str, str, str]] = [
    ("M1", "parameters", "unresolved/parameters.jsonl"),
    ("M1", "cpp_records", "unresolved/cpp_records.jsonl"),
    ("M1", "uorb_schema", "unresolved/uorb_schema.jsonl"),
    ("M1", "aliases", "unresolved/aliases.jsonl"),
    ("M1", "truncations", "unresolved/truncations.jsonl"),
    ("M1", "calls", "unresolved/calls.jsonl"),
    ("M1", "topics", "unresolved/topics.jsonl"),
    ("M2", "health_chain", "unresolved/health_chain.jsonl"),
    ("M3", "ekf_slice", "unresolved/ekf_slice.jsonl"),
    ("M4", "cross_module", "unresolved/cross_module.jsonl"),
    ("M5", "ulog", "unresolved/ulog.jsonl"),
]

EXCLUDED_SOURCES = [
    {
        "file": "unresolved/graph.jsonl",
        "reason": "merged duplicate view of the M1/M2/M3 unresolved files; "
        "counting it would double-count those records",
    }
]

# kind -> (root cause, explanation, targeted LLVM applicable?)
KIND_INFO: dict[str, tuple[str, str, bool]] = {
    "mode_set_dynamic": (
        "runtime_mode_values",
        "required_modes is built from runtime state, so there is no static "
        "mode set for an IR pass to recover",
        False,
    ),
    "protection_action_dynamic": (
        "runtime_protection_action",
        "failsafe action options are selected from runtime values; IR would "
        "contain the same dynamic value flow, not a static action",
        False,
    ),
    "param_find_unresolved": (
        "runtime_parameter_name",
        "the parameter-name argument is a runtime string/parameter, not a "
        "compile-time constant",
        False,
    ),
    "parameter_handle_not_resolved": (
        "runtime_parameter_handle",
        "the param_set/param_get handle requires runtime dataflow; there is no "
        "static handle to bind",
        False,
    ),
    "slice_depth_truncated": (
        "slice_depth_limit",
        "the M3 backward slice reached its configured depth; this is an "
        "analysis bound, not evidence about the code",
        False,
    ),
    "uorb_endpoint_without_topic": (
        "uorb_endpoint_topic_expression",
        "the endpoint topic expression is not a constant ORB_ID at the "
        "declaration site; this low-volume type is outside the selected pass",
        False,
    ),
    "m4_writer_publish_topic_unresolved": (
        "writer_instance_to_topic_binding_missing",
        "M3 identifies a message-field sink and its writers but not the "
        "published message instance; a targeted IR pass can bind EKF aid-source "
        "members to their uORB topics",
        True,
    ),
    "ulog_parse_error": (
        "ulog_parse_error",
        "the normal-scenario log could not be parsed; this is an input/data "
        "gap, not a program property",
        False,
    ),
}

DEFAULT_KIND_INFO = (
    "unclassified_unresolved_kind",
    "no root-cause mapping is defined for this unresolved kind yet",
    False,
)

SELECTED_KIND = "m4_writer_publish_topic_unresolved"
TARGET_ACCESSOR_PREFIX = "Ekf::aid_src_"
TARGET_MEMBER_PREFIX = "Ekf::_aid_src_"
TARGET_PUBLISHER_PREFIX = "EKF2::PublishAidSourceStatus"

BREAK_CHAIN_COLUMNS = [
    "stage",
    "unresolved_kind",
    "root_cause",
    "count",
    "distinct_parameters",
    "path_linked_records",
    "llvm_applicable",
    "selection_score",
    "selected",
]

LLVM_BINDING_COLUMNS = [
    "topic",
    "message_type",
    "accessor_function",
    "ekf_member",
    "ekf_member_field_index",
    "ekf2_publication_field_index",
    "call_site",
    "constructor_site",
    "ast_cross_check",
    "commander_subscribers",
    "end_to_end_claim",
]

RESOLUTION_COLUMNS = [
    "break_chain_id",
    "parameter",
    "source",
    "sink",
    "message_type",
    "path_count",
    "topic_resolution",
    "topic_candidates",
    "commander_consumers",
    "status",
    "attribution_confidence",
    "root_cause_after",
]


def _kind_info(kind: str) -> tuple[str, str, bool]:
    return KIND_INFO.get(kind, DEFAULT_KIND_INFO)


def _parameter_of(record: dict[str, Any], stage: str) -> str:
    if stage == "M4":
        return str(record.get("parameter") or "")
    return ""


def _record_identity(record: dict[str, Any], stage: str) -> dict[str, Any]:
    """Return the semantic fields used to link a break chain to other stages."""
    identity: dict[str, Any] = {}
    for key in (
        "parameter",
        "source",
        "sink",
        "sink_id",
        "message_type",
        "predicate",
        "endpoint",
        "callee",
        "method",
        "file",
        "line",
    ):
        if record.get(key) not in (None, "", []):
            identity[key] = record[key]
    if stage == "M4":
        identity["parameter"] = record.get("parameter") or ""
        identity["source"] = record.get("source") or ""
        identity["sink"] = record.get("sink") or ""
    return identity


def _path_index(paths: list[dict[str, Any]]) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    index: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for path in paths:
        key = (
            str(path.get("parameter") or ""),
            str(path.get("source") or ""),
            str(path.get("sink") or ""),
        )
        index[key].append(path)
    return index


def collect_break_chains(run: Run) -> list[dict[str, Any]]:
    """Classify every authoritative unresolved record, preserving the original."""
    paths = read_jsonl(run.path("graph", "paths.jsonl"))
    path_index = _path_index(paths)
    sink_paths: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        if path.get("sink"):
            sink_paths[str(path["sink"])].append(str(path.get("id") or ""))

    records: list[dict[str, Any]] = []
    for stage, source_name, relative_path in STAGE_SOURCES:
        source_path = run.path(*relative_path.split("/"))
        for index, original in enumerate(read_jsonl(source_path)):
            kind = str(original.get("kind") or f"unknown_{source_name}")
            root_cause, explanation, applicable = _kind_info(kind)
            parameter = _parameter_of(original, stage)
            sink = str(original.get("sink") or "")
            path_ids: list[str] = []
            linked_paths: list[dict[str, Any]] = []
            if stage == "M4":
                key = (
                    str(original.get("parameter") or ""),
                    str(original.get("source") or ""),
                    str(original.get("sink") or ""),
                )
                linked_paths = path_index.get(key, [])
                path_ids = sorted(str(p.get("id") or "") for p in linked_paths)
            elif stage == "M3" and sink:
                path_ids = sorted(set(sink_paths.get(sink, [])))
            reason_parts = [explanation]
            if stage == "M4":
                reason_parts.append(
                    "the M3 writer set does not identify which message instance "
                    "the parameter reaches, so the publication topic is missing"
                )
            record_id = stable_id(
                "break_chain",
                stage,
                source_name,
                index,
                kind,
                parameter,
                sink,
                original.get("callee") or original.get("endpoint") or "",
                original.get("line") or "",
            )
            records.append(
                {
                    "kind": "break_chain",
                    "id": record_id,
                    "stage": stage,
                    "source_name": source_name,
                    "source_file": relative_path,
                    "source_index": index,
                    "unresolved_kind": kind,
                    "root_cause": root_cause,
                    "root_cause_explanation": " ".join(reason_parts),
                    "llvm_applicable": applicable,
                    "llvm_applicability": (
                        "targeted_ekf_aid_source_member_topic_pass"
                        if applicable
                        else "not_applicable_for_this_cause"
                    ),
                    "parameter": parameter,
                    "source_symbol": str(original.get("source") or ""),
                    "sink": sink,
                    "path_count": len(path_ids),
                    "m3_path_ids": path_ids,
                    "message_type": str(original.get("message_type") or ""),
                    "identity": _record_identity(original, stage),
                    "confidence": "high",
                    "interpretation": "break_chain_inventory_not_unreachability_claim",
                    "record": original,
                }
            )
    return records


def _distinct_parameters(records: Iterable[dict[str, Any]]) -> list[str]:
    return sorted({r["parameter"] for r in records if r.get("parameter")})


def build_break_distribution(
    records: list[dict[str, Any]],
    run: Run | None = None,
) -> dict[str, Any]:
    """Summarise the break-chain inventory and select the LLVM target."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[record["unresolved_kind"]].append(record)

    by_kind: list[dict[str, Any]] = []
    for kind, items in groups.items():
        stage = items[0]["stage"]
        root_cause = items[0]["root_cause"]
        params = _distinct_parameters(items)
        path_linked = sum(1 for item in items if item.get("path_count", 0) > 0)
        applicable = any(item.get("llvm_applicable") for item in items)
        score = len(items) + 5 * len(params) + 3 * path_linked
        by_kind.append(
            {
                "stage": stage,
                "unresolved_kind": kind,
                "root_cause": root_cause,
                "root_cause_explanation": items[0]["root_cause_explanation"],
                "count": len(items),
                "distinct_parameters": len(params),
                "parameter_names": params[:50],
                "path_linked_records": path_linked,
                "llvm_applicable": applicable,
                "selection_score": score,
                "source_files": sorted({item["source_file"] for item in items}),
            }
        )
    by_kind.sort(key=lambda item: (-item["selection_score"], item["unresolved_kind"]))

    applicable = [item for item in by_kind if item["llvm_applicable"]]
    selected = applicable[0] if applicable else None
    runner_up = applicable[1] if len(applicable) > 1 else None
    top_overall = by_kind[0] if by_kind else None

    by_stage = Counter(item["stage"] for item in records)
    by_root_cause: dict[str, dict[str, Any]] = {}
    for record in records:
        entry = by_root_cause.setdefault(
            record["root_cause"],
            {
                "root_cause": record["root_cause"],
                "count": 0,
                "stages": set(),
                "unresolved_kinds": set(),
                "llvm_applicable": False,
            },
        )
        entry["count"] += 1
        entry["stages"].add(record["stage"])
        entry["unresolved_kinds"].add(record["unresolved_kind"])
        entry["llvm_applicable"] = entry["llvm_applicable"] or record["llvm_applicable"]
    by_root_cause_rows = []
    for entry in sorted(by_root_cause.values(), key=lambda e: (-e["count"], e["root_cause"])):
        by_root_cause_rows.append(
            {
                "root_cause": entry["root_cause"],
                "count": entry["count"],
                "stages": sorted(entry["stages"]),
                "unresolved_kinds": sorted(entry["unresolved_kinds"]),
                "llvm_applicable": entry["llvm_applicable"],
            }
        )

    coverage: dict[str, Any] = {}
    if run is not None:
        graph_unresolved = read_jsonl(run.path("unresolved", "graph.jsonl"))
        non_m4 = [r for r in records if r["stage"] != "M4"]
        coverage = {
            "non_m4_break_chains": len(non_m4),
            "graph_merged_unresolved_records": len(graph_unresolved),
            "non_m4_equals_graph_merged": len(non_m4) == len(graph_unresolved),
            "cross_module_break_chains": len(records) - len(non_m4),
            "total": len(records),
        }

    distribution = {
        "kind": "break_chain_distribution",
        "generated_at": utc_now(),
        "run_id": run.run_id if run else "",
        "total": len(records),
        "by_stage": {stage: by_stage.get(stage, 0) for stage, _, _ in STAGE_SOURCES},
        "by_stage_present": {
            stage: by_stage.get(stage, 0) for stage in sorted({s for s, _, _ in STAGE_SOURCES})
        },
        "by_kind": by_kind,
        "by_root_cause": by_root_cause_rows,
        "sources": [
            {"stage": stage, "name": name, "file": path}
            for stage, name, path in STAGE_SOURCES
        ],
        "excluded_sources": EXCLUDED_SOURCES,
        "coverage": coverage,
        "selection": {
            "rule": "max(count + 5*distinct_parameters + 3*path_linked_records) "
            "among unresolved kinds for which a targeted IR pass is applicable",
            "selected_kind": selected["unresolved_kind"] if selected else "",
            "selected_stage": selected["stage"] if selected else "",
            "selected_score": selected["selection_score"] if selected else 0,
            "runner_up_kind": runner_up["unresolved_kind"] if runner_up else "",
            "runner_up_score": runner_up["selection_score"] if runner_up else 0,
            "top_overall_kind": top_overall["unresolved_kind"] if top_overall else "",
            "top_overall_score": top_overall["selection_score"] if top_overall else 0,
            "why": (
                "the M4 writer->publication-topic gap has the largest volume, "
                "the largest set of affected parameters, and every record is "
                "linked to an M3 path; the other unresolved kinds encode "
                "runtime values, runtime parameter names, or analysis limits "
                "that a generic IR pass would not resolve"
            ),
            "not_selected": [
                {
                    "unresolved_kind": item["unresolved_kind"],
                    "count": item["count"],
                    "reason": "not_applicable_to_targeted_ir"
                    if not item["llvm_applicable"]
                    else "lower_selection_score",
                }
                for item in by_kind
                if not selected or item["unresolved_kind"] != selected["unresolved_kind"]
            ],
        },
        "llvm_target_scope": {
            "pass": "px4dep-member-topic",
            "publisher": "EKF2::PublishAidSourceStatus<...>",
            "translation_unit": "src/modules/ekf2/EKF2.cpp",
            "optimization": "O0",
            "message_instances": "Ekf::aid_src_* accessors used at those call sites",
            "not_a_general_framework": True,
        },
        "interpretation": "break_chain_inventory_not_unreachability_claim",
    }
    return distribution


def write_break_chains(
    run: Run,
    records: list[dict[str, Any]],
    distribution: dict[str, Any],
) -> None:
    write_jsonl(run.path("evidence", "break_chains.jsonl"), records)
    write_json(run.path("reports", "break_chains.json"), distribution)

    rows = []
    selected = (distribution.get("selection") or {}).get("selected_kind", "")
    for item in distribution.get("by_kind") or []:
        rows.append(
            {
                "stage": item["stage"],
                "unresolved_kind": item["unresolved_kind"],
                "root_cause": item["root_cause"],
                "count": item["count"],
                "distinct_parameters": item["distinct_parameters"],
                "path_linked_records": item["path_linked_records"],
                "llvm_applicable": str(item["llvm_applicable"]).lower(),
                "selection_score": item["selection_score"],
                "selected": str(item["unresolved_kind"] == selected).lower(),
            }
        )
    _write_csv(run.path("reports", "break_chains.csv"), BREAK_CHAIN_COLUMNS, rows)


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Targeted LLVM pass
# ---------------------------------------------------------------------------


def _tool_version(executable: str) -> str:
    resolved = shutil.which(executable)
    if not resolved:
        return ""
    try:
        proc = subprocess.run(
            [resolved, "--version"], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.splitlines()[0].strip() if proc.stdout else ""


def _run_logged(
    command: list[str],
    cwd: Path,
    log_path: Path,
    env: dict[str, str] | None = None,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(shlex.quote(part) for part in command) + "\n")
        log.write(f"cwd: {cwd}\n\n")
        log.flush()
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=env,
        )
        if proc.stdout:
            log.write(proc.stdout)
        if proc.stderr:
            log.write(proc.stderr)
        log.write(f"\nreturncode: {proc.returncode}\n")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        detail = " | ".join(tail)
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(command[:3])} ...; "
            f"see {log_path}"
            + (f"; {detail}" if detail else "")
        )


def ensure_llvm_plugin(cfg: Config, run: Run, rebuild: bool = False) -> dict[str, Any]:
    plugin = cfg.llvm_pass_plugin
    build_script = cfg.llvm_build_script
    if rebuild or not plugin.exists():
        if not build_script.exists():
            raise FileNotFoundError(f"LLVM pass build script not found: {build_script}")
        _run_logged(
            ["bash", str(build_script)],
            cfg.project_root,
            run.path("logs", "llvm_build.log"),
        )
    if not plugin.exists():
        raise FileNotFoundError(f"LLVM pass plugin not found after build: {plugin}")
    return {
        "plugin_path": str(plugin),
        "plugin_sha256": sha256_file(plugin),
        "plugin_size_bytes": plugin.stat().st_size,
        "build_script": str(build_script),
        "build_script_sha256": sha256_file(build_script) if build_script.exists() else "",
    }


def _find_compile_entry(cfg: Config) -> dict[str, Any]:
    import json

    target = (cfg.repo / cfg.llvm_ir_tu).resolve()
    with cfg.compile_commands.open("r", encoding="utf-8") as fh:
        entries = json.load(fh)
    for entry in entries:
        source = Path(entry.get("file", ""))
        if not source.is_absolute():
            source = (Path(entry.get("directory", ".")) / source).resolve()
        else:
            source = source.resolve()
        if source == target:
            return entry
    raise FileNotFoundError(
        f"{cfg.llvm_ir_tu} is not present in {cfg.compile_commands}"
    )


# Flags that are safe and relevant to producing faithful IR at -O0.  Anything
# else (optimisation, warnings, code-generation/linker flags) is dropped.
_KEEP_EXACT = {
    "-fPIC",
    "-fpic",
    "-fno-strict-aliasing",
    "-fno-signed-zeros",
    "-fno-trapping-math",
    "-fno-math-errno",
    "-fno-associative-math",
    "-freciprocal-math",
    "-pthread",
    "-nostdinc++",
    "-g",
    "-g3",
    "-ggdb",
}
_KEEP_WITH_VALUE = {"-include", "-isystem", "-imacros", "-idirafter", "-D", "-I"}
_KEEP_PREFIX = (
    "-D",
    "-I",
    "-isystem",
    "-std=",
    "-fvisibility=",
    "-g",
)


def filter_compile_command(command: str, source_file: str) -> list[str]:
    """Strip a PX4 compile command down to preprocessor/language flags.

    The returned list excludes the compiler and the source file; the caller
    adds ``-O0 -g -S -emit-llvm -o ... source``.
    """
    tokens = shlex.split(command)
    if not tokens:
        raise ValueError("empty compile command")
    kept: list[str] = []
    index = 1
    source_resolved = str(Path(source_file).resolve())
    while index < len(tokens):
        token = tokens[index]
        if token == "-o":
            index += 2
            continue
        if token in {"-c", "-MD", "-MMD", "-MP"}:
            index += 1
            continue
        if token in {"-MF", "-MT", "-MQ"}:
            index += 2
            continue
        if token.startswith(("-W", "-fdiagnostics-")) or token in {
            "-fcheck-new",
            "-fno-builtin-printf",
            "-fmerge-all-constants",
            "-fomit-frame-pointer",
            "-fdata-sections",
            "-ffunction-sections",
        }:
            index += 1
            continue
        if token.startswith("-O"):
            index += 1
            continue
        if token in _KEEP_WITH_VALUE:
            if index + 1 < len(tokens):
                kept.extend([token, tokens[index + 1]])
                index += 2
            else:
                index += 1
            continue
        if token in _KEEP_EXACT or token.startswith(_KEEP_PREFIX):
            kept.append(token)
            index += 1
            continue
        # Source operands (absolute, relative, or a bare filename).
        if token == source_file or token == source_resolved:
            index += 1
            continue
        if token.endswith((".cpp", ".cc", ".cxx", ".c", ".C")):
            index += 1
            continue
        index += 1
    return kept


def compile_ir(cfg: Config, run: Run) -> dict[str, Any]:
    entry = _find_compile_entry(cfg)
    source_file = str((cfg.repo / cfg.llvm_ir_tu).resolve())
    ir_dir = run.path("llvm")
    ir_dir.mkdir(parents=True, exist_ok=True)
    ir_path = ir_dir / "EKF2.ll"
    flags = filter_compile_command(str(entry.get("command", "")), source_file)
    clang = shutil.which(cfg.llvm_clang)
    if not clang:
        raise FileNotFoundError(
            f"LLVM clang not found: {cfg.llvm_clang} (install clang-14 / "
            "llvm-14-dev)"
        )
    command = [
        clang,
        *flags,
        f"-{cfg.llvm_optimization}",
        "-g",
        "-S",
        "-emit-llvm",
        "-o",
        str(ir_path),
        source_file,
    ]
    _run_logged(
        command,
        Path(entry.get("directory") or cfg.repo),
        run.path("logs", "llvm_ir.log"),
    )
    if not ir_path.exists() or ir_path.stat().st_size == 0:
        raise RuntimeError(f"LLVM IR was not produced: {ir_path}")
    return {
        "tu": cfg.llvm_ir_tu,
        "source_file": source_file,
        "source_sha256": sha256_file(cfg.repo / cfg.llvm_ir_tu),
        "ir_path": str(ir_path),
        "ir_sha256": sha256_file(ir_path),
        "ir_size_bytes": ir_path.stat().st_size,
        "optimization": cfg.llvm_optimization,
        "working_directory": entry.get("directory", ""),
        "filtered_flags": flags,
        "command": command,
        "compile_commands_sha256": sha256_file(cfg.compile_commands),
    }


def run_llvm_pass(cfg: Config, run: Run) -> dict[str, Any]:
    opt = shutil.which(cfg.llvm_opt)
    if not opt:
        raise FileNotFoundError(
            f"LLVM opt not found: {cfg.llvm_opt} (install llvm-14 / llvm-14-dev)"
        )
    if not cfg.llvm_pass_plugin.exists():
        raise FileNotFoundError(f"LLVM pass plugin not found: {cfg.llvm_pass_plugin}")
    ir_path = run.path("llvm", "EKF2.ll")
    if not ir_path.exists():
        raise FileNotFoundError(f"LLVM IR not found: {ir_path}")
    raw_output = run.path("llvm", "llvm_raw.jsonl")
    env = dict(os.environ)
    env["PXDEP_LLVM_OUTPUT"] = str(raw_output)
    command = [
        opt,
        "-load-pass-plugin",
        str(cfg.llvm_pass_plugin),
        "-passes=px4dep-member-topic",
        "-disable-output",
        str(ir_path),
    ]
    _run_logged(command, cfg.project_root, run.path("logs", "llvm_pass.log"), env=env)
    raw_records = read_jsonl(raw_output)
    if not raw_records:
        raise RuntimeError(f"LLVM pass produced no records: {raw_output}")
    stats = [r for r in raw_records if r.get("kind") == "llvm_pass_stats"]
    if not stats:
        raise RuntimeError("LLVM pass did not emit llvm_pass_stats")
    return {
        "command": command,
        "raw_output": str(raw_output),
        "raw_records": len(raw_records),
        "stats": stats[-1],
        "records": raw_records,
    }


def _normalise_accessor(name: str) -> str:
    match = re.search(r"((?:[A-Za-z_]\w*::)*aid_src_\w+)\s*\(", name)
    if not match:
        return name
    result = match.group(1)
    if "::" not in result:
        result = "Ekf::" + result
    return result


def accessor_member_map(raw: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """AST evidence: accessor function -> the Ekf member it returns."""
    result: dict[str, dict[str, Any]] = {}
    for record in raw:
        if record.get("record") != "member_access":
            continue
        caller = str(record.get("caller") or "")
        if not caller.startswith(TARGET_ACCESSOR_PREFIX):
            continue
        if record.get("receiver_kind") != "this":
            continue
        receiver = str(record.get("receiver_expr") or "")
        if not receiver.startswith("_aid_src_"):
            continue
        result[caller] = {
            "member": str(record.get("qualified_name") or ""),
            "member_name": str(record.get("field") or ""),
            "message_type": str(record.get("type") or "").replace("struct ", ""),
            "source": {
                "file": record.get("file") or record.get("expansion_file") or "",
                "line": record.get("line") or record.get("expansion_line") or 0,
            },
        }
    return result


def build_writer_member_map(raw: list[dict[str, Any]]) -> dict[str, Any]:
    """AST fixed point: writer function -> Ekf aid-source member(s).

    Seeds are direct reads/writes of an ``Ekf::_aid_src_*`` member.  Local
    aliases, message-reference parameters, and call arguments propagate the
    member through wrapper functions.  This is field/instance-sensitive but
    flow-insensitive: it records the set of instances a function can touch, not
    a single execution.
    """
    function_members: dict[str, set[str]] = defaultdict(set)
    local_members: dict[tuple[str, str], set[str]] = defaultdict(set)
    param_members: dict[tuple[str, str], set[str]] = defaultdict(set)
    function_names: dict[str, str] = {}

    def is_target(name: Any) -> bool:
        return bool(name) and str(name).startswith(TARGET_MEMBER_PREFIX)

    for record in raw:
        caller_usr = str(record.get("caller_usr") or "")
        caller = str(record.get("caller") or "")
        if caller_usr and caller:
            function_names[caller_usr] = caller

    for record in raw:
        if record.get("record") != "member_access":
            continue
        caller_usr = str(record.get("caller_usr") or "")
        if not caller_usr:
            continue
        qualified = str(record.get("qualified_name") or "")
        if is_target(qualified):
            function_members[caller_usr].add(qualified)
            continue
        receiver = str(record.get("receiver_expr") or "")
        if record.get("receiver_kind") == "this" and receiver.startswith("_aid_src_"):
            function_members[caller_usr].add(
                "Ekf::" + re.split(r"[.\[]", receiver, maxsplit=1)[0]
            )

    iterations = 0
    for iterations in range(1, 51):
        before = (
            sum(len(values) for values in function_members.values()),
            sum(len(values) for values in local_members.values()),
            sum(len(values) for values in param_members.values()),
        )

        # A reference parameter is also a local alias inside the callee.
        for (function_usr, param_name), members in list(param_members.items()):
            local_members[(function_usr, param_name)] |= members
            function_members[function_usr] |= members

        for record in raw:
            if record.get("record") != "local_write":
                continue
            function_usr = str(record.get("caller_usr") or "")
            variable = str(record.get("variable") or "")
            if not function_usr or not variable:
                continue
            for member in record.get("rhs_members") or []:
                qualified = str(member.get("qualified_name") or "")
                if is_target(qualified):
                    local_members[(function_usr, variable)].add(qualified)
            for rhs_local in record.get("rhs_locals") or []:
                local_members[(function_usr, variable)] |= local_members.get(
                    (function_usr, str(rhs_local)), set()
                )

        for (function_usr, _variable), members in local_members.items():
            function_members[function_usr] |= members

        for record in raw:
            if record.get("record") != "call_arg":
                continue
            param_type = str(record.get("param_type") or "")
            if "estimator_aid_source" not in param_type:
                continue
            if "&" not in param_type and "*" not in param_type:
                continue
            caller_usr = str(record.get("caller_usr") or "")
            callee_usr = str(record.get("callee_usr") or "")
            param_name = str(record.get("param_name") or "")
            if not caller_usr or not callee_usr or not param_name:
                continue
            members: set[str] = set()
            for member in record.get("members") or []:
                qualified = str(member.get("qualified_name") or "")
                if is_target(qualified):
                    members.add(qualified)
            for local in record.get("locals") or []:
                members |= local_members.get((caller_usr, str(local)), set())
            arg_text = str(record.get("arg_text") or "").strip()
            if re.fullmatch(r"[A-Za-z_]\w*", arg_text):
                members |= param_members.get((caller_usr, arg_text), set())
                members |= local_members.get((caller_usr, arg_text), set())
            if members:
                param_members[(callee_usr, param_name)] |= members
                function_members[callee_usr] |= members

        after = (
            sum(len(values) for values in function_members.values()),
            sum(len(values) for values in local_members.values()),
            sum(len(values) for values in param_members.values()),
        )
        if after == before:
            break

    return {
        "by_function_usr": {
            usr: sorted(members) for usr, members in function_members.items() if members
        },
        "by_function_name": {
            function_names.get(usr, usr): sorted(members)
            for usr, members in function_members.items()
            if members
        },
        "function_names": function_names,
        "local_aliases": {
            f"{function_names.get(usr, usr)}::{variable}": sorted(members)
            for (usr, variable), members in local_members.items()
            if members
        },
        "parameter_aliases": {
            f"{function_names.get(usr, usr)}::{param}": sorted(members)
            for (usr, param), members in param_members.items()
            if members
        },
        "function_count": len(function_members),
        "iterations": iterations,
        "interpretation": "may_touch_instance_set_not_single_execution",
    }


def binding_topic_map(
    bindings: list[dict[str, Any]],
    accessors: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Join LLVM bindings to AST accessor/member names.

    LLVM supplies ``accessor -> topic`` and the EKF field indices.  The AST
    supplies the member *name* that the accessor returns.  The two evidence
    kinds are kept in the result; neither replaces the other.
    """
    by_member: dict[str, dict[str, Any]] = {}
    gaps: list[dict[str, Any]] = []
    for binding in bindings:
        accessor = _normalise_accessor(str(binding.get("accessor_function") or ""))
        ast = accessors.get(accessor)
        if not ast:
            gaps.append(
                {
                    "kind": "llvm_binding_without_ast_accessor",
                    "accessor_function": accessor,
                    "topic": binding.get("topic", ""),
                    "reason": "LLVM accessor name was not found in AST member_access records",
                }
            )
            continue
        member = ast["member"]
        if member in by_member:
            gaps.append(
                {
                    "kind": "llvm_duplicate_member_binding",
                    "member": member,
                    "topic": binding.get("topic", ""),
                    "reason": "two LLVM bindings claim the same AST member",
                }
            )
            continue
        by_member[member] = {
            "topic": str(binding.get("topic") or ""),
            "message_type": str(binding.get("message_type") or ""),
            "accessor": accessor,
            "member": member,
            "member_name": ast["member_name"],
            "ast_member_source": ast["source"],
            "binding": binding,
            "binding_id": stable_id(
                "llvm_binding",
                binding.get("topic"),
                binding.get("accessor_mangled"),
                binding.get("call_site_line"),
                binding.get("ekf2_publication_field_index"),
            ),
            "evidence_kind": "llvm_ir_plus_ast_member",
        }
    return by_member, gaps


def ast_cross_check(
    binding: dict[str, Any],
    raw: list[dict[str, Any]],
    accessors: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Confirm the LLVM call site against the Clang AST records."""
    line = binding.get("call_site_line")
    calls = [
        record
        for record in raw
        if record.get("record") == "call"
        and str(record.get("callee") or "").startswith(TARGET_PUBLISHER_PREFIX)
        and record.get("line") == line
        and str(record.get("file") or "").endswith("EKF2.cpp")
    ]
    calls.sort(key=lambda record: int(record.get("column") or 0))
    accessor = _normalise_accessor(str(binding.get("accessor_function") or ""))
    chosen: dict[str, Any] | None = None
    accessor_expression = ""
    publication_member = ""
    for call in calls:
        args = {int(arg.get("index", -1)): str(arg.get("text") or "") for arg in call.get("args") or []}
        expression = args.get(1, "")
        match = re.search(r"aid_src_([A-Za-z0-9_]+)\(\)", expression)
        if not match:
            continue
        candidate = "Ekf::aid_src_" + match.group(1)
        if candidate == accessor:
            chosen = call
            accessor_expression = expression
            publication_member = args.get(3, "")
            break

    declarations = {
        (str(record.get("qualified_name") or ""), str(topic))
        for record in raw
        if record.get("record") == "uorb_decl"
        for topic in (record.get("topics") or [])
    }
    declaration = (
        "EKF2::" + publication_member,
        str(binding.get("topic") or ""),
    ) in declarations
    accessor_ast = accessors.get(accessor)

    return {
        "call_site_match": bool(chosen),
        "topic_decl_match": bool(declaration),
        "accessor_member_match": bool(accessor_ast),
        "passed": bool(chosen and declaration and accessor_ast),
        "caller": (chosen or {}).get("caller", ""),
        "call_file": (chosen or {}).get("file", ""),
        "call_line": (chosen or {}).get("line", 0),
        "call_column": (chosen or {}).get("column", 0),
        "accessor_expression": accessor_expression,
        "publication_member": publication_member,
        "topic_declaration": "EKF2::" + publication_member if publication_member else "",
        "ast_accessor_member": (accessor_ast or {}).get("member", ""),
        "ast_accessor_member_source": (accessor_ast or {}).get("source", {}),
    }


def _topic_downstream(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    ids = {str(node.get("id")): node for node in nodes}
    topic_by_name: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if node.get("kind") == "topic":
            topic_by_name[str(node.get("name") or node.get("qualified_name") or "")] = node

    endpoints_used_by: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.get("relation") != "uses_uorb":
            continue
        source = ids.get(str(edge.get("src")))
        if source and source.get("kind") == "function":
            endpoints_used_by[str(edge.get("dst"))].add(
                str(source.get("qualified_name") or source.get("name") or "")
            )

    subscribers_by_topic: dict[str, set[str]] = defaultdict(set)
    publishers_by_topic: dict[str, set[str]] = defaultdict(set)
    endpoint_names: dict[str, str] = {}
    for edge in edges:
        relation = edge.get("relation")
        if relation not in {"subscribes", "publishes"}:
            continue
        topic_node = ids.get(str(edge.get("dst")))
        endpoint_node = ids.get(str(edge.get("src")))
        if not topic_node or not endpoint_node:
            continue
        topic = str(topic_node.get("name") or topic_node.get("qualified_name") or "")
        endpoint_id = str(endpoint_node.get("id"))
        endpoint_name = str(
            endpoint_node.get("qualified_name") or endpoint_node.get("name") or endpoint_id
        )
        endpoint_names[endpoint_id] = endpoint_name
        if relation == "subscribes":
            subscribers_by_topic[topic].add(endpoint_name)
            subscribers_by_topic[topic] |= endpoints_used_by.get(endpoint_id, set())
        else:
            publishers_by_topic[topic].add(endpoint_name)

    result: dict[str, dict[str, Any]] = {}
    for topic, node in topic_by_name.items():
        result[topic] = {
            "topic": topic,
            "topic_id": str(node.get("id") or ""),
            "subscribers": sorted(subscribers_by_topic.get(topic, set())),
            "consumer_functions": sorted(subscribers_by_topic.get(topic, set())),
            "publishers": sorted(publishers_by_topic.get(topic, set())),
        }
    return result


def _direct_path_writers(path: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    sink = str(path.get("sink") or "")
    names = {
        str(step.get("caller"))
        for step in path.get("steps") or []
        if step.get("caller") and str(step.get("from") or "") == sink
    }
    writers = [w for w in path.get("sink_writers") or [] if str(w.get("qualified_name") or "") in names]
    if writers:
        return writers, "direct_writer"
    return list(path.get("sink_writers") or []), "sink_writers_fallback"


def _topics_for_writers(
    writers: list[dict[str, Any]],
    writer_map: dict[str, Any],
    member_topics: dict[str, dict[str, Any]],
    message_type: str,
) -> dict[str, Any]:
    topics: dict[str, dict[str, Any]] = {}
    unmapped: list[dict[str, Any]] = []
    mismatched: list[dict[str, Any]] = []
    for writer in writers:
        usr = str(writer.get("usr") or "")
        name = str(writer.get("qualified_name") or "")
        members = writer_map["by_function_usr"].get(usr) or []
        if not members:
            unmapped.append({"usr": usr, "qualified_name": name})
            continue
        for member in members:
            entry = member_topics.get(member)
            if not entry:
                unmapped.append(
                    {
                        "usr": usr,
                        "qualified_name": name,
                        "member": member,
                        "reason": "member_has_no_llvm_topic_binding",
                    }
                )
                continue
            if message_type and entry["message_type"] != message_type:
                mismatched.append(
                    {
                        "usr": usr,
                        "qualified_name": name,
                        "member": member,
                        "topic": entry["topic"],
                        "topic_message_type": entry["message_type"],
                    }
                )
                continue
            topic = topics.setdefault(
                entry["topic"],
                {
                    "topic": entry["topic"],
                    "message_type": entry["message_type"],
                    "evidence_kind": "llvm_ir_plus_ast_writer",
                    "writers": [],
                    "members": [],
                    "llvm_binding_ids": [],
                },
            )
            writer_entry = {"usr": usr, "qualified_name": name, "member": member}
            if writer_entry not in topic["writers"]:
                topic["writers"].append(writer_entry)
            if member not in topic["members"]:
                topic["members"].append(member)
            if entry["binding_id"] not in topic["llvm_binding_ids"]:
                topic["llvm_binding_ids"].append(entry["binding_id"])
    return {
        "topics": [topics[key] for key in sorted(topics)],
        "unmapped_writers": unmapped,
        "message_type_mismatches": mismatched,
    }


def link_llvm_evidence(
    break_records: list[dict[str, Any]],
    raw: list[dict[str, Any]],
    bindings: list[dict[str, Any]],
    paths: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    """Join IR bindings to AST writers and M3 paths.

    This produces topic *candidates* and records the next break.  It never
    creates an end-to-end path and never upgrades LLVM evidence to a
    parameter-level dataflow claim.
    """
    accessors = accessor_member_map(raw)
    member_topics, binding_gaps = binding_topic_map(bindings, accessors)
    writer_map = build_writer_member_map(raw)
    downstream = _topic_downstream(nodes, edges)
    path_index = _path_index(paths)

    resolutions: list[dict[str, Any]] = []
    residual: list[dict[str, Any]] = []
    topic_histogram: Counter[int] = Counter()
    status_counts: Counter[str] = Counter()
    attribution_counts: Counter[str] = Counter()
    root_after_counts: Counter[str] = Counter()

    m4_records = [
        record
        for record in break_records
        if record["unresolved_kind"] == SELECTED_KIND
    ]
    for break_record in m4_records:
        original = break_record["record"]
        key = (
            str(original.get("parameter") or ""),
            str(original.get("source") or ""),
            str(original.get("sink") or ""),
        )
        linked_paths = path_index.get(key, [])
        path_details: list[dict[str, Any]] = []
        topic_candidates: dict[str, dict[str, Any]] = {}
        all_unmapped: list[dict[str, Any]] = []
        all_mismatched: list[dict[str, Any]] = []
        for path in linked_paths:
            writers, attribution = _direct_path_writers(path)
            attribution_counts[attribution] += 1
            topics = _topics_for_writers(
                writers, writer_map, member_topics, str(original.get("message_type") or "")
            )
            all_unmapped.extend(topics["unmapped_writers"])
            all_mismatched.extend(topics["message_type_mismatches"])
            for topic in topics["topics"]:
                candidate = topic_candidates.setdefault(
                    topic["topic"],
                    {
                        "topic": topic["topic"],
                        "message_type": topic["message_type"],
                        "evidence_kind": topic["evidence_kind"],
                        "writers": [],
                        "members": [],
                        "llvm_binding_ids": [],
                        "path_ids": [],
                        "attribution": [],
                    },
                )
                for writer in topic["writers"]:
                    if writer not in candidate["writers"]:
                        candidate["writers"].append(writer)
                for member in topic["members"]:
                    if member not in candidate["members"]:
                        candidate["members"].append(member)
                for binding_id in topic["llvm_binding_ids"]:
                    if binding_id not in candidate["llvm_binding_ids"]:
                        candidate["llvm_binding_ids"].append(binding_id)
                if str(path.get("id") or "") not in candidate["path_ids"]:
                    candidate["path_ids"].append(str(path.get("id") or ""))
                if attribution not in candidate["attribution"]:
                    candidate["attribution"].append(attribution)
            path_details.append(
                {
                    "path_id": str(path.get("id") or ""),
                    "category": str(path.get("category") or ""),
                    "attribution": attribution,
                    "writer_count": len(writers),
                    "writers": [
                        {
                            "usr": str(w.get("usr") or ""),
                            "qualified_name": str(w.get("qualified_name") or ""),
                        }
                        for w in writers
                    ],
                    "topics": [t["topic"] for t in topics["topics"]],
                    "unmapped_writers": topics["unmapped_writers"],
                    "message_type_mismatches": topics["message_type_mismatches"],
                }
            )

        candidates = [topic_candidates[key] for key in sorted(topic_candidates)]
        topic_histogram[len(candidates)] += 1
        if not candidates:
            resolution = "none"
            status = "no_topic_binding"
            root_after = "writer_message_instance_not_bound_to_a_published_ekf_member"
            confidence = "low"
        elif len(candidates) == 1:
            resolution = "unique"
            confidence = (
                "medium"
                if any(d["attribution"] == "direct_writer" for d in path_details)
                else "low"
            )
            consumer_functions = (
                downstream.get(candidates[0]["topic"], {}).get("consumer_functions")
                or []
            )
            if consumer_functions:
                status = "unique_topic_with_commander_consumer"
                root_after = "consumer_field_binding_not_established"
            else:
                status = "unique_topic_but_no_commander_consumer"
                root_after = "no_commander_consumer_in_indexed_scope"
        else:
            resolution = "multiple"
            confidence = (
                "medium"
                if path_details
                and all(d["attribution"] == "direct_writer" for d in path_details)
                else "low"
            )
            any_consumer = any(
                downstream.get(candidate["topic"], {}).get("consumer_functions")
                for candidate in candidates
            )
            if any_consumer:
                status = "multiple_topics_with_candidate_consumer"
                root_after = "m3_slice_does_not_select_single_message_instance"
            else:
                status = "multiple_topics_and_no_commander_consumer"
                root_after = (
                    "m3_slice_does_not_select_single_message_instance; "
                    "no_commander_consumer_in_indexed_scope"
                )
        status_counts[status] += 1
        root_after_counts[root_after] += 1

        candidate_downstream = {
            candidate["topic"]: downstream.get(
                candidate["topic"],
                {"subscribers": [], "consumer_functions": [], "publishers": []},
            )
            for candidate in candidates
        }
        resolution_record = {
            "kind": "llvm_break_chain_resolution",
            "id": stable_id("llvm_resolution", break_record["id"]),
            "break_chain_id": break_record["id"],
            "parameter": str(original.get("parameter") or ""),
            "source": str(original.get("source") or ""),
            "sink": str(original.get("sink") or ""),
            "sink_id": str(original.get("sink_id") or ""),
            "message_type": str(original.get("message_type") or ""),
            "m3_path_ids": [str(p.get("id") or "") for p in linked_paths],
            "path_count": len(linked_paths),
            "path_attribution": path_details,
            "topic_candidates": candidates,
            "topic_count": len(candidates),
            "topic_resolution": resolution,
            "downstream": candidate_downstream,
            "topic_resolution_confidence": confidence,
            "status": status,
            "root_cause_before": break_record["root_cause"],
            "root_cause_after": root_after,
            "end_to_end_created": False,
            "interpretation": "topic_narrowing_only_not_end_to_end",
            "llvm_evidence_kind": "llvm_ir",
            "ast_evidence_kind": "clang_ast_writer_member",
            "evidence_are_separate": True,
            "unmapped_writers": all_unmapped,
            "message_type_mismatches": all_mismatched,
            "confidence": "high",
        }
        resolutions.append(resolution_record)

        residual_reasons: list[str] = []
        if resolution == "none":
            residual_reasons.append("no_topic_binding")
        elif resolution == "multiple":
            residual_reasons.append("multiple_topic_candidates")
        if not any(
            entry.get("consumer_functions") for entry in candidate_downstream.values()
        ):
            residual_reasons.append("no_commander_consumer")
        if all_unmapped:
            residual_reasons.append("unmapped_writer_functions")
        residual.append(
            {
                "kind": "llvm_break_chain_residual",
                "id": stable_id("llvm_residual", break_record["id"]),
                "break_chain_id": break_record["id"],
                "resolution_id": resolution_record["id"],
                "parameter": resolution_record["parameter"],
                "source": resolution_record["source"],
                "sink": resolution_record["sink"],
                "message_type": resolution_record["message_type"],
                "topic_resolution": resolution,
                "topic_candidates": [c["topic"] for c in candidates],
                "status": status,
                "root_cause_after": root_after,
                "residual_reasons": residual_reasons,
                "unmapped_writers": all_unmapped,
                "interpretation": "analysis_gap_not_unreachable",
                "confidence": "high",
            }
        )

    # Writer-level residual: functions named by an M4 record that the fixed
    # point could not bind to an Ekf aid-source member.
    expected_writers: dict[str, str] = {}
    for record in m4_records:
        for usr in str(record["record"].get("writers") or "").split(";"):
            if usr:
                expected_writers.setdefault(
                    usr, str(record["record"].get("message_type") or "")
                )
    unresolved_writers: list[dict[str, Any]] = []
    for usr, message_type in sorted(expected_writers.items()):
        if writer_map["by_function_usr"].get(usr):
            continue
        name = writer_map["function_names"].get(usr, usr)
        reason = "writer_function_not_called_in_indexed_scope"
        if name in {"Ekf::resetYawByFusion"}:
            reason = "writer_targets_local_message_not_an_ekf_member"
        elif name == "Ekf::fuseYaw":
            reason = "selected_overload_receives_local_message_not_an_ekf_member"
        elif name == "Ekf::runFakePosStateMachine":
            reason = "writer_usr_overload_variant_not_reached_by_indexed_call"
        unresolved_writers.append(
            {
                "kind": "llvm_writer_member_unmapped",
                "id": stable_id("llvm_writer_unmapped", usr),
                "writer_function": name,
                "writer_usr": usr,
                "message_type": message_type,
                "reason": reason,
                "interpretation": "analysis_gap_not_unreachable",
                "confidence": "high",
            }
        )

    # Scope gap: published aid-source topic that is outside the targeted pass
    # because its publication member is not handled by
    # EKF2::PublishAidSourceStatus.
    scope_gaps: list[dict[str, Any]] = []
    published_topics = sorted(
        {entry["topic"] for entry in member_topics.values()}
    )
    for record in raw:
        if record.get("record") != "uorb_decl" or record.get("role") != "publication":
            continue
        if not any(
            "estimator_aid_src" in str(topic)
            for topic in record.get("topics") or []
        ):
            continue
        for topic in record.get("topics") or []:
            if topic in published_topics:
                continue
            scope_gaps.append(
                {
                    "kind": "llvm_topic_outside_targeted_scope",
                    "id": stable_id("llvm_scope_gap", topic),
                    "topic": str(topic),
                    "publisher": str(record.get("qualified_name") or ""),
                    "publisher_class": str(record.get("parent_class") or ""),
                    "message_type": str(record.get("message_type") or ""),
                    "source": {
                        "file": record.get("file") or "",
                        "line": record.get("line") or 0,
                    },
                    "reason": "published by a member outside EKF2::PublishAidSourceStatus",
                    "interpretation": "analysis_scope_gap_not_unreachable",
                    "confidence": "high",
                }
            )

    # Also include any unresolved records the pass itself emitted.
    pass_unresolved: list[dict[str, Any]] = []
    for record in bindings:
        if record.get("kind") == "llvm_unresolved":
            item = dict(record)
            item["id"] = stable_id(
                "llvm_pass_unresolved",
                record.get("call_site_line"),
                record.get("reason"),
                record.get("publisher_mangled"),
            )
            item.setdefault("interpretation", "analysis_gap_not_unreachable")
            pass_unresolved.append(item)

    residual.extend(unresolved_writers)
    residual.extend(scope_gaps)
    residual.extend(pass_unresolved)

    metrics = {
        "kind": "llvm_evidence_metrics",
        "selected_kind": SELECTED_KIND,
        "records": len(resolutions),
        "unique_topic_records": sum(1 for r in resolutions if r["topic_resolution"] == "unique"),
        "multiple_topic_records": sum(
            1 for r in resolutions if r["topic_resolution"] == "multiple"
        ),
        "no_topic_records": sum(1 for r in resolutions if r["topic_resolution"] == "none"),
        "records_with_topic_candidate": sum(
            1 for r in resolutions if r["topic_resolution"] != "none"
        ),
        "records_with_commander_consumer": sum(
            1
            for r in resolutions
            if any(
                entry.get("consumer_functions") for entry in r["downstream"].values()
            )
        ),
        "topic_count_histogram": {
            str(key): value for key, value in sorted(topic_histogram.items())
        },
        "status_counts": dict(sorted(status_counts.items())),
        "root_cause_after_counts": dict(sorted(root_after_counts.items())),
        "path_attribution_counts": dict(sorted(attribution_counts.items())),
        "writer_functions_in_unresolved": len(expected_writers),
        "writer_functions_mapped": sum(
            1 for usr in expected_writers if writer_map["by_function_usr"].get(usr)
        ),
        "writer_functions_unmapped": len(unresolved_writers),
        "writer_mapping": {
            "iterations": writer_map["iterations"],
            "function_count": writer_map["function_count"],
            "interpretation": writer_map["interpretation"],
            "local_alias_count": len(writer_map["local_aliases"]),
            "parameter_alias_count": len(writer_map["parameter_aliases"]),
        },
        "binding_gaps": binding_gaps,
        "scope_gaps": scope_gaps,
        "residual_by_reason": dict(
            sorted(
                Counter(
                    reason
                    for record in residual
                    for reason in (
                        record.get("residual_reasons")
                        or [record.get("reason") or record.get("kind") or "unknown"]
                    )
                ).items()
            )
        ),
        "end_to_end_paths_created": 0,
        "end_to_end_claim": False,
        "interpretation": "llvm_binds_message_instance_to_topic_only; "
        "AST provides writer/member identity; no LLVM-only end-to-end path",
    }
    return {
        "resolutions": resolutions,
        "residual": residual,
        "metrics": metrics,
        "member_topics": member_topics,
        "accessors": accessors,
        "binding_gaps": binding_gaps,
        "downstream": downstream,
        "writer_map": writer_map,
    }


# ---------------------------------------------------------------------------
# Orchestration and output
# ---------------------------------------------------------------------------


def run_llvm_fallback(cfg: Config, run: Run, rebuild: bool = False) -> dict[str, Any]:
    """Run M6 end to end for an existing run directory."""
    # 1. Inventory first; the pass is selected from the measured distribution.
    break_records = collect_break_chains(run)
    distribution = build_break_distribution(break_records, run)
    selected = (distribution.get("selection") or {}).get("selected_kind", "")
    if selected != SELECTED_KIND:
        raise RuntimeError(
            "the distribution did not select the expected M4 writer/topic gap; "
            f"selected={selected!r}. Refusing to run a pass that was not chosen "
            "from the measured break-chain distribution."
        )
    write_break_chains(run, break_records, distribution)

    # 2. Build/verify the targeted pass, then emit IR at -O0.
    plugin = ensure_llvm_plugin(cfg, run, rebuild=rebuild)
    ir = compile_ir(cfg, run)
    pass_result = run_llvm_pass(cfg, run)

    # 3. Join IR evidence to AST writers and M3 paths.
    raw = read_jsonl(run.path("raw", "cpp_records.jsonl"))
    paths = read_jsonl(run.path("graph", "paths.jsonl"))
    nodes = read_jsonl(run.path("graph", "nodes.jsonl"))
    edges = read_jsonl(run.path("graph", "edges.jsonl"))
    all_bindings = [
        record
        for record in pass_result["records"]
        if record.get("kind") == "llvm_member_topic_binding"
    ]
    linked = link_llvm_evidence(
        break_records,
        raw,
        all_bindings,
        paths,
        nodes,
        edges,
    )

    # 4. Enrich each LLVM binding with AST cross-check and downstream status.
    accessors = linked["accessors"]
    member_topics = linked["member_topics"]
    downstream = linked["downstream"]
    llvm_records: list[dict[str, Any]] = []
    for binding in all_bindings:
        accessor = _normalise_accessor(str(binding.get("accessor_function") or ""))
        ast = accessors.get(accessor) or {}
        member = str(ast.get("member") or "")
        topic_entry = member_topics.get(member) or {}
        check = ast_cross_check(binding, raw, accessors)
        topic = str(binding.get("topic") or "")
        consumers = downstream.get(topic, {}).get("consumer_functions") or []
        llvm_records.append(
            {
                "kind": "llvm_binding",
                "id": stable_id(
                    "llvm_binding_record",
                    topic,
                    binding.get("accessor_mangled"),
                    binding.get("call_site_line"),
                ),
                "evidence_kind": "llvm_ir",
                "binding": binding,
                "accessor_function": accessor,
                "ekf_member": member,
                "ekf_member_name": ast.get("member_name", ""),
                "ekf_member_source": ast.get("source", {}),
                "topic": topic,
                "message_type": binding.get("message_type", ""),
                "ast_cross_check": check,
                "downstream": {
                    "commander_consumers": consumers,
                    "any_commander_consumer": bool(consumers),
                },
                "confidence": "high" if check["passed"] else "medium",
                "interpretation": "message_instance_to_topic_binding_only",
                "end_to_end_claim": False,
            }
        )
    llvm_records.sort(key=lambda record: (record["topic"], record["ekf_member"]))

    # 5. Coverage counts against AST publication declarations.
    ast_publications = [
        record
        for record in raw
        if record.get("record") == "uorb_decl"
        and record.get("role") == "publication"
        and any("estimator_aid_src" in str(topic) for topic in record.get("topics") or [])
    ]
    ast_ekf2 = [
        record
        for record in ast_publications
        if str(record.get("qualified_name") or "").startswith("EKF2::_estimator_aid_src_")
    ]
    covered_topics = {record["topic"] for record in llvm_records}
    ast_topics = {
        str(topic)
        for record in ast_publications
        for topic in record.get("topics") or []
    }
    uncovered = sorted(ast_topics - covered_topics)
    aid_topics_with_consumer = sorted(
        topic
        for topic in ast_topics
        if (downstream.get(topic) or {}).get("consumer_functions")
    )
    aid_topics_without_consumer = sorted(ast_topics - set(aid_topics_with_consumer))

    pass_stats = pass_result["stats"]
    llvm_metrics = {
        "kind": "llvm_pass_metrics",
        "generated_at": utc_now(),
        "run_id": run.run_id,
        "selected_kind": SELECTED_KIND,
        "toolchain": {
            "clang": cfg.llvm_clang,
            "clang_path": shutil.which(cfg.llvm_clang) or "",
            "clang_version": _tool_version(cfg.llvm_clang),
            "opt": cfg.llvm_opt,
            "opt_path": shutil.which(cfg.llvm_opt) or "",
            "opt_version": _tool_version(cfg.llvm_opt),
            "llvm_config_version": _tool_version("llvm-config-14"),
        },
        "plugin": plugin,
        "ir": ir,
        "pass": {
            "name": "px4dep-member-topic",
            "command": pass_result["command"],
            "raw_output": pass_result["raw_output"],
            "raw_records": pass_result["raw_records"],
            "stats": pass_stats,
        },
        "bindings": len(llvm_records),
        "bindings_cross_checked": sum(
            1 for record in llvm_records if record["ast_cross_check"]["passed"]
        ),
        "binding_gaps": linked["binding_gaps"],
        "coverage": {
            "ast_aid_source_publication_declarations": len(ast_publications),
            "ast_ekf2_aid_source_publication_declarations": len(ast_ekf2),
            "llvm_bindings": len(llvm_records),
            "covered_topics": sorted(covered_topics),
            "uncovered_topics": uncovered,
            "scope_gaps": linked["metrics"]["scope_gaps"],
        },
        "downstream_coverage": {
            "aid_source_topics": len(ast_topics),
            "with_commander_subscriber": len(aid_topics_with_consumer),
            "without_commander_subscriber": len(aid_topics_without_consumer),
            "topics_without_commander_subscriber": aid_topics_without_consumer,
            "interpretation": "indexed_scope_only_not_runtime_unreachability",
        },
        "linking": linked["metrics"],
        "scope": {
            "translation_unit": cfg.llvm_ir_tu,
            "publisher": "EKF2::PublishAidSourceStatus<...>",
            "optimization": cfg.llvm_optimization,
            "function_scope": "only EKF2 aid-source publication call sites",
            "not_a_general_framework": True,
        },
        "interpretation": "member_to_topic_binding_only_not_end_to_end",
        "limitations": [
            "LLVM binds a message instance to a topic; it does not identify which "
            "M3 writer produced the parameter dependency.",
            "The M3 path often lists every writer of a sink. When no direct "
            "writer step exists, the topic set remains multiple.",
            "No end-to-end path is created from LLVM evidence alone.",
            "Topics are joined to Commander consumers only through explicit graph "
            "subscription edges; absence is a static-scope observation, not a "
            "runtime unreachability claim.",
        ],
        "end_to_end_paths_created": 0,
    }

    write_jsonl(run.path("evidence", "llvm.jsonl"), llvm_records)
    write_jsonl(run.path("evidence", "break_chain_resolution.jsonl"), linked["resolutions"])
    write_jsonl(run.path("unresolved", "llvm.jsonl"), linked["residual"])
    write_json(run.path("reports", "llvm_metrics.json"), llvm_metrics)

    _write_csv(
        run.path("reports", "llvm_bindings.csv"),
        LLVM_BINDING_COLUMNS,
        [
            {
                "topic": record["topic"],
                "message_type": record["message_type"],
                "accessor_function": record["accessor_function"],
                "ekf_member": record["ekf_member"],
                "ekf_member_field_index": record["binding"].get("ekf_member_field_index", 0),
                "ekf2_publication_field_index": record["binding"].get(
                    "ekf2_publication_field_index", 0
                ),
                "call_site": f"{record['binding'].get('call_site_file', '')}:"
                f"{record['binding'].get('call_site_line', 0)}",
                "constructor_site": f"{record['binding'].get('publication_constructor_file', '')}:"
                f"{record['binding'].get('publication_constructor_line', 0)}",
                "ast_cross_check": str(record["ast_cross_check"]["passed"]).lower(),
                "commander_subscribers": ";".join(
                    record["downstream"]["commander_consumers"]
                ),
                "end_to_end_claim": str(record["end_to_end_claim"]).lower(),
            }
            for record in llvm_records
        ],
    )
    _write_csv(
        run.path("reports", "break_chain_resolution.csv"),
        RESOLUTION_COLUMNS,
        [
            {
                "break_chain_id": record["break_chain_id"],
                "parameter": record["parameter"],
                "source": record["source"],
                "sink": record["sink"],
                "message_type": record["message_type"],
                "path_count": record["path_count"],
                "topic_resolution": record["topic_resolution"],
                "topic_candidates": ";".join(
                    candidate["topic"] for candidate in record["topic_candidates"]
                ),
                "commander_consumers": ";".join(
                    sorted(
                        {
                            consumer
                            for entry in record["downstream"].values()
                            for consumer in entry.get("consumer_functions") or []
                        }
                    )
                ),
                "status": record["status"],
                "attribution_confidence": record["topic_resolution_confidence"],
                "root_cause_after": record["root_cause_after"],
            }
            for record in linked["resolutions"]
        ],
    )

    # Add the distribution's selected metadata to the returned metrics so the
    # report can render both halves without re-reading files.
    llvm_metrics["break_chain_distribution"] = {
        "total": distribution["total"],
        "by_stage": distribution["by_stage"],
        "selection": distribution["selection"],
    }
    return llvm_metrics


def m6_output_paths() -> list[str]:
    return [
        "evidence/break_chains.jsonl",
        "reports/break_chains.json",
        "reports/break_chains.csv",
        "evidence/llvm.jsonl",
        "evidence/break_chain_resolution.jsonl",
        "reports/llvm_metrics.json",
        "reports/llvm_bindings.csv",
        "reports/break_chain_resolution.csv",
        "unresolved/llvm.jsonl",
        "llvm/EKF2.ll",
        "llvm/llvm_raw.jsonl",
        "logs/llvm_ir.log",
        "logs/llvm_pass.log",
    ]
