"""
Reader over dbt's target/manifest.json.

manifest.json is dbt's own compiled description of the project: every model,
seed, source and test, with fully-qualified relation names, column-level
documentation and the dependency graph. Reading it is how the UI stays in sync
with the project without re-implementing any dbt parsing.

The file is reloaded whenever its mtime changes, so editing a model in the
editor and clicking Refresh in the UI is enough.
"""

from __future__ import annotations

import json
import pathlib
import threading
from typing import Any, Dict, List, Optional, Set

from . import config
from .config import LAYER_ORDER, layer_of


class ManifestNotFound(RuntimeError):
    """Raised when no manifest exists yet, i.e. dbt parse has never run."""


class Manifest:
    """One loaded manifest, plus the derived indexes the UI needs."""

    def __init__(self, raw: Dict[str, Any], path: pathlib.Path, mtime: float):
        self.raw = raw
        self.path = path
        self.mtime = mtime

        self.metadata: Dict[str, Any] = raw.get("metadata") or {}
        self.nodes: Dict[str, Any] = raw.get("nodes") or {}
        self.sources: Dict[str, Any] = raw.get("sources") or {}
        self.macros: Dict[str, Any] = raw.get("macros") or {}
        self.parent_map: Dict[str, List[str]] = raw.get("parent_map") or {}
        self.child_map: Dict[str, List[str]] = raw.get("child_map") or {}
        self.exposures: Dict[str, Any] = raw.get("exposures") or {}

        self._by_name: Dict[str, str] = {}
        self._sources_by_key: Dict[str, str] = {}
        self._build_indexes()

    # ------------------------------------------------------------------
    # indexes
    # ------------------------------------------------------------------

    def _build_indexes(self) -> None:
        for unique_id, node in self.nodes.items():
            if node.get("resource_type") in ("model", "seed", "snapshot"):
                self._by_name[node["name"]] = unique_id

        for unique_id, src in self.sources.items():
            key = f"{src.get('source_name')}.{src.get('name')}"
            self._sources_by_key[key] = unique_id

    # ------------------------------------------------------------------
    # lookups
    # ------------------------------------------------------------------

    def node_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        unique_id = self._by_name.get(name)
        return self.nodes.get(unique_id) if unique_id else None

    def unique_id_for_name(self, name: str) -> Optional[str]:
        return self._by_name.get(name)

    def any_by_unique_id(self, unique_id: str) -> Optional[Dict[str, Any]]:
        return self.nodes.get(unique_id) or self.sources.get(unique_id)

    def source_by_key(self, source_name: str, table_name: str) -> Optional[Dict[str, Any]]:
        unique_id = self._sources_by_key.get(f"{source_name}.{table_name}")
        return self.sources.get(unique_id) if unique_id else None

    def relation_for_name(self, name: str) -> Optional[str]:
        node = self.node_by_name(name)
        return node.get("relation_name") if node else None

    # ------------------------------------------------------------------
    # ref / source resolution, mirroring dbt's own behaviour
    # ------------------------------------------------------------------

    def ref_map(self) -> Dict[str, str]:
        """model/seed name -> fully qualified relation."""
        out: Dict[str, str] = {}
        for name, unique_id in self._by_name.items():
            node = self.nodes.get(unique_id) or {}
            relation = node.get("relation_name")
            if relation:
                out[name] = relation
        return out

    def source_map(self) -> Dict[str, str]:
        """'source_name.table_name' -> fully qualified relation."""
        out: Dict[str, str] = {}
        for key, unique_id in self._sources_by_key.items():
            src = self.sources.get(unique_id) or {}
            relation = src.get("relation_name")
            if relation:
                out[key] = relation
        return out

    # ------------------------------------------------------------------
    # summaries for the UI
    # ------------------------------------------------------------------

    def _test_index(self) -> Dict[str, List[Dict[str, Any]]]:
        """model unique_id -> the tests attached to it."""
        index: Dict[str, List[Dict[str, Any]]] = {}
        for unique_id, node in self.nodes.items():
            if node.get("resource_type") != "test":
                continue
            entry = {
                "unique_id": unique_id,
                "name": node.get("name"),
                "test_type": (node.get("test_metadata") or {}).get("name")
                or "singular",
                "column": (node.get("column_name") or None),
                "severity": (node.get("config") or {}).get("severity", "error"),
            }
            for parent in node.get("depends_on", {}).get("nodes", []) or []:
                index.setdefault(parent, []).append(entry)
        return index

    def buildable_nodes(self) -> List[Dict[str, Any]]:
        """Every model / seed / snapshot, summarised for the UI."""
        tests = self._test_index()
        out: List[Dict[str, Any]] = []

        for unique_id, node in self.nodes.items():
            resource_type = node.get("resource_type")
            if resource_type not in ("model", "seed", "snapshot"):
                continue

            cfg = node.get("config") or {}
            tags = node.get("tags") or []
            layer = layer_of(tags, node.get("fqn"), resource_type)
            node_tests = tests.get(unique_id, [])
            columns = node.get("columns") or {}

            documented = sum(
                1 for col in columns.values() if (col.get("description") or "").strip()
            )
            typed = sum(
                1 for col in columns.values() if (col.get("data_type") or "").strip()
            )

            out.append({
                "unique_id": unique_id,
                "name": node.get("name"),
                "resource_type": resource_type,
                "layer": layer,
                "layer_order": LAYER_ORDER.get(layer, 99),
                "description": node.get("description") or "",
                "materialized": cfg.get("materialized")
                or ("seed" if resource_type == "seed" else "view"),
                "database": node.get("database"),
                "schema": node.get("schema"),
                "alias": node.get("alias") or node.get("name"),
                "relation_name": node.get("relation_name"),
                "tags": tags,
                "path": node.get("path"),
                "original_file_path": node.get("original_file_path"),
                "patch_path": node.get("patch_path"),
                "column_count": len(columns),
                "documented_columns": documented,
                "typed_columns": typed,
                "test_count": len(node_tests),
                "tests": node_tests,
                "depends_on": [
                    dep for dep in
                    (node.get("depends_on", {}).get("nodes") or [])
                ],
                "children": [
                    child for child in self.child_map.get(unique_id, [])
                    if not child.startswith("test.")
                ],
                "partition_by": cfg.get("partition_by"),
                "cluster_by": cfg.get("cluster_by"),
                "has_description": bool((node.get("description") or "").strip()),
            })

        out.sort(key=lambda n: (n["layer_order"], n["name"]))
        return out

    def source_summaries(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for unique_id, src in self.sources.items():
            out.append({
                "unique_id": unique_id,
                "name": src.get("name"),
                "source_name": src.get("source_name"),
                "description": src.get("description") or "",
                "database": src.get("database"),
                "schema": src.get("schema"),
                "relation_name": src.get("relation_name"),
                "identifier": src.get("identifier"),
                "column_count": len(src.get("columns") or {}),
                "loaded_at_field": src.get("loaded_at_field"),
                "freshness": src.get("freshness"),
                "children": [
                    child for child in self.child_map.get(unique_id, [])
                    if not child.startswith("test.")
                ],
            })
        out.sort(key=lambda s: (s["source_name"] or "", s["name"] or ""))
        return out

    def node_detail(self, name: str) -> Optional[Dict[str, Any]]:
        """Everything the model drawer needs, including documented columns."""
        unique_id = self._by_name.get(name)
        if not unique_id:
            return None
        node = self.nodes[unique_id]
        summary = next(
            (n for n in self.buildable_nodes() if n["unique_id"] == unique_id),
            None,
        ) or {}

        columns = []
        for col_name, col in (node.get("columns") or {}).items():
            columns.append({
                "name": col_name,
                "data_type": (col.get("data_type") or "").upper() or None,
                "description": col.get("description") or "",
                "meta": col.get("meta") or {},
                "tags": col.get("tags") or [],
            })

        detail = dict(summary)
        detail.update({
            "columns": columns,
            "raw_code": node.get("raw_code") or "",
            "compiled_code": node.get("compiled_code") or "",
            "parents": [
                self._describe_ref(dep)
                for dep in (node.get("depends_on", {}).get("nodes") or [])
            ],
            "child_nodes": [
                self._describe_ref(child)
                for child in self.child_map.get(unique_id, [])
                if not child.startswith("test.")
            ],
        })
        return detail

    def _describe_ref(self, unique_id: str) -> Dict[str, Any]:
        node = self.any_by_unique_id(unique_id) or {}
        resource_type = node.get("resource_type", "")
        tags = node.get("tags") or []
        return {
            "unique_id": unique_id,
            "name": node.get("name") or unique_id.split(".")[-1],
            "resource_type": resource_type,
            "layer": layer_of(tags, node.get("fqn"), resource_type),
            "relation_name": node.get("relation_name"),
        }

    # ------------------------------------------------------------------
    # graph
    # ------------------------------------------------------------------

    def graph(self) -> Dict[str, Any]:
        """Node/edge lists for the lineage view. Tests are excluded as noise."""
        keep: Set[str] = set()
        nodes: List[Dict[str, Any]] = []

        for node in self.buildable_nodes():
            keep.add(node["unique_id"])
            nodes.append({
                "id": node["unique_id"],
                "name": node["name"],
                "layer": node["layer"],
                "layer_order": node["layer_order"],
                "resource_type": node["resource_type"],
                "materialized": node["materialized"],
                "test_count": node["test_count"],
            })

        for src in self.source_summaries():
            keep.add(src["unique_id"])
            nodes.append({
                "id": src["unique_id"],
                "name": f"{src['source_name']}.{src['name']}",
                "layer": "source",
                "layer_order": -1,
                "resource_type": "source",
                "materialized": "source",
                "test_count": 0,
            })

        edges = [
            {"source": parent, "target": child}
            for child, parents in self.parent_map.items()
            if child in keep
            for parent in (parents or [])
            if parent in keep
        ]

        return {"nodes": nodes, "edges": edges}

    def built_with_target(self) -> Optional[str]:
        """
        Which target dbt used when it parsed this manifest.

        dbt 1.12 does not put target_name in metadata, so it is inferred from a
        node's physical schema: a model whose schema is `dbt_dev_bronze` was
        parsed with the target whose dataset is `dbt_dev`. This matters because
        relation_name is frozen at parse time - the UI cannot repoint refs by
        changing a dropdown, it has to re-parse.
        """
        declared = self.metadata.get("target_name")
        if declared:
            return str(declared)

        from . import config  # local import: config imports nothing from here

        profiles = config.read_profiles()
        block = profiles.get(config.profile_name()) or {}
        outputs = block.get("outputs") or {}

        # Longest dataset first, so `dbt_dev` cannot shadow `dbt_dev_extra`.
        candidates = sorted(
            ((str(name), str((cfg or {}).get("dataset") or "").lower())
             for name, cfg in outputs.items()),
            key=lambda pair: -len(pair[1]),
        )

        schemas = {
            str(node.get("schema") or "").lower()
            for node in self.nodes.values()
            if node.get("resource_type") in ("model", "seed")
        }

        for target_name, dataset in candidates:
            if not dataset:
                continue
            if any(s == dataset or s.startswith(f"{dataset}_") for s in schemas):
                return target_name

        return None

    def target_mismatch(self, selected: Optional[str]) -> Optional[Dict[str, Any]]:
        """
        Warn when the selected target is not the one the manifest was built for.

        Silence here would be the worst option: every ref() would quietly
        resolve to the other environment's datasets.
        """
        built = self.built_with_target()
        if not selected or not built or str(built) == str(selected):
            return None

        return {
            "manifest_target": built,
            "selected_target": selected,
            "message": (
                f"The project was last loaded for the '{built}' environment, so "
                f"model references still point at {built}'s datasets even though "
                f"'{selected}' is selected."
            ),
            "fix": (
                f"Reload the project on '{selected}' to repoint them."
            ),
        }

    def stats(self) -> Dict[str, Any]:
        nodes = self.buildable_nodes()
        by_layer: Dict[str, int] = {}
        for node in nodes:
            by_layer[node["layer"]] = by_layer.get(node["layer"], 0) + 1

        total_columns = sum(n["column_count"] for n in nodes)
        documented_columns = sum(n["documented_columns"] for n in nodes)
        typed_columns = sum(n["typed_columns"] for n in nodes)
        test_count = sum(1 for n in self.nodes.values()
                         if n.get("resource_type") == "test")

        return {
            "dbt_version": self.metadata.get("dbt_version"),
            "project_name": self.metadata.get("project_name"),
            "adapter_type": self.metadata.get("adapter_type"),
            "generated_at": self.metadata.get("generated_at"),
            "target_name": self.metadata.get("target_name"),
            "model_count": sum(1 for n in nodes if n["resource_type"] == "model"),
            "seed_count": sum(1 for n in nodes if n["resource_type"] == "seed"),
            "source_count": len(self.sources),
            "test_count": test_count,
            "macro_count": sum(
                1 for m in self.macros.values()
                if (m.get("package_name") == self.metadata.get("project_name"))
            ),
            "by_layer": by_layer,
            "total_columns": total_columns,
            "documented_columns": documented_columns,
            "typed_columns": typed_columns,
            "doc_coverage": round(documented_columns / total_columns * 100, 1)
            if total_columns else 0.0,
            "type_coverage": round(typed_columns / total_columns * 100, 1)
            if total_columns else 0.0,
            "undocumented_models": [
                n["name"] for n in nodes if not n["has_description"]
            ],
            "untested_models": [
                n["name"] for n in nodes
                if n["test_count"] == 0 and n["resource_type"] == "model"
            ],
        }


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

_lock = threading.Lock()
_cached: Optional[Manifest] = None


def load(force: bool = False) -> Manifest:
    """Return the manifest, reloading when the file changed on disk."""
    global _cached

    path = config.MANIFEST_PATH
    if not path.exists():
        raise ManifestNotFound(
            "target/manifest.json not found. Run 'dbt parse' (the UI exposes "
            "this as Refresh manifest) to generate it."
        )

    mtime = path.stat().st_mtime
    with _lock:
        if _cached is not None and not force and _cached.mtime == mtime:
            return _cached

        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)

        _cached = Manifest(raw, path, mtime)
        return _cached


