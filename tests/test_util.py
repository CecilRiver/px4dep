"""Unit tests for shared helpers: stable ids, hashing and JSONL I/O."""

from __future__ import annotations

import json
from pathlib import Path

from px4dep.util import (
    read_jsonl,
    repo_rel,
    sha256_files,
    stable_id,
    write_jsonl,
)


def test_stable_id_is_deterministic_and_semantic():
    first = stable_id("commit", "variant", "parameter", "EKF2_BARO_CTRL")
    assert first == stable_id("commit", "variant", "parameter", "EKF2_BARO_CTRL")
    assert len(first) == 20
    assert all(ch in "0123456789abcdef" for ch in first)
    assert first != stable_id("commit", "variant", "param_wrapper", "EKF2_BARO_CTRL")
    assert first != stable_id("commit", "variant", "parameter", "EKF2_BARO_CTRL", "extra")


def test_sha256_files_is_stable_across_roots(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        (root / "src").mkdir(parents=True)
        (root / "src" / "a.cpp").write_text("int a;\n", encoding="utf-8")
        (root / "src" / "b.cpp").write_text("int b;\n", encoding="utf-8")

    paths_a = [first / "src" / "b.cpp", first / "src" / "a.cpp"]
    paths_b = [second / "src" / "a.cpp", second / "src" / "b.cpp"]
    assert sha256_files(paths_a, root=first) == sha256_files(paths_b, root=second)

    (second / "src" / "b.cpp").write_text("int b2;\n", encoding="utf-8")
    assert sha256_files(paths_a, root=first) != sha256_files(paths_b, root=second)


def test_repo_rel_falls_back_to_absolute_for_outside_paths(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "src" / "x.cpp"
    inside.parent.mkdir()
    inside.write_text("", encoding="utf-8")
    outside = tmp_path / "outside.cpp"
    outside.write_text("", encoding="utf-8")

    assert repo_rel(inside, root) == "src/x.cpp"
    assert repo_rel(outside, root) == outside.resolve().as_posix()


def test_jsonl_round_trip_and_append(tmp_path: Path):
    path = tmp_path / "records.jsonl"
    assert write_jsonl(path, [{"b": 2, "a": 1}]) == 1
    assert write_jsonl(path, [{"c": 3}], append=True) == 1
    records = read_jsonl(path)
    assert records == [{"a": 1, "b": 2}, {"c": 3}]
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0]) == {"a": 1, "b": 2}


def test_read_jsonl_missing_file_is_empty(tmp_path: Path):
    assert read_jsonl(tmp_path / "missing.jsonl") == []
