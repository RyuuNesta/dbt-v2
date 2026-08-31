"""
modelgen.py - turn an ad-hoc workbench query into a committable dbt model.

Why this exists in this shape
----------------------------
The original request was "save the query as a view in a dataset". That is not
what this does, deliberately. Writing a view straight into bronze_dbt or
silver_dbt would put an object in a production dataset that no dbt model
describes: it would not appear in the DAG, nothing would test it, `dbt build`
would not recreate it, and the next person to read the lineage graph would have
no idea it existed. It would also mean the read-only workbench had grown a write
path into production, which is the one thing the dataset allowlist is there to
prevent.

Writing a model *file* gets the same outcome by the route dbt is built for. The
file goes into the working copy, gets reviewed like any other change, and the
next build materialises it into the right dataset for the active target. Nothing
touches BigQuery here at all.

The interesting work is turning hardcoded table names back into ref() and
source(). A pasted query says `project`.`bronze_dbt`.`thing`, which pins the
model to one environment and severs it from the DAG. ref() restores both.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from . import config, jinja_sql

# --------------------------------------------------------------------------
# naming
# --------------------------------------------------------------------------

# dbt resource names are used as Python-ish identifiers and as BigQuery table
# names, so the safe intersection is lowercase word characters.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_RESERVED = {
    "select", "from", "where", "table", "view", "model", "source", "ref",
    "config", "target", "this", "schema", "database",
}


def validate_name(name: str, existing_names: Optional[set] = None,
                  layer: str = "") -> Tuple[str, List[str], List[str]]:
    """
    Normalise a model name and report anything wrong with it.

    Returns (normalised, errors, warnings). Errors block the save; warnings are
    style points the user can accept.
    """
    raw = str(name or "").strip()
    cleaned = re.sub(r"[^0-9a-zA-Z_]+", "_", raw).strip("_").lower()

    errors: List[str] = []
    warnings: List[str] = []

    if not cleaned:
        errors.append("Give the model a name.")
        return cleaned, errors, warnings

    if not _NAME_RE.match(cleaned):
        errors.append(
            f"'{cleaned}' is not a usable dbt model name. Use lowercase letters, "
            f"digits and underscores, starting with a letter."
        )

    if cleaned in _RESERVED:
        errors.append(f"'{cleaned}' is a reserved word in SQL or dbt. Pick another name.")

    if len(cleaned) > 60:
        errors.append("Keep the name to 60 characters or fewer.")

    if existing_names and cleaned in existing_names:
        errors.append(
            f"A model, seed or source called '{cleaned}' already exists. dbt "
            f"requires unique resource names, so this would break the next parse."
        )

    if cleaned != raw and raw:
        warnings.append(f"Name normalised from '{raw}' to '{cleaned}'.")

    # The project prefixes every model with its layer. Not enforced, because a
    # team may have a reason, but silence here would be unhelpful.
    if layer and not cleaned.startswith(f"{layer}_"):
        warnings.append(
            f"Every other model in this project starts with its layer, so "
            f"'{layer}_{cleaned}' would match the convention."
        )

    return cleaned, errors, warnings


# --------------------------------------------------------------------------
# relation index: physical name -> ref()/source() expression
# --------------------------------------------------------------------------

def _split_relation(relation: str) -> Tuple[str, str, str]:
    """`p`.`d`.`t` or p.d.t -> (project, dataset, table), lowercased."""
    parts = [p.strip().strip("`").strip('"').lower()
             for p in str(relation or "").split(".")]
    parts = [p for p in parts if p]
    while len(parts) < 3:
        parts.insert(0, "")
    return parts[-3], parts[-2], parts[-1]


def build_relation_index(mf) -> Dict[str, Dict[str, Any]]:
    """
    Three lookups from physical name to the dbt expression that produces it.

    'exact'   project.dataset.table
    'dataset' dataset.table
    'table'   table, but only where the name is unambiguous

    The table-only lookup is not a fudge. relation_name is frozen at parse time,
    so a manifest parsed on dev records dbt_dev_bronze while the query the user
    pasted may well name bronze_dbt. The dataset differs, the table does not, and
    rewriting to ref() is exactly right: ref() resolves against whichever target
    is active, which is the whole reason to prefer it over a literal.
    """
    exact: Dict[str, Dict[str, Any]] = {}
    by_dataset: Dict[str, Dict[str, Any]] = {}
    table_hits: Dict[str, List[Dict[str, Any]]] = {}

    def record(relation: str, expression: str, kind: str, name: str) -> None:
        if not relation:
            return
        project, dataset, table = _split_relation(relation)
        if not table:
            return
        entry = {
            "expression": expression,
            "kind": kind,
            "name": name,
            "relation": relation,
            "dataset": dataset,
            "table": table,
        }
        if project and dataset:
            exact.setdefault(f"{project}.{dataset}.{table}", entry)
        if dataset:
            by_dataset.setdefault(f"{dataset}.{table}", entry)
        table_hits.setdefault(table, []).append(entry)

    for name, relation in mf.ref_map().items():
        record(relation, f"{{{{ ref('{name}') }}}}", "ref", name)

    for key, relation in mf.source_map().items():
        source_name, _, table_name = key.partition(".")
        record(relation, f"{{{{ source('{source_name}', '{table_name}') }}}}",
               "source", key)

    # Only keep a table-only entry when exactly one resource claims that name.
    # Two models producing the same table name in different datasets cannot be
    # disambiguated from the table name alone, so guessing would be wrong.
    by_table = {
        table: entries[0]
        for table, entries in table_hits.items()
        if len({e["name"] for e in entries}) == 1
    }

    return {"exact": exact, "dataset": by_dataset, "table": by_table}


# --------------------------------------------------------------------------
# ref rewriting
# --------------------------------------------------------------------------

# A relation reference, in the forms BigQuery actually accepts:
#   `proj`.`dataset`.`table`   `proj.dataset.table`   proj.dataset.table
#   `dataset`.`table`          dataset.table
# Anchored to FROM / JOIN so a struct field access like t.payload.id is never
# mistaken for a table. That anchor is doing real work: without it this would
# happily corrupt nested-field SQL.
_PART = r"(?:`[^`]+`|[A-Za-z_][\w-]*)"
_RELATION_RE = re.compile(
    r"(?P<lead>\b(?:from|join)\s+)"
    r"(?P<relation>"
    r"`[^`]+\.[^`]+(?:\.[^`]+)?`"          # one backtick pair, dots inside
    rf"|{_PART}\s*\.\s*{_PART}(?:\s*\.\s*{_PART})?"  # dotted parts
    r")",
    re.IGNORECASE,
)


def rewrite_refs(sql: str, index: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Replace hardcoded relations with ref() / source().

    Every substitution is reported so the UI can show exactly what changed.
    Anything not found in the manifest is left alone and listed as unresolved:
    silently leaving a literal in place would produce a model that builds but
    has no lineage, which is worse than saying so.
    """
    replacements: List[Dict[str, str]] = []
    unresolved: List[Dict[str, str]] = []

    def resolve(text: str) -> Optional[Dict[str, Any]]:
        project, dataset, table = _split_relation(text)
        if project and dataset and f"{project}.{dataset}.{table}" in index["exact"]:
            return {**index["exact"][f"{project}.{dataset}.{table}"], "confidence": "exact"}
        if dataset and f"{dataset}.{table}" in index["dataset"]:
            return {**index["dataset"][f"{dataset}.{table}"], "confidence": "dataset"}
        if table in index["table"]:
            return {**index["table"][table], "confidence": "table"}
        return None

    def substitute(match: "re.Match[str]") -> str:
        lead = match.group("lead")
        relation = match.group("relation")

        # Already a Jinja expression: nothing to do.
        if "{{" in relation:
            return match.group(0)

        hit = resolve(relation)
        if hit is None:
            project, dataset, table = _split_relation(relation)
            if table:
                unresolved.append({
                    "literal": relation.strip(),
                    "dataset": dataset,
                    "table": table,
                })
            return match.group(0)

        replacements.append({
            "literal": relation.strip(),
            "expression": hit["expression"],
            "kind": hit["kind"],
            "name": hit["name"],
            "confidence": hit["confidence"],
        })
        return f"{lead}{hit['expression']}"

    rewritten = _RELATION_RE.sub(substitute, sql)

    # The FROM/JOIN anchor is what stops struct paths being mangled, but it also
    # means a relation reached some other way is never seen - a comma join, or a
    # table named inside a subquery construct the regex does not anchor on. That
    # would be a silent miss, so sweep the result for any *known* relation still
    # sitting there as a literal. Only known relations are reported, which keeps
    # false positives to column paths that happen to equal a real dataset.table.
    missed = _known_literals_remaining(rewritten, index)

    # Deduplicate while preserving order; the same table often appears twice.
    def dedupe(items: List[Dict[str, str]], key: str) -> List[Dict[str, str]]:
        seen = set()
        out = []
        for item in items:
            if item[key] in seen:
                continue
            seen.add(item[key])
            out.append(item)
        return out

    return {
        "sql": rewritten,
        "replacements": dedupe(replacements, "literal"),
        "unresolved": dedupe(unresolved, "literal"),
        "missed": missed,
    }


