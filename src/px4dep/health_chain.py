"""Commander health-report, failsafe-flag and protection-decision connector.

The Clang indexer emits framework-level records (``health_call``,
``flag_access``, ``failsafe_check``, ``mode_check``, ``action_ref``).  This
module turns those into reviewable graph entities while keeping the four
layers distinct:

* warning / health report
* arming and run-mode prohibition
* failsafe flag write/read
* protection action decision

No parameter names or threshold values are inferred here.  Associations that
are only lexical (for example a flag written in the same function as a health
call) are marked ``low`` confidence and named ``same_function_scope``.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from typing import Any, Iterable

import yaml

from .config import Config
from .util import stable_id

DEFAULT_CATALOG: dict[str, dict[str, Any]] = {
    "healthFailure": {"layer": "health_check", "effect": "health_report"},
    "armingCheckFailure": {"layer": "arming_check", "effect": "arming_report"},
    "setIsPresent": {"layer": "presence", "effect": "presence"},
    "setHealth": {"layer": "health_state", "effect": "health_state"},
    "clearArmingBits": {"layer": "mode_control", "effect": "arming_prohibition"},
    "clearCanRunBits": {"layer": "mode_control", "effect": "run_prohibition"},
    "canArm": {"layer": "mode_availability", "effect": "arming_query"},
    "canRun": {"layer": "mode_availability", "effect": "run_query"},
    "modePreventsArming": {"layer": "mode_control", "effect": "arming_query"},
    "failsafeFlags": {"layer": "flag_access", "effect": "flag_accessor"},
    "addExternalEvent": {"layer": "health_check", "effect": "external_event"},
    "checkFailsafe": {"layer": "protection_condition", "effect": "protection_check"},
    "modeCanRun": {"layer": "mode_availability", "effect": "mode_check"},
    "checkModeFallback": {"layer": "mode_fallback", "effect": "fallback_check"},
}

ACTION_CLASS = {
    "None": "none",
    "Warn": "warning",
    "FallbackPosCtrl": "fallback",
    "FallbackAltCtrl": "fallback",
    "FallbackStab": "fallback",
    "Hold": "recovery",
    "RTL": "recovery",
    "Land": "recovery",
    "Descend": "recovery",
    "Disarm": "termination",
    "Terminate": "termination",
}

SEVERITY_ORDER = {"debug": 0, "info": 1, "warning": 2, "error": 3}
LOG_SEVERITY = {
    "Debug": "debug",
    "Info": "info",
    "Warning": "warning",
    "Error": "error",
    "Critical": "error",
}
LOG_RE = re.compile(r"events::Log::(Debug|Info|Warning|Error|Critical)")
COMPONENT_RE = re.compile(r"health_component_t\s*::\s*([A-Za-z_][A-Za-z0-9_]*)")
EVENT_ID_RE = re.compile(r"events::ID\s*\(\s*\"([^\"]+)\"")
NAV_MODE_RE = re.compile(r"NavModes\s*::\s*([A-Za-z_][A-Za-z0-9_]*)")
NAV_STATE_RE = re.compile(r"\bNAVIGATION_STATE_[A-Z0-9_]+\b")
MODE_FLAG_RE = re.compile(r"\b(mode_req_[A-Za-z0-9_]+)\b")

HEALTH_LAYERS = {"health_check", "arming_check", "presence", "health_state", "mode_control"}
HEALTH_REPORT_LAYERS = {"health_check", "arming_check", "presence", "health_state"}
MODE_LAYERS = {"mode_availability", "mode_fallback"}


def _source(record: dict[str, Any]) -> dict[str, Any]:
    nested = record.get("source")
    if isinstance(nested, dict):
        record = {**nested, **{k: v for k, v in record.items() if k != "source"}}
    return {
        "file": record.get("file", ""),
        "line": record.get("line", 0),
        "column": record.get("column", 0),
        "expansion_file": record.get("expansion_file", ""),
        "expansion_line": record.get("expansion_line", 0),
        "spelling_file": record.get("spelling_file", ""),
        "spelling_line": record.get("spelling_line", 0),
    }


def _unique(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def _strings(record: dict[str, Any], key: str) -> list[str]:
    value = record.get(key) or []
    return [str(item) for item in value if item]


def _args(record: dict[str, Any]) -> list[str]:
    return [str(arg.get("text", "")) for arg in (record.get("args") or [])]


def _severity_from_texts(texts: Iterable[str]) -> str:
    best = "unknown"
    for text in texts:
        for match in LOG_RE.finditer(text or ""):
            severity = LOG_SEVERITY[match.group(1)]
            if best == "unknown" or SEVERITY_ORDER[severity] > SEVERITY_ORDER[best]:
                best = severity
    return best


def _component_from_texts(texts: Iterable[str]) -> str:
    for text in texts:
        match = COMPONENT_RE.search(text or "")
        if match:
            return match.group(1)
    return ""


def _event_id_from_texts(texts: Iterable[str]) -> str:
    for text in texts:
        match = EVENT_ID_RE.search(text or "")
        if match:
            return match.group(1)
    return ""


def _mode_facts_from_text(text: str) -> tuple[list[str], list[str], list[str], bool]:
    groups = _unique(NAV_MODE_RE.findall(text or ""))
    states = _unique(NAV_STATE_RE.findall(text or ""))
    flags = _unique(MODE_FLAG_RE.findall(text or ""))
    dynamic = not groups and not states and not flags and bool((text or "").strip())
    return groups, states, flags, dynamic


def _mode_facts(record: dict[str, Any], text: str = "") -> tuple[list[str], list[str], list[str], bool]:
    groups = _strings(record, "mode_groups")
    states = _strings(record, "nav_states")
    flags = _strings(record, "mode_flags")
    dynamic = bool(record.get("mode_dynamic", False))
    if not groups and not states and not flags:
        text_groups, text_states, text_flags, text_dynamic = _mode_facts_from_text(text)
        groups = text_groups
        states = text_states
        flags = text_flags
        # The source expression wins over the indexer's conservative
        # "dynamic" flag: a name that is spelled out is not dynamic.
        dynamic = text_dynamic or (dynamic and not (groups or states or flags))
    return groups, states, flags, dynamic


def load_sink_catalog(cfg: Config) -> dict[str, Any]:
    """Read framework matchers; never reads parameter names from this file."""
    catalog: dict[str, Any] = {"methods": {}, "macros": {}, "enums": {}}
    if not cfg.sink_catalog.exists():
        catalog["methods"] = dict(DEFAULT_CATALOG)
        return catalog
    with cfg.sink_catalog.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    for sink in data.get("sinks") or []:
        matcher = str(sink.get("matcher", ""))
        if ":" not in matcher:
            continue
        matcher_kind, body = matcher.split(":", 1)
        entry = {
            "layer": str(sink.get("layer", "")),
            "effect": str(sink.get("effect", "")),
            "description": str(sink.get("description", "")),
        }
        if matcher_kind == "call":
            method = body.rsplit("::", 1)[-1]
            catalog["methods"][method] = entry
        elif matcher_kind == "macro":
            catalog["macros"][body] = entry
        elif matcher_kind == "enum":
            catalog["enums"][body] = entry
    for method, entry in DEFAULT_CATALOG.items():
        catalog["methods"].setdefault(method, entry)
    return catalog


class _Builder:
    def __init__(
        self,
        cfg: Config,
        raw_records: list[dict[str, Any]],
        function_ids: dict[str, str],
        schema_nodes: list[dict[str, Any]],
        commit: str,
        variant: str,
    ) -> None:
        self.cfg = cfg
        self.raw = raw_records
        self.function_ids = function_ids
        self.commit = commit
        self.variant = variant
        self.catalog = load_sink_catalog(cfg)

        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.unresolved: list[dict[str, Any]] = []
        self.predicates: list[dict[str, Any]] = []
        self.health_calls = 0
        self.flag_accesses = 0
        self.flag_writes = 0
        self.flag_reads = 0
        self.mode_checks = 0
        self.mode_prohibitions = 0
        self.failsafe_checks = 0
        self.failsafe_actions_dynamic = 0
        self.action_refs = 0
        self.action_returns = 0
        self.action_assignments = 0

        self.field_by_qname: dict[str, dict[str, Any]] = {}
        self.topics_by_message: dict[str, list[str]] = {}
        for node in schema_nodes:
            if node.get("kind") == "topic_field":
                self.field_by_qname[node.get("qualified_name", "")] = node
            elif node.get("kind") == "topic":
                message = node.get("message_type", "")
                if message:
                    self.topics_by_message.setdefault(message, []).append(node.get("name", ""))

        self.writes_by_caller: dict[str, set[str]] = {}
        self.action_enum_seen: set[str] = set()

    # ------------------------------------------------------------------
    # graph primitives
    # ------------------------------------------------------------------
    def node_id(self, kind: str, qname: str, file: str = "", line: int = 0, column: int = 0) -> str:
        return stable_id(self.commit, self.variant, kind, qname, file, line, column)

    def add_node(self, node: dict[str, Any]) -> str:
        nid = node["id"]
        existing = self.nodes.get(nid)
        if existing is None:
            node.setdefault("build_variant", self.variant)
            node.setdefault("build_guards", [])
            node.setdefault("build_guard_status", "none")
            self.nodes[nid] = node
            return nid
        for key, value in node.items():
            if key not in existing:
                existing[key] = value
            elif key in {"topics", "source_files"} and isinstance(value, list):
                for item in value:
                    if item not in existing[key]:
                        existing[key].append(item)
        return nid

    def add_edge(
        self,
        relation: str,
        src: str,
        dst: str,
        *,
        rule: str,
        record: dict[str, Any] | None = None,
        confidence: str = "high",
        **evidence: Any,
    ) -> None:
        record = record or {}
        source = _source(record)
        eid = stable_id(
            self.commit,
            self.variant,
            "edge",
            relation,
            src,
            dst,
            source["file"],
            source["line"],
            source["column"],
            rule,
        )
        if eid in self.edges:
            return
        self.edges[eid] = {
            "id": eid,
            "kind": "edge",
            "relation": relation,
            "src": src,
            "dst": dst,
            "build_variant": self.variant,
            "evidence": {
                "rule": rule,
                "file": source["file"],
                "line": source["line"],
                "column": source["column"],
                "expansion_file": source["expansion_file"],
                "expansion_line": source["expansion_line"],
                **{key: value for key, value in evidence.items() if value not in (None, "", [], {})},
            },
            "confidence": confidence,
        }

    def unresolved_record(self, kind: str, record: dict[str, Any], **fields: Any) -> None:
        source = _source(record)
        self.unresolved.append(
            {
                "kind": kind,
                **fields,
                "file": source["file"],
                "line": source["line"],
                "column": source["column"],
            }
        )

    def function_id(self, record: dict[str, Any]) -> str | None:
        usr = record.get("caller_usr", "")
        name = record.get("caller", "")
        return self.function_ids.get(usr) or self.function_ids.get(name)

    def mode_node(
        self,
        qualified_name: str,
        name: str,
        domain: str,
        *,
        status: str = "known",
        record: dict[str, Any] | None = None,
        **attributes: Any,
    ) -> str:
        nid = self.node_id("mode_capability", qualified_name)
        self.add_node(
            {
                "id": nid,
                "kind": "mode_capability",
                "qualified_name": qualified_name,
                "name": name,
                "mode_domain": domain,
                "status": status,
                "source": _source(record or {}),
                "tu": (record or {}).get("tu", ""),
                **attributes,
            }
        )
        return nid

    def mode_targets(
        self,
        record: dict[str, Any],
        groups: list[str],
        states: list[str],
        mode_flags: list[str],
        *,
        text: str = "",
        dynamic: bool = False,
    ) -> list[tuple[str, str]]:
        targets: list[tuple[str, str]] = []
        for group in groups:
            if group == "None":
                continue
            qname = f"NavModes::{group}"
            targets.append(
                (
                    self.mode_node(
                        qname,
                        group,
                        "mode_group",
                        record=record,
                        is_all=group == "All",
                    ),
                    "mode_group",
                )
            )
        for state in states:
            qname = f"vehicle_status_s::{state}"
            targets.append((self.mode_node(qname, state, "nav_state", record=record), "nav_state"))
        for flag in mode_flags:
            qname = f"mode_req:{flag}"
            targets.append(
                (
                    self.mode_node(
                        qname,
                        flag,
                        "flag_derived",
                        status="derived",
                        record=record,
                        source_flag=f"failsafe_flags_s::{flag}",
                    ),
                    "flag_derived",
                )
            )
        if not targets and dynamic and text.strip():
            expression = " ".join(text.split())
            qname = f"dynamic:{expression}"
            targets.append(
                (
                    self.mode_node(
                        qname,
                        expression,
                        "dynamic",
                        status="dynamic",
                        record=record,
                        expression=expression,
                    ),
                    "dynamic",
                )
            )
        return targets

    def health_flag(self, field: str, record: dict[str, Any] | None = None) -> str:
        qname = f"failsafe_flags_s::{field}"
        field_node = self.field_by_qname.get(qname)
        if field_node:
            source = dict(field_node.get("source") or {})
            topics = list(self.topics_by_message.get("failsafe_flags_s", []))
        else:
            source = _source(record or {})
            topics = []
            self.unresolved_record(
                "health_flag_without_message_field",
                record or {},
                flag=qname,
            )
        nid = self.node_id("health_flag", qname)
        self.add_node(
            {
                "id": nid,
                "kind": "health_flag",
                "qualified_name": qname,
                "name": field,
                "message_type": "failsafe_flags_s",
                "topics": topics,
                "source": source,
                "tu": (record or {}).get("tu", ""),
                "declared_in": source.get("file", ""),
            }
        )
        if field_node and not any(
            edge["relation"] == "maps_to_field"
            and edge["src"] == nid
            and edge["dst"] == field_node["id"]
            for edge in self.edges.values()
        ):
            self.add_edge(
                "maps_to_field",
                nid,
                field_node["id"],
                rule="failsafe_flags_message_field",
                record=record or {},
                field=qname,
            )
        return nid

    def health_component_node(self, component: str, record: dict[str, Any]) -> str:
        qname = f"health_component_t::{component}"
        nid = self.node_id("health_component", qname)
        self.add_node(
            {
                "id": nid,
                "kind": "health_component",
                "qualified_name": qname,
                "name": component,
                "enum_name": "health_component_t",
                "source": _source(record),
                "tu": record.get("tu", ""),
            }
        )
        return nid

    def decision_node(self, action: str, record: dict[str, Any] | None = None) -> str:
        qname = f"FailsafeBase::Action::{action}"
        nid = self.node_id("protection_decision", qname)
        self.add_node(
            {
                "id": nid,
                "kind": "protection_decision",
                "qualified_name": qname,
                "name": action,
                "action": action,
                "action_class": ACTION_CLASS.get(action, "other"),
                "enum_name": "FailsafeBase::Action",
                "source": _source(record or {}),
                "tu": (record or {}).get("tu", ""),
            }
        )
        return nid

    def add_predicate(
        self,
        record: dict[str, Any],
        layer: str,
        effect: str,
        qname: str,
        **attributes: Any,
    ) -> dict[str, Any]:
        source = _source(record)
        nid = self.node_id("predicate", qname, source["file"], source["line"], source["column"])
        node = {
            "id": nid,
            "kind": "predicate",
            "qualified_name": qname,
            "name": record.get("method") or record.get("record", "predicate"),
            "predicate_layer": layer,
            "effect": effect,
            "caller": record.get("caller", ""),
            "caller_usr": record.get("caller_usr", ""),
            "source": source,
            "tu": record.get("tu", ""),
            **attributes,
        }
        self.add_node(node)
        caller_id = self.function_id(record)
        if caller_id:
            relation = (
                "has_protection_condition"
                if layer == "protection_condition"
                else ("has_mode_check" if layer in MODE_LAYERS else "reports_health")
            )
            self.add_edge(
                relation,
                caller_id,
                nid,
                rule="health_chain_call_site",
                record=record,
                method=record.get("method", ""),
                layer=layer,
                effect=effect,
            )
        else:
            self.unresolved_record(
                "health_caller_not_indexed",
                record,
                caller=record.get("caller", ""),
                caller_usr=record.get("caller_usr", ""),
                method=record.get("method", ""),
            )
        self.predicates.append(node)
        return node

    # ------------------------------------------------------------------
    # record handlers
    # ------------------------------------------------------------------
    def handle_health_call(self, record: dict[str, Any]) -> None:
        method = record.get("method", "")
        catalog = self.catalog["methods"].get(method, {})
        layer = catalog.get("layer") or DEFAULT_CATALOG.get(method, {}).get("layer", "health_check")
        effect = catalog.get("effect") or DEFAULT_CATALOG.get(method, {}).get("effect", "health_report")
        if method == "failsafeFlags":
            self.health_calls += 1
            return
        if method in {"canArm", "canRun", "modePreventsArming"}:
            # The Clang tool also emits a richer mode_check record for these
            # calls; use that one to avoid duplicate predicates.
            return

        texts = _args(record)
        groups, states, mode_flags, mode_dynamic = _mode_facts(record, texts[0] if texts else "")
        if method in {"setHealth", "setIsPresent"}:
            groups, states, mode_flags, mode_dynamic = [], [], [], False
        if method == "modePreventsArming":
            mode_flags = _unique([*mode_flags, "mode_req_prevent_arming"])

        severity = _severity_from_texts(texts)
        if method == "setHealth":
            if len(texts) > 3 and texts[3].strip() in {"true", "1"}:
                severity = "error"
            elif len(texts) > 2 and texts[2].strip() in {"true", "1"}:
                severity = "warning"
        if method in {"healthFailure", "armingCheckFailure"}:
            component = _component_from_texts(texts[1:2])
        else:
            component = _component_from_texts(texts[:1])
        event_id = _event_id_from_texts(texts)

        if method == "healthFailure":
            effect = "health_warning" if severity == "warning" else (
                "health_error" if severity in {"error", "critical"} else "health_report"
            )
        elif method == "armingCheckFailure":
            effect = "arming_warning" if severity == "warning" else (
                "arming_error" if severity in {"error", "critical"} else "arming_report"
            )
        elif method == "clearArmingBits":
            effect = "arming_prohibition"
        elif method == "clearCanRunBits":
            effect = "run_prohibition"

        source = _source(record)
        qname = f"health:{record.get('caller', '')}::{method}@{source['file']}:{source['line']}:{source['column']}"
        predicate = self.add_predicate(
            record,
            layer,
            effect,
            qname,
            method=method,
            severity=severity,
            component=component,
            event_id=event_id,
            mode_groups=groups,
            nav_states=states,
            mode_flags=mode_flags,
            mode_dynamic=mode_dynamic,
            optional_check="None" in (_strings(record, "mode_groups") or []),
            receiver_type=record.get("receiver_type", ""),
        )
        self.health_calls += 1

        if component:
            component_id = self.health_component_node(component, record)
            self.add_edge(
                "reports_component",
                predicate["id"],
                component_id,
                rule="report_component_argument",
                record=record,
                component=component,
            )

        if mode_dynamic:
            self.unresolved_record(
                "mode_set_dynamic",
                record,
                predicate=predicate["qualified_name"],
                method=method,
                expression=(texts[0] if texts else ""),
            )
        targets = self.mode_targets(
            record,
            groups,
            states,
            mode_flags,
            text=(texts[0] if texts and method not in {"setHealth", "setIsPresent"} else ""),
            dynamic=mode_dynamic,
        )
        for target, domain in targets:
            if effect in {"arming_query", "run_query"}:
                self.add_edge(
                    "queries_mode",
                    predicate["id"],
                    target,
                    rule="mode_availability_query",
                    record=record,
                    method=method,
                    mode_domain=domain,
                )
            elif layer in HEALTH_LAYERS:
                self.add_edge(
                    "requires_mode",
                    predicate["id"],
                    target,
                    rule="health_report_required_modes",
                    record=record,
                    method=method,
                    mode_domain=domain,
                )
            if method in {"healthFailure", "armingCheckFailure", "clearArmingBits"}:
                self.add_edge(
                    "restricts_arming",
                    predicate["id"],
                    target,
                    rule="report_clears_arming_bits",
                    record=record,
                    method=method,
                    severity=severity,
                    mode_domain=domain,
                )
                self.mode_prohibitions += 1
            if method == "clearCanRunBits":
                self.add_edge(
                    "restricts_run",
                    predicate["id"],
                    target,
                    rule="explicit_clear_can_run_bits",
                    record=record,
                    method=method,
                    mode_domain=domain,
                )
                self.mode_prohibitions += 1

        for flag in mode_flags:
            flag_id = self.health_flag(flag, record)
            self.add_edge(
                "reads_flag",
                predicate["id"],
                flag_id,
                rule="mode_requirement_flag_read",
                record=record,
                method=method,
                flag=flag,
            )

    def handle_mode_check(self, record: dict[str, Any]) -> None:
        method = record.get("method", "")
        texts = _args(record)
        mode_expr = str(record.get("mode_expr", ""))
        groups, states, mode_flags, mode_dynamic = _mode_facts(record, mode_expr)
        if method == "modePreventsArming":
            mode_flags = _unique([*mode_flags, "mode_req_prevent_arming"])
        source = _source(record)
        qname = f"mode:{record.get('caller', '')}::{method}@{source['file']}:{source['line']}:{source['column']}"
        negated = bool(record.get("negated", False))
        if method == "canArm":
            layer, effect = "mode_availability", "arming_query"
        elif method == "canRun":
            layer, effect = "mode_availability", "run_query"
        elif method == "modePreventsArming":
            layer, effect = "mode_control", "arming_query"
        else:
            layer, effect = "mode_availability", ("mode_prohibition" if negated else "mode_check")
        predicate = self.add_predicate(
            record,
            layer,
            effect,
            qname,
            method=method,
            mode_expr=mode_expr,
            mode_groups=groups,
            nav_states=states,
            mode_flags=mode_flags,
            mode_dynamic=mode_dynamic,
            negated=negated,
            receiver_type=record.get("receiver_type", ""),
        )
        self.mode_checks += 1
        if mode_dynamic:
            self.unresolved_record(
                "mode_set_dynamic",
                record,
                predicate=predicate["qualified_name"],
                method=method,
                expression=mode_expr,
            )
        targets = self.mode_targets(
            record, groups, states, mode_flags, text=mode_expr, dynamic=mode_dynamic
        )
        for target, domain in targets:
            self.add_edge(
                "checks_mode",
                predicate["id"],
                target,
                rule="mode_availability_check",
                record=record,
                method=method,
                mode_domain=domain,
            )
            if negated:
                self.add_edge(
                    "prevents_mode",
                    predicate["id"],
                    target,
                    rule="negated_mode_availability_check",
                    record=record,
                    method=method,
                    mode_domain=domain,
                )
                self.mode_prohibitions += 1
        for flag in mode_flags:
            flag_id = self.health_flag(flag, record)
            self.add_edge(
                "reads_flag",
                predicate["id"],
                flag_id,
                rule="mode_availability_flag_read",
                record=record,
                method=method,
                flag=flag,
            )

    def handle_failsafe_check(self, record: dict[str, Any]) -> None:
        flag = str(record.get("flag", ""))
        condition_flags = _unique(_strings(record, "condition_flags") + ([flag] if flag else []))
        actions = _unique(_strings(record, "actions"))
        action_dynamic = bool(record.get("action_dynamic", not actions))
        macro = record.get("macro", "") or "checkFailsafe"
        source = _source(record)
        qname = f"protection:{record.get('caller', '')}::{macro}@{source['file']}:{source['line']}:{source['column']}"
        predicate = self.add_predicate(
            record,
            "protection_condition",
            "protection_check",
            qname,
            macro=macro,
            flag=flag,
            flags=condition_flags,
            actions=actions,
            action_dynamic=action_dynamic,
            last_condition=record.get("last_condition", ""),
            current_condition=record.get("current_condition", ""),
            options=record.get("options", ""),
        )
        self.failsafe_checks += 1
        if action_dynamic:
            self.failsafe_actions_dynamic += 1
            self.unresolved_record(
                "protection_action_dynamic",
                record,
                predicate=predicate["qualified_name"],
                flag=flag,
                options=record.get("options", ""),
            )
        for condition_flag in condition_flags:
            flag_id = self.health_flag(condition_flag, record)
            self.add_edge(
                "reads_flag",
                predicate["id"],
                flag_id,
                rule="protection_condition_flag_read",
                record=record,
                flag=condition_flag,
            )
        for action in actions:
            if action == "Count":
                continue
            decision_id = self.decision_node(action, record)
            self.add_edge(
                "selects_action",
                predicate["id"],
                decision_id,
                rule="protection_action_options",
                record=record,
                action=action,
                macro=macro,
                flag=flag,
            )

    def handle_fallback_check(self, record: dict[str, Any]) -> None:
        mode_expr = str(record.get("mode_expr", ""))
        groups, states, mode_flags, mode_dynamic = _mode_facts(record, mode_expr)
        source = _source(record)
        qname = f"fallback:{record.get('caller', '')}::checkModeFallback@{source['file']}:{source['line']}:{source['column']}"
        predicate = self.add_predicate(
            record,
            "mode_fallback",
            "fallback_check",
            qname,
            method="checkModeFallback",
            mode_expr=mode_expr,
            nav_states=states,
            mode_flags=mode_flags,
            mode_dynamic=mode_dynamic,
        )
        if mode_dynamic:
            self.unresolved_record(
                "mode_set_dynamic",
                record,
                predicate=predicate["qualified_name"],
                method="checkModeFallback",
                expression=mode_expr,
            )
        for target, domain in self.mode_targets(
            record, groups, states, mode_flags, text=mode_expr, dynamic=mode_dynamic
        ):
            self.add_edge(
                "checks_mode",
                predicate["id"],
                target,
                rule="mode_fallback_call",
                record=record,
                mode_domain=domain,
            )
        for flag in mode_flags:
            flag_id = self.health_flag(flag, record)
            self.add_edge(
                "reads_flag",
                predicate["id"],
                flag_id,
                rule="mode_fallback_flag_read",
                record=record,
                flag=flag,
            )

    def handle_flag_access(self, record: dict[str, Any]) -> None:
        field = str(record.get("field", ""))
        if not field:
            return
        self.flag_accesses += 1
        flag_id = self.health_flag(field, record)
        caller_id = self.function_id(record)
        access = record.get("access", "read")
        if access == "write":
            self.flag_writes += 1
            self.writes_by_caller.setdefault(record.get("caller_usr", "") or record.get("caller", ""), set()).add(field)
            if caller_id:
                self.add_edge(
                    "writes_flag",
                    caller_id,
                    flag_id,
                    rule="failsafe_flag_write",
                    record=record,
                    flag=field,
                    write_op=record.get("write_op", ""),
                    receiver=record.get("receiver_expr", ""),
                )
            else:
                self.unresolved_record(
                    "health_caller_not_indexed",
                    record,
                    caller=record.get("caller", ""),
                    caller_usr=record.get("caller_usr", ""),
                    method=f"write:{field}",
                )
        else:
            self.flag_reads += 1
            if caller_id:
                self.add_edge(
                    "reads_flag",
                    caller_id,
                    flag_id,
                    rule="failsafe_flag_read",
                    record=record,
                    flag=field,
                    receiver=record.get("receiver_expr", ""),
                )
            else:
                self.unresolved_record(
                    "health_caller_not_indexed",
                    record,
                    caller=record.get("caller", ""),
                    caller_usr=record.get("caller_usr", ""),
                    method=f"read:{field}",
                )

    def handle_enum_value(self, record: dict[str, Any]) -> None:
        if record.get("enum_name") != "FailsafeBase::Action":
            return
        value = str(record.get("value_name", ""))
        if not value or value == "Count":
            return
        self.action_enum_seen.add(value)
        self.decision_node(value, record)

    def handle_action_ref(self, record: dict[str, Any]) -> None:
        action = str(record.get("action", ""))
        if not action or action == "Count":
            return
        self.action_refs += 1
        self.action_enum_seen.add(action)
        decision_id = self.decision_node(action, record)
        caller_id = self.function_id(record)
        context = record.get("context", "other")
        if context not in {"return", "assignment"}:
            return
        if not caller_id:
            self.unresolved_record(
                "health_caller_not_indexed",
                record,
                caller=record.get("caller", ""),
                caller_usr=record.get("caller_usr", ""),
                method=f"action:{action}",
            )
            return
        if context == "return":
            self.action_returns += 1
            self.add_edge(
                "returns_action",
                caller_id,
                decision_id,
                rule="action_return",
                record=record,
                action=action,
            )
        else:
            self.action_assignments += 1
            self.add_edge(
                "assigns_action",
                caller_id,
                decision_id,
                rule="action_assignment",
                record=record,
                action=action,
            )

    def add_same_function_flag_associations(self) -> None:
        """Link a health report to flags written by the same function.

        This is a lexical association, not dataflow.  It is deliberately
        low-confidence and labelled ``same_function_scope`` so reviewers do
        not mistake it for a causal edge.
        """
        for predicate in self.predicates:
            layer = predicate.get("predicate_layer", "")
            if layer not in HEALTH_REPORT_LAYERS:
                continue
            flags = self.writes_by_caller.get(predicate.get("caller_usr", ""))
            if flags is None:
                flags = self.writes_by_caller.get(predicate.get("caller", ""))
            if not flags:
                continue
            for field in sorted(flags):
                flag_id = self.health_flag(field, predicate)
                self.add_edge(
                    "reports_flag",
                    predicate["id"],
                    flag_id,
                    rule="same_function_scope",
                    record=predicate,
                    flag=field,
                    confidence="low",
                )

    def run(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        for record in self.raw:
            kind = record.get("record")
            if kind == "health_call":
                self.handle_health_call(record)
            elif kind == "mode_check":
                self.handle_mode_check(record)
            elif kind == "failsafe_check":
                self.handle_failsafe_check(record)
            elif kind == "fallback_check":
                self.handle_fallback_check(record)
            elif kind == "flag_access":
                self.handle_flag_access(record)
            elif kind == "enum_value":
                self.handle_enum_value(record)
            elif kind == "action_ref":
                self.handle_action_ref(record)

        self.add_same_function_flag_associations()

        nodes = list(self.nodes.values())
        edges = list(self.edges.values())
        node_counts = Counter(node["kind"] for node in nodes)
        edge_counts = Counter(edge["relation"] for edge in edges)
        layer_counts = Counter(p.get("predicate_layer", "") for p in self.predicates)
        effect_counts = Counter(p.get("effect", "") for p in self.predicates)
        severity_counts = Counter(p.get("severity", "") for p in self.predicates if p.get("severity"))
        action_counts = Counter(
            node.get("action", "")
            for node in nodes
            if node.get("kind") == "protection_decision"
        )
        health_flags = [n for n in nodes if n.get("kind") == "health_flag"]
        mapped_flags = [
            n for n in health_flags
            if any(
                e["relation"] == "maps_to_field" and e["src"] == n["id"]
                for e in edges
            )
        ]
        metrics = {
            "health_chain_nodes": len(nodes),
            "health_chain_edges": len(edges),
            "health_calls": self.health_calls,
            "health_predicates": layer_counts.get("health_check", 0)
            + layer_counts.get("arming_check", 0)
            + layer_counts.get("presence", 0)
            + layer_counts.get("health_state", 0),
            "arming_check_predicates": layer_counts.get("arming_check", 0),
            "mode_control_predicates": layer_counts.get("mode_control", 0),
            "mode_availability_predicates": layer_counts.get("mode_availability", 0),
            "mode_fallback_predicates": layer_counts.get("mode_fallback", 0),
            "protection_predicates": layer_counts.get("protection_condition", 0),
            "predicate_layers": dict(sorted(layer_counts.items())),
            "effect_counts": dict(sorted(effect_counts.items())),
            "severity_counts": dict(sorted(severity_counts.items())),
            "health_flags": len(health_flags),
            "health_flags_mapped": len(mapped_flags),
            "health_flags_unmapped": len(health_flags) - len(mapped_flags),
            "flag_accesses": self.flag_accesses,
            "flag_writes": self.flag_writes,
            "flag_reads": self.flag_reads,
            "mode_checks": self.mode_checks,
            "mode_prohibitions": self.mode_prohibitions,
            "failsafe_checks": self.failsafe_checks,
            "failsafe_checks_dynamic_action": self.failsafe_actions_dynamic,
            "protection_decisions": node_counts.get("protection_decision", 0),
            "action_references": self.action_refs,
            "action_returns": self.action_returns,
            "action_assignments": self.action_assignments,
            "actions": dict(sorted(action_counts.items())),
            "nodes_by_kind": dict(sorted(node_counts.items())),
            "edges_by_relation": dict(sorted(edge_counts.items())),
            "unresolved": len(self.unresolved),
        }
        return nodes, edges, metrics, self.unresolved


def build_health_chain(
    cfg: Config,
    raw_records: list[dict[str, Any]],
    function_ids: dict[str, str],
    schema_nodes: list[dict[str, Any]],
    commit: str = "",
    variant: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    builder = _Builder(cfg, raw_records, function_ids, schema_nodes, commit, variant)
    return builder.run()


def write_health_chain_csv(path, predicates: list[dict[str, Any]]) -> None:
    columns = [
        "predicate",
        "layer",
        "effect",
        "severity",
        "component",
        "event_id",
        "modes",
        "flags",
        "actions",
        "dynamic",
        "caller",
        "file",
        "line",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for predicate in predicates:
            source = predicate.get("source") or {}
            writer.writerow(
                {
                    "predicate": predicate.get("qualified_name", ""),
                    "layer": predicate.get("predicate_layer", ""),
                    "effect": predicate.get("effect", ""),
                    "severity": predicate.get("severity", ""),
                    "component": predicate.get("component", ""),
                    "event_id": predicate.get("event_id", ""),
                    "modes": ";".join(
                        [
                            *(predicate.get("mode_groups") or []),
                            *(predicate.get("nav_states") or []),
                            *(predicate.get("mode_flags") or []),
                        ]
                    ),
                    "flags": ";".join(predicate.get("flags") or []),
                    "actions": ";".join(predicate.get("actions") or []),
                    "dynamic": str(
                        bool(predicate.get("mode_dynamic") or predicate.get("action_dynamic"))
                    ).lower(),
                    "caller": predicate.get("caller", ""),
                    "file": source.get("file", ""),
                    "line": source.get("line", 0),
                }
            )
