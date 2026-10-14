"""Configuration loading for the PX4 dependency experiment."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml



PROJECT_VERSION = "0.1.0"


@dataclass(frozen=True)
class Config:
    path: Path
    project_root: Path
    raw: dict[str, Any]

    @property
    def repo(self) -> Path:
        return Path(self.raw["px4"]["repo"]).expanduser().resolve()

    @property
    def build_dir(self) -> Path:
        return (self.repo / self.raw["px4"]["build_dir"]).resolve()

    @property
    def build_variant(self) -> str:
        return self.raw["px4"]["build_variant"]

    @property
    def runs_root(self) -> Path:
        value = Path(self.raw.get("runs_root", "runs"))
        if value.is_absolute():
            return value
        return (self.project_root / value).resolve()

    @property
    def parameters_json(self) -> Path:
        return (self.repo / self.raw["px4"]["parameters_json"]).resolve()

    @property
    def parameters_xml(self) -> Path:
        return (self.repo / self.raw["px4"]["parameters_xml"]).resolve()

    @property
    def compile_commands(self) -> Path:
        return (self.repo / self.raw["px4"]["compile_commands"]).resolve()

    @property
    def events_json(self) -> Path | None:
        value = self.raw["px4"].get("events_json")
        return (self.repo / value).resolve() if value else None

    @property
    def msg_dir(self) -> Path:
        return (self.repo / self.raw["px4"]["msg_dir"]).resolve()

    @property
    def msg_dirs(self) -> list[Path]:
        dirs = self.raw["px4"].get("msg_dirs")
        if not dirs:
            return [self.msg_dir]
        return [(self.repo / d).resolve() for d in dirs]

    @property
    def msg_files(self) -> list[Path]:
        files: list[Path] = []
        for directory in self.msg_dirs:
            files.extend(sorted(directory.glob("*.msg")))
        return sorted(set(files))

    @property
    def scope_dirs(self) -> list[str]:
        return list(self.raw["px4"].get("scope_dirs", []))

    @property
    def tus(self) -> list[str]:
        """Configured translation units, expanding repository-relative globs."""
        entries = list(self.raw["px4"].get("tus", []))
        resolved: list[str] = []
        repo = self.repo
        for entry in entries:
            if any(char in entry for char in "*?["):
                for path in sorted(repo.glob(entry)):
                    if path.is_file():
                        resolved.append(path.relative_to(repo).as_posix())
            else:
                resolved.append(entry)
        return list(dict.fromkeys(resolved))

    @property
    def sink_catalog(self) -> Path:
        value = Path(
            self.raw["px4"].get("sink_catalog", "configs/sink_catalog.yaml")
        )
        if value.is_absolute():
            return value
        return (self.project_root / value).resolve()

    @property
    def param_yaml_globs(self) -> list[str]:
        return list(self.raw["px4"].get("param_yaml_globs", []))

    @property
    def exclude_dirs(self) -> list[str]:
        return list(self.raw["px4"].get("exclude_dirs", []))

    @property
    def ulog_path(self) -> Path | None:
        """Authorized normal-scenario ULog used only for reachability evidence."""
        value = (self.raw.get("ulog") or {}).get("path")
        if not value:
            return None
        path = Path(value).expanduser()
        if path.is_absolute():
            return path.resolve()
        return (self.project_root / path).resolve()

    @property
    def ulog_scenario(self) -> str:
        return str((self.raw.get("ulog") or {}).get("scenario", ""))

    @property
    def clang_tool(self) -> Path:
        value = Path(self.raw["clang_tool"]["binary"])
        if value.is_absolute():
            return value
        return (self.project_root / value).resolve()

    @property
    def clang_build_script(self) -> Path:
        value = Path(self.raw["clang_tool"]["build_script"])
        if value.is_absolute():
            return value
        return (self.project_root / value).resolve()

    @property
    def _llvm(self) -> dict[str, Any]:
        return self.raw.get("llvm") or {}

    @property
    def llvm_clang(self) -> str:
        return str(self._llvm.get("clang", "clang++-14"))

    @property
    def llvm_opt(self) -> str:
        return str(self._llvm.get("opt", "opt-14"))

    @property
    def llvm_pass_plugin(self) -> Path:
        value = Path(self._llvm.get("pass_plugin", ".deps/bin/px4dep_member_topic.so"))
        if value.is_absolute():
            return value
        return (self.project_root / value).resolve()

    @property
    def llvm_build_script(self) -> Path:
        value = Path(
            self._llvm.get("build_script", "cpp/px4dep_llvm_pass/build.sh")
        )
        if value.is_absolute():
            return value
        return (self.project_root / value).resolve()

    @property
    def llvm_ir_tu(self) -> str:
        return str(self._llvm.get("ir_tu", "src/modules/ekf2/EKF2.cpp"))

    @property
    def llvm_optimization(self) -> str:
        return str(self._llvm.get("optimization", "O0"))

    def input_paths(self) -> list[Path]:
        paths = [
            self.parameters_json,
            self.parameters_xml,
            self.compile_commands,
        ]
        if self.events_json:
            paths.append(self.events_json)
        if self.sink_catalog.exists():
            paths.append(self.sink_catalog)
        if self.ulog_path and self.ulog_path.exists():
            paths.append(self.ulog_path)
        paths.extend(self.msg_dir.glob("*.msg"))
        paths.extend(self.repo / tu for tu in self.tus)
        for pattern in self.param_yaml_globs:
            paths.extend(self.repo.glob(pattern))
        return [p for p in paths if p.exists()]


def load_config(path: str | Path) -> Config:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    project_root = config_path.parent.parent if config_path.parent.name == "configs" else config_path.parent
    return Config(path=config_path, project_root=project_root.resolve(), raw=raw)
