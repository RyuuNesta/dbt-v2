"""
BigQuery type normalisation.

The BigQuery REST API still reports legacy type names (INTEGER, FLOAT,
BOOLEAN, RECORD) while everything a data engineer writes in GoogleSQL and in
dbt YAML uses the standard names (INT64, FLOAT64, BOOL, STRUCT). Emitting the
legacy spelling into a schema file would be misleading, so every type crossing
into the UI is normalised here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Legacy REST name -> GoogleSQL standard name.
LEGACY_TO_STANDARD: Dict[str, str] = {
    "INTEGER": "INT64",
    "INT": "INT64",
    "SMALLINT": "INT64",
    "BIGINT": "INT64",
    "TINYINT": "INT64",
    "BYTEINT": "INT64",
    "FLOAT": "FLOAT64",
    "DOUBLE": "FLOAT64",
    "BOOLEAN": "BOOL",
    "RECORD": "STRUCT",
    "DECIMAL": "NUMERIC",
    "BIGDECIMAL": "BIGNUMERIC",
}

# Coarse buckets the UI reasons about: which aggregations make sense, which
# icon to show, whether a column is a plausible partition key.
NUMERIC_TYPES = {"INT64", "FLOAT64", "NUMERIC", "BIGNUMERIC"}
INTEGER_TYPES = {"INT64"}
TEMPORAL_TYPES = {"DATE", "DATETIME", "TIME", "TIMESTAMP"}
TEXT_TYPES = {"STRING", "BYTES"}
BOOLEAN_TYPES = {"BOOL"}
COMPLEX_TYPES = {"STRUCT", "ARRAY", "JSON", "GEOGRAPHY", "RANGE", "INTERVAL"}


def standard_type(field_type: Optional[str], mode: Optional[str] = None) -> str:
    """
    Normalise one BigQuery type to its GoogleSQL spelling.

    A REPEATED field is an ARRAY of the underlying type, which is how you would
    have to declare it in DDL, so that is how it is rendered.
    """
    raw = (field_type or "").strip().upper()
    if not raw:
        return "STRING"

    base = LEGACY_TO_STANDARD.get(raw, raw)

    if (mode or "").strip().upper() == "REPEATED":
        return f"ARRAY<{base}>"

    return base


def category(std_type: str) -> str:
    """Coarse bucket for a normalised type."""
    base = (std_type or "").upper()
    if base.startswith("ARRAY<"):
        return "array"
    if base.startswith("STRUCT"):
        return "struct"
    if base in NUMERIC_TYPES:
        return "numeric"
    if base in TEMPORAL_TYPES:
        return "temporal"
    if base in BOOLEAN_TYPES:
        return "boolean"
    if base in TEXT_TYPES:
        return "text"
    if base in COMPLEX_TYPES:
        return "complex"
    return "other"


def is_numeric(std_type: str) -> bool:
    return category(std_type) == "numeric"


def is_temporal(std_type: str) -> bool:
    return category(std_type) == "temporal"


def is_text(std_type: str) -> bool:
    return category(std_type) == "text"


def is_boolean(std_type: str) -> bool:
    return category(std_type) == "boolean"


def is_aggregatable(std_type: str) -> bool:
    """True when SUM / AVG are meaningful."""
    return is_numeric(std_type)


def schema_field_to_dict(field: Any, path_prefix: str = "") -> Dict[str, Any]:
    """
    Turn a google.cloud.bigquery.SchemaField into a plain dict.

    Nested STRUCT fields are flattened into dotted names as well as kept in a
    children list, so the UI can show either a flat column list or a tree.
    """
    name = f"{path_prefix}{field.name}"
    std = standard_type(field.field_type, field.mode)
    out: Dict[str, Any] = {
        "name": name,
        "data_type": std,
        "data_type_yaml": std.lower(),
        "mode": (field.mode or "NULLABLE").upper(),
        "nullable": (field.mode or "NULLABLE").upper() != "REQUIRED",
        "repeated": (field.mode or "").upper() == "REPEATED",
        "category": category(std),
        "description": getattr(field, "description", None) or "",
        "children": [],
    }

    sub_fields = getattr(field, "fields", None) or ()
    if sub_fields:
        out["children"] = [
            schema_field_to_dict(sub, path_prefix=f"{name}.")
            for sub in sub_fields
        ]

    return out


def schema_to_columns(schema: Any) -> List[Dict[str, Any]]:
    """Convert a BigQuery result schema into the UI column list."""
    if not schema:
        return []
    return [schema_field_to_dict(field) for field in schema]


def flatten_columns(columns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Depth-first flatten so nested STRUCT leaves appear as dotted columns."""
    flat: List[Dict[str, Any]] = []
    for col in columns:
        flat.append(col)
        if col.get("children"):
            flat.extend(flatten_columns(col["children"]))
    return flat
