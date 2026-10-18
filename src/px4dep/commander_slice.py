"""Commander/Failsafe parameter slice.

EKF2 parameters reach the graph through ``estimator::parameters`` config
fields.  Commander/Failsafe parameters use a different construct: a
``px4::params::NAME`` wrapper member (``_param_name``) that is read with
``.get()`` inside health checks, mode checks, failsafe conditions and
protection-action selection.

This module turns those wrapper reads into explicit *slice* evidence without
weakening the EKF2 gates:

* ``data`` paths exist when the exact wrapper token appears in an expression
  owned by a sink (health-report arguments, failsafe options/conditions,
  mode expressions, failsafe-flag right-hand sides);
* ``control`` paths exist when the token appears in an enclosing ``if`` /
  ``switch`` / ternary condition recorded at the sink;
* ``context`` paths exist when a wrapper is read in the same function as a
  sink, or in a function reached by at most two reverse call edges.  These
  are deliberately labelled ``context_only``: they are function-level
  proximity, not a data or control dependency.

Nothing is inferred from parameter names alone.  Every path is anchored to a
concrete source expression, a sink call site and an indexed parameter that is
present in the current build.  Unmatched or ambiguous uses are preserved in
``unresolved/commander_slice.jsonl``.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict, deque
from typing import Any, Iterable

from .util import stable_id

MAX_CONTEXT_HOPS = 2
MAX_CONTEXT_PATHS_PER_PARAM = 6
COMMANDER_SOURCE_MARKER = "src/modules/commander/"
SKIP_HEALTH_METHODS = {"failsafeFlags", "canArm", "canRun", "modePreventsArming"}

# Context paths are illustrative only.  Prefer the layers a reviewer would
# inspect first when a parameter has many co-located sinks.
CONTEXT_SINK_RANK = {
    "health_predicate": 0,
    "mode_restriction": 1,
    "protection_action_option": 2,
    "failsafe_flag_write": 3,
    "protection_check": 4,
    "protection_action": 5,
    "failsafe_flag_read": 6,
}

CONTROL_SINK_FIELDS = {"health_call", "failsafe_check", "mode_check", "fallback_check"}


def _source(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "file": record.get("file", ""),
        "line": int(record.get("line") or 0),
        "column": int(record.get("column") or 0),
        "expansion_file": record.get("expansion_file", ""),
        "expansion_line": int(record.get("expansion_line") or 0),
        "spelling_file": record.get("spelling_file", ""),
        "spelling_line": int(record.get("spelling_line") or 0),
    }


def _norm_text(text: str) -> str:
    return " ".join((text or "").split())


def _args(record: dict[str, Any]) -> list[str]:
    return [str(arg.get("text", "")) for arg in (record.get("args") or [])]


def _control_texts(record: dict[str, Any]) -> list[str]:
    return [
        str(condition.get("text", ""))
        for condition in (record.get("control_conditions") or [])
    ]


def _caller_class(caller: str) -> str:
    return caller.rsplit("::", 1)[0] if "::" in caller else ""


def _token_re(field_name: str) -> re.Pattern[str]:
    return re.compile(
        r"(?<![A-Za-z0-9_])" + re.escape(field_name) + r"(?![A-Za-z0-9_])"
    )


def _predicate_qname(record: dict[str, Any]) -> tuple[str, str, str]:
    """Return (qualified_name, sink_kind, sink_layer) for a sink record."""
    kind = record.get("record", "")
    source = _source(record)
    caller = record.get("caller", "")
    suffix = f"@{source['file']}:{source['line']}:{source['column']}"
    if kind == "health_call":
        method = record.get("method", "")
        return (
            f"health:{caller}::{method}{suffix}",
            "health_predicate",
            "health",
        )
    if kind == "mode_check":
        method = record.get("method", "")
        return f"mode:{caller}::{method}{suffix}", "mode_restriction", "mode"
    if kind == "fallback_check":
        return (
            f"fallback:{caller}::checkModeFallback{suffix}",
            "mode_restriction",
            "mode",
        )
    if kind == "failsafe_check":
        macro = record.get("macro", "") or "checkFailsafe"
        return (
            f"protection:{caller}::{macro}{suffix}",
            "protection_check",
            "protection_condition",
        )
    if kind == "flag_access":
        field = record.get("field", "")
        sink_kind = (
            "failsafe_flag_write"
            if record.get("access") == "write"
            else "failsafe_flag_read"
        )
        return f"failsafe_flags_s::{field}", sink_kind, "failsafe_flag"
    if kind == "action_ref":
        return (
            f"FailsafeBase::Action::{record.get('action', '')}",
            "protection_action",
            "protection_action",
        )
    if kind == "enum_value":
        return (
            f"FailsafeBase::Action::{record.get('value_name', '')}",
            "protection_action",
            "protection_action",
        )
    return "", "", ""


def _sink_expressions(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Expression texts owned by a sink, tagged with their semantic layer."""
    kind = record.get("record", "")
    caller = record.get("caller", "")
    source = _source(record)
    texts = _args(record)

    def item(text: str, sink_kind: str, layer: str, role: str) -> dict[str, Any]:
        return {
            "text": _norm_text(text),
            "sink_kind": sink_kind,
            "sink_layer": layer,
            "role": role,
            "caller": caller,
            "file": source["file"],
            "line": source["line"],
            "column": source["column"],
        }

    out: list[dict[str, Any]] = []
    if kind == "health_call":
        for text in texts:
            out.append(item(text, "health_predicate", "health", "health_argument"))
    elif kind == "mode_check":
        out.append(
            item(str(record.get("mode_expr", "")), "mode_restriction", "mode", "mode_expression")
        )
        for text in texts:
            out.append(item(text, "mode_restriction", "mode", "mode_argument"))
    elif kind == "fallback_check":
        out.append(
            item(str(record.get("mode_expr", "")), "mode_restriction", "mode", "mode_expression")
        )
        for text in texts:
            out.append(item(text, "mode_restriction", "mode", "mode_argument"))
    elif kind == "failsafe_check":
        for key, role in (
            ("flag", "condition_flag"),
            ("last_condition", "condition_expression"),
            ("current_condition", "condition_expression"),
        ):
            out.append(
                item(str(record.get(key, "")), "protection_check", "protection_condition", role)
            )
        for text in record.get("condition_flags") or []:
            out.append(
                item(str(text), "protection_check", "protection_condition", "condition_flag")
            )
        for text in record.get("actions") or []:
            out.append(
                item(str(text), "protection_check", "protection_condition", "action_value")
            )
        out.append(
            item(
                str(record.get("options", "")),
                "protection_action_option",
                "protection_action",
                "action_options",
            )
        )
        for index, text in enumerate(texts):
            if index == 3:
                out.append(
                    item(
                        text,
                        "protection_action_option",
                        "protection_action",
                        "action_options",
                    )
                )
            else:
                out.append(
                    item(text, "protection_check", "protection_condition", "condition_argument")
                )
    elif kind == "flag_access":
        if record.get("access") == "write":
            out.append(
                item(str(record.get("rhs_text", "")), "failsafe_flag_write", "failsafe_flag", "rhs")
            )
            out.append(
                item(
                    str(record.get("receiver_expr", "")),
                    "failsafe_flag_write",
                    "failsafe_flag",
                    "receiver",
                )
            )
        else:
            out.append(
                item(
                    str(record.get("receiver_expr", "")),
                    "failsafe_flag_read",
                    "failsafe_flag",
                    "receiver",
                )
            )
    elif kind in {"action_ref", "enum_value"}:
        value = record.get("action") or record.get("value_name") or ""
        out.append(item(str(value), "protection_action", "protection_action", "action_value"))
        out.append(
            item(
                str(record.get("qualified_name", "")),
                "protection_action",
                "protection_action",
                "action_qualified_name",
            )
        )
    return [entry for entry in out if entry["text"]]


