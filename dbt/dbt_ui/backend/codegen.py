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
    sets = _transform_sets(by_category)
    trims = sets["trims"]
    uppers = sets["uppers"]
    blanks = sets["blanks"]
    casts = sets["casts"]
    flags = sets["flags"]
    splits = sets["splits"]
    labels = sets["labels"]
    periods = sets["periods"]

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


# --------------------------------------------------------------------------
# transformation plan (the transparency preview)
# --------------------------------------------------------------------------
#
# silver_plan() answers "what is about to happen, and why" before a line of SQL
# is generated. It is deliberately derived from the same _transform_sets() the
# generator uses, so the preview cannot describe one thing while the SQL does
# another. If a category is added to _build_select and not here, the plan is
# incomplete rather than wrong - and the column walk below will still show the
# passthrough, because it starts from the profile rather than from the SQL.

# Columns the asg_audit_columns('silver') macro appends. Hardcoded because the
# macro is Jinja that only dbt can expand, and the UI must not pretend to
# compile it. Kept beside the macro's definition in macros/asg_helpers.sql.
AUDIT_MACRO_COLUMNS = [
    ("_silver_loaded_at", "timestamp", "current_timestamp()"),
    ("_dbt_invocation_id", "string", "the dbt run that produced the row"),
    ("_dbt_target", "string", "the target the run used"),
]


def _transform_sets(by_category: Dict[str, List[Dict[str, Any]]]) -> Dict[str, set]:
    """
    Which columns each transformation applies to, keyed by transformation.

    Read off the accepted recommendations' sql_hint rather than the category
    alone, because one category can carry two different treatments:
    standardisation covers both `trim(x)` and `upper(trim(x))`, and
    categorisation covers both a `case` label and an `abs()` sign split.
    """
    def columns_where(category: str, predicate) -> set:
        return {
            column
            for rec in by_category.get(category, [])
            for column in rec["columns"]
            if predicate(rec["sql_hint"])
        }

    return {
        "trims": columns_where("standardisation", lambda hint: "trim" in hint),
        "uppers": columns_where("standardisation", lambda hint: "upper" in hint),
        "blanks": columns_where("null_handling", lambda hint: "nullif" in hint),
        "casts": columns_where("type_cast", lambda _: True),
        "flags": columns_where("quality_flag", lambda _: True),
        "splits": columns_where("categorisation", lambda hint: "abs(" in hint),
        "labels": columns_where("categorisation",
                                lambda hint: hint.startswith("case ")),
        "periods": columns_where("partitioning", lambda _: True),
    }


def _step(kind: str, title: str, detail: str,
          columns: Optional[List[str]] = None,
          evidence: str = "", sql: str = "") -> Dict[str, Any]:
    return {
        "kind": kind,
        "title": title,
        "detail": detail,
        "columns": sorted(columns or []),
        "evidence": evidence,
        "sql": sql,
    }