def _known_literals_remaining(sql: str, index: Dict[str, Dict[str, Any]]) -> List[Dict[str, str]]:
    """Relations that are in the manifest but are still literals in the SQL."""
    found: List[Dict[str, str]] = []
    seen = set()

    for entry in index["dataset"].values():
        pattern = re.compile(
            r"`?" + re.escape(entry["dataset"]) + r"`?\s*\.\s*`?"
            + re.escape(entry["table"]) + r"`?",
            re.IGNORECASE,
        )
        if pattern.search(sql) and entry["name"] not in seen:
            seen.add(entry["name"])
            found.append({
                "literal": f"{entry['dataset']}.{entry['table']}",
                "expression": entry["expression"],
                "name": entry["name"],
            })

    return found


# --------------------------------------------------------------------------
# the model file
# --------------------------------------------------------------------------

# From dbt_project.yml. A config block that only restates the project default is
# noise, so it is omitted unless the choice actually differs.
LAYER_DEFAULT_MATERIALIZATION = {
    "bronze": "table",
    "silver": "view",
}

MATERIALIZATIONS = ("view", "table", "incremental", "ephemeral")


def prepare_sql(sql: str) -> Tuple[str, List[str]]:
    """
    Make an interactive query safe to be a model body.

    A model is one expression, so a trailing semicolon is a syntax error once dbt
    wraps it in `create ... as (...)`. An exploratory LIMIT is legal but almost
    never intended in a model, so it is flagged rather than removed - removing it
    silently would change what the user tested.
    """
    warnings: List[str] = []
    body = str(sql or "").strip()

    if body.endswith(";"):
        body = body.rstrip(";").rstrip()
        warnings.append(
            "Removed the trailing semicolon. dbt wraps the model in "
            "create ... as ( ... ), and a semicolon inside that is a syntax error."
        )

    # A second statement cannot be a model, and would be a surprise if half of it
    # were silently dropped.
    if ";" in body:
        warnings.append(
            "This looks like more than one statement. A dbt model must be a "
            "single select - split the rest into its own model."
        )

    if re.search(r"\blimit\s+\d+\s*$", body, re.IGNORECASE):
        warnings.append(
            "The query still ends in a LIMIT. That was useful while exploring, "
            "but it will silently cap the built table. Remove it unless you "
            "meant it."
        )

    if re.search(r"\bselect\s+\*", body, re.IGNORECASE):
        warnings.append(
            "select * will absorb any column added upstream, including ones your "
            "tests and documentation do not know about. Listing columns is worth "
            "the typing in a committed model."
        )

    return body, warnings


