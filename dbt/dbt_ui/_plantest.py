"""
Offline check of codegen.silver_plan() and its agreement with silver_model().

No warehouse, no manifest: a synthetic profile is fed straight into
recommend.analyse(), which is exactly what the API does after profiling. The
point is to prove three things:

  1. the plan payload has the shape the frontend reads
  2. every column the generator emits appears in the plan, and vice versa
  3. the row estimate follows the duplicate check rather than guessing

Run:  python dbt_ui\_plantest.py
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dbt_ui.backend import codegen, recommend  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))
        FAILURES.append(label)


def column(name, data_type, category, **kw):
    """A profiled column with sensible defaults."""
    base = {
        "name": name,
        "data_type": data_type.upper(),
        "data_type_yaml": data_type.lower(),
        "category": category,
        "mode": "NULLABLE",
        "description": "",
        "null_count": 0,
        "null_pct": 0.0,
        "distinct_count": 10,
        "distinct_pct": 50.0,
        "blank_count": 0,
        "negative_count": 0 if category == "numeric" else None,
        "min": "1",
        "max": "9",
        "is_unique": False,
        "is_constant": False,
        "is_all_null": False,
    }
    base.update(kw)
    return base


def build_profile():
    rows = 20
    return {
        "relation": "`data-analytics-asg`.`bronze_dbt`.`bronze_gl_entries`",
        "row_count": rows,
        "declared_row_count": rows,
        "sampled": False,
        "sample_rows": None,
        "bytes_processed": 4096,
        "duration_ms": 810,
        "table": {"table": "bronze_gl_entries", "row_count": rows},
        "columns": [
            column("gl_entry_key", "string", "text",
                   distinct_count=18, distinct_pct=90.0),
            column("document_number", "int64", "numeric",
                   distinct_count=18, distinct_pct=90.0),
            column("posting_date", "date", "temporal",
                   distinct_count=12, distinct_pct=60.0),
            column("currency", "string", "text",
                   distinct_count=1, distinct_pct=5.0, is_constant=True),
            column("document_type", "string", "text",
                   distinct_count=3, distinct_pct=15.0),
            # distinct_pct at the low-cardinality threshold, so this one earns a
            # categorisation label and exercises the plan's label branch.
            column("posting_key", "string", "text",
                   distinct_count=3, distinct_pct=5.0),
            column("cost_centre", "string", "text",
                   null_count=14, null_pct=70.0, distinct_count=4,
                   distinct_pct=20.0, blank_count=2),
            column("amount_local", "float64", "numeric",
                   distinct_count=19, distinct_pct=95.0, negative_count=7),
            column("legacy_flag", "string", "text",
                   null_count=rows, null_pct=100.0, distinct_count=0,
                   is_all_null=True),
            column("_bronze_loaded_at", "timestamp", "temporal"),
        ],
    }


def main():
    profile = build_profile()

    # ---------------- case 1: duplicates present, dedup accepted ----------
    duplicate = {
        "key": ["gl_entry_key"],
        "checked": True,
        "key_groups": 18,
        "duplicated_keys": 2,
        "surplus_rows": 2,
        "worst_group": 2,
        "is_unique": False,
    }
    analysis = recommend.analyse(profile, duplicate)
    analysis["duplicate_check"] = duplicate

    print(f"\nanalyse() produced {len(analysis['recommendations'])} recommendations")

    # Accept everything, so every branch of the plan is exercised.
    all_ids = [rec["id"] for rec in analysis["recommendations"]]

    plan = codegen.silver_plan(
        source_model="bronze_gl_entries",
        analysis=analysis,
        profile=profile,
        accepted_ids=all_ids,
    )

    print("\n-- shape --")
    for key in ("model_name", "source_model", "path", "materialized", "sources",
                "steps", "columns", "column_count", "dropped_columns",
                "key_columns", "row_estimate", "tests", "applied", "skipped"):
        check(f"plan has '{key}'", key in plan)

    check("model name is derived", plan["model_name"] == "silver_gl_entries",
          plan["model_name"])
    check("path lands in models/silver",
          plan["path"] == "models/silver/silver_gl_entries.sql", plan["path"])
    check("at least a read and an audit step", len(plan["steps"]) >= 2,
          str(len(plan["steps"])))
    check("first step is the read", plan["steps"][0]["kind"] == "read",
          plan["steps"][0]["kind"])
    check("last step is the audit stamp", plan["steps"][-1]["kind"] == "audit",
          plan["steps"][-1]["kind"])

    kinds = [step["kind"] for step in plan["steps"]]
    print("\n-- steps --")
    for step in plan["steps"]:
        print(f"  {step['kind']:<14} {step['title']}")
    check("deduplication is explained", "deduplicate" in kinds, str(kinds))
    check("pruning is explained", "prune" in kinds, str(kinds))
    check("categorisation is explained", "categorise" in kinds, str(kinds))

    print("\n-- row estimate --")
    est = plan["row_estimate"]
    print(f"  {est}")
    check("output rows follow the group-by count", est["rows"] == 18, str(est["rows"]))
    check("source rows reported", est["source_rows"] == 20, str(est["source_rows"]))
    check("surplus rows reported as removed", est["removed"] == 2, str(est["removed"]))
    check("estimate flagged exact on a full profile", est["exact"] is True)

    print("\n-- resulting schema --")
    for col in plan["columns"]:
        print(f"  {col['name']:<26} {col['data_type']:<10} {col['origin']:<12} {col['note']}")

    names = [col["name"] for col in plan["columns"]]
    check("no duplicate column names", len(names) == len(set(names)))
    check("key column comes first", names[0] == "gl_entry_key", names[0])
    check("all-null column omitted", "legacy_flag" not in names)
    check("categorised column gains a label", "posting_key_label" in names)
    check("audit macro columns present",
          {"_silver_loaded_at", "_dbt_invocation_id", "_dbt_target"} <= set(names))
    check("bronze audit column carried through", "_bronze_loaded_at" in names)

    # ---------------- agreement with the generator ----------------------
    generated = codegen.silver_model(
        source_model="bronze_gl_entries",
        analysis=analysis,
        profile=profile,
        accepted_ids=all_ids,
    )
    sql = generated["sql"]

    print("\n-- agreement with silver_model() --")
    check("same dropped columns",
          plan["dropped_columns"] == generated["dropped_columns"],
          f"{plan['dropped_columns']} vs {generated['dropped_columns']}")
    check("same key columns",
          plan["key_columns"] == generated["key_columns"],
          f"{plan['key_columns']} vs {generated['key_columns']}")
    check("same applied count",
          len(plan["applied"]) == len(generated["applied"]),
          f"{len(plan['applied'])} vs {len(generated['applied'])}")

    # Every non-macro column the plan promises must actually be aliased in the
    # SQL. The macro columns cannot be checked this way: they are inside Jinja.
    macro_names = {name for name, _, _ in codegen.AUDIT_MACRO_COLUMNS}
    missing = [
        col["name"] for col in plan["columns"]
        if col["name"] not in macro_names and col["name"] not in sql
    ]
    check("every promised column appears in the generated SQL", not missing,
          f"missing: {missing}")

    # And nothing the plan omitted should be selected forward.
    for dropped in plan["dropped_columns"]:
        check(f"omitted column '{dropped}' is absent from the select list",
              f" {dropped}," not in sql and f"        {dropped}" not in sql)

    # ---------------- case 2: unique key, no dedup ----------------------
    print("\n-- case 2: key already unique --")
    unique_dup = {
        "key": ["gl_entry_key"],
        "checked": True,
        "key_groups": 20,
        "duplicated_keys": 0,
        "surplus_rows": 0,
        "worst_group": 1,
        "is_unique": True,
    }
    analysis2 = recommend.analyse(profile, unique_dup)
    analysis2["duplicate_check"] = unique_dup
    plan2 = codegen.silver_plan(
        source_model="bronze_gl_entries",
        analysis=analysis2,
        profile=profile,
        accepted_ids=[r["id"] for r in analysis2["recommendations"]],
    )
    est2 = plan2["row_estimate"]
    print(f"  {est2}")
    check("no dedup step when the key is unique",
          "deduplicate" not in [s["kind"] for s in plan2["steps"]])
    check("row count carries through unchanged", est2["rows"] == 20, str(est2["rows"]))
    check("nothing removed", est2["removed"] == 0, str(est2["removed"]))

    # ---------------- case 3: nothing accepted --------------------------
    print("\n-- case 3: every recommendation unticked --")
    plan3 = codegen.silver_plan(
        source_model="bronze_gl_entries",
        analysis=analysis,
        profile=profile,
        accepted_ids=[],
    )
    check("still explains the read and the audit stamp",
          [s["kind"] for s in plan3["steps"]] == ["read", "audit"],
          str([s["kind"] for s in plan3["steps"]]))
    check("nothing dropped", plan3["dropped_columns"] == [])
    check("all-null column now carried through",
          "legacy_flag" in [c["name"] for c in plan3["columns"]])
    check("row count unchanged", plan3["row_estimate"]["rows"] == 20)

    # ---------------- case 4: sampled profile ---------------------------
    print("\n-- case 4: sampled profile --")
    sampled = dict(profile)
    sampled["sampled"] = True
    sampled["declared_row_count"] = 500000
    analysis4 = recommend.analyse(sampled, duplicate)
    analysis4["duplicate_check"] = duplicate
    plan4 = codegen.silver_plan(
        source_model="bronze_gl_entries",
        analysis=analysis4,
        profile=sampled,
        accepted_ids=[r["id"] for r in analysis4["recommendations"]],
    )
    est4 = plan4["row_estimate"]
    print(f"  {est4}")
    check("a sampled profile never claims an exact count", est4["exact"] is False)
    check("source rows use the declared count", est4["source_rows"] == 500000,
          str(est4["source_rows"]))

    # ---------------- case 5: dedup proposed but key unverified ---------
    print("\n-- case 5: dedup proposed, key never verified --")
    analysis5 = recommend.analyse(profile, None)
    analysis5["duplicate_check"] = None
    plan5 = codegen.silver_plan(
        source_model="bronze_gl_entries",
        analysis=analysis5,
        profile=profile,
        accepted_ids=[r["id"] for r in analysis5["recommendations"]],
    )
    est5 = plan5["row_estimate"]
    print(f"  {est5}")
    if "deduplicate" in [s["kind"] for s in plan5["steps"]]:
        check("unknown rather than a guess", est5["rows"] is None, str(est5["rows"]))
        check("basis explains why it is unknown",
              "never verified" in est5["basis"], est5["basis"])
    else:
        check("no dedup step, so a count is given", est5["rows"] is not None)

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
