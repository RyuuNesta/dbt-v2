"""
Entity relationship model, derived from the dbt project.

The lineage graph on the Catalog page answers "what builds what". An ERD answers
a different question: "how do these tables join, and on what". This module
derives the second from three independent signals, ordered by how much they can
be trusted:

  declared   a dbt `relationships` test. The project has explicitly stated that
             column X of A references column Y of B, and the build fails if it
             stops being true. Nothing beats this.
  lineage    a ref() dependency. B reads A, so they are certainly related; the
             join key is found by intersecting their keys. The dependency is a
             fact, the key is an inference.
  inferred   column naming. A column called `<stem>_id` or `<stem>_key` in one
             table matching the primary key of another. Convention, not proof.
  constraint a BigQuery PRIMARY KEY / FOREIGN KEY declaration read from
             INFORMATION_SCHEMA. Unenforced in BigQuery, but a real statement of
             intent. Costs a query, so it is opt-in.

Every relationship carries its `kind`, a `confidence`, and the `evidence` that
produced it, because an inferred edge and a tested edge look identical on a
diagram and should not. A reviewer needs to know which lines are load-bearing.

Nothing here queries the warehouse unless asked to: the whole model comes from
target/manifest.json, so the ERD works with expired credentials and costs
nothing to open.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import config

# Suffixes that make a column look like an identifier rather than a value. Order
# matters only for stem extraction, not for matching.
KEY_SUFFIXES = ("_key", "_id", "_code", "_number", "_no", "_fk", "_sk")

# Audit and lineage bookkeeping. These appear in every table by design, so
# matching on them would relate everything to everything.
AUDIT_PREFIX = "_"
NEVER_A_KEY = {
    "_source_relation",
    "_bronze_loaded_at",
    "_silver_loaded_at",
    "_gold_loaded_at",
    "_dbt_invocation_id",
    "_dbt_target",
    "_loaded_at",
}

# Columns whose name looks like a key but which are really low-cardinality
# attributes shared by every table in a warehouse. Joining on these produces a
# fan-out, not a relationship.
GENERIC_COLUMNS = {
    "currency",
    "period_month",
    "period_year",
    "period_quarter",
    "fiscal_year",
    "posting_date",
    "created_at",
    "updated_at",
}

_REF_RE = re.compile(r"ref\(\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_SOURCE_RE = re.compile(
    r"source\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]", re.IGNORECASE
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _dataset_of(relation: str) -> str:
    """`project`.`dataset`.`table` -> dataset, tolerating missing backticks."""
    parts = [p.strip("`") for p in str(relation or "").split(".")]
    return parts[1].lower() if len(parts) >= 3 else ""


def _is_audit(name: str) -> bool:
    return name in NEVER_A_KEY or name.startswith(AUDIT_PREFIX)


def _looks_like_key(name: str) -> bool:
    if _is_audit(name) or name in GENERIC_COLUMNS:
        return False
    return name.endswith(KEY_SUFFIXES)


def _key_stem(name: str) -> str:
    """`gl_entry_key` -> `gl_entry`. Used to match a column against a table."""
    for suffix in KEY_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _singular(name: str) -> str:
    """
    Crude singularisation, enough for table-to-column matching.

    `gl_entries` -> `gl_entry` is the case that matters: this project names
    tables in the plural and keys in the singular, so without the -ies rule a
    column would never match the table it points at.
    """
    if name.endswith("ies") and len(name) > 3:
        return f"{name[:-3]}y"
    if name.endswith("ss"):
        return name
    if name.endswith("s") and len(name) > 1:
        return name[:-1]
    return name


def _match_stem(name: str) -> str:
    """The comparable form of a column or table name."""
    return _singular(_key_stem(name))


def _strip_layer_prefix(name: str) -> str:
    for prefix in ("bronze_", "silver_", "gold_", "stg__", "stg_", "staging_",
                   "raw_", "src_", "dim_", "fact_", "kpi_"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _cardinality(left_unique: bool, right_unique: bool) -> str:
    """
    Cardinality written left-to-right, matching the emitted from/to order.

    Uniqueness is read from `unique` tests and PK declarations, so "1" means the
    project asserts one row per key - not merely that today's data happens to
    have one.
    """
    if left_unique and right_unique:
        return "1:1"
    if left_unique:
        return "1:N"
    if right_unique:
        return "N:1"
    return "N:N"


# --------------------------------------------------------------------------
# table extraction
# --------------------------------------------------------------------------

def _column_tests(mf) -> Dict[Tuple[str, str], Set[str]]:
    """(node unique_id, column name) -> the generic tests declared on it."""
    index: Dict[Tuple[str, str], Set[str]] = {}
    for node in mf.nodes.values():
        if node.get("resource_type") != "test":
            continue
        column = node.get("column_name")
        if not column:
            continue
        test_type = (node.get("test_metadata") or {}).get("name") or "singular"
        for parent in (node.get("depends_on", {}).get("nodes") or []):
            index.setdefault((parent, str(column)), set()).add(str(test_type))
    return index


def _tables(mf, target: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Every model, seed and source as an ERD entity, keyed by unique_id."""
    tests = _column_tests(mf)
    out: Dict[str, Dict[str, Any]] = {}

    for summary in mf.buildable_nodes():
        unique_id = summary["unique_id"]
        raw = mf.nodes.get(unique_id) or {}
        dataset = _dataset_of(summary.get("relation_name") or "")

        columns = []
        for name, column in (raw.get("columns") or {}).items():
            declared = sorted(tests.get((unique_id, name), set()))
            columns.append({
                "name": name,
                "data_type": (column.get("data_type") or "").lower() or None,
                "description": column.get("description") or "",
                "tests": declared,
                "is_unique": "unique" in declared,
                "not_null": "not_null" in declared,
                "looks_like_key": _looks_like_key(name),
                "is_audit": _is_audit(name),
            })

        out[unique_id] = {
            "id": unique_id,
            "name": summary["name"],
            "layer": summary["layer"],
            "layer_order": summary["layer_order"],
            "resource_type": summary["resource_type"],
            "materialized": summary["materialized"],
            "relation": summary.get("relation_name"),
            "dataset": dataset,
            "in_scope": config.dataset_allowed(dataset, target),
            "description": summary.get("description") or "",
            "test_count": summary.get("test_count") or 0,
            "tags": summary.get("tags") or [],
            "columns": columns,
            "column_count": len(columns),
            "row_count": None,
            "size_bytes": None,
            "last_modified": None,
        }

    for source in mf.source_summaries():
        unique_id = source["unique_id"]
        raw = mf.sources.get(unique_id) or {}
        dataset = _dataset_of(source.get("relation_name") or "")

        columns = []
        for name, column in (raw.get("columns") or {}).items():
            declared = sorted(tests.get((unique_id, name), set()))
            columns.append({
                "name": name,
                "data_type": (column.get("data_type") or "").lower() or None,
                "description": column.get("description") or "",
                "tests": declared,
                "is_unique": "unique" in declared,
                "not_null": "not_null" in declared,
                "looks_like_key": _looks_like_key(name),
                "is_audit": _is_audit(name),
            })

        out[unique_id] = {
            "id": unique_id,
            "name": f"{source['source_name']}.{source['name']}",
            "layer": "source",
            "layer_order": -1,
            "resource_type": "source",
            "materialized": "source",
            "relation": source.get("relation_name"),
            "dataset": dataset,
            "in_scope": config.dataset_allowed(dataset, target),
            "description": source.get("description") or "",
            "test_count": 0,
            "tags": [],
            "columns": columns,
            "column_count": len(columns),
            "row_count": None,
            "size_bytes": None,
            "last_modified": None,
        }

    # Primary key per table, most trustworthy signal first.
    for table in out.values():
        primary, declared = _primary_key(table)
        table["primary_key"] = primary
        # Whether the key is asserted by a `unique` test or merely matches the
        # naming convention. Inference leans on this: guessing a foreign key
        # against another guess compounds two assumptions into a drawn line.
        table["primary_key_declared"] = declared
        table["key_columns"] = sorted(
            {*primary,
             *(c["name"] for c in table["columns"] if c["looks_like_key"])}
        )
        pk = set(primary)
        for column in table["columns"]:
            column["is_primary"] = column["name"] in pk

    return out


