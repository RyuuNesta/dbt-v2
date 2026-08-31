"""
Extract the table references a statement names, for dataset scoping.

This is the syntactic half of the dataset guard. It answers "which datasets did
the author actually write down". The other half lives in warehouse.py and uses
BigQuery's own dry-run `referenced_tables`, which answers "which physical tables
would really be read".

Both are needed, because they catch different things:

  syntactic   catches intent. `select * from gold_dbt.x` is refused even though
              BigQuery would happily run it.
  referenced  catches leakage through views. A view inside an allowed dataset
              can select from a forbidden one; the text never mentions the
              forbidden dataset but the data still reaches the screen.

Neither alone is sufficient. A view hides the real source from the text, and
`referenced_tables` reports the expanded physical tables rather than the view the
author named, so it would wrongly reject nothing while wrongly accepting the
author's stated intent.
"""

from __future__ import annotations

import re
from typing import Dict, List, Set, Tuple

# `from` / `join` followed by a possibly-qualified, possibly-backticked name.
# Anchoring on the keyword avoids mistaking struct field access (a.b.c) for a
# three part table reference.
_TABLE_REF = re.compile(
    r"""
    \b(?:from|join)\s+
    (                                   # the reference
      (?: `[^`]+` | [A-Za-z_][A-Za-z0-9_$-]* )
      (?: \s*\.\s* (?: `[^`]+` | [A-Za-z_][A-Za-z0-9_$-]* ) ){0,2}
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Names that appear after FROM but are not tables.
_NOT_TABLES = {
    "select", "unnest", "lateral", "values", "with", "using", "on",
    "cross", "inner", "left", "right", "full", "outer", "join",
}


def strip_noise(sql: str) -> str:
    """
    Remove comments and string literals.

    Done before scanning so a dataset name mentioned inside a comment or a
    string cannot trip the guard, and so a `--` inside a string cannot swallow
    the rest of a line.
    """
    out: List[str] = []
    i = 0
    length = len(sql)

    while i < length:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < length else ""

        if ch == "-" and nxt == "-":
            end = sql.find("\n", i)
            i = length if end == -1 else end
            continue
        if ch == "#":
            end = sql.find("\n", i)
            i = length if end == -1 else end
            continue
        if ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            i = length if end == -1 else end + 2
            out.append(" ")
            continue

        if ch in ("'", '"'):
            quote = ch
            # BigQuery triple-quoted strings.
            triple = sql[i:i + 3] == quote * 3
            if triple:
                end = sql.find(quote * 3, i + 3)
                i = length if end == -1 else end + 3
            else:
                j = i + 1
                while j < length:
                    if sql[j] == "\\":
                        j += 2
                        continue
                    if sql[j] == quote:
                        j += 1
                        break
                    j += 1
                i = j
            out.append(" ")
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _split_reference(raw: str) -> List[str]:
    """
    Split a captured reference into its parts.

    Handles all the spellings BigQuery accepts:
        `proj`.`ds`.`tbl`      three backtick groups
        `proj.ds.tbl`          one backtick group containing dots
        proj.ds.tbl            bare
        ds.tbl                 two part
    """
    text = raw.strip()

    # One backtick group wrapping everything: unwrap, then split on dots.
    if text.startswith("`") and text.endswith("`") and text.count("`") == 2:
        return [part.strip() for part in text[1:-1].split(".") if part.strip()]

    parts: List[str] = []
    for chunk in re.split(r"\.(?=(?:[^`]*`[^`]*`)*[^`]*$)", text):
        cleaned = chunk.strip().strip("`").strip()
        if cleaned:
            parts.append(cleaned)
    return parts


def extract_references(sql: str, default_project: str = "") -> List[Dict[str, str]]:
    """
    Every table reference the statement names.

    CTE names and single-part identifiers are skipped: they resolve inside the
    query, not to a dataset. Returns dicts with project / dataset / table, where
    project may be empty for a two part reference.
    """
    cleaned = strip_noise(sql)
    cte_names = _cte_names(cleaned)

    references: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str, str]] = set()

    for match in _TABLE_REF.finditer(cleaned):
        parts = _split_reference(match.group(1))
        if not parts:
            continue

        first = parts[0].lower()
        if first in _NOT_TABLES:
            continue

        if len(parts) == 1:
            # A CTE, an alias, or an unqualified table in the default dataset.
            # Nothing to scope on unless it names a CTE we already know about.
            continue

        if len(parts) == 2:
            project = default_project
            dataset, table = parts[0], parts[1]
        else:
            project, dataset, table = parts[0], parts[1], ".".join(parts[2:])

        if dataset.lower() in cte_names:
            continue

        key = (project.lower(), dataset.lower(), table.lower())
        if key in seen:
            continue
        seen.add(key)

        references.append({
            "project": project,
            "dataset": dataset,
            "table": table,
            "reference": f"{project + '.' if project else ''}{dataset}.{table}",
        })

    return references


def _cte_names(cleaned_sql: str) -> Set[str]:
    """
    Names bound by WITH, so they are not mistaken for datasets.

    Only used to avoid false positives; a CTE cannot grant access to anything.
    """
    names: Set[str] = set()
    for match in re.finditer(
        r"(?:\bwith\b|,)\s*([A-Za-z_][A-Za-z0-9_$]*)\s+as\s*\(",
        cleaned_sql,
        re.IGNORECASE,
    ):
        names.add(match.group(1).lower())
    return names


def datasets_named(sql: str, default_project: str = "") -> Set[str]:
    """Just the dataset names, lower-cased, for an allowlist comparison."""
    return {
        reference["dataset"].lower()
        for reference in extract_references(sql, default_project)
        if reference["dataset"]
    }
