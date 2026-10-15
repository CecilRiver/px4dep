"""Preprocessor build-guard attribution.

Clang's AST only contains the active branch, so build conditions cannot be read
from the AST directly.  This module re-scans the source text with the SITL
macro environment and records the enclosing #if conditions for each line.
Conditions that cannot be evaluated are kept verbatim and marked unknown; the
AST remains authoritative for whether the entity exists in this build.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .util import repo_rel


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DEFINED = re.compile(r"defined\s*(?:\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)|([A-Za-z_][A-Za-z0-9_]*))")


@dataclass
class Frame:
    parent_active: bool
    condition: str
    active: bool
    any_taken: bool
    prior: list[str] = field(default_factory=list)
    include_guard: bool = False
    unknown: bool = False
    any_unknown: bool = False


def _include_guard_name(lines: list[str]) -> str:
    """Return the file's conventional include-guard macro, if any."""
    limit = min(len(lines), 200)
    for idx in range(limit):
        stripped = lines[idx].strip()
        match = re.match(r"#\s*(?:ifndef\s+([A-Za-z_][A-Za-z0-9_]*)"
                         r"|if\s+!\s*defined\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\))", stripped)
        if not match:
            continue
        name = match.group(1) or match.group(2)
        for follow in lines[idx + 1: idx + 6]:
            if re.match(rf"#\s*define\s+{re.escape(name)}\b", follow.strip()):
                return name
    return ""