def _control_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    if record.get("record") not in CONTROL_SINK_FIELDS:
        return []
    qname, sink_kind, layer = _predicate_qname(record)
    if not qname:
        return []
    source = _source(record)
    out: list[dict[str, Any]] = []
    for condition in record.get("control_conditions") or []:
        text = _norm_text(str(condition.get("text", "")))
        if not text:
            continue
        out.append(
            {
                "text": text,
                "sink_kind": sink_kind,
                "sink_layer": layer,
                "role": f"control:{condition.get('kind', '')}",
                "caller": record.get("caller", ""),
                "file": condition.get("file", source["file"]),
                "line": condition.get("line", source["line"]),
                "column": condition.get("column", source["column"]),
                "condition_kind": condition.get("kind", ""),
                "condition_operator": condition.get("operator", ""),
            }
        )
    return out


class _CommanderSlicer:
    def __init__(
        self,
        raw_records: list[dict[str, Any]],
        health_nodes: list[dict[str, Any]],
        wrapper_nodes: list[dict[str, Any]],
        parameter_records: list[dict[str, Any]],
        function_ids: dict[str, str],
        commit: str,
        variant: str,
    ) -> None:
        self.raw = raw_records
        self.commit = commit
        self.variant = variant
        self.function_ids = function_ids
        self.parameter_by_name = {
            record.get("name", ""): record
            for record in parameter_records
            if record.get("kind") == "parameter" and record.get("name")
        }
        self.parameter_names = {
            name for name, record in self.parameter_by_name.items() if record.get("in_build")
        }
        self.health_by_qname = {
            node.get("qualified_name", ""): node
            for node in health_nodes
            if node.get("kind") in {"predicate", "health_flag", "protection_decision"}
        }
        self.wrapper_nodes = wrapper_nodes

        self.paths: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.unresolved: list[dict[str, Any]] = []
        self.unresolved_seen: set[tuple[Any, ...]] = set()
        self.metrics: Counter[str] = Counter()

        self.wrappers_by_field: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.wrapper_by_qname: dict[str, dict[str, Any]] = {}
        for node in wrapper_nodes:
            entry = {
                "id": node.get("id", ""),
                "qualified_name": node.get("qualified_name", ""),
                "field_name": node.get("field_name", ""),
                "parameter_name": node.get("parameter_name", ""),
                "parent_class": node.get("parent_class", ""),
                "usr": node.get("usr", ""),
                "in_build": node.get("parameter_name", "") in self.parameter_names,
                "source": node.get("source") or {},
                "build_guards": node.get("build_guards") or [],
                "build_guard_status": node.get("build_guard_status", "none"),
            }
            if not entry["field_name"]:
                continue
            self.wrappers_by_field[entry["field_name"]].append(entry)
            if entry["qualified_name"]:
                self.wrapper_by_qname[entry["qualified_name"]] = entry
        for field in self.wrappers_by_field:
            self.wrappers_by_field[field].sort(
                key=lambda item: (item["qualified_name"], item["parameter_name"])
            )
        self.field_regex = {
            field: _token_re(field) for field in self.wrappers_by_field
        }

        self.metrics["wrapper_fields_total"] = len(wrapper_nodes)
        self.metrics["wrapper_fields_unique"] = len(self.wrappers_by_field)
        self.metrics["parameters_in_build"] = len(self.parameter_names)
        # Commander/Failsafe scope only: wrappers declared in the commander
        # module.  EKF2 wrappers are handled by the EKF2 slice, and counting
        # them here would misreport which parameters this extension covers.
        self.commander_wrapper_parameters = {
            entry["parameter_name"]
            for entries in self.wrappers_by_field.values()
            for entry in entries
            if entry["in_build"]
            and COMMANDER_SOURCE_MARKER
            in (
                entry["source"].get("file", "")
                or entry["source"].get("expansion_file", "")
            )
        }
        self.metrics["parameters_with_wrapper"] = len(
            self.commander_wrapper_parameters
        )

        self.sinks: list[dict[str, Any]] = []
        self.sinks_by_id: dict[str, dict[str, Any]] = {}
        self.sinks_by_caller: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self._collect_sinks()
        self._collect_context_graph()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _unresolved_once(self, kind: str, key: tuple[Any, ...], **fields: Any) -> None:
        token = (kind, key)
        if token in self.unresolved_seen:
            return
        self.unresolved_seen.add(token)
        self.unresolved.append({"kind": kind, **fields})

    def _path_id(self, sink_id: str, source_id: str, category: str) -> str:
        return stable_id(
            self.commit, self.variant, "path", sink_id, source_id, category
        )

    def _add_path(
        self,
        *,
        sink_id: str,
        sink: dict[str, Any],
        sink_kind: str,
        sink_layer: str,
        source: dict[str, Any],
        parameter_name: str,
        category: str,
        confidence: str,
        step: dict[str, Any],
        context_only: bool = False,
        cross_function: bool = False,
        depth: int = 1,
        read_sites: list[dict[str, Any]] | None = None,
        via_functions: list[str] | None = None,
    ) -> str:
        path_id = self._path_id(sink_id, source["id"], category)
        parameter_node = self.parameter_by_name.get(parameter_name) or {}
        if path_id not in self.paths:
            self.paths[path_id] = {
                "id": path_id,
                "kind": "slice_path",
                "rule": "commander_wrapper_expression_slice",
                "analysis": "commander_failsafe_slice",
                "scope": "commander",
                "sink_id": sink_id,
                "sink_kind": sink_kind,
                "sink_layer": sink_layer,
                "sink": sink.get("qualified_name", sink_id),
                "source_id": source["id"],
                "source": source["qualified_name"],
                "source_field": source["field_name"],
                "source_kind": "param_wrapper",
                "parameter": parameter_name,
                "parameter_node_id": parameter_node.get("id", ""),
                "category": category,
                "context_only": context_only,
                "depth": depth,
                "cross_function": cross_function,
                "via_functions": via_functions or [],
                "read_sites": read_sites or [],
                "steps": [step],
                "build_variant": self.variant,
                "confidence": confidence,
                "build_guards": source.get("build_guards") or [],
                "build_guard_status": source.get("build_guard_status", "none"),
            }
            self.metrics["paths"] += 1
            self.metrics[f"{category}_paths"] += 1
            if context_only:
                self.metrics["context_only_paths"] += 1
            if cross_function:
                self.metrics["cross_function_paths"] += 1
        else:
            existing = self.paths[path_id]
            if step not in existing["steps"] and len(existing["steps"]) < 20:
                existing["steps"].append(step)
            for site in read_sites or []:
                if site not in existing["read_sites"]:
                    existing["read_sites"].append(site)
        return path_id

    def _add_edge(
        self,
        *,
        relation: str,
        sink_id: str,
        source: dict[str, Any],
        parameter_name: str,
        sink_kind: str,
        expression: str,
        confidence: str,
        step: dict[str, Any],
    ) -> None:
        rule = "commander_wrapper_expression_match"
        eid = stable_id(
            self.commit,
            self.variant,
            "edge",
            relation,
            sink_id,
            source["id"],
            rule,
        )
        if eid in self.edges:
            return
        self.edges[eid] = {
            "id": eid,
            "kind": "edge",
            "relation": relation,
            "src": sink_id,
            "dst": source["id"],
            "build_variant": self.variant,
            "build_guards": source.get("build_guards") or [],
            "build_guard_status": source.get("build_guard_status", "none"),
            "evidence": {
                "rule": rule,
                "scope": "commander",
                "parameter": parameter_name,
                "sink_kind": sink_kind,
                "source_field": source["qualified_name"],
                "expression": expression,
                "caller": step.get("caller", ""),
                "file": step.get("file", ""),
                "line": step.get("line", 0),
            },
            "confidence": confidence,
        }

    # ------------------------------------------------------------------
    # sink collection
    # ------------------------------------------------------------------
    def _collect_sinks(self) -> None:
        for record in self.raw:
            kind = record.get("record", "")
            if kind not in {
                "health_call",
                "mode_check",
                "fallback_check",
                "failsafe_check",
                "flag_access",
                "action_ref",
                "enum_value",
            }:
                continue
            if kind == "health_call" and record.get("method") in SKIP_HEALTH_METHODS:
                continue
            if kind == "action_ref" and record.get("action") in {"", "Count"}:
                continue
            if kind == "enum_value" and record.get("value_name") in {"", "Count"}:
                continue
            qname, sink_kind, sink_layer = _predicate_qname(record)
            if not qname:
                continue
            node = self.health_by_qname.get(qname)
            if node is None:
                self.metrics["sink_records_without_node"] += 1
                continue
            sink = {
                "record": kind,
                "node": node,
                "qname": qname,
                "sink_kind": sink_kind,
                "sink_layer": sink_layer,
                "caller": record.get("caller", ""),
                "caller_usr": record.get("caller_usr", ""),
                "source": _source(record),
                "record_ref": record,
            }
            sink_id = node["id"]
            existing = self.sinks_by_id.get(sink_id)
            if existing is None:
                self.sinks_by_id[sink_id] = sink
            elif existing["sink_kind"] != "protection_action_option" and sink_kind in {
                "protection_action_option"
            }:
                existing["sink_kind"] = sink_kind
                existing["sink_layer"] = sink_layer
            # Keep every record: the same node (e.g. one FailsafeFlags field)
            # is written and read at many sites, and each site owns its own
            # expression.  Deduplicating by node here would hide wrapper reads
            # that occur at all but the first site.
            self.sinks.append(sink)
            if sink["caller_usr"]:
                self.sinks_by_caller[sink["caller_usr"]].setdefault(sink_id, sink)
        self.metrics["sink_records"] = sum(
            1
            for record in self.raw
            if record.get("record")
            in {
                "health_call",
                "mode_check",
                "fallback_check",
                "failsafe_check",
                "flag_access",
                "action_ref",
                "enum_value",
            }
        )
        self.metrics["sink_nodes"] = len(self.sinks_by_id)
        self.metrics["sinks_by_kind"] = dict(
            Counter(sink["sink_kind"] for sink in self.sinks_by_id.values())
        )

    def _collect_context_graph(self) -> None:
        self.reverse_calls: dict[str, set[str]] = defaultdict(set)
        self.function_name_by_usr: dict[str, str] = {}
        for record in self.raw:
            if record.get("record") != "call":
                continue
            caller_usr = record.get("caller_usr", "")
            callee_usr = record.get("callee_usr", "")
            caller = record.get("caller", "")
            callee = record.get("callee", "")
            if caller_usr and caller:
                self.function_name_by_usr.setdefault(caller_usr, caller)
            if callee_usr and callee:
                self.function_name_by_usr.setdefault(callee_usr, callee)
            if caller_usr and callee_usr:
                self.reverse_calls[callee_usr].add(caller_usr)
        for sink in self.sinks_by_id.values():
            if sink["caller_usr"] and sink["caller"]:
                self.function_name_by_usr.setdefault(sink["caller_usr"], sink["caller"])

        self.read_uses = [
            record
            for record in self.raw
            if record.get("record") == "parameter_use"
            and record.get("use_kind") == "get"
            and record.get("qualified_name") in self.wrapper_by_qname
        ]

    # ------------------------------------------------------------------
    # matching
    # ------------------------------------------------------------------
    def _select_wrappers(
        self, field_name: str, caller: str
    ) -> list[dict[str, Any]]:
        candidates = self.wrappers_by_field.get(field_name) or []
        if not candidates:
            return []
        caller_class = _caller_class(caller)
        matching = [
            entry
            for entry in candidates
            if caller_class and entry["parent_class"] == caller_class
        ]
        if matching:
            chosen = matching
            ambiguous = False
        elif len({entry["parameter_name"] for entry in candidates}) == 1:
            chosen = [candidates[0]]
            ambiguous = True
        else:
            self._unresolved_once(
                "commander_wrapper_field_ambiguous",
                (field_name, caller),
                field_name=field_name,
                caller=caller,
                candidates=[entry["qualified_name"] for entry in candidates],
                reason="same_field_maps_to_multiple_parameters",
            )
            return []
        if len({entry["parameter_name"] for entry in chosen}) > 1:
            self._unresolved_once(
                "commander_wrapper_field_ambiguous",
                (field_name, caller),
                field_name=field_name,
                caller=caller,
                candidates=[entry["qualified_name"] for entry in chosen],
                reason="class_match_still_ambiguous",
            )
            return []
        for entry in chosen:
            entry["wrapper_ambiguous"] = ambiguous
        return chosen

    def _match_expression(
        self,
        sink: dict[str, Any],
        entry: dict[str, Any],
        category: str,
        step: dict[str, Any],
    ) -> None:
        field_name = entry["field_name"]
        parameter_name = entry["parameter_name"]
        if not entry["in_build"]:
            self._unresolved_once(
                "commander_parameter_not_in_build",
                (entry["id"], sink["qname"]),
                parameter=parameter_name,
                wrapper=entry["qualified_name"],
                sink=sink["qname"],
                file=step.get("file", ""),
                line=step.get("line", 0),
                reason="wrapper_parameter_absent_from_build_catalog",
            )
            return
        relation = "data_depends_on" if category == "data" else "control_depends_on"
        confidence = "medium" if category == "data" else "low"
        path_id = self._add_path(
            sink_id=sink["node"]["id"],
            sink=sink["node"],
            sink_kind=step["sink_kind"],
            sink_layer=step["sink_layer"],
            source=entry,
            parameter_name=parameter_name,
            category=category,
            confidence=confidence,
            step=step,
        )
        self._add_edge(
            relation=relation,
            sink_id=sink["node"]["id"],
            source=entry,
            parameter_name=parameter_name,
            sink_kind=step["sink_kind"],
            expression=step.get("expression", ""),
            confidence=confidence,
            step=step,
        )
        self.metrics[f"{category}_sink_source_pairs"] += 1
        self._sink_source_pairs.add((sink["node"]["id"], entry["id"]))
        self.metrics["wrapper_ambiguous_matches"] += int(
            bool(entry.get("wrapper_ambiguous"))
        )

    def _match_sinks(self) -> None:
        self._sink_source_pairs: set[tuple[str, str]] = set()
        for sink in self.sinks:
            record = sink["record_ref"]
            for expression in _sink_expressions(record):
                for field_name, regex in self.field_regex.items():
                    if not regex.search(expression["text"]):
                        continue
                    wrappers = self._select_wrappers(field_name, expression["caller"])
                    for entry in wrappers:
                        step = {
                            "kind": "commander_wrapper_read",
                            "relation": "data_depends_on",
                            "from": sink["qname"],
                            "to": entry["qualified_name"],
                            "parameter": entry["parameter_name"],
                            "source_field": entry["field_name"],
                            "sink_kind": expression["sink_kind"],
                            "sink_layer": expression["sink_layer"],
                            "role": expression["role"],
                            "expression": expression["text"],
                            "caller": expression["caller"],
                            "file": expression["file"],
                            "line": expression["line"],
                        }
                        self._match_expression(sink, entry, "data", step)
                        self._direct_parameters.add(entry["parameter_name"])
            for expression in _control_items(record):
                for field_name, regex in self.field_regex.items():
                    if not regex.search(expression["text"]):
                        continue
                    wrappers = self._select_wrappers(field_name, expression["caller"])
                    for entry in wrappers:
                        step = {
                            "kind": "commander_control_condition",
                            "relation": "control_depends_on",
                            "from": sink["qname"],
                            "to": entry["qualified_name"],
                            "parameter": entry["parameter_name"],
                            "source_field": entry["field_name"],
                            "sink_kind": expression["sink_kind"],
                            "sink_layer": expression["sink_layer"],
                            "role": expression["role"],
                            "condition": expression["text"],
                            "condition_kind": expression.get("condition_kind", ""),
                            "caller": expression["caller"],
                            "file": expression["file"],
                            "line": expression["line"],
                        }
                        self._match_expression(sink, entry, "control", step)
                        self._control_parameters.add(entry["parameter_name"])

    # ------------------------------------------------------------------
    # function-level context
    # ------------------------------------------------------------------
    def _context_sinks(self, caller_usr: str) -> list[tuple[dict[str, Any], int, list[str]]]:
        if not caller_usr:
            return []
        found: dict[str, tuple[dict[str, Any], int, list[str]]] = {}
        queue: deque[tuple[str, int, list[str]]] = deque([(caller_usr, 0, [])])
        visited = {caller_usr}
        while queue:
            usr, hops, via = queue.popleft()
            for sink in self.sinks_by_caller.get(usr, {}).values():
                existing = found.get(sink["node"]["id"])
                if existing is None or hops < existing[1]:
                    found[sink["node"]["id"]] = (sink, hops, via)
            if hops >= MAX_CONTEXT_HOPS:
                continue
            for parent in sorted(self.reverse_calls.get(usr, set())):
                if parent in visited:
                    continue
                visited.add(parent)
                name = self.function_name_by_usr.get(parent, parent)
                queue.append((parent, hops + 1, via + [name]))
        return list(found.values())

    def _match_context(self) -> None:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for use in self.read_uses:
            entry = self.wrapper_by_qname.get(use.get("qualified_name", ""))
            if entry is None or not entry["in_build"]:
                continue
            grouped[(entry["parameter_name"], entry["qualified_name"])].append(use)

        for (parameter_name, source_qname), uses in sorted(grouped.items()):
            entry = self.wrapper_by_qname[source_qname]
            candidates: dict[str, dict[str, Any]] = {}
            for use in uses:
                caller_usr = use.get("caller_usr", "")
                caller = use.get("caller", "")
                site = {
                    "caller": caller,
                    "caller_usr": caller_usr,
                    "file": use.get("file", ""),
                    "line": use.get("line", 0),
                    "column": use.get("column", 0),
                    "control_conditions": _control_texts(use),
                }
                for sink, hops, via_functions in self._context_sinks(caller_usr):
                    pair = (sink["node"]["id"], entry["id"])
                    if pair in self._sink_source_pairs:
                        continue
                    sink_id = sink["node"]["id"]
                    existing = candidates.get(sink_id)
                    if existing is None:
                        candidates[sink_id] = {
                            "sink": sink,
                            "hops": hops,
                            "via_functions": via_functions,
                            "sites": [site],
                        }
                    else:
                        existing["sites"].append(site)
                        if hops < existing["hops"]:
                            existing["hops"] = hops
                            existing["via_functions"] = via_functions
            if not candidates:
                continue
            ordered = sorted(
                candidates.values(),
                key=lambda item: (
                    item["hops"],
                    CONTEXT_SINK_RANK.get(item["sink"]["sink_kind"], 9),
                    item["sink"]["qname"],
                ),
            )
            selected: list[dict[str, Any]] = []
            seen_kinds: set[str] = set()
            for item in ordered:
                if len(selected) >= MAX_CONTEXT_PATHS_PER_PARAM:
                    break
                kind = item["sink"]["sink_kind"]
                if kind not in seen_kinds:
                    selected.append(item)
                    seen_kinds.add(kind)
            for item in ordered:
                if len(selected) >= MAX_CONTEXT_PATHS_PER_PARAM:
                    break
                if item not in selected:
                    selected.append(item)
            self.metrics["context_candidate_paths"] += len(ordered)
            self.metrics["context_paths_suppressed"] += len(ordered) - len(selected)

            for item in selected:
                sink = item["sink"]
                hops = item["hops"]
                step = {
                    "kind": "commander_function_context",
                    "relation": "context_depends_on",
                    "from": sink["qname"],
                    "to": entry["qualified_name"],
                    "parameter": parameter_name,
                    "source_field": entry["field_name"],
                    "sink_kind": sink["sink_kind"],
                    "sink_layer": sink["sink_layer"],
                    "role": "function_context",
                    "context_hops": hops,
                    "via_functions": item["via_functions"],
                    "caller": item["sites"][0]["caller"],
                    "file": item["sites"][0]["file"],
                    "line": item["sites"][0]["line"],
                }
                self._add_path(
                    sink_id=sink["node"]["id"],
                    sink=sink["node"],
                    sink_kind=sink["sink_kind"],
                    sink_layer=sink["sink_layer"],
                    source=entry,
                    parameter_name=parameter_name,
                    category="context",
                    confidence="low",
                    step=step,
                    context_only=True,
                    cross_function=hops > 0,
                    depth=hops,
                    read_sites=sorted(
                        item["sites"],
                        key=lambda site: (site["file"], site["line"], site["column"]),
                    ),
                    via_functions=item["via_functions"],
                )
                self._context_parameters.add(parameter_name)
                if hops == 0:
                    self.metrics["context_paths_without_call_hop"] += 1

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------
    def run(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        self._direct_parameters: set[str] = set()
        self._control_parameters: set[str] = set()
        self._context_parameters: set[str] = set()

        self._match_sinks()
        self._match_context()

        self.metrics["direct_parameters"] = len(self._direct_parameters)
        self.metrics["control_parameters"] = len(self._control_parameters)
        self.metrics["direct_or_control_parameters"] = len(
            self._direct_parameters | self._control_parameters
        )
        self.metrics["context_parameters"] = len(self._context_parameters)
        with_path = {
            path["parameter"] for path in self.paths.values() if path.get("parameter")
        }
        self.metrics["parameters_with_path"] = len(with_path)
        categories_by_parameter: dict[str, set[str]] = defaultdict(set)
        for path in self.paths.values():
            if path.get("parameter"):
                categories_by_parameter[path["parameter"]].add(path["category"])
        self.metrics["context_only_parameters"] = sum(
            1 for categories in categories_by_parameter.values()
            if categories == {"context"}
        )
        self.metrics["path_sink_kinds"] = dict(
            Counter(path.get("sink_kind", "") for path in self.paths.values())
        )
        self.metrics["direct_path_sink_kinds"] = dict(
            Counter(
                path.get("sink_kind", "")
                for path in self.paths.values()
                if path.get("category") in {"data", "control"}
            )
        )

        read_parameters = {
            use.get("parameter_name", "")
            for use in self.read_uses
            if use.get("parameter_name")
        }
        self.metrics["read_parameters"] = len(read_parameters)
        wrapper_parameters = self.commander_wrapper_parameters
        without_path = sorted(wrapper_parameters - with_path)
        self.metrics["parameters_without_path"] = without_path
        self.metrics["parameters_without_path_count"] = len(without_path)
        for parameter in without_path:
            self._unresolved_once(
                "commander_parameter_without_path",
                (parameter,),
                parameter=parameter,
                reason="no_direct_control_or_context_sink",
            )

        for use in self.read_uses:
            entry = self.wrapper_by_qname.get(use.get("qualified_name", ""))
            if entry is None or not entry["in_build"]:
                continue
            if not self._context_sinks(use.get("caller_usr", "")):
                self._unresolved_once(
                    "commander_read_site_without_nearby_sink",
                    (
                        use.get("caller_usr", ""),
                        entry["qualified_name"],
                        use.get("file", ""),
                        use.get("line", 0),
                    ),
                    parameter=entry["parameter_name"],
                    wrapper=entry["qualified_name"],
                    caller=use.get("caller", ""),
                    file=use.get("file", ""),
                    line=use.get("line", 0),
                    reason="no_sink_in_same_function_or_two_reverse_call_hops",
                )

        self.metrics["edges"] = len(self.edges)
        self.metrics["unresolved"] = len(self.unresolved)
        self.metrics["paths_without_steps"] = sum(
            1 for path in self.paths.values() if not path.get("steps")
        )
        self.metrics["paths_without_expression"] = sum(
            1
            for path in self.paths.values()
            if not path.get("context_only")
            and not any(
                step.get("expression") or step.get("condition")
                for step in path.get("steps") or []
            )
        )
        self.metrics["context_paths_without_read_site"] = sum(
            1
            for path in self.paths.values()
            if path.get("context_only") and not path.get("read_sites")
        )
        self.metrics["data_context_free"] = sum(
            1
            for path in self.paths.values()
            if path["category"] in {"data", "control"} and not path["context_only"]
        )
        self.metrics["analysis_scope"] = "commander_failsafe"
        return (
            list(self.edges.values()),
            sorted(self.paths.values(), key=lambda item: item["id"]),
            dict(self.metrics),
            self.unresolved,
        )


def build_commander_slice(
    cfg,
    raw_records: Iterable[dict[str, Any]],
    health_nodes: list[dict[str, Any]],
    wrapper_nodes: list[dict[str, Any]],
    parameter_records: list[dict[str, Any]],
    function_ids: dict[str, str],
    commit: str = "",
    variant: str = "",
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
]:
    slicer = _CommanderSlicer(
        list(raw_records),
        health_nodes,
        wrapper_nodes,
        parameter_records,
        function_ids,
        commit,
        variant,
    )
    return slicer.run()


def write_commander_slice(
    run,
    edges: list[dict[str, Any]],
    paths: list[dict[str, Any]],
    metrics: dict[str, Any],
    unresolved: list[dict[str, Any]],
) -> None:
    from .util import write_json, write_jsonl

    write_jsonl(run.path("graph", "commander_paths.jsonl"), paths)
    write_jsonl(run.path("normalized", "commander_slice_edges.jsonl"), edges)
    write_jsonl(run.path("unresolved", "commander_slice.jsonl"), unresolved)
    write_json(run.path("reports", "commander_slice_metrics.json"), metrics)
    columns = [
        "parameter",
        "sink",
        "sink_kind",
        "sink_layer",
        "category",
        "context_only",
        "depth",
        "cross_function",
        "confidence",
        "source",
        "file",
        "line",
    ]
    path = run.path("reports", "commander_slice.csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for item in paths:
            step = (item.get("steps") or [{}])[0]
            writer.writerow(
                {
                    "parameter": item.get("parameter", ""),
                    "sink": item.get("sink", ""),
                    "sink_kind": item.get("sink_kind", ""),
                    "sink_layer": item.get("sink_layer", ""),
                    "category": item.get("category", ""),
                    "context_only": item.get("context_only", False),
                    "depth": item.get("depth", ""),
                    "cross_function": item.get("cross_function", ""),
                    "confidence": item.get("confidence", ""),
                    "source": item.get("source", ""),
                    "file": step.get("file", ""),
                    "line": step.get("line", 0),
                }
            )
