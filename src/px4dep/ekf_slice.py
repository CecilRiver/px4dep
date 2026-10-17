"""M3: field-sensitive backward slice through the EKF2 estimator.

The slice starts at estimator output sinks (message fields, control/fault
flags and innovation criteria fields) and walks backwards through local
definitions, call arguments, return values and field-sensitive memory
predecessors.  Data and control dependencies are kept in separate edge
classes; they are never collapsed into one "influences" relation.

The traversal is intentionally static and conservative.  It is
flow-insensitive for shared class memory: every write to the same
``receiver_expr.field`` pair is considered a predecessor of a read.  This is
reported in the path evidence so a reviewer can distinguish a proven local
definition from a possible memory predecessor.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Iterable

from .util import stable_id

MAX_DEPTH = 12
MAX_ITEMS_PER_SINK = 4000

CRITERIA_MESSAGES = {
    "estimator_status_s",
    "estimator_innovations_s",
    "estimator_innovation_variances_s",
    "estimator_innovation_test_ratios_s",
    "estimator_states_s",
}
CRITERIA_PREFIXES = ("estimator_aid_source",)

_MASK_RE = re.compile(
    r"([A-Za-z_]\w*(?:[.:]{1,2}\w+)*)\s*&|&\s*([A-Za-z_]\w*(?:[.:]{1,2}\w+)*)"
)


@dataclass(frozen=True)
class FieldRef:
    qname: str
    name: str
    parent: str
    receiver: str
    receiver_kind: str
    receiver_root: str


@dataclass(frozen=True)
class Prev:
    parent: tuple[str, tuple[str, ...]] | None
    step: dict[str, Any] | None


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


def _norm_expr(text: str) -> str:
    text = " ".join((text or "").split())
    if text.startswith("this->"):
        text = text[len("this->") :]
    return text


def _field_from_dict(raw: dict[str, Any]) -> FieldRef:
    return FieldRef(
        qname=raw.get("qualified_name", ""),
        name=raw.get("name") or raw.get("field", ""),
        parent=raw.get("parent_class", ""),
        receiver=_norm_expr(raw.get("receiver_expr", "")),
        receiver_kind=raw.get("receiver_kind", ""),
        receiver_root=raw.get("receiver_root", ""),
    )


def _is_parameter(field: FieldRef) -> bool:
    return field.parent == "estimator::parameters" or field.qname.startswith(
        "estimator::parameters::"
    )


def _flag_domain(field: FieldRef) -> str:
    if "filter_control_status_u" in field.parent or "filter_control_status_u" in field.qname:
        return "control"
    if "fault_status_u" in field.parent or "fault_status_u" in field.qname:
        return "fault"
    return ""


def _message_class(field: FieldRef) -> str:
    parent = field.parent
    if parent.endswith("_s"):
        return parent
    return ""


def _is_criteria_message(message: str) -> bool:
    return message in CRITERIA_MESSAGES or message.startswith(CRITERIA_PREFIXES)


def _memory_key(field: FieldRef, caller_usr: str) -> str:
    if _is_parameter(field):
        return "parameter:" + field.qname
    receiver = field.receiver
    if field.receiver_kind in {"this", "member_field"} or receiver.startswith("_") or receiver.startswith(
        "this"
    ):
        root = "this" if field.receiver_kind == "this" else receiver
        return f"member:{root}:{field.parent}::{field.name}"
    return f"local:{caller_usr}:{receiver}:{field.parent}::{field.name}"


def _field_label(field: FieldRef, key: str) -> str:
    return f"field[{key}] {field.qname}"


def _items_from_refs(
    refs: Iterable[dict[str, Any]],
    locals_: Iterable[str],
    caller_usr: str,
) -> list[tuple[tuple[str, ...], FieldRef | None]]:
    """Convert a reference list to worklist items.

    Parameters become source items immediately; every other member access
    becomes a field-memory read that must be expanded through its writers.
    """
    items: list[tuple[tuple[str, ...], FieldRef | None]] = []
    for raw in refs:
        field = _field_from_dict(raw)
        if _is_parameter(field):
            items.append((("param", field.qname), field))
        else:
            items.append((("field", _memory_key(field, caller_usr)), field))
    for name in locals_:
        items.append((("local", caller_usr, name), None))
    return items


def _mask_symbols(text: str) -> list[str]:
    symbols: list[str] = []
    for left, right in _MASK_RE.findall(text or ""):
        symbol = left or right
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def _condition_steps(
    conditions: Iterable[dict[str, Any]], caller_usr: str
) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    steps: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    for condition in conditions or []:
        items = _items_from_refs(
            condition.get("members") or [], condition.get("locals") or [], caller_usr
        )
        text = condition.get("text", "")
        for item, field in items:
            label = _field_label(field, item[1]) if field is not None else f"local[{item[1]}] {item[2]}"
            steps.append(
                (
                    item,
                    {
                        "relation": "control_depends_on",
                        "kind": "control_condition",
                        "condition_kind": condition.get("kind", ""),
                        "condition": text,
                        "mask_symbols": _mask_symbols(text),
                        "from": "sink",
                        "to": label,
                        "file": "condition",
                        "line": int(condition.get("line") or 0),
                    },
                )
            )
    return steps


class _Slicer:
    def __init__(
        self,
        cfg,
        raw_records: list[dict[str, Any]],
        cpp_nodes: list[dict[str, Any]],
        schema_nodes: list[dict[str, Any]],
        function_ids: dict[str, str],
        binding_edges: list[dict[str, Any]],
        parameter_records: list[dict[str, Any]],
        commit: str,
        variant: str,
    ) -> None:
        self.cfg = cfg
        self.commit = commit
        self.variant = variant
        self.raw = raw_records
        self.cpp_nodes = cpp_nodes
        self.function_ids = function_ids
        self.parameter_by_id = {p["id"]: p for p in parameter_records}
        self.parameter_by_config_field = {
            e["dst"]: self.parameter_by_id.get(e["src"], {})
            for e in binding_edges
            if e.get("relation") == "configures"
        }

        self.config_field_by_qname = {
            n["qualified_name"]: n
            for n in cpp_nodes
            if n.get("kind") == "config_field" and n.get("qualified_name")
        }
        self.topic_field_by_qname = {
            n["qualified_name"]: n
            for n in schema_nodes
            if n.get("kind") == "topic_field" and n.get("qualified_name")
        }
        self.schema_messages: set[str] = set()
        for node in schema_nodes:
            if node.get("kind") == "message_type":
                if node.get("qualified_name"):
                    self.schema_messages.add(node["qualified_name"])
                if node.get("name"):
                    self.schema_messages.add(node["name"])
            if node.get("message_type"):
                self.schema_messages.add(node["message_type"])

        self.member_records = [
            r for r in raw_records if r.get("record") == "member_access"
        ]
        self.local_writes = [
            r for r in raw_records if r.get("record") == "local_write"
        ]
        self.call_args = [r for r in raw_records if r.get("record") == "call_arg"]
        self.call_results = [
            r for r in raw_records if r.get("record") == "call_result"
        ]
        self.return_refs = [
            r for r in raw_records if r.get("record") == "return_ref"
        ]

        self.writes_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in self.member_records:
            if record.get("access") != "write":
                continue
            field = _field_from_dict(record)
            self.writes_by_key[_memory_key(field, record.get("caller_usr", ""))].append(record)

        self.local_writes_by_fn: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for record in self.local_writes:
            self.local_writes_by_fn[
                (record.get("caller_usr", ""), record.get("variable", ""))
            ].append(record)

        self.call_args_by_param: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for record in self.call_args:
            self.call_args_by_param[
                (record.get("callee_usr", ""), record.get("param_name", ""))
            ].append(record)

        self.return_refs_by_callee: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in self.return_refs:
            self.return_refs_by_callee[record.get("caller_usr", "")].append(record)

        self.call_results_by_target: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(
            list
        )
        for record in self.call_results:
            self.call_results_by_target[
                (
                    record.get("caller_usr", ""),
                    record.get("target_kind", ""),
                    record.get("target_name", "") or record.get("target_field", ""),
                )
            ].append(record)

        self.publish_functions = {
            r.get("caller_usr", "")
            for r in raw_records
            if r.get("record") == "uorb_call" and r.get("operation") == "publish"
        }

        self.sink_nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.paths: dict[str, dict[str, Any]] = {}
        self.unresolved: list[dict[str, Any]] = []
        self.boundary_fields: set[str] = set()
        self.metrics: dict[str, Any] = defaultdict(int)
        self._unresolved_keys: set[tuple[Any, ...]] = set()

    # -- diagnostics -----------------------------------------------------
    def _unresolved_once(self, kind: str, **detail: Any) -> None:
        key = (kind, tuple(sorted((k, str(v)) for k, v in detail.items() if k not in {"window"})))
        if key in self._unresolved_keys:
            return
        self._unresolved_keys.add(key)
        self.unresolved.append({"kind": kind, **detail})

    # -- sink discovery --------------------------------------------------
    def _sink_for(
        self, field: FieldRef, record: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]] | None:
        if record.get("access") != "write":
            return None
        domain = _flag_domain(field)
        if domain:
            # The unions expose a whole-word ``value`` member; it aggregates
            # every flag and is not itself a meaningful sink.
            if field.name == "value" and field.parent in {
                "estimator::filter_control_status_u",
                "estimator::fault_status_u",
            }:
                return None
            node = self.sink_nodes.get("flag:" + field.qname)
            if node is None:
                node = {
                    "id": stable_id(
                        self.commit, self.variant, "estimator_flag", field.qname, "", ""
                    ),
                    "kind": "estimator_flag",
                    "qualified_name": field.qname,
                    "name": field.name,
                    "flag_domain": domain,
                    "field_parent": field.parent,
                    "build_variant": self.variant,
                    "build_guards": [],
                    "build_guard_status": "none",
                    "source": _source(record),
                    "tu": record.get("tu", ""),
                }
                self.sink_nodes["flag:" + field.qname] = node
            return node["id"], f"{domain}_flag", node

        message = _message_class(field)
        if not message or message not in self.schema_messages:
            return None

        if _is_criteria_message(message):
            sink_kind = "criteria_state"
        elif record.get("caller_usr", "") in self.publish_functions:
            sink_kind = "message_field"
        else:
            return None

        existing = self.topic_field_by_qname.get(field.qname)
        if existing is not None:
            return existing["id"], sink_kind, existing

        fallback_key = "slice:" + field.qname
        node = self.sink_nodes.get(fallback_key)
        if node is None:
            node = {
                "id": stable_id(
                    self.commit, self.variant, "slice_sink", field.qname, "", ""
                ),
                "kind": "slice_sink",
                "qualified_name": field.qname,
                "name": field.name,
                "sink_domain": sink_kind,
                "message_type": message,
                "build_variant": self.variant,
                "build_guards": [],
                "build_guard_status": "none",
                "source": _source(record),
                "tu": record.get("tu", ""),
            }
            self.sink_nodes[fallback_key] = node
        self.metrics[f"{sink_kind}_not_in_schema"] += 1
        self._unresolved_once(
            "slice_message_field_not_in_schema",
            qname=field.qname,
            message_type=message,
            file=record.get("file", ""),
            line=record.get("line", 0),
        )
        return node["id"], sink_kind, node

    # -- backward worklist ----------------------------------------------
    def _expand_call_results(
        self, caller_usr: str, target_kind: str, target_name: str, category: str
    ) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
        out: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        for result in self.call_results_by_target.get(
            (caller_usr, target_kind, target_name), []
        ):
            callee_usr = result.get("callee_usr", "")
            if not callee_usr:
                continue
            out.append(
                (
                    ("ret", callee_usr),
                    {
                        "relation": (
                            "data_depends_on"
                            if category == "data"
                            else "control_depends_on"
                        ),
                        "kind": "return_value",
                        "from": f"local[{target_name}]",
                        "to": result.get("callee", callee_usr),
                        "file": result.get("file", ""),
                        "line": int(result.get("line") or 0),
                    },
                )
            )
        return out

    def _neighbors(
        self, item: tuple[str, ...], category: str
    ) -> list[tuple[tuple[str, ...], str, dict[str, Any]]]:
        """Return (neighbor, category, step evidence) triples."""
        out: list[tuple[tuple[str, ...], str, dict[str, Any]]] = []

        if item[0] == "file":
            self.boundary_fields.add(item[1])
            return out

        if item[0] == "field":
            key = item[1]
            writers = self.writes_by_key.get(key, [])
            if not writers:
                self.boundary_fields.add(key)
            for writer in writers:
                caller_usr = writer.get("caller_usr", "")
                writer_field = _field_from_dict(writer)
                for member in writer.get("rhs_members") or []:
                    field = _field_from_dict(member)
                    if _is_parameter(field):
                        neighbor = ("param", field.qname)
                        label = f"parameter[{field.qname}]"
                    else:
                        neighbor = ("field", _memory_key(field, caller_usr))
                        label = _field_label(field, neighbor[1])
                    out.append(
                        (
                            neighbor,
                            category,
                            {
                                "relation": (
                                    "data_depends_on"
                                    if category == "data"
                                    else "control_depends_on"
                                ),
                                "kind": "memory_write",
                                "from": f"field[{key}] {writer_field.qname}",
                                "to": label,
                                "writer": writer.get("caller", ""),
                                "writer_usr": caller_usr,
                                "write_op": writer.get("write_op", ""),
                                "file": writer.get("file", ""),
                                "line": int(writer.get("line") or 0),
                            },
                        )
                    )
                for local_name in writer.get("rhs_locals") or []:
                    out.append(
                        (
                            ("local", caller_usr, local_name),
                            category,
                            {
                                "relation": (
                                    "data_depends_on"
                                    if category == "data"
                                    else "control_depends_on"
                                ),
                                "kind": "memory_write_local",
                                "from": f"field[{key}] {writer_field.qname}",
                                "to": f"local[{local_name}]",
                                "writer": writer.get("caller", ""),
                                "writer_usr": caller_usr,
                                "file": writer.get("file", ""),
                                "line": int(writer.get("line") or 0),
                            },
                        )
                    )
                for neighbor, step in _condition_steps(
                    writer.get("control_conditions") or [], caller_usr
                ):
                    step = {
                        **step,
                        "from": f"field[{key}] {writer_field.qname}",
                        "kind": "write_control_condition",
                        "writer": writer.get("caller", ""),
                        "file": writer.get("file", ""),
                        "line": int(writer.get("line") or 0),
                    }
                    out.append((neighbor, "control", step))
            return out

        if item[0] == "local":
            caller_usr, name = item[1], item[2]
            writes = self.local_writes_by_fn.get((caller_usr, name), [])
            if not writes:
                for call in self.call_args_by_param.get((caller_usr, name), []):
                    arg_caller = call.get("caller_usr", "")
                    for member in call.get("members") or []:
                        field = _field_from_dict(member)
                        if _is_parameter(field):
                            neighbor = ("param", field.qname)
                            label = f"parameter[{field.qname}]"
                        else:
                            neighbor = ("field", _memory_key(field, arg_caller))
                            label = _field_label(field, neighbor[1])
                        out.append(
                            (
                                neighbor,
                                category,
                                {
                                    "relation": (
                                        "data_depends_on"
                                        if category == "data"
                                        else "control_depends_on"
                                    ),
                                    "kind": "call_argument",
                                    "from": f"parameter[{name}]",
                                    "to": label,
                                    "callee": call.get("callee", ""),
                                    "caller": call.get("caller", ""),
                                    "file": call.get("file", ""),
                                    "line": int(call.get("line") or 0),
                                },
                            )
                        )
                    for local_name in call.get("locals") or []:
                        out.append(
                            (
                                ("local", arg_caller, local_name),
                                category,
                                {
                                    "relation": (
                                        "data_depends_on"
                                        if category == "data"
                                        else "control_depends_on"
                                    ),
                                    "kind": "call_argument",
                                    "from": f"parameter[{name}]",
                                    "to": f"local[{local_name}]",
                                    "callee": call.get("callee", ""),
                                    "caller": call.get("caller", ""),
                                    "file": call.get("file", ""),
                                    "line": int(call.get("line") or 0),
                                },
                            )
                        )
                    for neighbor, step in _condition_steps(
                        call.get("control_conditions") or [], arg_caller
                    ):
                        out.append(
                            (
                                neighbor,
                                "control",
                                {
                                    **step,
                                    "from": f"parameter[{name}]",
                                    "kind": "call_control_condition",
                                    "callee": call.get("callee", ""),
                                    "file": call.get("file", ""),
                                    "line": int(call.get("line") or 0),
                                },
                            )
                        )
                return out

            for writer in writes:
                writer_category = category
                writer_init = writer.get("rhs_text", "")
                for member in writer.get("rhs_members") or []:
                    field = _field_from_dict(member)
                    if _is_parameter(field):
                        neighbor = ("param", field.qname)
                        label = f"parameter[{field.qname}]"
                    else:
                        neighbor = ("field", _memory_key(field, caller_usr))
                        label = _field_label(field, neighbor[1])
                    out.append(
                        (
                            neighbor,
                            writer_category,
                            {
                                "relation": (
                                    "data_depends_on"
                                    if writer_category == "data"
                                    else "control_depends_on"
                                ),
                                "kind": "local_definition",
                                "from": f"local[{name}]",
                                "to": label,
                                "rhs": writer_init,
                                "write_kind": writer.get("write_kind", ""),
                                "file": writer.get("file", ""),
                                "line": int(writer.get("line") or 0),
                            },
                        )
                    )
                for local_name in writer.get("rhs_locals") or []:
                    out.append(
                        (
                            ("local", caller_usr, local_name),
                            writer_category,
                            {
                                "relation": (
                                    "data_depends_on"
                                    if writer_category == "data"
                                    else "control_depends_on"
                                ),
                                "kind": "local_definition",
                                "from": f"local[{name}]",
                                "to": f"local[{local_name}]",
                                "rhs": writer_init,
                                "file": writer.get("file", ""),
                                "line": int(writer.get("line") or 0),
                            },
                        )
                    )
                for neighbor, step in _condition_steps(
                    writer.get("control_conditions") or [], caller_usr
                ):
                    out.append(
                        (
                            neighbor,
                            "control",
                            {
                                **step,
                                "from": f"local[{name}]",
                                "kind": "local_control_condition",
                                "file": writer.get("file", ""),
                                "line": int(writer.get("line") or 0),
                            },
                        )
                    )
                for neighbor, step in self._expand_call_results(
                    caller_usr, "local", name, category
                ):
                    out.append((neighbor, category, step))
            return out

        if item[0] == "ret":
            callee_usr = item[1]
            for ret in self.return_refs_by_callee.get(callee_usr, []):
                for member in ret.get("members") or []:
                    field = _field_from_dict(member)
                    if _is_parameter(field):
                        neighbor = ("param", field.qname)
                        label = f"parameter[{field.qname}]"
                    else:
                        neighbor = ("field", _memory_key(field, ret.get("caller_usr", "")))
                        label = _field_label(field, neighbor[1])
                    out.append(
                        (
                            neighbor,
                            category,
                            {
                                "relation": "data_depends_on",
                                "kind": "return_value",
                                "from": ret.get("caller", ""),
                                "to": label,
                                "return_expr": ret.get("text", ""),
                                "file": ret.get("file", ""),
                                "line": int(ret.get("line") or 0),
                            },
                        )
                    )
                for local_name in ret.get("locals") or []:
                    out.append(
                        (
                            ("local", ret.get("caller_usr", ""), local_name),
                            category,
                            {
                                "relation": "data_depends_on",
                                "kind": "return_value",
                                "from": ret.get("caller", ""),
                                "to": f"local[{local_name}]",
                                "file": ret.get("file", ""),
                                "line": int(ret.get("line") or 0),
                            },
                        )
                    )
                for neighbor, step in _condition_steps(
                    ret.get("control_conditions") or [], ret.get("caller_usr", "")
                ):
                    out.append(
                        (
                            neighbor,
                            "control",
                            {
                                **step,
                                "from": ret.get("caller", ""),
                                "kind": "return_control_condition",
                                "file": ret.get("file", ""),
                                "line": int(ret.get("line") or 0),
                            },
                        )
                    )
            return out

        return out

    def _walk_sink(
        self, sink_id: str, sink_kind: str, sink_node: dict[str, Any], records: list[dict[str, Any]]
    ) -> None:
        prev: dict[tuple[str, tuple[str, ...]], Prev] = {}
        pending: deque[tuple[tuple[str, ...], str, int]] = deque()
        initial_steps: list[tuple[tuple[str, ...], str, dict[str, Any]]] = []
        sink_callers = {r.get("caller_usr", "") for r in records}
        # Callers that write this sink are the only functions that may later be
        # tied to a uORB publication.  M4 uses this usr-level evidence instead
        # of matching publisher names.
        sink_writers = sorted(
            {
                (record.get("caller_usr", ""), record.get("caller", ""))
                for record in records
                if record.get("caller_usr")
            },
            key=lambda item: (item[1], item[0]),
        )

        for record in records:
            caller_usr = record.get("caller_usr", "")
            for member in record.get("rhs_members") or []:
                field = _field_from_dict(member)
                if _is_parameter(field):
                    item = ("param", field.qname)
                    label = f"parameter[{field.qname}]"
                else:
                    item = ("field", _memory_key(field, caller_usr))
                    label = _field_label(field, item[1])
                initial_steps.append(
                    (
                        item,
                        "data",
                        {
                            "relation": "data_depends_on",
                            "kind": "rhs_member",
                            "from": sink_node.get("qualified_name", sink_id),
                            "to": label,
                            "caller": record.get("caller", ""),
                            "file": record.get("file", ""),
                            "line": int(record.get("line") or 0),
                        },
                    )
                )
            for local_name in record.get("rhs_locals") or []:
                initial_steps.append(
                    (
                        ("local", caller_usr, local_name),
                        "data",
                        {
                            "relation": "data_depends_on",
                            "kind": "rhs_local",
                            "from": sink_node.get("qualified_name", sink_id),
                            "to": f"local[{local_name}]",
                            "caller": record.get("caller", ""),
                            "file": record.get("file", ""),
                            "line": int(record.get("line") or 0),
                        },
                    )
                )
            for neighbor, step in self._expand_call_results(
                caller_usr, "field", sink_node.get("qualified_name", ""), "data"
            ):
                initial_steps.append((neighbor, "data", step))
            for neighbor, step in _condition_steps(
                record.get("control_conditions") or [], caller_usr
            ):
                initial_steps.append((neighbor, "control", step))

        for item, category, step in initial_steps:
            key = (category, item)
            if key not in prev:
                prev[key] = Prev(None, step)
                pending.append((item, category, 1))

        items_seen = 0
        truncated = False
        source_hits: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}

        while pending:
            item, category, depth = pending.popleft()
            items_seen += 1
            if items_seen > MAX_ITEMS_PER_SINK:
                truncated = True
                self._unresolved_once(
                    "slice_item_limit",
                    sink=sink_node.get("qualified_name", sink_id),
                    file=(sink_node.get("source") or {}).get("file", ""),
                    line=(sink_node.get("source") or {}).get("line", 0),
                )
                break

            if item[0] == "param":
                qname = item[1]
                config_node = self.config_field_by_qname.get(qname)
                if config_node is None:
                    self._unresolved_once(
                        "slice_parameter_field_without_node",
                        qname=qname,
                        sink=sink_node.get("qualified_name", sink_id),
                    )
                    continue
                source_hits[(category, config_node["id"])] = (item[1], config_node)
                continue

            if depth > MAX_DEPTH:
                truncated = True
                self._unresolved_once(
                    "slice_depth_truncated",
                    sink=sink_node.get("qualified_name", sink_id),
                    depth=MAX_DEPTH,
                )
                continue

            for neighbor, next_category, step in self._neighbors(item, category):
                if len(neighbor) == 0:
                    continue
                key = (next_category, neighbor)
                if key in prev:
                    continue
                prev[key] = Prev((category, item), step)
                pending.append((neighbor, next_category, depth + 1))

        if truncated:
            self.metrics["truncated_sinks"] += 1

        # Emit one direct graph edge per (sink, source, dependency class).
        for (category, source_id), (source_qname, config_node) in source_hits.items():
            relation = "data_depends_on" if category == "data" else "control_depends_on"
            edge_id = stable_id(
                self.commit,
                self.variant,
                "edge",
                relation,
                sink_id,
                source_id,
            )
            parameter = self.parameter_by_config_field.get(source_id, {})
            evidence = {
                "rule": "ekf2_field_sensitive_backward_slice",
                "sink_kind": sink_kind,
                "sink": sink_node.get("qualified_name", sink_id),
                "source_field": source_qname,
                "parameter": parameter.get("name", ""),
                "dependency_class": category,
                "memory_predecessors": True,
                "flow_sensitive": False,
                "sink_writers": [
                    {"usr": usr, "qualified_name": name}
                    for usr, name in sink_writers
                ],
                "file": (sink_node.get("source") or {}).get("file", ""),
                "line": (sink_node.get("source") or {}).get("line", 0),
            }
            existing = self.edges.get(edge_id)
            if existing is None:
                self.edges[edge_id] = {
                    "id": edge_id,
                    "kind": "edge",
                    "relation": relation,
                    "src": sink_id,
                    "dst": source_id,
                    "build_variant": self.variant,
                    "build_guards": [],
                    "build_guard_status": "none",
                    "confidence": "medium" if category == "data" else "low",
                    "evidence": evidence,
                }
            elif parameter.get("name") and not existing["evidence"].get("parameter"):
                existing["evidence"]["parameter"] = parameter["name"]

            # Reconstruct a concrete path for the path view.
            chain: list[dict[str, Any]] = []
            cursor: tuple[str, ...] | None = (
                ("param", source_qname) if source_qname.startswith("estimator::parameters::") else None
            )
            # The BFS reached the source through a ("param", qname) item; find it.
            for (cat, candidate), prev_entry in prev.items():
                if cat == category and candidate[0] == "param" and candidate[1] == source_qname:
                    cursor = candidate
                    break
            if cursor is not None:
                cursor_category = category
                seen: set[tuple[str, tuple[str, ...]]] = set()
                while True:
                    key = (cursor_category, cursor)
                    if key in seen:
                        break
                    seen.add(key)
                    entry = prev.get(key)
                    if entry is None:
                        break
                    if entry.step:
                        chain.append(entry.step)
                    if entry.parent is None:
                        break
                    cursor_category, cursor = entry.parent
                chain.reverse()
            cross_function = any(
                step.get("kind") in {"call_argument", "return_value"}
                or step.get("kind") == "memory_write"
                and step.get("writer_usr") not in sink_callers
                for step in chain
            )
            path_id = stable_id(
                self.commit,
                self.variant,
                "path",
                sink_id,
                source_id,
                category,
            )
            if path_id not in self.paths:
                self.paths[path_id] = {
                    "id": path_id,
                    "kind": "slice_path",
                    "rule": "ekf2_field_sensitive_backward_slice",
                    "sink_id": sink_id,
                    "sink_kind": sink_kind,
                    "sink": sink_node.get("qualified_name", sink_id),
                    "source_id": source_id,
                    "source": source_qname,
                    "parameter": parameter.get("name", ""),
                    "category": category,
                    "depth": len(chain),
                    "cross_function": bool(cross_function),
                    "sink_writers": [
                        {"usr": usr, "qualified_name": name}
                        for usr, name in sink_writers
                    ],
                    "steps": chain,
                    "build_variant": self.variant,
                    "confidence": "medium" if category == "data" else "low",
                }
                self.metrics["paths"] += 1
                self.metrics[f"{category}_paths"] += 1
                if cross_function:
                    self.metrics["cross_function_paths"] += 1

        # Track parameters that participate only as control conditions.
        for (category, _source_id) in source_hits:
            self.metrics[f"{category}_sink_source_pairs"] += 1

    # -- entry point -----------------------------------------------------
    def run(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        self.metrics["member_access_records"] = len(self.member_records)
        self.metrics["local_write_records"] = len(self.local_writes)
        self.metrics["call_arg_records"] = len(self.call_args)
        self.metrics["return_ref_records"] = len(self.return_refs)
        self.metrics["call_result_records"] = len(self.call_results)

        param_fields: dict[str, dict[str, Any]] = {}
        sinks: dict[str, list[dict[str, Any]]] = defaultdict(list)
        sink_kinds: dict[str, str] = {}
        sink_nodes_by_id: dict[str, dict[str, Any]] = {}

        for record in self.member_records:
            field = _field_from_dict(record)
            if _is_parameter(field):
                param_fields.setdefault(field.qname, record)
            sink = self._sink_for(field, record)
            if sink is None:
                continue
            sink_id, sink_kind, sink_node = sink
            sinks[sink_id].append(record)
            sink_kinds[sink_id] = sink_kind
            sink_nodes_by_id[sink_id] = sink_node

        self.metrics["parameter_fields_used"] = len(param_fields)
        for qname in param_fields:
            if qname in self.config_field_by_qname:
                self.metrics["parameter_fields_bound"] += 1
            else:
                self.metrics["parameter_fields_unbound"] += 1
                self._unresolved_once(
                    "slice_parameter_field_without_field_node",
                    qname=qname,
                    file=param_fields[qname].get("file", ""),
                    line=param_fields[qname].get("line", 0),
                )
        self.metrics["parameter_binding_complete"] = (
            1 if self.metrics["parameter_fields_unbound"] == 0 else 0
        )

        self.metrics["sink_nodes"] = len(sinks)
        self.metrics["sink_writes"] = sum(len(v) for v in sinks.values())
        for sink_id, kind in sink_kinds.items():
            self.metrics[f"sinks_{kind}"] += 1
        for sink_id, records in sinks.items():
            self.metrics[f"sink_writes_{sink_kinds[sink_id]}"] += len(records)

        for sink_id, records in sinks.items():
            self._walk_sink(
                sink_id, sink_kinds[sink_id], sink_nodes_by_id[sink_id], records
            )

        # Counts derived from emitted structures.
        self.metrics["data_edges"] = sum(
            1 for e in self.edges.values() if e["relation"] == "data_depends_on"
        )
        self.metrics["control_edges"] = sum(
            1 for e in self.edges.values() if e["relation"] == "control_depends_on"
        )
        self.metrics["distinct_sources"] = len(
            {e["dst"] for e in self.edges.values()}
        )
        self.metrics["distinct_sinks_reached"] = len(
            {e["src"] for e in self.edges.values()}
        )
        self.metrics["boundary_fields"] = len(self.boundary_fields)
        self.metrics["paths_without_steps"] = sum(
            1 for p in self.paths.values() if not p.get("steps")
        )
        self.metrics["unresolved"] = len(self.unresolved)

        sink_nodes = list(self.sink_nodes.values())
        edges = list(self.edges.values())
        paths = list(self.paths.values())
        return sink_nodes, edges, paths, dict(self.metrics), self.unresolved


def build_ekf_slice(
    cfg,
    raw_records: Iterable[dict[str, Any]],
    cpp_nodes: list[dict[str, Any]],
    schema_nodes: list[dict[str, Any]],
    function_ids: dict[str, str],
    binding_edges: list[dict[str, Any]],
    parameter_records: list[dict[str, Any]],
    commit: str = "",
    variant: str = "",
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
]:
    slicer = _Slicer(
        cfg,
        list(raw_records),
        cpp_nodes,
        schema_nodes,
        function_ids,
        binding_edges,
        parameter_records,
        commit,
        variant,
    )
    return slicer.run()


def write_ekf_slice_csv(path, edges: list[dict[str, Any]], paths: list[dict[str, Any]]) -> None:
    columns = [
        "sink",
        "sink_kind",
        "source",
        "parameter",
        "category",
        "depth",
        "cross_function",
        "confidence",
        "file",
        "line",
    ]
    by_key = {
        (p["sink_id"], p["source_id"], p["category"]): p for p in paths
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for edge in edges:
            evidence = edge.get("evidence") or {}
            path_record = by_key.get(
                (edge["src"], edge["dst"], evidence.get("dependency_class", "data"))
            )
            writer.writerow(
                {
                    "sink": evidence.get("sink", edge["src"]),
                    "sink_kind": evidence.get("sink_kind", ""),
                    "source": evidence.get("source_field", edge["dst"]),
                    "parameter": evidence.get("parameter", ""),
                    "category": evidence.get("dependency_class", ""),
                    "depth": (path_record or {}).get("depth", ""),
                    "cross_function": (path_record or {}).get("cross_function", ""),
                    "confidence": edge.get("confidence", ""),
                    "file": evidence.get("file", ""),
                    "line": evidence.get("line", 0),
                }
            )