def _primary_key(table: Dict[str, Any]) -> Tuple[List[str], bool]:
    """
    The column set that identifies one row, and whether it was declared.

    A `unique` test is a declaration and wins outright. Failing that, a column
    named after the table with a key suffix is this project's convention
    (`silver_gl_entries` -> `gl_entry_key`). The second is a guess, so the caller
    is told which it got.
    """
    declared = [c["name"] for c in table["columns"] if c["is_unique"]]
    if declared:
        return sorted(declared), True

    stem = _match_stem(_strip_layer_prefix(table["name"]))
    for column in table["columns"]:
        name = column["name"]
        if not _looks_like_key(name):
            continue
        if _match_stem(name) == stem:
            return [name], False

    return [], False


# --------------------------------------------------------------------------
# relationship detection
# --------------------------------------------------------------------------

def _relationship(
    from_table: Dict[str, Any],
    from_columns: Sequence[str],
    to_table: Dict[str, Any],
    to_columns: Sequence[str],
    kind: str,
    confidence: str,
    evidence: str,
    cardinality: str = "",
) -> Dict[str, Any]:
    return {
        "id": f"{kind}:{from_table['id']}->{to_table['id']}:"
              f"{'+'.join(from_columns) or '?'}",
        "from_table": from_table["id"],
        "from_name": from_table["name"],
        "from_columns": list(from_columns),
        "to_table": to_table["id"],
        "to_name": to_table["name"],
        "to_columns": list(to_columns),
        "kind": kind,
        "confidence": confidence,
        "evidence": evidence,
        "cardinality": cardinality,
    }


