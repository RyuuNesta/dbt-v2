"""
Silver Advisor - turn a bronze profile into concrete silver work.

Every recommendation is derived from a measurement, carries the evidence that
triggered it, and maps to a specific transformation. The output is not prose:
it is a list of decisions plus a runnable silver model that implements the ones
marked as applied.

Recommendation categories:

  deduplication   a candidate key repeats
  null_handling   a column is partly null or has blank strings
  type_cast       a value is stored in a type that will misbehave downstream
  standardisation text needs trimming or case folding
  categorisation  low-cardinality codes want a readable label
  aggregation     numeric measures worth summing, and a grain to sum them at
  quality_flag    a column that should be flagged rather than silently coerced
  pruning         constant or all-null columns that carry no information
  partitioning    a date column that should drive physical layout
  testing         a generic test the profile justifies

Confidence is high / medium / low, so a reviewer can act on the high ones and
argue about the rest.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from . import typing_map

# Thresholds. Named and gathered so the reasoning is inspectable rather than
# scattered through the code as magic numbers.
KEY_DISTINCT_THRESHOLD = 99.0     # distinct_pct at or above this looks like a key
LOW_CARDINALITY_MAX = 25          # distinct values at or below this is a code list
LOW_CARDINALITY_PCT = 5.0
HIGH_NULL_PCT = 40.0
SOME_NULL_PCT = 0.0

AUDIT_COLUMN_RE = re.compile(r"^_(.*_loaded_at|dbt_.*|source_.*|is_.*|has_.*)$")
KEY_NAME_RE = re.compile(
    r"(^|_)(id|key|code|number|num|no|nr|uuid|guid)$", re.IGNORECASE
)
# Numeric columns whose digits identify something rather than measure it.
# gl_account is the motivating case: summing account numbers is meaningless,
# but nothing about its name or type says so.
IDENTIFIER_HINT_RE = re.compile(
    r"(account|acct|ledger|voucher|invoice_no|batch|segment|company|"
    r"cost_center|profit_center|plant|branch|zip|postal|phone|year|month|"
    r"quarter|week|day)", re.IGNORECASE
)
DATE_NAME_RE = re.compile(
    r"(date|dt|time|timestamp|_at|period|month|year)", re.IGNORECASE
)
AMOUNT_NAME_RE = re.compile(
    r"(amount|amt|value|price|cost|total|qty|quantity|balance|revenue|"
    r"expense|debit|credit|net|gross|sum)", re.IGNORECASE
)


def _is_audit(name: str) -> bool:
    return bool(AUDIT_COLUMN_RE.match(name))


def _rec(
    category: str,
    title: str,
    detail: str,
    evidence: str,
    confidence: str,
    columns: Optional[List[str]] = None,
    sql_hint: str = "",
    applies: bool = True,
) -> Dict[str, Any]:
    return {
        "id": f"{category}:{'+'.join(columns or []) or 'table'}",
        "category": category,
        "title": title,
        "detail": detail,
        "evidence": evidence,
        "confidence": confidence,
        "columns": columns or [],
        "sql_hint": sql_hint,
        "default_applied": applies,
    }


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def analyse(
    profile: Dict[str, Any],
    duplicate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the recommendation set from a profile (and optional dup check)."""
    columns: List[Dict[str, Any]] = profile.get("columns") or []
    row_count = int(profile.get("row_count") or 0)
    recommendations: List[Dict[str, Any]] = []

    business = [col for col in columns if not _is_audit(col["name"])]

    key_candidates = _key_candidates(business, row_count)
    grain = _grain_candidates(business)
    measures = _measure_candidates(business)
    date_columns = _date_candidates(business)

    # ---------------- deduplication ----------------
    if duplicate and duplicate.get("checked"):
        if not duplicate.get("is_unique"):
            recommendations.append(_rec(
                "deduplication",
                f"Deduplicate on {', '.join(duplicate['key'])}",
                "Bronze is append-only, so a re-ingested extract duplicates "
                "business keys. Keep the most recent row per key with "
                "row_number() and filter to 1.",
                f"{duplicate['duplicated_keys']:,} key(s) repeat, "
                f"{duplicate['surplus_rows']:,} surplus row(s), worst group has "
                f"{duplicate['worst_group']:,} rows.",
                "high",
                duplicate["key"],
                sql_hint="row_number() over (partition by <key> order by "
                         "_bronze_loaded_at desc) = 1",
            ))
        else:
            recommendations.append(_rec(
                "deduplication",
                f"Add a uniqueness guard on {', '.join(duplicate['key'])}",
                "The key is unique today. Lock that in with a unique test so a "
                "future duplicate breaks the build instead of quietly "
                "double-counting.",
                f"{duplicate['key_groups']:,} distinct key(s) across "
                f"{row_count:,} row(s), zero duplicates.",
                "high",
                duplicate["key"],
                sql_hint="data_tests: [unique, not_null]",
            ))
    elif key_candidates:
        recommendations.append(_rec(
            "deduplication",
            f"Deduplicate on {', '.join(key_candidates)}",
            "These columns look like the business key. Deduplicate in silver "
            "so downstream aggregates cannot double-count a replayed load.",
            "Selected because "
            + "; ".join(
                f"{col['name']} is {col['distinct_pct']}% distinct"
                for col in business if col["name"] in key_candidates
            ),
            "medium",
            key_candidates,
            sql_hint="row_number() over (partition by <key> order by "
                     "_bronze_loaded_at desc) = 1",
        ))

    # ---------------- per-column findings ----------------
    for col in business:
        name = col["name"]
        kind = col["category"]
        null_pct = col["null_pct"]

        if col["is_all_null"]:
            recommendations.append(_rec(
                "pruning", f"Drop {name}", 
                "Every value is null, so the column carries no information "
                "into silver. Leave it in bronze for fidelity and omit it "
                "downstream.",
                f"{col['null_count']:,} of {row_count:,} rows are null (100%).",
                "high", [name], applies=True,
            ))
            continue

        if col["is_constant"] and row_count > 1:
            recommendations.append(_rec(
                "pruning", f"{name} is constant",
                "A single distinct value. Either drop it or promote it to a "
                "model-level fact instead of storing it on every row.",
                f"1 distinct value across {row_count:,} rows "
                f"(min {col['min']!r}).",
                "medium", [name], applies=False,
            ))

        if null_pct >= HIGH_NULL_PCT:
            recommendations.append(_rec(
                "quality_flag", f"Flag missing {name}",
                "Mostly empty. Rather than coalescing to a fake default, add a "
                "boolean flag so consumers can see the gap and decide.",
                f"{col['null_count']:,} nulls ({null_pct}%).",
                "high", [name],
                sql_hint=f"{name} is null as _is_missing_{name}",
            ))
        elif null_pct > SOME_NULL_PCT:
            recommendations.append(_rec(
                "null_handling", f"Decide the null policy for {name}",
                "Partially populated. Pick one: coalesce to a documented "
                "default, or keep null and record that null is legitimate.",
                f"{col['null_count']:,} nulls ({null_pct}%).",
                "medium", [name],
                sql_hint=f"coalesce({name}, <default>) as {name}",
            ))
        elif kind != "boolean":
            recommendations.append(_rec(
                "testing", f"not_null test on {name}",
                "Fully populated today. A not_null test freezes that "
                "expectation so a future upstream change surfaces immediately.",
                f"0 nulls across {row_count:,} rows.",
                "high", [name],
                sql_hint="data_tests: [not_null]",
                applies=True,
            ))

        if kind == "text":
            if col["blank_count"]:
                recommendations.append(_rec(
                    "null_handling", f"Normalise blanks in {name}",
                    "Empty strings behave differently from NULL in aggregates "
                    "and joins. Collapse them to NULL so there is one way to "
                    "say 'absent'.",
                    f"{col['blank_count']:,} empty-string value(s).",
                    "high", [name],
                    sql_hint=f"nullif(trim({name}), '') as {name}",
                ))
            else:
                recommendations.append(_rec(
                    "standardisation", f"Trim {name}",
                    "Defensive trim in silver. Leading and trailing whitespace "
                    "from source extracts is invisible in a preview but "
                    "silently splits a group by.",
                    f"Value length ranges {col['min']} to {col['max']} "
                    f"characters.",
                    "low", [name],
                    sql_hint=f"trim({name}) as {name}",
                    applies=False,
                ))

            if (col["distinct_count"] <= LOW_CARDINALITY_MAX
                    and col["distinct_pct"] <= LOW_CARDINALITY_PCT
                    and col["distinct_count"] > 1):
                recommendations.append(_rec(
                    "categorisation", f"Map {name} to readable labels",
                    "A small closed set of codes. Map them to business labels "
                    "in silver, with an explicit else branch so an unseen code "
                    "becomes 'Unmapped' instead of NULL.",
                    f"{col['distinct_count']} distinct value(s) across "
                    f"{row_count:,} rows ({col['distinct_pct']}%).",
                    "high", [name],
                    sql_hint=f"case {name} when ... else 'Unmapped' end as "
                             f"{name}_label",
                ))
                recommendations.append(_rec(
                    "testing", f"accepted_values test on {name}",
                    "Pin the allowed set so a new code fails the build rather "
                    "than flowing through unnoticed.",
                    f"{col['distinct_count']} distinct value(s) observed.",
                    "medium", [name],
                    sql_hint="data_tests: [accepted_values]",
                ))

            if name.lower().endswith(("currency", "country", "code")) or \
                    name.lower() in ("currency", "iso_code"):
                recommendations.append(_rec(
                    "standardisation", f"Upper-case {name}",
                    "Codes are compared and joined on. Fold the case once in "
                    "silver so 'idr' and 'IDR' cannot become two groups.",
                    f"{col['distinct_count']} distinct value(s).",
                    "medium", [name],
                    sql_hint=f"upper(trim({name})) as {name}",
                ))

        if kind == "numeric":
            if AMOUNT_NAME_RE.search(name):
                if col["data_type"] == "FLOAT64":
                    recommendations.append(_rec(
                        "type_cast", f"Cast {name} to NUMERIC",
                        "FLOAT64 cannot represent decimal money exactly, so "
                        "totals drift by fractions of a unit and then fail "
                        "reconciliation. NUMERIC is exact.",
                        f"Currently FLOAT64, range {col['min']} to "
                        f"{col['max']}.",
                        "high", [name],
                        sql_hint=f"cast({name} as numeric) as {name}",
                    ))
                if col["negative_count"]:
                    recommendations.append(_rec(
                        "categorisation",
                        f"Split {name} into debit and credit",
                        "Mixed signs in one measure force every consumer to "
                        "re-derive direction. Emit signed, absolute, debit and "
                        "credit columns once here.",
                        f"{col['negative_count']:,} negative value(s) "
                        f"({round(col['negative_count'] / row_count * 100, 1) if row_count else 0}%), "
                        f"range {col['min']} to {col['max']}.",
                        "high", [name],
                        sql_hint=f"abs({name}) as {name}_abs, case when ... end",
                    ))

        if kind == "temporal" and not _is_audit(name):
            recommendations.append(_rec(
                "partitioning", f"Derive period columns from {name}",
                "Pre-compute month, quarter and year in silver. Gold then "
                "partitions and groups on a stored column instead of calling "
                "date_trunc on every scan.",
                f"Range {col['min']} to {col['max']}, "
                f"{col['distinct_count']:,} distinct value(s).",
                "high", [name],
                sql_hint=f"date_trunc({name}, month) as period_month",
            ))

    # ---------------- table-level aggregation advice ----------------
    if measures and grain:
        recommendations.append(_rec(
            "aggregation",
            f"Aggregate {', '.join(m['name'] for m in measures[:4])} by "
            f"{', '.join(grain[:3])}",
            "This is the gold grain this table supports. Sum the measures at "
            "this grain, and add count(*) plus count(distinct <key>) so row "
            "inflation from a bad join is visible.",
            f"{len(measures)} numeric measure(s) and "
            f"{len(grain)} low-cardinality dimension(s) detected.",
            "high",
            [m["name"] for m in measures],
            sql_hint="group by " + ", ".join(grain[:3]),
        ))

    if date_columns and measures:
        recommendations.append(_rec(
            "aggregation",
            "Add running totals (YTD) alongside the period sums",
            "Finance reporting reads month-to-date and year-to-date together. "
            "A window sum over the period column gives YTD without a second "
            "model.",
            f"Date column(s) {', '.join(date_columns[:3])} present with "
            f"{len(measures)} measure(s).",
            "medium",
            date_columns[:1] + [m["name"] for m in measures[:2]],
            sql_hint="sum(x) over (partition by ... order by period_month "
                     "rows between unbounded preceding and current row)",
        ))

    recommendations.append(_rec(
        "quality_flag", "Carry audit columns into silver",
        "Stamp _silver_loaded_at, and keep the bronze timestamp. Two "
        "timestamps on a row make bronze-to-silver latency measurable and let "
        "you trace any row back to the run that produced it.",
        "Matches the _silver_loaded_at / _gold_loaded_at convention already in "
        "gold_dbt.fact_financial.",
        "high", [],
        sql_hint="current_timestamp() as _silver_loaded_at",
    ))

    by_category: Dict[str, int] = {}
    for rec in recommendations:
        by_category[rec["category"]] = by_category.get(rec["category"], 0) + 1

    return {
        "relation": profile.get("relation"),
        "row_count": row_count,
        "sampled": profile.get("sampled", False),
        "recommendations": recommendations,
        "summary": {
            "total": len(recommendations),
            "by_category": by_category,
            "high_confidence": sum(
                1 for r in recommendations if r["confidence"] == "high"
            ),
        },
        "plan": {
            "key_columns": (duplicate or {}).get("key") or key_candidates,
            "grain_columns": grain,
            "measure_columns": [m["name"] for m in measures],
            "date_columns": date_columns,
        },
    }