def invalidate() -> None:
    global _cached
    with _lock:
        _cached = None


def try_load() -> Optional[Manifest]:
    try:
        return load()
    except (ManifestNotFound, json.JSONDecodeError, OSError):
        return None


# --------------------------------------------------------------------------
# run_results.json - outcome of the most recent dbt invocation
# --------------------------------------------------------------------------

def last_run_results() -> Optional[Dict[str, Any]]:
    path = config.RUN_RESULTS_PATH
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return None

    results = []
    for item in raw.get("results", []) or []:
        unique_id = item.get("unique_id") or ""
        results.append({
            "unique_id": unique_id,
            "name": unique_id.split(".")[-1],
            "resource_type": unique_id.split(".")[0],
            "status": item.get("status"),
            "execution_time": round(float(item.get("execution_time") or 0), 3),
            "rows_affected": (item.get("adapter_response") or {}).get("rows_affected"),
            "bytes_processed": (item.get("adapter_response") or {}).get("bytes_processed"),
            "message": item.get("message"),
            "failures": item.get("failures"),
        })

    counts: Dict[str, int] = {}
    for item in results:
        key = str(item["status"])
        counts[key] = counts.get(key, 0) + 1

    metadata = raw.get("metadata") or {}
    return {
        "generated_at": metadata.get("generated_at"),
        "invocation_id": metadata.get("invocation_id"),
        "dbt_version": metadata.get("dbt_version"),
        "elapsed_time": round(float(raw.get("elapsed_time") or 0), 2),
        "args": {
            key: raw.get("args", {}).get(key)
            for key in ("which", "target", "select", "exclude", "full_refresh")
        },
        "counts": counts,
        "results": sorted(results, key=lambda r: -r["execution_time"]),
    }
