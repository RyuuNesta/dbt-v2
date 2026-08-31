"""
Generators for schema YAML, column documentation, and silver models.

Three outputs, all designed to be reviewed and committed rather than trusted
blindly:

  schema_yaml()   name + data_type + description for every column, in the
                  layout dbt expects, ready to paste into models/<layer>/*.yml
  describe()      a first-draft description per column, inferred from the name,
                  the type, and what profiling actually measured
  silver_model()  a runnable silver model implementing the accepted
                  recommendations

The documentation heuristics are intentionally conservative. A wrong-but-
confident description is worse than none, so anything uncertain is emitted with
a TODO marker that is easy to grep for.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import typing_map

INDENT = "  "


# --------------------------------------------------------------------------
# YAML emission (hand-rolled so key order and comments survive)
# --------------------------------------------------------------------------

_PLAIN_SCALAR = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ ./+-]*$")


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if _PLAIN_SCALAR.match(text) and text.lower() not in (
        "yes", "no", "true", "false", "null", "on", "off", "~"
    ):
        return text
    return "'" + text.replace("'", "''") + "'"


def _yaml_block(text: str, indent: str) -> List[str]:
    """
    Render a description as a folded block scalar.

    `>` keeps long prose readable in the file and avoids the quoting traps that
    come with colons and hashes inside a single-quoted string.
    """
    words = " ".join(str(text).split())
    if not words:
        return []
    if len(words) <= 70 and ":" not in words and "#" not in words:
        return [f"{indent}description: {_yaml_scalar(words)}"]

    lines = [f"{indent}description: >"]
    current = ""
    for word in words.split(" "):
        if len(current) + len(word) + 1 > 74:
            lines.append(f"{indent}{INDENT}{current}")
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(f"{indent}{INDENT}{current}")
    return lines


# --------------------------------------------------------------------------
# documentation heuristics
# --------------------------------------------------------------------------

# Name fragment -> description template. Ordered: first match wins.
_NAME_HINTS: Sequence[tuple] = (
    (r"^_(?P<layer>bronze|silver|gold)_loaded_at$",
     "UTC timestamp recording when this row was materialised into the "
     "{layer} layer."),
    (r"^_dbt_invocation_id$",
     "dbt invocation id that produced this row. Ties the record back to a "
     "specific run."),
    (r"^_dbt_target$",
     "dbt target the row was built with (dev, test or prod)."),
    (r"^_source_relation$",
     "Relation this row was read from, retained for lineage debugging."),
    (r"^_is_(?P<rest>.+)$", "Quality flag: true when {rest_words}."),
    (r"^_has_(?P<rest>.+)$", "Quality flag: true when the row has {rest_words}."),
    (r"(^|_)surrogate_key$|_key$",
     "Deterministic surrogate key. Stable across runs, safe to join on."),
    (r"(^|_)id$|^id$", "Unique identifier for the record."),
    (r"document_number", "Source system accounting document number."),
    (r"company_code", "Company code identifying the legal entity."),
    (r"fiscal_year", "Fiscal year the record was posted into."),
    (r"posting_date", "Date the document was posted to the ledger."),
    (r"gl_account", "General ledger account number."),
    (r"cost_center", "Cost center the posting is attributed to."),
    (r"document_type", "Source system document type code."),
    (r"vendor_customer", "Counterparty identifier (vendor or customer)."),
    (r"^currency$|_currency$", "ISO 4217 currency code."),
    (r"debit_credit", "Debit or credit indicator."),
    (r"^period_month$", "First day of the reporting month."),
    (r"^period_quarter$", "Calendar quarter (1-4) of the reporting period."),
    (r"^period_year$", "Calendar year of the reporting period."),
    (r"^period_date$", "Reporting period date."),
    (r"_ytd$", "Year-to-date running total."),
    (r"_mtd$", "Month-to-date total for the period."),
    (r"_abs$", "Absolute value, ignoring sign."),
    (r"^debit_amount$", "Amount when the entry is a debit, otherwise zero."),
    (r"^credit_amount$", "Amount when the entry is a credit, otherwise zero."),
    (r"_count$|^count_", "Row count for the group."),
    (r"^description$|_description$", "Free-text description from the source."),
    (r"^name$|_name$", "Human-readable name."),
    (r"^status$|_status$", "Current status value."),
    (r"created_at|created_date", "Timestamp when the record was created."),
    (r"updated_at|modified_at", "Timestamp when the record was last changed."),
    (r"amount|amt", "Monetary amount."),
    (r"qty|quantity", "Quantity."),
    (r"price", "Unit price."),
    (r"email", "Email address."),
    (r"phone", "Phone number."),
)

_TYPE_FALLBACK = {
    "numeric": "Numeric measure.",
    "temporal": "Date or timestamp value.",
    "text": "Text value.",
    "boolean": "Boolean flag.",
    "array": "Repeated field.",
    "struct": "Nested record.",
}


def _words(fragment: str) -> str:
    return fragment.replace("_", " ").strip()


def describe(column: Dict[str, Any], profile: Optional[Dict[str, Any]] = None,
             existing: str = "") -> Dict[str, Any]:
    """
    Draft a description for one column.

    Returns the text plus how it was arrived at, so the UI can show which
    descriptions are trustworthy and which need a human.
    """
    if (existing or "").strip():
        return {
            "description": existing.strip(),
            "source": "existing",
            "needs_review": False,
        }

    name = str(column.get("name", ""))
    data_type = str(column.get("data_type", "STRING"))
    kind = typing_map.category(data_type)

    for pattern, template in _NAME_HINTS:
        match = re.search(pattern, name, re.IGNORECASE)
        if not match:
            continue
        groups = {k: v for k, v in (match.groupdict() or {}).items() if v}
        text = template
        if "{layer}" in text and "layer" in groups:
            text = text.replace("{layer}", groups["layer"])
        if "{rest_words}" in text:
            text = text.replace("{rest_words}", _words(groups.get("rest", "")))
        detail = _profile_sentence(profile, kind)
        return {
            "description": f"{text}{detail}",
            "source": "pattern",
            "needs_review": False,
        }

    base = _TYPE_FALLBACK.get(kind, "Value from the source system.")
    detail = _profile_sentence(profile, kind)
    return {
        "description": f"TODO describe {_words(name)}. {base}{detail}".strip(),
        "source": "fallback",
        "needs_review": True,
    }


def _profile_sentence(profile: Optional[Dict[str, Any]], kind: str) -> str:
    """Append a measured fact when profiling data is available."""
    if not profile:
        return ""

    bits: List[str] = []
    null_pct = profile.get("null_pct")
    if null_pct is not None:
        if null_pct == 0:
            bits.append("always populated")
        elif null_pct >= 40:
            bits.append(f"{null_pct}% null")
        else:
            bits.append(f"{null_pct}% null")

    distinct = profile.get("distinct_count")
    if distinct is not None and kind in ("text", "boolean") and distinct <= 25:
        bits.append(f"{distinct} distinct value(s)")

    if kind in ("numeric", "temporal") and profile.get("min") is not None:
        bits.append(f"observed range {profile['min']} to {profile['max']}")

    return f" Profiled: {', '.join(bits)}." if bits else ""


# --------------------------------------------------------------------------
# schema YAML
# --------------------------------------------------------------------------

def _generic_tests(column: Dict[str, Any],
                   profile: Optional[Dict[str, Any]]) -> List[str]:
    """Only propose a test the profile actually justifies."""
    if not profile:
        return []

    tests: List[str] = []
    name = column["name"]
    data_type = column["data_type"]

    if profile.get("is_unique") and re.search(r"(_key|_id|^id$|number)$", name, re.I):
        tests.append("unique")
    if profile.get("null_pct") == 0:
        tests.append("not_null")

    distinct = profile.get("distinct_count") or 0
    if (0 < distinct <= 10
            and typing_map.is_text(data_type)
            and (profile.get("distinct_pct") or 100) < 50):
        tests.append("accepted_values")

    return tests


def schema_yaml(
    name: str,
    columns: List[Dict[str, Any]],
    resource_type: str = "model",
    description: str = "",
    profiles: Optional[Dict[str, Dict[str, Any]]] = None,
    existing_descriptions: Optional[Dict[str, str]] = None,
    include_tests: bool = True,
    include_descriptions: bool = True,
    materialized: str = "",
    ai_descriptions: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Emit a dbt schema YAML block: every column with its name and data_type.

    This is the artifact to paste into models/<layer>/_<layer>__models.yml. The
    data_type values are the real BigQuery types read back from the warehouse,
    normalised to GoogleSQL spelling (int64, not integer).

    `ai_descriptions` lets the AI engine supply the prose while everything else
    about the output stays identical, so both engines produce the same shape of
    file and can be compared side by side.
    """
    profiles = profiles or {}
    existing_descriptions = existing_descriptions or {}
    ai_descriptions = ai_descriptions or {}
    flat = typing_map.flatten_columns(columns)

    lines: List[str] = ["version: 2", ""]
    plural = {"model": "models", "seed": "seeds", "source": "sources"}.get(
        resource_type, "models"
    )
    lines.append(f"{plural}:")
    lines.append(f"{INDENT}- name: {name}")

    if include_descriptions:
        model_description = description or (
            f"TODO describe {_words(name)}."
        )
        lines += _yaml_block(model_description, INDENT * 2)

    if materialized:
        lines.append(f"{INDENT * 2}config:")
        lines.append(f"{INDENT * 3}materialized: {materialized}")

    lines.append("")
    lines.append(f"{INDENT * 2}columns:")

    review_needed: List[str] = []
    documented = 0

    for column in flat:
        col_name = column["name"]
        std_type = column["data_type"]
        col_profile = profiles.get(col_name)

        lines.append(f"{INDENT * 3}- name: {col_name}")
        # Lower-cased to match dbt convention in YAML, e.g. `data_type: int64`.
        lines.append(f"{INDENT * 4}data_type: {std_type.lower()}")

        if include_descriptions:
            ai_text = ai_descriptions.get(col_name, "").strip()
            if ai_text:
                # The model flags its own uncertainty with an "Unclear:" prefix,
                # which is the AI equivalent of the pattern engine's TODO.
                drafted = {
                    "description": ai_text,
                    "needs_review": ai_text.lower().startswith("unclear"),
                }
            else:
                drafted = describe(
                    column, col_profile, existing_descriptions.get(col_name, "")
                )
            lines += _yaml_block(drafted["description"], INDENT * 4)
            if drafted["needs_review"]:
                review_needed.append(col_name)
            else:
                documented += 1

        if include_tests:
            tests = _generic_tests(column, col_profile)
            if tests:
                lines.append(f"{INDENT * 4}data_tests:")
                for test in tests:
                    if test == "accepted_values":
                        values = [
                            entry["value"]
                            for entry in (col_profile or {}).get("top_values", [])
                        ]
                        # The nested mapping has to sit deeper than the
                        # 'accepted_values' key itself, not level with it, or
                        # dbt reads 'arguments' as a sibling test.
                        lines.append(f"{INDENT * 5}- accepted_values:")
                        lines.append(f"{INDENT * 7}arguments:")
                        if values:
                            rendered = ", ".join(_yaml_scalar(v) for v in values)
                            lines.append(f"{INDENT * 8}values: [{rendered}]")
                        else:
                            lines.append(f"{INDENT * 8}values: []  # TODO fill in")
                        if typing_map.is_numeric(std_type):
                            # accepted_values quotes its values by default,
                            # which BigQuery rejects on an INT64 column.
                            lines.append(f"{INDENT * 8}quote: false")
                    else:
                        lines.append(f"{INDENT * 5}- {test}")

        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()

    return {
        "yaml": "\n".join(lines) + "\n",
        "column_count": len(flat),
        "documented": documented,
        "needs_review": review_needed,
        "columns": [
            {"name": c["name"], "data_type": c["data_type"].lower()}
            for c in flat
        ],
    }