# --------------------------------------------------------------------------
# candidate detection
# --------------------------------------------------------------------------

def _key_candidates(columns: List[Dict[str, Any]], row_count: int) -> List[str]:
    """Prefer a single unique column; otherwise a small composite."""
    unique = [
        col["name"] for col in columns
        if col["is_unique"] and not _is_audit(col["name"])
    ]
    if unique:
        named = [n for n in unique if KEY_NAME_RE.search(n)]
        return [named[0]] if named else [unique[0]]

    near_key = [
        col for col in columns
        if KEY_NAME_RE.search(col["name"])
        and col["distinct_pct"] >= 1.0
        and not col["is_constant"]
    ]
    near_key.sort(key=lambda c: -c["distinct_pct"])
    return [col["name"] for col in near_key[:3]]


def _grain_candidates(columns: List[Dict[str, Any]]) -> List[str]:
    """Low-cardinality dimensions plus any period column."""
    grain: List[str] = []
    for col in columns:
        if col["category"] == "temporal":
            continue
        if col["is_constant"] or col["is_all_null"]:
            continue
        if (col["distinct_count"] <= LOW_CARDINALITY_MAX
                and col["distinct_pct"] <= 50.0
                and col["distinct_count"] > 1):
            grain.append(col["name"])

    for col in columns:
        if col["category"] == "temporal" and not _is_audit(col["name"]):
            grain.insert(0, col["name"])
            break

    return grain[:6]


def _measure_candidates(columns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Numeric columns that read as measures rather than identifiers."""
    measures: List[Dict[str, Any]] = []
    for col in columns:
        if col["category"] != "numeric":
            continue
        name = col["name"]
        # An explicit amount-ish name wins over every exclusion below, so a
        # column like invoice_amount is still treated as a measure.
        if AMOUNT_NAME_RE.search(name):
            if not (col["is_constant"] or col["is_all_null"]):
                measures.append(col)
            continue
        if KEY_NAME_RE.search(name):
            continue          # document_number, gl_entry_key: an id
        if IDENTIFIER_HINT_RE.search(name):
            continue          # gl_account, company_code, fiscal_year
        if DATE_NAME_RE.search(name):
            continue
        if col["is_constant"] or col["is_all_null"]:
            continue
        measures.append(col)
    return measures


def _date_candidates(columns: List[Dict[str, Any]]) -> List[str]:
    return [
        col["name"] for col in columns
        if col["category"] == "temporal" and not _is_audit(col["name"])
    ]