def _strip_outer_parens(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        balanced = True
        for idx, ch in enumerate(expr):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and idx != len(expr) - 1:
                    balanced = False
                    break
        if not balanced:
            break
        expr = expr[1:-1].strip()
    return expr


class GuardEvaluator:
    def __init__(self, macros: dict[str, str]):
        self.macros = macros

    def defined(self, name: str) -> bool:
        return name in self.macros

    def value(self, name: str) -> int | None:
        if name not in self.macros:
            return 0
        raw = self.macros[name].strip()
        if raw == "":
            return 1
        try:
            return int(raw, 0)
        except ValueError:
            return None

    def eval(self, expr: str) -> tuple[bool | None, bool]:
        """Return (value, used_unknown)."""
        expr = _strip_outer_parens(expr)
        if not expr:
            return None, True

        # defined(X) / defined X
        def repl_defined(match: re.Match[str]) -> str:
            name = match.group(1) or match.group(2)
            return "1" if self.defined(name) else "0"

        expr = _DEFINED.sub(repl_defined, expr)
        expr = _strip_outer_parens(expr)

        # Ternary is not used in PX4 build guards.
        for op in ("||", "&&"):
            parts = self._split_top(expr, op)
            if len(parts) > 1:
                unknown = False
                values = []
                for part in parts:
                    value, unknown_part = self.eval(part)
                    unknown = unknown or unknown_part
                    values.append(value)
                if op == "||":
                    if any(v is True for v in values):
                        return True, unknown
                    if all(v is False for v in values) and not unknown:
                        return False, False
                    return None, True
                if any(v is False for v in values):
                    return False, unknown
                if all(v is True for v in values) and not unknown:
                    return True, False
                return None, True

        if expr.startswith("!"):
            value, unknown = self.eval(expr[1:])
            if value is None:
                return None, True
            return (not value), unknown

        for op in ("==", "!=", ">=", "<=", ">", "<"):
            parts = self._split_top(expr, op)
            if len(parts) == 2:
                left = self._numeric(parts[0])
                right = self._numeric(parts[1])
                if left is None or right is None:
                    return None, True
                ops = {
                    "==": left == right,
                    "!=": left != right,
                    ">=": left >= right,
                    "<=": left <= right,
                    ">": left > right,
                    "<": left < right,
                }
                return ops[op], False

        expr = expr.strip()
        if re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", expr):
            return int(expr, 0) != 0, False
        if _IDENT.fullmatch(expr):
            value = self.value(expr)
            if value is None:
                return None, True
            return value != 0, False
        return None, True

    @staticmethod
    def _split_top(expr: str, op: str) -> list[str]:
        parts: list[str] = []
        depth = 0
        start = 0
        idx = 0
        while idx < len(expr):
            ch = expr[idx]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and expr.startswith(op, idx):
                parts.append(expr[start:idx])
                start = idx + len(op)
                idx += len(op)
                continue
            idx += 1
        if parts:
            parts.append(expr[start:])
        return parts

    def _numeric(self, expr: str) -> int | None:
        expr = _strip_outer_parens(expr)
        expr = _DEFINED.sub(
            lambda m: "1" if self.defined(m.group(1) or m.group(2)) else "0",
            expr,
        ).strip()
        if re.fullmatch(r"0[xX][0-9a-fA-F]+|\d+", expr):
            return int(expr, 0)
        if _IDENT.fullmatch(expr):
            # Resolve the macro's value rather than its truthiness.
            return self.value(expr)
        value, unknown = self.eval(expr)
        if unknown or value is None:
            return None
        return int(value)


def load_macro_environment(cfg: Config) -> dict[str, str]:
    macros: dict[str, str] = {}
    boardconfig = cfg.build_dir / "px4_boardconfig.h"
    if boardconfig.exists():
        for line in boardconfig.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*#\s*define\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s+(.*))?", line)
            if m:
                macros[m.group(1)] = (m.group(2) or "").split("//")[0].strip()
    board_lines = cfg.build_dir / "boardconfig"
    if board_lines.exists():
        for line in board_lines.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("CONFIG_"):
                macros.setdefault(key, "1" if value.strip() in {"y", "Y", "1"} else "0")

    if cfg.compile_commands.exists():
        try:
            commands = json.loads(cfg.compile_commands.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            commands = []
        for entry in commands:
            try:
                args = shlex.split(entry.get("command", ""))
            except ValueError:
                continue
            for arg in args:
                if arg.startswith("-D") and len(arg) > 2:
                    body = arg[2:]
                    if "=" in body:
                        key, value = body.split("=", 1)
                    else:
                        key, value = body, ""
                    macros.setdefault(key, value)
                elif arg.startswith("-U") and len(arg) > 2:
                    macros.pop(arg[2:], None)
    return macros


class GuardIndex:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.evaluator = GuardEvaluator(load_macro_environment(cfg))
        self._cache: dict[tuple[str, int], tuple[list[str], str]] = {}

    def _file_guards(self, file: str) -> dict[int, tuple[list[str], str]]:
        path = self.cfg.repo / file
        if not path.exists():
            return {}
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return {}

        result: dict[int, tuple[list[str], str]] = {}
        stack: list[Frame] = []
        active = True
        unknown_stack = False
        include_guard = _include_guard_name(lines)

        for lineno, text in enumerate(lines, start=1):
            stripped = text.strip()
            directive = None
            cond_text = ""
            if re.match(r"#\s*if(n?def)?\b", stripped):
                directive = "if"
                m = re.match(r"#\s*(ifdef|ifndef)\s+([A-Za-z_][A-Za-z0-9_]*)", stripped)
                if m:
                    kind, name = m.group(1), m.group(2)
                    cond_text = f"defined({name})" if kind == "ifdef" else f"!defined({name})"
                    value, unknown = self.evaluator.eval(cond_text)
                else:
                    m = re.match(r"#\s*if\s+(.*)", stripped)
                    cond_text = m.group(1).strip() if m else ""
                    value, unknown = self.evaluator.eval(cond_text)
                taken = (bool(value) if value is not None else True) and active
                is_include_guard = bool(
                    include_guard
                    and not stack
                    and cond_text == f"!defined({include_guard})"
                )
                frame = Frame(
                    parent_active=active,
                    condition=cond_text,
                    active=taken,
                    any_taken=taken,
                    prior=[cond_text],
                    include_guard=is_include_guard,
                    unknown=unknown,
                    any_unknown=unknown,
                )
                stack.append(frame)
                active = taken
                unknown_stack = unknown_stack or unknown
            elif re.match(r"#\s*elif", stripped):
                if stack:
                    frame = stack[-1]
                    m = re.match(r"#\s*elif\s*(?:def|ndef)?\s*(.*)", stripped)
                    cond_text = m.group(1).strip() if m else ""
                    if re.match(r"#\s*elifdef\b", stripped):
                        name = cond_text.split()[0] if cond_text else ""
                        cond_text = f"defined({name})"
                    elif re.match(r"#\s*elifndef\b", stripped):
                        name = cond_text.split()[0] if cond_text else ""
                        cond_text = f"!defined({name})"
                    value, unknown = self.evaluator.eval(cond_text)
                    taken = (
                        bool(value) if value is not None else True
                    ) and frame.parent_active and not frame.any_taken
                    frame.condition = cond_text
                    frame.active = taken
                    frame.prior.append(cond_text)
                    frame.any_taken = frame.any_taken or taken
                    frame.unknown = unknown
                    frame.any_unknown = frame.any_unknown or unknown
                    active = taken
                    unknown_stack = unknown_stack or unknown
            elif re.match(r"#\s*else\b", stripped):
                if stack:
                    frame = stack[-1]
                    cond_text = "!(" + " || ".join(f"({c})" for c in frame.prior) + ")"
                    taken = frame.parent_active and not frame.any_taken
                    frame.condition = cond_text
                    frame.active = taken
                    frame.any_taken = True
                    frame.unknown = frame.any_unknown
                    active = taken
            elif re.match(r"#\s*endif\b", stripped):
                if stack:
                    frame = stack.pop()
                    active = frame.parent_active
                    if not stack:
                        unknown_stack = False
                    else:
                        unknown_stack = any(
                            self.evaluator.eval(f.condition)[1] for f in stack
                        )

            # Record the guard for every line, including directive lines.
            conds = [f.condition for f in stack if f.active and not f.include_guard]
            unknown = any(
                f.unknown for f in stack if f.active and not f.include_guard
            ) or unknown_stack
            status = "unknown" if unknown else (
                "evaluated" if conds else "none"
            )
            result[lineno] = (conds, status)
        return result

    def guards_for(self, file: str, line: int) -> tuple[list[str], str]:
        if not file or line <= 0:
            return [], "none"
        key = (file, line)
        if key in self._cache:
            return self._cache[key]
        guards = self._file_guards(file).get(line, ([], "none"))
        self._cache[key] = guards
        return guards

    def for_record(self, record: dict[str, Any]) -> list[str]:
        file = record.get("spelling_file") or record.get("file") or ""
        line = int(record.get("spelling_line") or record.get("line") or 0)
        if file and line and not (self.cfg.repo / file).exists():
            file = record.get("file") or ""
            line = int(record.get("line") or 0)
        guards, _status = self.guards_for(file, line)
        return guards

    def status_for_record(self, record: dict[str, Any]) -> str:
        file = record.get("spelling_file") or record.get("file") or ""
        line = int(record.get("spelling_line") or record.get("line") or 0)
        if file and line and not (self.cfg.repo / file).exists():
            file = record.get("file") or ""
            line = int(record.get("line") or 0)
        _guards, status = self.guards_for(file, line)
        return status