def model_file(
    name: str,
    sql: str,
    layer: str,
    materialized: str = "",
    description: str = "",
    unresolved: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Render the .sql file, header comment and config block included."""
    lines: List[str] = []

    default = LAYER_DEFAULT_MATERIALIZATION.get(layer, "view")
    chosen = (materialized or default).lower()

    header = [f"-- {name}"]
    if description.strip():
        header.extend(f"-- {piece}" for piece in _wrap(description.strip(), 76))
    header.append(
        f"-- Saved from the dbt Studio workbench. Materialised as a {chosen} "
        f"in the {layer} layer."
    )
    if chosen == default:
        header.append(
            f"-- No config block needed: dbt_project.yml already materialises "
            f"{layer} as {default}."
        )

    if unresolved:
        header.append("--")
        header.append("-- TODO: these tables are still hardcoded, so this model has")
        header.append("--       no lineage to them. Declare them as sources and")
        header.append("--       switch to source() to fix that:")
        for item in unresolved:
            header.append(f"--         {item['literal']}")

    lines.extend(header)
    lines.append("")

    if chosen != default:
        lines.append(f"{{{{ config(materialized='{chosen}') }}}}")
        lines.append("")

    lines.append(sql.strip())
    lines.append("")

    return "\n".join(lines)


def _wrap(text: str, width: int) -> List[str]:
    words = text.split()
    out: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            out.append(current)
            current = word
        else:
            current = candidate
    if current:
        out.append(current)
    return out


def source_stub(unresolved: List[Dict[str, str]]) -> str:
    """
    A sources.yml block for tables dbt does not know about yet.

    Offered rather than written. Most tables in this warehouse predate the dbt
    project, so a query will usually reference something undeclared; handing over
    the exact YAML is more useful than a warning that stops at "declare a
    source".
    """
    if not unresolved:
        return ""

    by_dataset: Dict[str, List[str]] = {}
    for item in unresolved:
        by_dataset.setdefault(item["dataset"] or "unknown_dataset", []).append(item["table"])

    lines = ["version: 2", "", "sources:"]
    for dataset, tables in sorted(by_dataset.items()):
        lines.append(f"  - name: {dataset}")
        lines.append(f"    schema: {dataset}")
        lines.append("    description: >")
        lines.append(f"      Tables in {dataset} that predate this dbt project.")
        lines.append("    tables:")
        for table in sorted(set(tables)):
            lines.append(f"      - name: {table}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def scaffold(
    mf,
    name: str,
    sql: str,
    layer: str,
    materialized: str = "",
    description: str = "",
    rewrite: bool = True,
) -> Dict[str, Any]:
    """
    Everything needed to preview and then write a model, without writing it.

    Raises ValueError for a condition the user must fix before anything is worth
    previewing.
    """
    layer = str(layer or "").strip().lower()

    allowed = [lay for lay in config.ALLOWED_LAYERS
               if lay not in config.blocked_build_layers()]
    if layer not in allowed:
        raise ValueError(
            f"'{layer or 'none'}' is not a layer this instance may write to. "
            f"Choose one of: {', '.join(allowed)}."
        )

    body = str(sql or "").strip()
    if not body:
        raise ValueError("There is no SQL to save.")

    read_only, reason = jinja_sql.is_read_only(body)
    if not read_only:
        raise ValueError(
            f"Only a select can become a model. {reason}"
        )

    existing = set(mf.ref_map()) | set(mf.source_map())
    # Source keys are 'source.table'; the bare table name can also collide.
    existing |= {key.split(".", 1)[-1] for key in mf.source_map()}

    clean_name, errors, warnings = validate_name(name, existing, layer)

    prepared, sql_warnings = prepare_sql(body)
    warnings.extend(sql_warnings)

    missed: List[Dict[str, str]] = []
    if rewrite:
        index = build_relation_index(mf)
        result = rewrite_refs(prepared, index)
        final_sql = result["sql"]
        replacements = result["replacements"]
        unresolved = result["unresolved"]
        missed = result["missed"]
    else:
        final_sql = prepared
        replacements = []
        unresolved = []

    soft_matches = [r for r in replacements if r["confidence"] == "table"]
    if soft_matches:
        warnings.append(
            f"{len(soft_matches)} reference{'s' if len(soft_matches) > 1 else ''} "
            f"matched on table name rather than full dataset path, because the "
            f"manifest was parsed against a different target. ref() will point "
            f"at whichever target you build with, which is what you want - but "
            f"check the rewrite below."
        )

    if missed:
        names = ", ".join(f"{m['literal']} ({m['name']})" for m in missed)
        warnings.append(
            f"Still written as a plain table name after rewriting: {names}. "
            f"These are dbt models, so they should be ref() - but they are not in "
            f"a FROM or JOIN the rewriter recognises, most likely a comma join. "
            f"Replace them by hand before saving."
        )

    if unresolved:
        warnings.append(
            f"{len(unresolved)} table{'s' if len(unresolved) > 1 else ''} could "
            f"not be matched to a model or source, so {'they remain' if len(unresolved) > 1 else 'it remains'} "
            f"a hardcoded name. The model will build, but the lineage graph will "
            f"not show the dependency."
        )

    default = LAYER_DEFAULT_MATERIALIZATION.get(layer, "view")
    chosen = (materialized or default).lower()
    if chosen not in MATERIALIZATIONS:
        raise ValueError(
            f"'{chosen}' is not a materialization. Choose one of: "
            f"{', '.join(MATERIALIZATIONS)}."
        )

    content = model_file(
        clean_name or "unnamed_model",
        final_sql,
        layer,
        materialized=chosen,
        description=description,
        unresolved=unresolved,
    )

    path = f"models/{layer}/{clean_name or 'unnamed_model'}.sql"
    full_path = config.PROJECT_DIR / path

    return {
        "name": clean_name,
        "layer": layer,
        "materialized": chosen,
        "default_materialized": default,
        "uses_config_block": chosen != default,
        "path": path,
        "exists": full_path.is_file(),
        "content": content,
        "sql": final_sql,
        "replacements": replacements,
        "unresolved": unresolved,
        "missed": missed,
        "source_stub": source_stub(unresolved),
        "errors": errors,
        "warnings": warnings,
        "ok": not errors,
    }