def silver_plan(
    source_model: str,
    analysis: Dict[str, Any],
    profile: Dict[str, Any],
    accepted_ids: Optional[Sequence[str]] = None,
    model_name: str = "",
    materialized: str = "view",
) -> Dict[str, Any]:
    """
    Explain how the silver model would be built, without building it.

    Returns the source relations, the ordered transformation steps, the column
    schema that would result, and an estimated row count. Nothing here queries
    the warehouse: every number comes from the profile and duplicate check that
    have already been paid for.
    """
    accepted = _applied(analysis.get("recommendations") or [], accepted_ids)
    by_category: Dict[str, List[Dict[str, Any]]] = {}
    for rec in accepted:
        by_category.setdefault(rec["category"], []).append(rec)

    columns = {c["name"]: c for c in (profile.get("columns") or [])}
    plan_meta = analysis.get("plan") or {}
    key_columns = [c for c in (plan_meta.get("key_columns") or []) if c in columns]

    sets = _transform_sets(by_category)
    dropped = {
        column for rec in by_category.get("pruning", []) for column in rec["columns"]
    }

    target_name = model_name or _silver_name(source_model)

    # ---------------- sources ----------------

    sources = [{
        "model": source_model,
        "relation": profile.get("relation"),
        "row_count": profile.get("declared_row_count") or profile.get("row_count"),
        "reference": f"{{{{ ref('{source_model}') }}}}",
        "note": "Resolved by dbt at build time, so the same SQL works on every "
                "target.",
    }]

    # ---------------- steps, in the order the SQL applies them ----------------

    steps: List[Dict[str, Any]] = [
        _step(
            "read",
            f"Read every row of {source_model}",
            "The model opens with an unfiltered read. Bronze is the faithful "
            "copy, so nothing is excluded here.",
            sql=f"select * from {{{{ ref('{source_model}') }}}}",
        ),
    ]

    dedup_recs = [
        rec for rec in by_category.get("deduplication", [])
        if rec["sql_hint"].startswith("row_number")
    ]
    order_column = _recency_column(columns)
    duplicate = analysis.get("duplicate_check") or {}

    if dedup_recs and key_columns:
        steps.append(_step(
            "deduplicate",
            f"Keep one row per {', '.join(key_columns)}",
            f"Ranks rows within each key by {order_column} descending and keeps "
            "the newest. Rows are removed here, and only here.",
            key_columns,
            dedup_recs[0]["evidence"],
            f"row_number() over (partition by {', '.join(key_columns)} "
            f"order by {order_column} desc) = 1",
        ))

    if dropped:
        steps.append(_step(
            "prune",
            f"Omit {len(dropped)} column(s) that carry no information",
            "Constant or entirely null in the profile. They stay in bronze for "
            "fidelity and are simply not selected forward.",
            list(dropped),
            "; ".join(
                rec["evidence"] for rec in by_category.get("pruning", [])
            ),
        ))

    if sets["blanks"]:
        steps.append(_step(
            "null_handling",
            "Normalise empty strings to null",
            "An empty string and a NULL behave differently in comparisons and "
            "aggregates. Collapsing them here means downstream only has one "
            "case to handle.",
            list(sets["blanks"]),
            "; ".join(rec["evidence"] for rec in by_category.get("null_handling", [])),
            "nullif(trim(<column>), '')",
        ))

    standardised = sets["uppers"] | (sets["trims"] - sets["uppers"] - sets["blanks"])
    if standardised:
        steps.append(_step(
            "standardise",
            "Trim and case-fold codes",
            "Codes that are joined or compared on are normalised once here, so "
            "no downstream model has to remember to do it.",
            list(standardised),
            "; ".join(rec["evidence"] for rec in by_category.get("standardisation", [])),
            "upper(trim(<column>))",
        ))

    if sets["casts"]:
        steps.append(_step(
            "type_cast",
            "Cast money to NUMERIC",
            "FLOAT64 cannot represent large decimal amounts exactly, so totals "
            "drift. NUMERIC is exact.",
            list(sets["casts"]),
            "; ".join(rec["evidence"] for rec in by_category.get("type_cast", [])),
            "cast(<column> as numeric)",
        ))

    if sets["periods"]:
        steps.append(_step(
            "derive_period",
            "Derive period columns from the date",
            "Month, year and quarter are computed once here so gold does not "
            "re-derive them on every query.",
            list(sets["periods"]),
            "",
            "date_trunc(<column>, month), extract(year from <column>), "
            "extract(quarter from <column>)",
        ))

    if sets["labels"]:
        steps.append(_step(
            "categorise",
            "Add a readable label per code",
            "Emits a CASE with an explicit `else 'Unmapped'`, so a new code "
            "shows up as unmapped instead of silently becoming null. The "
            "mappings themselves are left as a TODO for you to fill in.",
            list(sets["labels"]),
            "; ".join(rec["evidence"] for rec in by_category.get("categorisation", [])),
            "case <column> ... else 'Unmapped' end as <column>_label",
        ))

    if sets["splits"]:
        steps.append(_step(
            "split_sign",
            "Split mixed-sign amounts into debit and credit",
            "A single signed column forces every consumer to know the sign "
            "convention. Splitting it makes both measures additive.",
            list(sets["splits"]),
            "",
            "abs(<column>) as <column>_abs, plus debit_amount / credit_amount",
        ))

    if sets["flags"]:
        steps.append(_step(
            "quality_flag",
            "Stamp quality flags instead of dropping rows",
            "Silver never drops a row for a quality reason. The problem is "
            "flagged on the row so it stays visible and countable.",
            list(sets["flags"]),
            "; ".join(rec["evidence"] for rec in by_category.get("quality_flag", [])),
            "<column> is null as _is_missing_<column>",
        ))

    steps.append(_step(
        "audit",
        "Stamp audit columns",
        "Every row records the run that produced it, via the "
        "asg_audit_columns('silver') macro.",
        [name for name, _, _ in AUDIT_MACRO_COLUMNS],
        "",
        "{{ asg_audit_columns('silver') }}",
    ))

    # ---------------- resulting schema ----------------

    out_columns = _plan_columns(columns, sets, key_columns, dropped)

    # ---------------- estimated rows ----------------

    estimate = _plan_row_estimate(profile, duplicate, bool(dedup_recs and key_columns))

    tests: List[Dict[str, Any]] = [
        {
            "column": ", ".join(rec["columns"]) or "table",
            "tests": rec["sql_hint"] or "data_tests",
            "why": rec["evidence"],
        }
        for rec in by_category.get("testing", [])
    ]
    if key_columns:
        tests.append({
            "column": ", ".join(key_columns),
            "tests": "data_tests: [unique, not_null]",
            "why": "The business key silver deduplicates on.",
        })

    return {
        "model_name": target_name,
        "source_model": source_model,
        "path": f"models/silver/{target_name}.sql",
        "materialized": materialized,
        "sources": sources,
        "steps": steps,
        "columns": out_columns,
        "column_count": len(out_columns),
        "dropped_columns": sorted(dropped),
        "key_columns": key_columns,
        "row_estimate": estimate,
        "tests": tests,
        "applied": [
            {"id": rec["id"], "category": rec["category"], "title": rec["title"]}
            for rec in accepted
        ],
        "skipped": [
            {"id": rec["id"], "category": rec["category"], "title": rec["title"]}
            for rec in (analysis.get("recommendations") or [])
            if rec not in accepted
        ],
    }


