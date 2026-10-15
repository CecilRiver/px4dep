"""Build the parameter catalog from compiled metadata and PX4 YAML definitions."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

import yaml

from .config import Config
from .util import repo_rel, stable_id, write_jsonl


def _mapping_value(node: Any, key: str) -> Any:
    if not isinstance(node, yaml.MappingNode):
        return None
    for k, v in node.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return v
    return None


def _yaml_parameter_nodes(doc: Any):
    if not isinstance(doc, yaml.MappingNode):
        return
    params = _mapping_value(doc, "parameters")
    if not isinstance(params, yaml.SequenceNode):
        return
    for item in params.value:
        definitions = _mapping_value(item, "definitions")
        if not isinstance(definitions, yaml.MappingNode):
            continue
        for key_node, value_node in definitions.value:
            if isinstance(key_node, yaml.ScalarNode):
                yield key_node.value, value_node, key_node.start_mark.line + 1


def index_yaml_sources(cfg: Config) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Map parameter name -> YAML definition + source line."""
    index: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    files = sorted({p for pattern in cfg.param_yaml_globs for p in cfg.repo.glob(pattern) if p.is_file()})
    for path in files:
        try:
            with path.open("r", encoding="utf-8") as fh:
                docs = list(yaml.compose_all(fh))
            with path.open("r", encoding="utf-8") as fh:
                loaded_docs = list(yaml.safe_load_all(fh))
        except (OSError, yaml.YAMLError):
            continue

        line_by_name: dict[str, int] = {}
        for doc in docs:
            for name, _node, line in _yaml_parameter_nodes(doc):
                line_by_name.setdefault(name, line)

        definitions: dict[str, Any] = {}
        for doc in loaded_docs:
            if not isinstance(doc, dict):
                continue
            for group in doc.get("parameters", []) or []:
                if not isinstance(group, dict):
                    continue
                defs = group.get("definitions", {})
                if isinstance(defs, dict):
                    definitions.update(defs)

        for name, definition in definitions.items():
            if name in index:
                duplicates.append(name)
                continue
            if not isinstance(definition, dict):
                definition = {}
            index[name] = {
                "file": repo_rel(path, cfg.repo),
                "line": line_by_name.get(name, 0),
                "yaml": definition,
                "group": (definition.get("group") or ""),
            }
    return index, sorted(set(duplicates))