def _declared_relationships(mf, tables: Dict[str, Any]) -> List[Dict[str, Any]]:
    """dbt `relationships` tests: the only edges the build itself enforces."""
    found: List[Dict[str, Any]] = []

    for node in mf.nodes.values():
        if node.get("resource_type") != "test":
            continue
        metadata = node.get("test_metadata") or {}
        if (metadata.get("name") or "").lower() != "relationships":
            continue

        kwargs = metadata.get("kwargs") or {}
        child_column = str(node.get("column_name") or kwargs.get("column_name") or "")
        parent_field = str(kwargs.get("field") or "")

        # `to` is rendered Jinja: ref('x') or source('a','b').
        to_raw = str(kwargs.get("to") or "")
        parent_id = None
        ref_match = _REF_RE.search(to_raw)
        source_match = _SOURCE_RE.search(to_raw)
        if ref_match:
            parent_id = mf.unique_id_for_name(ref_match.group(1))
        elif source_match:
            source_node = mf.source_by_key(source_match.group(1), source_match.group(2))
            parent_id = source_node.get("unique_id") if source_node else None
            if parent_id is None and source_node is not None:
                # source_summaries carries the id; fall back to a key lookup.
                parent_id = next(
                    (uid for uid, src in mf.sources.items() if src is source_node),
                    None,
                )

        depends = [
            dep for dep in (node.get("depends_on", {}).get("nodes") or [])
            if dep in tables
        ]
        child_id = node.get("attached_node")
        if child_id not in tables:
            child_id = next((dep for dep in depends if dep != parent_id), None)
        if parent_id not in tables:
            parent_id = next((dep for dep in depends if dep != child_id), None)

        if not child_id or not parent_id or child_id not in tables or parent_id not in tables:
            continue

        child = tables[child_id]
        parent = tables[parent_id]
        child_unique = _columns_unique(child, [child_column])
        parent_unique = _columns_unique(parent, [parent_field])

        found.append(_relationship(
            child, [child_column] if child_column else [],
            parent, [parent_field] if parent_field else [],
            "declared",
            "high",
            f"A dbt relationships test asserts every {child['name']}."
            f"{child_column} exists in {parent['name']}.{parent_field}. "
            "The build fails if that stops being true.",
            _cardinality(child_unique, parent_unique),
        ))

    return found


