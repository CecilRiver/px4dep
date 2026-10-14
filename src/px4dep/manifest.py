"""Run manifest and run-directory management.

The manifest records every input hash and the analyzer environment so a run can
be reproduced (or explicitly marked non-reproducible).
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import Config, PROJECT_VERSION
from .util import (
    read_json,
    repo_rel,
    sha256_file,
    sha256_files,
    utc_now,
    write_json,
)

REQUIRED_INPUTS = ("parameters_json", "compile_commands")
SUBDIRS = ("normalized", "raw", "graph", "evidence", "reports", "unresolved", "logs", "llvm")


@dataclass(frozen=True)
class Run:
    cfg: Config
    run_id: str
    root: Path

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.yaml"

    @property
    def manifest_json_path(self) -> Path:
        return self.root / "manifest.json"

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    def ensure_dirs(self) -> None:
        for name in SUBDIRS:
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def load_manifest(self) -> dict[str, Any]:
        if self.manifest_json_path.exists():
            return read_json(self.manifest_json_path, {})
        with self.manifest_path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def is_ready(self) -> bool:
        data = self.load_manifest()
        return data.get("status") in {"ok", "ok_with_warnings"}


def _git(cfg: Config, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cfg.repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _git_status(cfg: Config) -> list[str]:
    out = _git(cfg, "status", "--porcelain")
    return [line for line in out.splitlines() if line.strip()]


def _package_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return "not-installed"


def _compiler_version() -> str:
    for compiler in ("clang++-14", "clang++", "clang"):
        exe = shutil.which(compiler)
        if not exe:
            continue
        try:
            proc = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
            if proc.returncode == 0:
                return proc.stdout.splitlines()[0].strip()
        except (OSError, subprocess.SubprocessError):
            continue
    return "not-found"


def _artifact_time(path: Path) -> str:
    if not path.exists():
        return ""
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _clean_run_id(commit: str) -> str:
    stamp = utc_now().replace("-", "").replace(":", "").replace("T", "T").replace("Z", "Z")
    return f"{stamp}_{commit[:12] or 'unknown'}"


def build_manifest(cfg: Config) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []

    missing = []
    for name in REQUIRED_INPUTS:
        path = getattr(cfg, name)
        if not path.exists():
            missing.append({"input": name, "path": str(path)})
    if not cfg.parameters_xml.exists():
        warnings.append("parameters_xml_missing")
    if not cfg.msg_dir.exists():
        missing.append({"input": "msg_dir", "path": str(cfg.msg_dir)})
    if not cfg.clang_tool.exists():
        warnings.append("clang_tool_not_built_yet")
    if missing:
        errors.append("required_inputs_missing")

    commit = _git(cfg, "rev-parse", "HEAD")
    describe = _git(cfg, "describe", "--tags", "--always", "--dirty")
    dirty_lines = _git_status(cfg)
    dirty = bool(dirty_lines)

    inputs: dict[str, Any] = {
        "compile_commands": {
            "path": repo_rel(cfg.compile_commands, cfg.repo),
            "sha256": sha256_file(cfg.compile_commands) if cfg.compile_commands.exists() else "",
            "mtime": _artifact_time(cfg.compile_commands),
        },
        "parameters_json": {
            "path": repo_rel(cfg.parameters_json, cfg.repo),
            "sha256": sha256_file(cfg.parameters_json) if cfg.parameters_json.exists() else "",
            "mtime": _artifact_time(cfg.parameters_json),
        },
        "parameters_xml": {
            "path": repo_rel(cfg.parameters_xml, cfg.repo),
            "sha256": sha256_file(cfg.parameters_xml) if cfg.parameters_xml.exists() else "",
            "mtime": _artifact_time(cfg.parameters_xml),
        },
    }
    if cfg.events_json:
        inputs["events_json"] = {
            "path": repo_rel(cfg.events_json, cfg.repo),
            "sha256": sha256_file(cfg.events_json) if cfg.events_json.exists() else "",
            "mtime": _artifact_time(cfg.events_json),
        }

    if cfg.sink_catalog.exists():
        inputs["sink_catalog"] = {
            "path": repo_rel(cfg.sink_catalog, cfg.project_root),
            "sha256": sha256_file(cfg.sink_catalog),
            "mtime": _artifact_time(cfg.sink_catalog),
        }

    msg_files = cfg.msg_files
    inputs["msg_dir"] = {
        "path": repo_rel(cfg.msg_dir, cfg.repo),
        "dirs": [repo_rel(d, cfg.repo) for d in cfg.msg_dirs],
        "file_count": len(msg_files),
        "aggregate_sha256": sha256_files(msg_files, cfg.repo) if msg_files else "",
    }

    yaml_files = sorted({p for pattern in cfg.param_yaml_globs for p in cfg.repo.glob(pattern) if p.is_file()})
    inputs["parameter_yaml"] = {
        "file_count": len(yaml_files),
        "aggregate_sha256": sha256_files(yaml_files, cfg.repo) if yaml_files else "",
        "files": [repo_rel(p, cfg.repo) for p in yaml_files],
    }

    ulog_path = cfg.ulog_path
    if ulog_path:
        inputs["ulog"] = {
            "path": str(ulog_path),
            "sha256": sha256_file(ulog_path) if ulog_path.exists() else "",
            "mtime": _artifact_time(ulog_path),
            "size_bytes": ulog_path.stat().st_size if ulog_path.exists() else 0,
            "scenario_declared": cfg.ulog_scenario,
            "usage": "runtime reachability only; not causal evidence",
        }
        if not ulog_path.exists():
            warnings.append("ulog_missing")

    tus = []
    for tu in cfg.tus:
        path = (cfg.repo / tu).resolve()
        tus.append(
            {
                "path": tu,
                "exists": path.exists(),
                "sha256": sha256_file(path) if path.exists() else "",
            }
        )
    inputs["translation_units"] = tus

    llvm_plugin = cfg.llvm_pass_plugin
    llvm_build_script = cfg.llvm_build_script
    inputs["llvm"] = {
        "clang": cfg.llvm_clang,
        "opt": cfg.llvm_opt,
        "ir_tu": cfg.llvm_ir_tu,
        "optimization": cfg.llvm_optimization,
        "pass_plugin": {
            "path": str(llvm_plugin),
            "exists": llvm_plugin.exists(),
            "sha256": sha256_file(llvm_plugin) if llvm_plugin.exists() else "",
        },
        "build_script": {
            "path": str(llvm_build_script),
            "sha256": sha256_file(llvm_build_script)
            if llvm_build_script.exists()
            else "",
        },
        "scope": "targeted EKF aid-source member->topic pass; not a general LLVM framework",
    }
    if not llvm_plugin.exists():
        warnings.append("llvm_pass_plugin_not_built_yet")

    analyzer = {
        "name": "px4dep",
        "version": PROJECT_VERSION,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "packages": {
            "PyYAML": _package_version("PyYAML"),
            "jsonschema": _package_version("jsonschema"),
            "pyulog": _package_version("pyulog"),
        },
        "compiler": _compiler_version(),
        "clang_tool": {
            "path": str(cfg.clang_tool),
            "sha256": sha256_file(cfg.clang_tool) if cfg.clang_tool.exists() else "",
        },
    }

    status = "ok"
    if warnings:
        status = "ok_with_warnings"
    if errors:
        status = "non_reproducible"

    return {
        "run_id": "",
        "created_at": utc_now(),
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "project": {
            "name": "px4_config_dependency_experiment",
            "version": PROJECT_VERSION,
            "config": repo_rel(cfg.path, cfg.project_root),
        },
        "px4": {
            "repo": str(cfg.repo),
            "commit": commit,
            "describe": describe,
            "dirty": dirty,
            "dirty_paths": dirty_lines[:50],
            "build_variant": cfg.build_variant,
            "build_dir": repo_rel(cfg.build_dir, cfg.repo),
            "read_only": True,
        },
        "scope": {
            "dirs": cfg.scope_dirs,
            "translation_units": cfg.tus,
            "exclude_dirs": cfg.exclude_dirs,
            "description": "EKF2 and Commander estimation/health/failsafe chain (M1/M2) with EKF2 field-sensitive parameter slice (M3), explicit uORB cross-module paths (M4), normal-scenario ULog reachability plus graded review candidates (M5), and a break-chain inventory with one targeted LLVM pass for the selected unresolved type (M6)",
        },
        "inputs": inputs,
        "analyzer": analyzer,
        "missing_inputs": missing,
        "logs": [],
    }


def create_run(cfg: Config) -> Run:
    data = build_manifest(cfg)
    run_id = _clean_run_id(data["px4"]["commit"])
    data["run_id"] = run_id
    run = Run(cfg=cfg, run_id=run_id, root=cfg.runs_root / run_id)
    run.ensure_dirs()
    with run.manifest_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
    write_json(run.manifest_json_path, data)
    return run


def record_ulog_input(
    run: Run, ulog_path: Path, scenario: str, ver_sw: str = ""
) -> dict[str, Any]:
    """Record a ULog supplied via the CLI in the run manifest."""
    data = run.load_manifest()
    entry = {
        "path": str(ulog_path),
        "sha256": sha256_file(ulog_path) if ulog_path.exists() else "",
        "mtime": _artifact_time(ulog_path),
        "size_bytes": ulog_path.stat().st_size if ulog_path.exists() else 0,
        "scenario_declared": scenario,
        "ver_sw": ver_sw,
        "usage": "runtime reachability only; not causal evidence",
    }
    data.setdefault("inputs", {})["ulog"] = entry
    write_json(run.manifest_json_path, data)
    with run.manifest_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
    return entry


def resolve_run(cfg: Config, run_id: str | None) -> Run:
    if run_id and run_id != "latest":
        run = Run(cfg=cfg, run_id=run_id, root=cfg.runs_root / run_id)
        if not run.manifest_path.exists():
            raise FileNotFoundError(f"run not found: {run.root}")
        return run

    candidates = sorted(
        (p for p in cfg.runs_root.iterdir() if p.is_dir() and (p / "manifest.json").exists()),
        key=lambda p: (p / "manifest.json").stat().st_mtime,
        reverse=True,
    ) if cfg.runs_root.exists() else []
    if not candidates:
        raise FileNotFoundError(f"no runs under {cfg.runs_root}; run `px4dep manifest` first")
    chosen = candidates[0]
    return Run(cfg=cfg, run_id=chosen.name, root=chosen)
