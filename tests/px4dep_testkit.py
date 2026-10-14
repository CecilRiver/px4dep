"""Helpers shared by unit and integration tests."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = Path(__file__).resolve().parent / "golden"

if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from px4dep.config import Config  # noqa: E402


def make_config(repo: Path, **overrides: object) -> Config:
    """Build a Config for a fixture tree without touching the project YAML."""
    px4: dict[str, object] = {
        "repo": str(repo),
        "build_variant": "test_variant",
        "build_dir": "build",
        "parameters_json": "parameters.json",
        "parameters_xml": "parameters.xml",
        "compile_commands": "compile_commands.json",
        "msg_dir": "msg",
        "msg_dirs": ["msg"],
        "scope_dirs": ["src"],
        "exclude_dirs": [],
        "tus": [],
        "param_yaml_globs": ["**/*.yaml"],
    }
    px4.update(overrides)
    raw = {
        "px4": px4,
        "clang_tool": {"binary": "tool", "build_script": "build.sh"},
        "runs_root": "runs",
    }
    return Config(
        path=PROJECT_ROOT / "configs" / "fixture.yaml",
        project_root=PROJECT_ROOT,
        raw=raw,
    )