def _columns_unique(table: Dict[str, Any], columns: Sequence[str]) -> bool:
    """True when the given column set is declared unique on that table."""
    wanted = [c for c in columns if c]
    if not wanted:
        return False
    primary = set(table.get("primary_key") or [])
    if primary and set(wanted) == primary:
        return True
    by_name = {c["name"]: c for c in table["columns"]}
    return all((by_name.get(name) or {}).get("is_unique") for name in wanted)


def _lineage_relationships(mf, tables: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    ref() dependencies, with the join key inferred by intersecting keys.

    The dependency is a fact - dbt will not build the child before the parent.
    The *key* is the inference, so an edge where no shared key could be found is
    still emitted, with an empty column list and evidence saying so. Dropping it
    would make the diagram claim two tables are unrelated when the project says
    otherwise.
    """
    found: List[Dict[str, Any]] = []

    for child_id, parents in mf.parent_map.items():
        if child_id not in tables:
            continue
        child = tables[child_id]

        for parent_id in (parents or []):
            if parent_id not in tables or parent_id == child_id:
                continue
            parent = tables[parent_id]

            shared = _shared_keys(parent, child)
            parent_unique = _columns_unique(parent, shared)
            child_unique = _columns_unique(child, shared)

            if shared:
                evidence = (
                    f"{child['name']} reads {parent['name']} through ref(), and "
                    f"both declare {', '.join(shared)}"
                    + (" as unique" if parent_unique and child_unique else "")
                    + "."
                )
                confidence = "high" if (parent_unique or child_unique) else "medium"
                cardinality = _cardinality(parent_unique, child_unique)
            else:
                evidence = (
                    f"{child['name']} reads {parent['name']} through ref(), but "
                    "no column is declared as a key on both, so the join key "
                    "could not be determined from the project."
                )
                confidence = "low"
                cardinality = ""

            found.append(_relationship(
                parent, shared, child, shared,
                "lineage", confidence, evidence, cardinality,
            ))

    return found


def _shared_keys(left: Dict[str, Any], right: Dict[str, Any]) -> List[str]:
    """
    The best shared join key between two tables.

    Preference order: a primary key both sides carry, then any key-looking
    column present on both, then nothing. Generic columns are never a key on
    their own - `period_month` appearing in two tables is a grain, not a
    relationship - but they are allowed to travel alongside a real key.
    """
    left_names = {c["name"] for c in left["columns"]}
    right_names = {c["name"] for c in right["columns"]}
    common = left_names & right_names

    primary = set(left.get("primary_key") or []) | set(right.get("primary_key") or [])
    shared_primary = sorted(primary & common)
    if shared_primary:
        return shared_primary

    key_like = sorted(
        name for name in common
        if _looks_like_key(name)
    )
    return key_like


def _lineage_connected(mf, tables: Dict[str, Any]) -> Set[Tuple[str, str]]:
    """
    Every pair already joined by a lineage path, in both directions.

    Used to suppress inferred edges between tables the DAG already connects. If
    silver reads bronze which reads the source, the source's relationship to
    silver is already on the diagram as two lineage hops; adding a guessed
    foreign key on top of that draws a third line that points the wrong way.
    """
    ancestors: Dict[str, Set[str]] = {}

    def walk(node_id: str, seen: Set[str]) -> Set[str]:
        if node_id in ancestors:
            return ancestors[node_id]
        if node_id in seen:
            return set()          # cyclic manifests should not hang the ERD
        seen = seen | {node_id}
        found: Set[str] = set()
        for parent in (mf.parent_map.get(node_id) or []):
            found.add(parent)
            found |= walk(parent, seen)
        ancestors[node_id] = found
        return found

    pairs: Set[Tuple[str, str]] = set()
    for unique_id in tables:
        for ancestor in walk(unique_id, set()):
            if ancestor in tables:
                pairs.add((unique_id, ancestor))
                pairs.add((ancestor, unique_id))
    return pairs


def _inferred_relationships(mf,
                            tables: Dict[str, Any],
                            existing: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Foreign keys guessed from column naming.

    Two conditions have to hold together, and it is the pair that keeps this
    from being noise:

      1. the column's name (or its key stem) must name a table that actually
         exists in the diagram
      2. that table must declare a primary key whose stem matches

    So `cost_center` finds `silver_cost_center` because that table calls its key
    `cost_center_key`. `vendor_customer` invents nothing, because no such table
    exists. And `company_code` never relates two fact tables that merely both
    carry it, because neither declares it as a key.

    A key suffix is deliberately *not* required on the child column: this
    project's foreign keys are `cost_center`, `gl_account` and `vendor_customer`,
    with no suffix at all. Requiring one would detect nothing real.

    Two further suppressions, both learned from what this project produced:

      - the parent's key must be *declared* unique, not itself guessed from the
        naming convention. Otherwise two tables that merely follow the same
        convention get linked, which is how a source ended up "referencing" the
        seed feeding the same table.
      - tables already connected by a lineage path are skipped. That
        relationship is on the diagram already, and the inferred version of it
        frequently points upstream.
    """
    found: List[Dict[str, Any]] = []
    seen = {(rel["from_table"], rel["to_table"]) for rel in existing}
    seen |= {(rel["to_table"], rel["from_table"]) for rel in existing}
    seen |= _lineage_connected(mf, tables)

    by_stem: Dict[str, List[Dict[str, Any]]] = {}
    for table in tables.values():
        stem = _match_stem(_strip_layer_prefix(table["name"]))
        by_stem.setdefault(stem, []).append(table)

    for child in tables.values():
        for column in child["columns"]:
            name = column["name"]
            if column["is_primary"] or column["is_audit"]:
                continue
            if name in GENERIC_COLUMNS:
                continue

            stem = _match_stem(name)
            if not stem:
                continue

            for parent in by_stem.get(stem, []):
                if parent["id"] == child["id"]:
                    continue
                if (child["id"], parent["id"]) in seen:
                    continue

                parent_pk = parent.get("primary_key") or []
                # A guessed foreign key pointing at a guessed primary key is two
                # assumptions stacked. Require the target to be declared.
                if not parent_pk or not parent.get("primary_key_declared"):
                    continue

                exact = name in parent_pk
                stem_match = stem in {_match_stem(pk) for pk in parent_pk}
                # The guess only stands if the other table agrees this is its key.
                if not exact and not stem_match:
                    continue

                target_column = name if exact else parent_pk[0]
                found.append(_relationship(
                    child, [name], parent, [target_column],
                    "inferred",
                    "medium" if exact else "low",
                    f"{child['name']}.{name} is named after {parent['name']}, "
                    f"whose key is {target_column}. No relationships test "
                    "confirms this - add one to make it enforced.",
                    _cardinality(_columns_unique(child, [name]), True),
                ))
                seen.add((child["id"], parent["id"]))

    return found


# --------------------------------------------------------------------------
# BigQuery declared constraints (opt-in)
# --------------------------------------------------------------------------

def constraint_relationships(
    tables: Dict[str, Any],
    target: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Read PRIMARY KEY / FOREIGN KEY declarations from INFORMATION_SCHEMA.

    BigQuery does not enforce these, but a team that declared them meant them.
    One query per dataset, and every failure is downgraded to a warning: older
    regions and older tables simply do not expose these views, and an ERD that
    refused to draw because of that would be useless.
    """
    from . import warehouse  # local: keeps the module importable without BigQuery

    warnings: List[str] = []
    found: List[Dict[str, Any]] = []

    by_relation: Dict[str, Dict[str, Any]] = {}
    datasets: Set[str] = set()
    for table in tables.values():
        relation = str(table.get("relation") or "")
        if not relation:
            continue
        parts = [p.strip("`") for p in relation.split(".")]
        if len(parts) < 3:
            continue
        by_relation[f"{parts[1].lower()}.{parts[2].lower()}"] = table
        if table["in_scope"]:
            datasets.add(parts[1].lower())

    for dataset in sorted(datasets):
        sql = f"""
select
  kcu.table_name          as child_table,
  kcu.column_name         as child_column,
  ccu.table_name          as parent_table,
  ccu.column_name         as parent_column,
  tc.constraint_name      as constraint_name
from `{dataset}`.INFORMATION_SCHEMA.TABLE_CONSTRAINTS       as tc
join `{dataset}`.INFORMATION_SCHEMA.KEY_COLUMN_USAGE        as kcu
  on tc.constraint_name = kcu.constraint_name
join `{dataset}`.INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE as ccu
  on tc.constraint_name = ccu.constraint_name
where tc.constraint_type = 'FOREIGN KEY'
"""
        try:
            result = warehouse.execute(
                sql, target=target, limit=500, apply_limit=False,
            )
        except Exception as exc:  # noqa: BLE001 - any failure is non-fatal here
            warnings.append(
                f"Could not read declared constraints for {dataset}: {exc}"
            )
            continue

        for row in result.rows:
            child_table, child_column, parent_table, parent_column, name = row[:5]
            child = by_relation.get(f"{dataset}.{str(child_table).lower()}")
            parent = by_relation.get(f"{dataset}.{str(parent_table).lower()}")
            if not child or not parent:
                continue

            found.append(_relationship(
                child, [str(child_column)], parent, [str(parent_column)],
                "constraint",
                "high",
                f"BigQuery constraint {name} declares "
                f"{child['name']}.{child_column} references "
                f"{parent['name']}.{parent_column}. BigQuery does not enforce "
                "it, but it was declared deliberately.",
                _cardinality(False, True),
            ))

    return found, warnings


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def build(
    mf,
    target: Optional[str] = None,
    *,
    in_scope_only: bool = False,
    datasets: Optional[Sequence[str]] = None,
    only_tables: Optional[Sequence[str]] = None,
    include_staging: bool = True,
    include_sources: bool = True,
    with_counts: bool = False,
    with_constraints: bool = False,
) -> Dict[str, Any]:
    """
    Assemble the ERD.

    `only_tables` accepts model names or `dataset.table`, which is what the
    shared table picker hands out.

    Visibility deliberately mirrors the existing lineage graph rather than the
    read-scope guard: gold and anything else outside the permitted datasets is
    shown by default, each table carrying `in_scope` so the frontend can dim it.
    The project already rejected hiding out-of-scope nodes elsewhere - "hiding
    them would make the lineage look wrong" - and an ERD that a reviewer opens
    to see how gold actually depends on silver should not go blank on the one
    layer that question is about. `in_scope_only` is offered for someone who
    explicitly wants a bronze/silver-only diagram to export.
    """
    warnings: List[str] = []
    everything = _tables(mf, target)

    # ---------------- scope selection ----------------

    wanted: Dict[str, Dict[str, Any]] = {}
    hidden_out_of_scope = 0

    requested = {str(name).lower() for name in (only_tables or [])}
    dataset_filter = {str(name).lower() for name in (datasets or [])}

    for unique_id, table in everything.items():
        if in_scope_only and not table["in_scope"]:
            hidden_out_of_scope += 1
            continue
        if not include_sources and table["resource_type"] == "source":
            continue
        if not include_staging and _is_staging(table):
            continue
        if dataset_filter and table["dataset"] not in dataset_filter:
            continue
        if requested:
            qualified = f"{table['dataset']}.{_relation_table(table)}".lower()
            if table["name"].lower() not in requested and qualified not in requested:
                continue
        wanted[unique_id] = table

    # ---------------- relationships ----------------

    relationships = _declared_relationships(mf, wanted)
    relationships += _lineage_relationships(mf, wanted)
    relationships += _inferred_relationships(mf, wanted, relationships)

    if with_constraints:
        constraint_edges, constraint_warnings = constraint_relationships(wanted, target)
        relationships += constraint_edges
        warnings.extend(constraint_warnings)

    # ---------------- physical annotation ----------------

    if with_counts:
        warnings.extend(_annotate_counts(wanted, target))

    # ---------------- stats ----------------

    by_kind: Dict[str, int] = {}
    for relationship in relationships:
        by_kind[relationship["kind"]] = by_kind.get(relationship["kind"], 0) + 1

    if hidden_out_of_scope:
        warnings.append(
            f"{hidden_out_of_scope} table(s) outside the permitted dataset "
            "scope are not shown."
        )

    tables_out = sorted(
        wanted.values(),
        key=lambda t: (t["layer_order"], t["name"]),
    )

    return {
        "tables": tables_out,
        "relationships": relationships,
        "layers": [
            {"key": layer.key, "label": layer.label, "order": layer.order}
            for layer in config.LAYERS
        ],
        "stats": {
            "table_count": len(tables_out),
            "column_count": sum(t["column_count"] for t in tables_out),
            "relationship_count": len(relationships),
            "by_kind": by_kind,
            "keyless_tables": [
                t["name"] for t in tables_out if not t["primary_key"]
            ],
            "hidden_out_of_scope": hidden_out_of_scope,
            "blocked_layers": sorted(set(config.blocked_build_layers())),
        },
        "scope": config.scope_description(target),
        "manifest_generated_at": (mf.metadata or {}).get("generated_at"),
        "warnings": warnings,
    }


def _relation_table(table: Dict[str, Any]) -> str:
    parts = [p.strip("`") for p in str(table.get("relation") or "").split(".")]
    return parts[2].lower() if len(parts) >= 3 else str(table["name"]).lower()


def _is_staging(table: Dict[str, Any]) -> bool:
    name = str(table["name"])
    return (
        name.startswith("stg__")
        or name.startswith("stg_")
        or name.startswith("staging_")
        or "staging" in (table.get("tags") or [])
    )


def _annotate_counts(tables: Dict[str, Any], target: Optional[str]) -> List[str]:
    """
    Attach row counts and sizes from free table metadata.

    Best effort on purpose. The ERD is a structural diagram; losing the counts
    because credentials expired should not lose the diagram.
    """
    from . import warehouse

    try:
        data = warehouse.inventory(target)
    except Exception as exc:  # noqa: BLE001
        return [f"Row counts unavailable: {exc}"]

    by_qualified = {
        f"{row['dataset']}.{row['table']}".lower(): row
        for row in (data.get("tables") or [])
    }

    for table in tables.values():
        parts = [p.strip("`") for p in str(table.get("relation") or "").split(".")]
        if len(parts) < 3:
            continue
        row = by_qualified.get(f"{parts[1]}.{parts[2]}".lower())
        if not row:
            continue
        table["row_count"] = row.get("row_count")
        table["size_bytes"] = row.get("size_bytes")
        table["last_modified"] = row.get("last_modified")

    return [data["error"]] if data.get("error") else []


# --------------------------------------------------------------------------
# text exports
# --------------------------------------------------------------------------

# Mermaid identifiers cannot contain a dot, and DBML quoting differs, so both
# generators normalise names rather than emitting whatever dbt happened to use.

def _mermaid_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(name))


_MERMAID_CARDINALITY = {
    "1:1": "||--||",
    "1:N": "||--o{",
    "N:1": "}o--||",
    "N:N": "}o--o{",
    "": "..",
}


def to_mermaid(erd: Dict[str, Any], keys_only: bool = False) -> str:
    """
    Mermaid erDiagram source, for pasting into a README or Confluence page.

    Mermaid renders on GitHub and in most wikis without a plugin, which is why
    this is offered as text rather than as another image.
    """
    lines: List[str] = [
        "%% Generated by dbt Studio from target/manifest.json",
        "%% Relationship kinds: declared (tested) / lineage (ref) / inferred (naming)",
        "erDiagram",
    ]

    for table in erd["tables"]:
        lines.append(f"    {_mermaid_name(table['name'])} {{")
        for column in table["columns"]:
            if keys_only and not (column["is_primary"] or column["looks_like_key"]):
                continue
            data_type = _mermaid_name(column["data_type"] or "unknown")
            marker = "PK" if column["is_primary"] else (
                "FK" if column["looks_like_key"] else ""
            )
            lines.append(
                f"        {data_type} {_mermaid_name(column['name'])}"
                + (f" {marker}" if marker else "")
            )
        lines.append("    }")

    lines.append("")

    for relationship in erd["relationships"]:
        connector = _MERMAID_CARDINALITY.get(relationship["cardinality"], "..")
        label = relationship["kind"]
        columns = "+".join(relationship["from_columns"])
        if columns:
            label = f"{label} on {columns}"
        lines.append(
            f"    {_mermaid_name(relationship['from_name'])} {connector} "
            f"{_mermaid_name(relationship['to_name'])} : \"{label}\""
        )

    return "\n".join(lines) + "\n"


def to_dbml(erd: Dict[str, Any]) -> str:
    """
    DBML, for importing into dbdiagram.io.

    Only `declared` and `constraint` edges become DBML references. dbdiagram
    draws a reference as a real foreign key, and rendering a naming guess that
    way would turn an inference into an assertion. The rest are emitted as
    comments so nothing is silently lost.
    """
    lines: List[str] = [
        "// Generated by dbt Studio from target/manifest.json",
        f"// {erd['stats']['table_count']} tables, "
        f"{erd['stats']['relationship_count']} detected relationships",
        "",
    ]

    for table in erd["tables"]:
        note = str(table["description"] or "").replace("'", "").replace("\n", " ")
        lines.append(f"Table {table['name']} {{")
        for column in table["columns"]:
            settings: List[str] = []
            if column["is_primary"]:
                settings.append("pk")
            if column["not_null"] and not column["is_primary"]:
                settings.append("not null")
            description = str(column["description"] or "").replace("'", "").replace("\n", " ")
            if description:
                settings.append(f"note: '{description[:180]}'")
            suffix = f" [{', '.join(settings)}]" if settings else ""
            lines.append(
                f"  {column['name']} {column['data_type'] or 'unknown'}{suffix}"
            )
        if note:
            lines.append("")
            lines.append(f"  Note: '{note[:400]}'")
        lines.append("}")
        lines.append("")

    hard = [r for r in erd["relationships"] if r["kind"] in ("declared", "constraint")]
    soft = [r for r in erd["relationships"] if r["kind"] not in ("declared", "constraint")]

    if hard:
        lines.append("// Enforced or declared foreign keys")
        for relationship in hard:
            if not relationship["from_columns"] or not relationship["to_columns"]:
                continue
            operator = ">" if relationship["cardinality"] in ("N:1", "N:N") else "-"
            lines.append(
                f"Ref: {relationship['from_name']}.{relationship['from_columns'][0]} "
                f"{operator} {relationship['to_name']}.{relationship['to_columns'][0]}"
            )
        lines.append("")

    if soft:
        lines.append("// Detected but not declared. Add a dbt relationships test")
        lines.append("// to promote one of these into a real Ref above.")
        for relationship in soft:
            columns = "+".join(relationship["from_columns"]) or "?"
            lines.append(
                f"// {relationship['from_name']}.{columns} -> "
                f"{relationship['to_name']} "
                f"({relationship['kind']}, {relationship['confidence']} confidence"
                + (f", {relationship['cardinality']}" if relationship["cardinality"] else "")
                + ")"
            )

    return "\n".join(lines) + "\n"