def _normalize_enum_values(raw_values: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(raw_values, list):
        for item in raw_values:
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if value is None:
                continue
            out[str(value)] = str(item.get("description", ""))
    elif isinstance(raw_values, dict):
        for key, value in raw_values.items():
            out[str(key)] = str(value)
    return out


def normalize_build_parameter(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": raw.get("name", ""),
        "type": raw.get("type", ""),
        "default": raw.get("default"),
        "min": raw.get("min"),
        "max": raw.get("max"),
        "increment": raw.get("increment"),
        "units": raw.get("units", ""),
        "group": raw.get("group", ""),
        "category": raw.get("category", ""),
        "reboot_required": bool(raw.get("rebootRequired", False)),
        "short_desc": raw.get("shortDesc", ""),
        "long_desc": raw.get("longDesc", ""),
        "decimal_places": raw.get("decimalPlaces"),
        "enum_values": _normalize_enum_values(raw.get("values")),
        "bitmask": raw.get("bitmask"),
        "volatile": bool(raw.get("volatile", False)),
    }


def _template_pattern(name: str) -> re.Pattern[str] | None:
    """PX4 YAML parameter names may use ${i}-style instance templates."""
    if "${" not in name:
        return None
    literals = re.split(r"\$\{[^}]+\}", name)
    parts = [re.escape(part) for part in literals]
    return re.compile(r"[0-9]+".join(parts) + r"$")


def _expand_template_sources(
    yaml_index: dict[str, dict[str, Any]],
    build_names: Iterable[str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    expanded: dict[str, dict[str, Any]] = {}
    ambiguities: list[dict[str, Any]] = []
    build_names = list(build_names)
    for template_name, source in sorted(yaml_index.items()):
        pattern = _template_pattern(template_name)
        if pattern is None:
            continue
        matches = [name for name in build_names if pattern.match(name)]
        for name in matches:
            if name in expanded:
                ambiguities.append(
                    {
                        "kind": "parameter_template_source_ambiguous",
                        "parameter_name": name,
                        "first_template": expanded[name].get("template_name", ""),
                        "second_template": template_name,
                    }
                )
                continue
            expanded[name] = {**source, "template_name": template_name}
    return expanded, ambiguities


def _yaml_definition_to_parameter(name: str, source: dict[str, Any]) -> dict[str, Any]:
    definition = source.get("yaml", {})
    description = definition.get("description", {})
    if not isinstance(description, dict):
        description = {}
    return {
        "name": name,
        "type": definition.get("type", ""),
        "default": definition.get("default"),
        "min": definition.get("min"),
        "max": definition.get("max"),
        "increment": definition.get("increment"),
        "units": definition.get("unit", ""),
        "group": source.get("group", ""),
        "category": "",
        "reboot_required": bool(definition.get("reboot_required", False)),
        "short_desc": description.get("short", ""),
        "long_desc": description.get("long", ""),
        "decimal_places": definition.get("decimal"),
        "enum_values": _normalize_enum_values(definition.get("values")),
        "bitmask": definition.get("bitmask"),
        "volatile": False,
    }


def _param_find_registrations(raw_records: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    registrations: dict[str, list[dict[str, Any]]] = {}
    for record in raw_records:
        if record.get("record") != "call" or record.get("api") != "parameter":
            continue
        # Only param_find* takes the parameter name as its first argument;
        # param_get/param_set take an opaque param_t handle.
        if record.get("method") not in {"param_find", "param_find_no_notification"}:
            continue
        args = record.get("args") or []
        if not args:
            continue
        text = str(args[0].get("text", "")).strip().strip('"')
        if not text or not text[0].isalpha():
            continue
        registrations.setdefault(text, []).append(
            {
                "file": record.get("file", ""),
                "line": record.get("line", 0),
                "column": record.get("column", 0),
                "caller": record.get("caller", ""),
                "method": record.get("method", ""),
            }
        )
    return registrations


def build_catalog(
    cfg: Config,
    raw_records: Iterable[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    raw_records = list(raw_records or [])
    yaml_index, yaml_duplicates = index_yaml_sources(cfg)

    with cfg.parameters_json.open("r", encoding="utf-8") as fh:
        build_data = json.load(fh)
    build_params = [normalize_build_parameter(p) for p in build_data.get("parameters", [])]
    build_by_name = {p["name"]: p for p in build_params}
    expanded_sources, template_ambiguities = _expand_template_sources(
        yaml_index, build_by_name.keys()
    )

    def source_for(name: str) -> dict[str, Any] | None:
        return yaml_index.get(name) or expanded_sources.get(name)

    wrappers_by_param: dict[str, list[dict[str, Any]]] = {}
    for record in raw_records:
        if record.get("record") != "param_wrapper":
            continue
        wrappers_by_param.setdefault(record.get("parameter_name", ""), []).append(record)

    registrations = _param_find_registrations(raw_records)
    commit = _git_commit(cfg)
    variant = cfg.build_variant

    records: list[dict[str, Any]] = []
    for param in build_params:
        name = param["name"]
        source = source_for(name)
        wrapper_records = wrappers_by_param.get(name, [])
        wrapper_ids = [
            stable_id(commit, variant, "param_wrapper", w.get("qualified_name", ""), w.get("file", ""), w.get("line", 0))
            for w in wrapper_records
        ]
        record = {
            "id": stable_id(commit, variant, "parameter", name, "", ""),
            "kind": "parameter",
            "qualified_name": name,
            "name": name,
            "build_variant": variant,
            "in_build": True,
            "source": source if source else None,
            "source_kind": "yaml" if source else "build_metadata_only",
            "wrapper_entities": wrapper_ids,
            "wrapper_count": len(wrapper_ids),
            "registration_points": registrations.get(name, []),
            "status": "in_build",
            **param,
        }
        if source:
            record["status"] = "resolved"
        if wrapper_ids:
            record["status"] = "bound" if source else "bound_no_source"
        records.append(record)

    source_only = []
    for name, source in sorted(yaml_index.items()):
        if name in build_by_name or "${" in name:
            continue
        base = _yaml_definition_to_parameter(name, source)
        record = {
            "id": stable_id(commit, variant, "parameter", name, "", ""),
            "kind": "parameter",
            "qualified_name": name,
            "build_variant": variant,
            "in_build": False,
            "source": source,
            "source_kind": "yaml",
            "wrapper_entities": [],
            "wrapper_count": 0,
            "registration_points": registrations.get(name, []),
            "status": "excluded_by_build",
            **base,
        }
        records.append(record)
        source_only.append(name)

    unresolved = list(template_ambiguities)
    for name, wrappers in sorted(wrappers_by_param.items()):
        if name in build_by_name:
            continue
        unresolved.append(
            {
                "kind": "parameter_missing_from_build",
                "parameter_name": name,
                "wrappers": wrappers,
            }
        )

    template_names = [name for name in yaml_index if "${" in name]
    metrics = {
        "build_parameter_count": len(build_params),
        "source_yaml_parameter_count": len(yaml_index),
        "source_yaml_template_count": len(template_names),
        "source_only_parameter_count": len(source_only),
        "source_only_parameters": source_only,
        "yaml_duplicate_names": yaml_duplicates,
        "template_expanded_parameters": len(expanded_sources),
        "template_ambiguities": len(template_ambiguities),
        "wrapper_count": sum(len(v) for v in wrappers_by_param.values()),
        "build_parameters_with_wrapper": sum(1 for p in build_params if p["name"] in wrappers_by_param),
        "build_parameters_with_yaml_source": sum(1 for p in build_params if source_for(p["name"])),
        "parameters_with_param_find": len(registrations),
        "unresolved_parameter_wrappers": sum(
            1 for r in unresolved if r["kind"] == "parameter_missing_from_build"
        ),
        "parameter_parse_rate": round(len(build_params) / max(1, len(build_data.get("parameters", []))), 6),
    }
    return records, metrics, unresolved


def _git_commit(cfg: Config) -> str:
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "-C", str(cfg.repo), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def write_catalog(run, records: list[dict[str, Any]], metrics: dict[str, Any], unresolved: list[dict[str, Any]]) -> None:
    write_jsonl(run.path("normalized", "parameters.jsonl"), records)
    write_jsonl(run.path("unresolved", "parameters.jsonl"), unresolved)
    from .util import write_json

    write_json(run.path("reports", "parameter_metrics.json"), metrics)
