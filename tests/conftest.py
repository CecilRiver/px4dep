"""Shared pytest fixtures and configuration helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from px4dep_testkit import FIXTURES, make_config  # noqa: E402


@pytest.fixture
def uorb_config(tmp_path: Path):
    """Config over the checked-in uORB fixture tree."""
    return make_config(
        FIXTURES / "uorb",
        msg_dirs=["msg", "msg/versioned"],
    )


@pytest.fixture
def parameter_config(tmp_path: Path):
    return make_config(
        FIXTURES,
        parameters_json="parameters.json",
        param_yaml_globs=["param_yaml/*.yaml"],
    )


@pytest.fixture
def guard_repo(tmp_path: Path):
    """Config with a build directory exposing a controlled macro set."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "guarded.cpp").write_text(
        (FIXTURES / "guards" / "guarded.cpp").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    build = repo / "build"
    build.mkdir()
    (build / "px4_boardconfig.h").write_text(
        "#define CONFIG_SAMPLE 1\n#define CONFIG_VALUE 2\n",
        encoding="utf-8",
    )
    return make_config(repo, build_dir="build")