def columns_yaml_fragment(columns: List[Dict[str, Any]]) -> str:
    """
    The bare name / data_type list.

    Exactly the shape asked for when all you want is the contract:

        - name: document_number
          data_type: int64
    """
    out: List[str] = []
    for column in typing_map.flatten_columns(columns):
        out.append(f"- name: {column['name']}")
        out.append(f"{INDENT}data_type: {column['data_type'].lower()}")
    return "\n".join(out) + "\n"


def markdown_table(columns: List[Dict[str, Any]],
                   profiles: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """Markdown contract table, for pasting into Confluence or a PR."""
    profiles = profiles or {}
    flat = typing_map.flatten_columns(columns)

    rows = ["| Column | Type | Null % | Distinct | Description |",
            "| --- | --- | --- | --- | --- |"]
    for column in flat:
        prof = profiles.get(column["name"]) or {}
        drafted = describe(column, prof, column.get("description", ""))
        description = drafted["description"].replace("|", "\\|")
        null_pct = f"{prof.get('null_pct')}%" if prof.get("null_pct") is not None else "-"
        distinct = f"{prof.get('distinct_count'):,}" if prof.get("distinct_count") is not None else "-"
        rows.append(
            f"| `{column['name']}` | {column['data_type'].lower()} | "
            f"{null_pct} | {distinct} | {description} |"
        )
    return "\n".join(rows) + "\n"


# --------------------------------------------------------------------------
# silver model generation
# --------------------------------------------------------------------------

def _applied(recommendations: Iterable[Dict[str, Any]],
             accepted_ids: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
    if accepted_ids is None:
        return [r for r in recommendations if r.get("default_applied")]
    wanted = set(accepted_ids)
    return [r for r in recommendations if r["id"] in wanted]


def silver_model(
    source_model: str,
    analysis: Dict[str, Any],
    profile: Dict[str, Any],
    accepted_ids: Optional[Sequence[str]] = None,
    model_name: str = "",
    materialized: str = "view",
) -> Dict[str, Any]:
    """
    Build a silver model from the accepted recommendations.

    The generated SQL is meant to be read and edited. Every non-obvious
    transformation carries the measurement that motivated it as a comment, so a
    reviewer can see why the column is shaped that way.
    """
    accepted = _applied(analysis.get("recommendations") or [], accepted_ids)
    by_category: Dict[str, List[Dict[str, Any]]] = {}
    for rec in accepted:
        by_category.setdefault(rec["category"], []).append(rec)

    columns = {c["name"]: c for c in (profile.get("columns") or [])}
    plan = analysis.get("plan") or {}
    key_columns = [c for c in (plan.get("key_columns") or []) if c in columns]
    date_columns = [c for c in (plan.get("date_columns") or []) if c in columns]

    target_name = model_name or _silver_name(source_model)
    dropped = {
        col for rec in by_category.get("pruning", []) for col in rec["columns"]
    }

    header = [
        "{{",
        "    config(",
        f"        materialized = '{materialized}'",
        "    )",
        "}}",
        "",
        "/*",
        f"    SILVER - cleaned and conformed {_words(_strip_layer(source_model))}.",
        "",
        f"    Generated by the dbt Studio Silver Advisor on "
        f"{datetime.date.today().isoformat()} from a profile of",
        f"    {profile.get('relation')} ({profile.get('row_count', 0):,} rows"
        + (", sampled" if profile.get("sampled") else "")
        + ").",
        "",
        "    Review before merging. Each transformation below records the",
        "    measurement that motivated it.",
        "*/",
        "",
    ]

    body: List[str] = ["with bronze as (", "", f"    select * from {{{{ ref('{source_model}') }}}}", "", "),", ""]

    dedup_recs = [
        r for r in by_category.get("deduplication", [])
        if r["sql_hint"].startswith("row_number")
    ]
    order_column = _recency_column(columns)

    if dedup_recs and key_columns:
        body += [
            "-- Deduplication: " + dedup_recs[0]["evidence"],
            "deduplicated as (",
            "",
            "    select",
            "        *,",
            "        row_number() over (",
            f"            partition by {', '.join(key_columns)}",
            f"            order by {order_column} desc",
            "        ) as _row_recency",
            "",
            "    from bronze",
            "",
            "),",
            "",
            "latest_only as (",
            "",
            "    select * except (_row_recency)",
            "    from deduplicated",
            "    where _row_recency = 1",
            "",
            "),",
            "",
        ]
        upstream = "latest_only"
    else:
        upstream = "bronze"

    select_lines = _build_select(
        columns, by_category, key_columns, date_columns, dropped
    )

    body += ["cleaned as (", "", "    select"]
    body += [f"        {line}" for line in select_lines]
    body += ["", f"    from {upstream}", "", ")", "", "select * from cleaned", ""]

    sql = "\n".join(header + body)

    return {
        "model_name": target_name,
        "sql": sql,
        "path": f"models/silver/{target_name}.sql",
        "applied": [
            {"id": r["id"], "category": r["category"], "title": r["title"]}
            for r in accepted
        ],
        "skipped": [
            {"id": r["id"], "category": r["category"], "title": r["title"]}
            for r in (analysis.get("recommendations") or [])
            if r not in accepted
        ],
        "dropped_columns": sorted(dropped),
        "key_columns": key_columns,
    }


def _silver_name(source_model: str) -> str:
    stem = _strip_layer(source_model)
    return f"silver_{stem}"


def _strip_layer(name: str) -> str:
    for prefix in ("bronze_", "raw_", "stg_", "staging_", "src_"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _recency_column(columns: Dict[str, Any]) -> str:
    """Pick the column that orders 'most recent' for deduplication."""
    for candidate in ("_bronze_loaded_at", "_loaded_at", "updated_at",
                      "modified_at", "created_at"):
        if candidate in columns:
            return candidate
    for name, col in columns.items():
        if col.get("category") == "temporal":
            return name
    return "1"


def _build_select(
    columns: Dict[str, Any],
    by_category: Dict[str, List[Dict[str, Any]]],
    key_columns: List[str],
    date_columns: List[str],
    dropped: set,
) -> List[str]:
    """Assemble the SELECT list, applying the accepted transformations."""
    trims = {c for rec in by_category.get("standardisation", [])
             for c in rec["columns"] if "trim" in rec["sql_hint"]}
    uppers = {c for rec in by_category.get("standardisation", [])
              for c in rec["columns"] if "upper" in rec["sql_hint"]}
    blanks = {c for rec in by_category.get("null_handling", [])
              for c in rec["columns"] if "nullif" in rec["sql_hint"]}
    casts = {c for rec in by_category.get("type_cast", [])
             for c in rec["columns"]}
    flags = {c for rec in by_category.get("quality_flag", [])
             for c in rec["columns"]}
    splits = {c for rec in by_category.get("categorisation", [])
              for c in rec["columns"]
              if "abs(" in rec["sql_hint"]}
    labels = {c for rec in by_category.get("categorisation", [])
              for c in rec["columns"]
              if rec["sql_hint"].startswith("case ")}
    periods = {c for rec in by_category.get("partitioning", [])
               for c in rec["columns"]}

    lines: List[str] = []
    audit_lines: List[str] = []
    width = 44

    def emit(expression: str, alias: str, comment: str = "") -> None:
        text = expression if expression == alias else \
            f"{expression.ljust(width)} as {alias}"
        lines.append(f"{text},{'  -- ' + comment if comment else ''}")

    if key_columns:
        lines.append("-- keys")
        for name in key_columns:
            emit(name, name)
        lines.append("")

    remaining = [n for n in columns if n not in key_columns and n not in dropped]
    business = [n for n in remaining if not n.startswith("_")]
    audit = [n for n in remaining if n.startswith("_")]

    if any(n in periods for n in business):
        lines.append("-- period grain, pre-derived so gold does not re-compute it")
        for name in business:
            if name not in periods:
                continue
            emit(name, name)
            emit(f"date_trunc({name}, month)", "period_month")
            emit(f"extract(year from {name})", "period_year")
            emit(f"extract(quarter from {name})", "period_quarter")
        lines.append("")

    lines.append("-- descriptors and measures")
    for name in business:
        if name in periods:
            continue
        col = columns[name]
        kind = col.get("category")
        expression = name

        if name in blanks:
            expression = f"nullif(trim({name}), '')"
        elif name in uppers:
            expression = f"upper(trim({name}))"
        elif name in trims:
            expression = f"trim({name})"

        if name in casts:
            inner = expression if expression != name else name
            expression = f"cast({inner} as numeric)"

        comment = ""
        if name in blanks:
            comment = "empty strings normalised to null"
        elif name in casts:
            comment = "exact decimal, not float"

        emit(expression, name, comment)

        if name in labels:
            lines.append(f"case {name}")
            lines.append(f"    -- TODO map the codes observed in {name}")
            lines.append("    else 'Unmapped'")
            lines.append(f"end{' '.rjust(width - 3)} as {name}_label,")

        if name in splits and kind == "numeric":
            emit(f"abs({name})", f"{name}_abs")
            for alias, comparison in (
                ("debit_amount", ">= 0"),
                ("credit_amount", "< 0"),
            ):
                lines.append(f"case when {name} {comparison} then abs({name})")
                lines.append("     else cast(0 as numeric)")
                lines.append(f"end{' '.rjust(width - 3)} as {alias},")

    if flags:
        lines.append("")
        lines.append("-- quality flags: surface problems, never drop rows")
        for name in sorted(flags):
            if name not in columns:
                continue
            emit(f"{name} is null", f"_is_missing_{name}")

    lines.append("")
    lines.append("-- lineage")
    for name in audit:
        audit_lines.append(name)
        emit(name, name)
    lines.append("{{ asg_audit_columns('silver') }}")

    return lines
