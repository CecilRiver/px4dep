"""Unit tests for preprocessor guard evaluation and attribution."""

from __future__ import annotations

from px4dep.guards import GuardEvaluator, GuardIndex


def test_evaluator_defined_and_boolean_operators():
    evaluator = GuardEvaluator({"CONFIG_SAMPLE": "1", "CONFIG_VALUE": "2", "ZERO": "0"})
    assert evaluator.eval("defined(CONFIG_SAMPLE)") == (True, False)
    assert evaluator.eval("defined CONFIG_SAMPLE") == (True, False)
    assert evaluator.eval("defined(MISSING)") == (False, False)
    assert evaluator.eval("CONFIG_SAMPLE && !ZERO") == (True, False)
    assert evaluator.eval("ZERO || CONFIG_VALUE") == (True, False)
    assert evaluator.eval("!CONFIG_SAMPLE") == (False, False)
    assert evaluator.eval("0") == (False, False)
    assert evaluator.eval("1") == (True, False)


def test_evaluator_numeric_comparisons():
    evaluator = GuardEvaluator({"CONFIG_VALUE": "2", "HEX": "0x10", "EMPTY": ""})
    assert evaluator.eval("CONFIG_VALUE > 1") == (True, False)
    assert evaluator.eval("CONFIG_VALUE >= 2 && CONFIG_VALUE < 3") == (True, False)
    assert evaluator.eval("HEX == 16") == (True, False)
    assert evaluator.eval("EMPTY") == (True, False)
    assert evaluator.eval("CONFIG_VALUE != 2") == (False, False)


def test_evaluator_marks_unknown_expressions():
    evaluator = GuardEvaluator({"CONFIG_VALUE": "2"})
    assert evaluator.eval("SOME_MACRO(2)") == (None, True)
    assert evaluator.eval("CONFIG_VALUE > UNKNOWN_FUNC(1)") == (None, True)
    # An undefined plain identifier follows the C preprocessor rule: 0.
    assert evaluator.eval("UNDEFINED_MACRO") == (False, False)


def test_guard_index_attributes_active_conditions(guard_repo):
    index = GuardIndex(guard_repo)

    guards, status = index.guards_for("src/guarded.cpp", 5)
    assert guards == ["defined(CONFIG_SAMPLE) && CONFIG_VALUE > 1"]
    assert status == "evaluated"

    guards, status = index.guards_for("src/guarded.cpp", 19)
    assert guards == ["CONFIG_VALUE >= 2"]
    assert status == "evaluated"


def test_guard_index_marks_unknown_expressions(guard_repo):
    index = GuardIndex(guard_repo)
    guards, status = index.guards_for("src/guarded.cpp", 23)
    assert guards == ["SOME_FEATURE(1)"]
    assert status == "unknown"

    # A known-false branch is not attributed to entities (the AST omits them).
    guards, status = index.guards_for("src/guarded.cpp", 11)
    assert guards == []
    assert status == "none"


def test_include_guard_is_not_reported(guard_repo):
    index = GuardIndex(guard_repo)
    for line in (5, 11, 15, 19, 23):
        guards, _status = index.guards_for("src/guarded.cpp", line)
        assert all("GUARDED_H" not in guard for guard in guards)


def test_record_guard_uses_spelling_location(guard_repo):
    index = GuardIndex(guard_repo)
    record = {
        "file": "generated.cpp",
        "line": 1,
        "spelling_file": "src/guarded.cpp",
        "spelling_line": 19,
    }
    assert index.for_record(record) == ["CONFIG_VALUE >= 2"]
    assert index.status_for_record(record) == "evaluated"
