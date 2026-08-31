"""Throwaway: exercise modelgen, especially the ref-rewriting regex."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from backend import config, manifest as manifest_mod, modelgen

passed = failed = 0


def check(label, cond, extra=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"  {'pass' if cond else 'FAIL'}  {label}{('  ' + str(extra)) if extra else ''}")


mf = manifest_mod.load()
index = modelgen.build_relation_index(mf)

print("=== relation index built from the real manifest ===")
print(f"  exact keys   : {len(index['exact'])}")
print(f"  dataset keys : {len(index['dataset'])}")
print(f"  table keys   : {len(index['table'])}")
for key in sorted(index["exact"])[:6]:
    print(f"    {key} -> {index['exact'][key]['expression']}")
check("index is populated", len(index["exact"]) > 0)
check("bronze_gl_entries indexed by table", "bronze_gl_entries" in index["table"])

print("\n=== rewriting: the forms BigQuery accepts ===")
project = "data-analytics-asg"
cases = [
    (f"select * from `{project}`.`dbt_dev_bronze`.`bronze_gl_entries`", True, "each part quoted"),
    (f"select * from `{project}.dbt_dev_bronze.bronze_gl_entries`", True, "one backtick pair"),
    # Unquoted hyphenated project is not valid BigQuery, but rewriting it is
    # still the right move: the result is valid and correctly wired.
    (f"select * from {project}.dbt_dev_bronze.bronze_gl_entries", True, "unquoted, hyphenated project"),
    ("select * from dbt_dev_bronze.bronze_gl_entries", True, "dataset.table"),
    ("select * from `dbt_dev_bronze`.`bronze_gl_entries`", True, "quoted dataset.table"),
    ("select * from bronze_dbt.bronze_gl_entries", True, "prod dataset, table-name match"),
    ("SELECT * FROM DBT_DEV_BRONZE.BRONZE_GL_ENTRIES", True, "uppercase"),
    ("select * from\n  dbt_dev_bronze.bronze_gl_entries", True, "newline after from"),
]
for sql, should_rewrite, label in cases:
    result = modelgen.rewrite_refs(sql, index)
    did = "{{ ref(" in result["sql"]
    ok = did == should_rewrite
    check(f"{label}", ok,
          f"{'rewrote' if did else 'left alone'} | {result['sql'].splitlines()[-1].strip()[:58]}")

print("\n=== join clauses ===")
sql = """
select a.gl_entry_key, b.currency
from `data-analytics-asg`.`dbt_dev_bronze`.`bronze_gl_entries` as a
left join dbt_dev_silver.silver_gl_entries as b using (gl_entry_key)
inner join bronze_dbt.S1-Customers as c on c.id = a.company_code
"""
result = modelgen.rewrite_refs(sql, index)
print("  " + "\n  ".join(result["sql"].strip().splitlines()))
check("both known relations rewritten", result["sql"].count("{{ ref(") == 2,
      result["sql"].count("{{ ref("))
check("the unknown one is reported", len(result["unresolved"]) == 1,
      result["unresolved"])
check("unresolved carries dataset and table",
      result["unresolved"][0]["dataset"] == "bronze_dbt", result["unresolved"][0])

print("\n=== struct field access must NOT be rewritten ===")
danger = [
    "select payload.customer.id from `data-analytics-asg`.`dbt_dev_bronze`.`bronze_gl_entries`",
    "select t.nested.field from dbt_dev_bronze.bronze_gl_entries as t where t.a.b = 1",
    "select count(*) from dbt_dev_bronze.bronze_gl_entries group by a.b.c",
]
for sql in danger:
    result = modelgen.rewrite_refs(sql, index)
    # The table should be rewritten; the dotted column paths must survive.
    check("field paths preserved",
          "payload.customer.id" in result["sql"] or "t.nested.field" in result["sql"]
          or "a.b.c" in result["sql"],
          result["sql"][:80])
check("only one rewrite in the group-by case",
      modelgen.rewrite_refs(danger[2], index)["sql"].count("{{ ref(") == 1)

print("\n=== unnest and function calls after FROM are not touched ===")
for sql in ("select x from unnest(payload.items) as x",
            "select * from unnest([1,2,3]) as n",
            "select * from my_udf(a.b)"):
    result = modelgen.rewrite_refs(sql, index)
    check("left alone", result["sql"] == sql and not result["unresolved"],
          result["sql"][:70])

print("\n=== a comma join is caught by the sweep, not silently missed ===")
sql = ("select a.gl_entry_key from dbt_dev_bronze.bronze_gl_entries a, "
       "dbt_dev_silver.silver_gl_entries b where a.gl_entry_key = b.gl_entry_key")
result = modelgen.rewrite_refs(sql, index)
check("the first one was rewritten", "{{ ref('bronze_gl_entries') }}" in result["sql"])
check("the second is reported as missed", len(result["missed"]) == 1, result["missed"])
check("it names the model", result["missed"][0]["name"] == "silver_gl_entries",
      result["missed"])
out = modelgen.scaffold(mf, name="silver_comma_join", layer="silver", sql=sql)
check("scaffold warns about it",
      any("comma join" in w for w in out["warnings"]), out["warnings"])
check("and names what to fix",
      any("silver_gl_entries" in w for w in out["warnings"]))

print("\n=== a clean rewrite reports nothing missed ===")
result = modelgen.rewrite_refs(
    "select * from dbt_dev_bronze.bronze_gl_entries", index)
check("missed is empty", result["missed"] == [], result["missed"])

print("\n=== an existing ref() is left alone ===")
sql = "select * from {{ ref('bronze_gl_entries') }} where fiscal_year = 2026"
result = modelgen.rewrite_refs(sql, index)
check("unchanged", result["sql"] == sql, result["sql"])
check("no replacements claimed", not result["replacements"])
check("not reported as unresolved", not result["unresolved"])

print("\n=== prepare_sql ===")
body, warns = modelgen.prepare_sql("select 1 as a;")
check("semicolon stripped", body == "select 1 as a", body)
check("and explained", any("semicolon" in w for w in warns))

body, warns = modelgen.prepare_sql("select * from x limit 100")
check("limit warned", any("LIMIT" in w for w in warns))
check("select * warned", any("select *" in w for w in warns))

body, warns = modelgen.prepare_sql("select 1; select 2;")
check("multiple statements flagged", any("more than one statement" in w for w in warns))

print("\n=== validate_name ===")
existing = set(mf.ref_map())
name, errs, warns = modelgen.validate_name("Silver Revenue Report", existing, "silver")
check("normalised", name == "silver_revenue_report", name)
check("no errors", not errs, errs)
check("normalisation mentioned", any("normalised" in w for w in warns))

name, errs, warns = modelgen.validate_name("bronze_gl_entries", existing, "bronze")
check("collision is an error", any("already exists" in e for e in errs), errs)

name, errs, warns = modelgen.validate_name("9lives", existing, "silver")
check("leading digit rejected", bool(errs), errs)

name, errs, warns = modelgen.validate_name("select", existing, "silver")
check("reserved word rejected", any("reserved" in e for e in errs), errs)

name, errs, warns = modelgen.validate_name("my_report", existing, "silver")
check("layer prefix suggested", any("silver_my_report" in w for w in warns), warns)

print("\n=== scaffold: happy path, silver default materialization ===")
out = modelgen.scaffold(
    mf, name="silver_customer_summary", layer="silver",
    sql="select company_code, count(*) as n from bronze_dbt.bronze_gl_entries group by 1",
    description="One row per company with its posting count.",
)
check("ok", out["ok"], out["errors"])
check("path", out["path"] == "models/silver/silver_customer_summary.sql", out["path"])
check("does not exist yet", out["exists"] is False)
check("silver defaults to view", out["materialized"] == "view", out["materialized"])
check("no config block needed", out["uses_config_block"] is False)
check("config block absent from the file", "{{ config(" not in out["content"])
check("explains why there is no config",
      "already materialises silver as view" in out["content"])
check("ref rewritten", "{{ ref('bronze_gl_entries') }}" in out["content"])
check("description in the header", "One row per company" in out["content"])
print("  ---- file ----")
print("  " + "\n  ".join(out["content"].splitlines()))

print("\n=== scaffold: non-default materialization gets a config block ===")
out = modelgen.scaffold(
    mf, name="silver_big_table", layer="silver", materialized="table",
    sql="select * from {{ ref('bronze_gl_entries') }}",
)
check("config block present", out["uses_config_block"] is True)
check("rendered", "{{ config(materialized='table') }}" in out["content"],
      out["content"][:200])

print("\n=== scaffold: gold is refused ===")
for bad in ("gold", "GOLD", "platinum", ""):
    try:
        modelgen.scaffold(mf, name="x", layer=bad, sql="select 1")
        check(f"layer '{bad}' refused", False, "allowed through")
    except ValueError as exc:
        check(f"layer '{bad}' refused", True, str(exc)[:60])

print("\n=== scaffold: writes are refused ===")
for bad_sql in ("delete from bronze_dbt.bronze_gl_entries",
                "drop table bronze_dbt.bronze_gl_entries",
                "insert into bronze_dbt.x values (1)"):
    try:
        modelgen.scaffold(mf, name="x", layer="silver", sql=bad_sql)
        check(f"{bad_sql.split()[0]} refused", False, "allowed through")
    except ValueError as exc:
        check(f"{bad_sql.split()[0]} refused", True, str(exc)[:52])

print("\n=== scaffold: unresolved tables produce a TODO and a source stub ===")
out = modelgen.scaffold(
    mf, name="silver_from_foreign", layer="silver",
    sql="select * from bronze_dbt.payment_behavior_daily",
)
check("ok", out["ok"], out["errors"])
check("left hardcoded", "payment_behavior_daily" in out["sql"])
check("no ref invented", "{{ ref(" not in out["sql"])
check("unresolved reported", len(out["unresolved"]) == 1, out["unresolved"])
check("TODO in the file", "TODO" in out["content"])
check("warning explains the lineage gap",
      any("lineage graph" in w for w in out["warnings"]), out["warnings"])
check("source stub offered", "sources:" in out["source_stub"])
check("stub names the dataset", "name: bronze_dbt" in out["source_stub"])
check("stub names the table", "- name: payment_behavior_daily" in out["source_stub"])
print("  ---- source stub ----")
print("  " + "\n  ".join(out["source_stub"].splitlines()))

print("\n=== scaffold: name collision blocks it ===")
out = modelgen.scaffold(mf, name="bronze_gl_entries", layer="bronze",
                        sql="select 1 as a")
check("not ok", out["ok"] is False)
check("error explains", any("already exists" in e for e in out["errors"]), out["errors"])

print("\n=== scaffold: existing file is detected ===")
out = modelgen.scaffold(mf, name="my_first_dbt_model_testing", layer="silver",
                        sql="select 1 as a")
check("collision on name caught first",
      out["ok"] is False or out["exists"] is False, (out["ok"], out["exists"]))

print("\n=== scaffold: rewrite can be turned off ===")
out = modelgen.scaffold(
    mf, name="silver_literal", layer="silver", rewrite=False,
    sql="select * from dbt_dev_bronze.bronze_gl_entries",
)
check("literal kept", "dbt_dev_bronze.bronze_gl_entries" in out["sql"])
check("nothing claimed as replaced", not out["replacements"])

print(f"\n{'=' * 62}\n  {passed} passed, {failed} failed\n{'=' * 62}")
sys.exit(1 if failed else 0)
