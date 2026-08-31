"""
Column profiling.

Everything the Silver Advisor recommends is grounded in a measurement taken
from the actual relation, never in a guess from the column name. One pass over
the table produces per-column null rates, cardinality, min/max, blank counts
and numeric sign distribution, plus table-level duplicate detection on candidate
keys.

It is a single query with one aggregate per column rather than one query per
column: on a partitioned table that is one scan instead of N.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import typing_map, warehouse

# Guard against building a multi-megabyte statement on a very wide table.
MAX_PROFILED_COLUMNS = 120


def _quote(name: str) -> str:
    """Backtick-quote an identifier, escaping any embedded backtick."""
    return "`" + str(name).replace("`", r"\`") + "`"


def _profile_expressions(columns: List[Dict[str, Any]]) -> List[str]:
    """One set of aggregate expressions per column."""
    parts: List[str] = []

    for index, col in enumerate(columns):
        name = col["name"]
        data_type = col["data_type"]
        kind = typing_map.category(data_type)
        ref = _quote(name)
        alias = f"c{index}"

        # Nested and repeated columns cannot be aggregated directly.
        if kind in ("struct", "array") or "." in name:
            parts.append(f"cast(null as int64) as {alias}_nulls")
            parts.append(f"cast(null as int64) as {alias}_distinct")
            parts.append(f"cast(null as string) as {alias}_min")
            parts.append(f"cast(null as string) as {alias}_max")
            parts.append(f"cast(null as int64) as {alias}_blank")
            parts.append(f"cast(null as int64) as {alias}_negative")
            continue

        parts.append(f"countif({ref} is null) as {alias}_nulls")
        parts.append(f"count(distinct {ref}) as {alias}_distinct")

        if kind in ("numeric", "temporal"):
            parts.append(f"cast(min({ref}) as string) as {alias}_min")
            parts.append(f"cast(max({ref}) as string) as {alias}_max")
        elif kind == "text":
            parts.append(f"cast(min(length({ref})) as string) as {alias}_min")
            parts.append(f"cast(max(length({ref})) as string) as {alias}_max")
        elif kind == "boolean":
            parts.append(f"cast(countif({ref}) as string) as {alias}_min")
            parts.append(f"cast(countif(not {ref}) as string) as {alias}_max")
        else:
            parts.append(f"cast(null as string) as {alias}_min")
            parts.append(f"cast(null as string) as {alias}_max")

        if kind == "text":
            parts.append(f"countif(trim({ref}) = '') as {alias}_blank")
        else:
            parts.append(f"cast(0 as int64) as {alias}_blank")

        if kind == "numeric":
            parts.append(f"countif({ref} < 0) as {alias}_negative")
        else:
            parts.append(f"cast(null as int64) as {alias}_negative")

    return parts


def profile_relation(
    relation: str,
    target: Optional[str] = None,
    sample_rows: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Profile every scalar column of a relation.

    `sample_rows` bounds the scan on a large table. The result records whether
    it sampled, so the UI never presents an estimate as a full census.
    """
    described = warehouse.describe_relation(relation, target=target)
    all_columns = typing_map.flatten_columns(described["columns"])

    scalar = [
        col for col in all_columns
        if typing_map.category(col["data_type"]) not in ("struct", "array")
    ][:MAX_PROFILED_COLUMNS]

    if not scalar:
        return {
            "relation": relation,
            "row_count": described["row_count"],
            "columns": [],
            "sampled": False,
            "table": described,
        }

    row_count = int(described.get("row_count") or 0)
    limit = int(sample_rows or 0)
    sampled = bool(limit and row_count > limit)

    source = (
        f"(select * from {relation} limit {limit})" if sampled else relation
    )

    sql = (
        "select\n  count(*) as _total_rows,\n  "
        + ",\n  ".join(_profile_expressions(scalar))
        + f"\nfrom {source}"
    )

    result = warehouse.execute(sql, target=target, limit=1, apply_limit=False)
    if not result.rows:
        raise warehouse.WarehouseError(
            f"Profiling returned no rows for {relation}."
        )

    row = dict(zip([c["name"] for c in result.columns], result.rows[0]))
    total = int(row.get("_total_rows") or 0)

    profiled: List[Dict[str, Any]] = []
    for index, col in enumerate(scalar):
        alias = f"c{index}"
        nulls = _as_int(row.get(f"{alias}_nulls"))
        distinct = _as_int(row.get(f"{alias}_distinct"))
        blank = _as_int(row.get(f"{alias}_blank"))
        negative = row.get(f"{alias}_negative")

        null_pct = round(nulls / total * 100, 2) if total else 0.0
        distinct_pct = round(distinct / total * 100, 2) if total else 0.0

        profiled.append({
            "name": col["name"],
            "data_type": col["data_type"],
            "data_type_yaml": col["data_type"].lower(),
            "category": typing_map.category(col["data_type"]),
            "mode": col.get("mode"),
            "description": col.get("description") or "",
            "null_count": nulls,
            "null_pct": null_pct,
            "distinct_count": distinct,
            "distinct_pct": distinct_pct,
            "blank_count": blank,
            "negative_count": _as_int(negative) if negative is not None else None,
            "min": row.get(f"{alias}_min"),
            "max": row.get(f"{alias}_max"),
            "is_unique": bool(total and distinct == total and nulls == 0),
            "is_constant": bool(total and distinct <= 1),
            "is_all_null": bool(total and nulls == total),
        })

    return {
        "relation": relation,
        "row_count": total,
        "declared_row_count": row_count,
        "sampled": sampled,
        "sample_rows": limit if sampled else None,
        "columns": profiled,
        "bytes_processed": result.bytes_processed,
        "duration_ms": result.duration_ms,
        "table": described,
    }


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def duplicate_check(
    relation: str,
    key_columns: List[str],
    target: Optional[str] = None,
) -> Dict[str, Any]:
    """Count how far a candidate key is from being unique."""
    if not key_columns:
        return {"key": [], "checked": False}

    keys = ", ".join(_quote(col) for col in key_columns)
    sql = f"""
select
  count(*)                                as key_groups,
  countif(n > 1)                          as duplicated_keys,
  coalesce(sum(if(n > 1, n - 1, 0)), 0)   as surplus_rows,
  coalesce(max(n), 0)                     as worst_group
from (
  select {keys}, count(*) as n
  from {relation}
  group by {', '.join(str(i + 1) for i in range(len(key_columns)))}
)
"""
    result = warehouse.execute(sql, target=target, limit=1, apply_limit=False)
    if not result.rows:
        return {"key": key_columns, "checked": False}

    row = dict(zip([c["name"] for c in result.columns], result.rows[0]))
    duplicated = _as_int(row.get("duplicated_keys"))

    return {
        "key": key_columns,
        "checked": True,
        "key_groups": _as_int(row.get("key_groups")),
        "duplicated_keys": duplicated,
        "surplus_rows": _as_int(row.get("surplus_rows")),
        "worst_group": _as_int(row.get("worst_group")),
        "is_unique": duplicated == 0,
    }


def value_distribution(
    relation: str,
    column: str,
    target: Optional[str] = None,
    top_n: int = 20,
) -> Dict[str, Any]:
    """Top values for a column, used to propose accepted_values tests."""
    ref = _quote(column)
    sql = f"""
select
  cast({ref} as string) as value,
  count(*)              as row_count
from {relation}
group by 1
order by row_count desc
limit {int(top_n)}
"""
    result = warehouse.execute(sql, target=target, limit=top_n, apply_limit=False)
    total = sum(_as_int(row[1]) for row in result.rows) or 1

    return {
        "column": column,
        "values": [
            {
                "value": row[0],
                "row_count": _as_int(row[1]),
                "pct": round(_as_int(row[1]) / total * 100, 2),
            }
            for row in result.rows
        ],
        "bytes_processed": result.bytes_processed,
    }