def _plan_columns(
    columns: Dict[str, Any],
    sets: Dict[str, set],
    key_columns: List[str],
    dropped: set,
) -> List[Dict[str, Any]]:
    """
    The columns the generated model would output, in emission order.

    Mirrors _build_select. `origin` says where each column came from, which is
    the part that makes the preview trustworthy: a reviewer can see at a glance
    that nothing appeared out of nowhere.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()

    def emit(name: str, data_type: str, origin: str, note: str = "",
             source: str = "") -> None:
        if name in seen:
            return
        seen.add(name)
        out.append({
            "name": name,
            "data_type": data_type,
            "origin": origin,
            "note": note,
            "source_column": source or (name if origin == "passthrough" else ""),
        })

    def declared(name: str) -> str:
        return str((columns.get(name) or {}).get("data_type_yaml") or "unknown")

    for name in key_columns:
        emit(name, declared(name), "key", "Business key, carried through unchanged.")

    remaining = [n for n in columns if n not in key_columns and n not in dropped]
    business = [n for n in remaining if not n.startswith("_")]
    audit = [n for n in remaining if n.startswith("_")]

    for name in business:
        if name not in sets["periods"]:
            continue
        emit(name, declared(name), "passthrough", "The date itself.")
        emit("period_month", "date", "derived", f"date_trunc({name}, month)", name)
        emit("period_year", "int64", "derived", f"extract(year from {name})", name)
        emit("period_quarter", "int64", "derived", f"extract(quarter from {name})", name)

    for name in business:
        if name in sets["periods"]:
            continue

        data_type = declared(name)
        origin = "passthrough"
        note = ""

        if name in sets["casts"]:
            data_type = "numeric"
            origin = "recast"
            note = f"cast from {declared(name)} for exact decimal arithmetic"
        elif name in sets["blanks"]:
            origin = "cleaned"
            note = "empty strings normalised to null"
        elif name in sets["uppers"]:
            origin = "cleaned"
            note = "upper(trim(...))"
        elif name in sets["trims"]:
            origin = "cleaned"
            note = "trim(...)"

        emit(name, data_type, origin, note)

        if name in sets["labels"]:
            emit(f"{name}_label", "string", "derived",
                 "CASE mapping, unmapped codes fall through to 'Unmapped'", name)

        if name in sets["splits"] and (columns.get(name) or {}).get("category") == "numeric":
            emit(f"{name}_abs", "numeric", "derived", f"abs({name})", name)
            emit("debit_amount", "numeric", "derived",
                 f"{name} when it is zero or positive", name)
            emit("credit_amount", "numeric", "derived",
                 f"{name} when it is negative, as a positive number", name)

    for name in sorted(sets["flags"]):
        if name not in columns:
            continue
        emit(f"_is_missing_{name}", "bool", "flag",
             f"true when {name} is null", name)

    for name in audit:
        emit(name, declared(name), "passthrough", "Bronze lineage column.")

    for name, data_type, note in AUDIT_MACRO_COLUMNS:
        emit(name, data_type, "macro", note)

    return out


def _plan_row_estimate(
    profile: Dict[str, Any],
    duplicate: Dict[str, Any],
    deduplicating: bool,
) -> Dict[str, Any]:
    """
    Estimate the output row count from measurements already taken.

    Deliberately not a dry run. The generated model calls project macros, so
    only dbt can compile it - a dry run would fail on the Jinja, not on the
    logic. The profile and the group-by already answer the question exactly in
    the common case, and where they cannot the basis says so.
    """
    declared = int(profile.get("declared_row_count") or profile.get("row_count") or 0)
    sampled = bool(profile.get("sampled"))

    if not deduplicating:
        return {
            "rows": declared,
            "exact": not sampled,
            "basis": "Silver never drops rows for quality reasons, and no "
                     "deduplication was accepted, so the row count carries "
                     "through unchanged.",
            "source_rows": declared,
            "removed": 0,
        }

    if duplicate.get("checked") and duplicate.get("key_groups") is not None:
        groups = int(duplicate.get("key_groups") or 0)
        surplus = int(duplicate.get("surplus_rows") or 0)
        return {
            "rows": groups,
            "exact": not sampled,
            "basis": f"Deduplication keeps one row per key. The group-by counted "
                     f"{groups:,} distinct key(s), so {surplus:,} surplus row(s) "
                     f"would be removed.",
            "source_rows": declared,
            "removed": surplus,
        }

    return {
        "rows": None,
        "exact": False,
        "basis": "Deduplication is proposed but the key was never verified with "
                 "a group-by, so the output count is unknown.",
        "source_rows": declared,
        "removed": None,
    }
