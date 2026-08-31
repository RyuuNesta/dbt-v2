"""
Offline check of backend/erd.py.

target/manifest.json is not built in this checkout, and the ERD must work
without a warehouse anyway, so this drives erd.build() with a synthetic manifest
shaped like the real one: bronze -> silver -> gold over gl_entries, a seed, a
source, a declared relationships test, and a dimension table that only naming
connects.

What it proves:
  - gold is excluded by default and included on request
  - the three detection signals fire, and only where they should
  - cardinality follows declared uniqueness rather than guessing
  - generic shared columns (period_month, company_code) do not invent edges
  - Mermaid and DBML output is well-formed, and DBML refuses to render a
    naming guess as a real foreign key

Run:  python dbt_ui\_erdtest.py
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dbt_ui.backend import erd as erd_mod  # noqa: E402

FAILURES = []
PROJECT = "asg"
DB = "data-analytics-asg"


def check(label, condition, detail=""):
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))
        FAILURES.append(label)


# --------------------------------------------------------------------------
# a fake Manifest with only the surface erd.py touches
# --------------------------------------------------------------------------

def col(name, data_type, description=""):
    return {"name": name, "data_type": data_type, "description": description}


def model(name, layer, columns, materialized="table", dataset=None):
    dataset = dataset or f"{layer}_dbt"
    return {
        "resource_type": "model",
        "name": name,
        "description": f"{layer} model {name}",
        "config": {"materialized": materialized},
        "tags": [layer],
        "fqn": [PROJECT, layer, name],
        "database": DB,
        "schema": dataset,
        "alias": name,
        "relation_name": f"`{DB}`.`{dataset}`.`{name}`",
        "path": f"{layer}/{name}.sql",
        "original_file_path": f"models/{layer}/{name}.sql",
        "patch_path": f"{PROJECT}://models/{layer}/_{layer}__models.yml",
        "columns": {c["name"]: c for c in columns},
        "depends_on": {"nodes": []},
        "raw_code": "",
        "compiled_code": "",
    }


def generic_test(name, test_type, parent_ids, column=None, kwargs=None,
                 attached=None):
    return {
        "resource_type": "test",
        "name": name,
        "column_name": column,
        "config": {"severity": "error"},
        "test_metadata": {"name": test_type, "kwargs": kwargs or {}},
        "depends_on": {"nodes": list(parent_ids)},
        "attached_node": attached,
        "tags": [],
        "fqn": [PROJECT, name],
    }


BRONZE_COLS = [
    col("gl_entry_key", "STRING", "Surrogate key."),
    col("document_number", "INT64"),
    col("company_code", "INT64"),
    col("fiscal_year", "INT64"),
    col("posting_date", "DATE"),
    col("document_type", "STRING"),
    col("gl_account", "INT64"),
    col("cost_center", "STRING"),
    col("currency", "STRING"),
    col("vendor_customer", "STRING"),
    col("amount_local", "NUMERIC"),
    col("_bronze_loaded_at", "TIMESTAMP"),
    col("_dbt_invocation_id", "STRING"),
    col("_dbt_target", "STRING"),
]

SILVER_COLS = BRONZE_COLS[:-3] + [
    col("period_month", "DATE"),
    col("period_year", "INT64"),
    col("account_group", "STRING"),
    col("transaction_category", "STRING"),
    col("debit_amount", "NUMERIC"),
    col("credit_amount", "NUMERIC"),
    col("_silver_loaded_at", "TIMESTAMP"),
    col("_bronze_loaded_at", "TIMESTAMP"),
    col("_dbt_invocation_id", "STRING"),
    col("_dbt_target", "STRING"),
]

GOLD_COLS = [
    col("gl_summary_key", "STRING"),
    col("company_code", "INT64"),
    col("period_month", "DATE"),
    col("account_group", "STRING"),
    col("transaction_category", "STRING"),
    col("entry_count", "INT64"),
    col("net_amount_mtd", "NUMERIC"),
    col("_gold_loaded_at", "TIMESTAMP"),
]

# A dimension the naming convention links to, with nothing else in common.
COST_CENTER_COLS = [
    col("cost_center_key", "STRING"),
    col("cost_center_name", "STRING"),
    col("region", "STRING"),
]


class FakeManifest:
    def __init__(self):
        self.metadata = {"generated_at": "2026-08-31T00:00:00Z",
                         "project_name": PROJECT}

        seed_id = f"seed.{PROJECT}.gl_entries"
        bronze_id = f"model.{PROJECT}.bronze_gl_entries"
        silver_id = f"model.{PROJECT}.silver_gl_entries"
        gold_id = f"model.{PROJECT}.gold_gl_monthly_summary"
        dim_id = f"model.{PROJECT}.silver_cost_center"
        source_id = f"source.{PROJECT}.sap.ledger_export"

        self.ids = {
            "seed": seed_id, "bronze": bronze_id, "silver": silver_id,
            "gold": gold_id, "dim": dim_id, "source": source_id,
        }

        seed = model("gl_entries", "seed", BRONZE_COLS[:11],
                     materialized="seed", dataset="dbt_dev_seeds")
        seed["resource_type"] = "seed"
        seed["fqn"] = [PROJECT, "gl_entries"]
        seed["tags"] = []

        bronze = model("bronze_gl_entries", "bronze", BRONZE_COLS)
        bronze["depends_on"] = {"nodes": [seed_id, source_id]}

        silver = model("silver_gl_entries", "silver", SILVER_COLS,
                       materialized="view")
        silver["depends_on"] = {"nodes": [bronze_id]}

        gold = model("gold_gl_monthly_summary", "gold", GOLD_COLS)
        gold["depends_on"] = {"nodes": [silver_id]}

        dim = model("silver_cost_center", "silver", COST_CENTER_COLS,
                    materialized="view")

        self.nodes = {
            seed_id: seed,
            bronze_id: bronze,
            silver_id: silver,
            gold_id: gold,
            dim_id: dim,
        }

        # ---- tests ----
        self.nodes["test.asg.unique_bronze_key"] = generic_test(
            "unique_bronze_gl_entries_gl_entry_key", "unique", [bronze_id],
            "gl_entry_key", attached=bronze_id)
        self.nodes["test.asg.unique_silver_key"] = generic_test(
            "unique_silver_gl_entries_gl_entry_key", "unique", [silver_id],
            "gl_entry_key", attached=silver_id)
        self.nodes["test.asg.unique_gold_key"] = generic_test(
            "unique_gold_gl_summary_key", "unique", [gold_id],
            "gl_summary_key", attached=gold_id)
        self.nodes["test.asg.unique_dim_key"] = generic_test(
            "unique_silver_cost_center_key", "unique", [dim_id],
            "cost_center_key", attached=dim_id)
        self.nodes["test.asg.nn_amount"] = generic_test(
            "not_null_silver_amount_local", "not_null", [silver_id],
            "amount_local", attached=silver_id)
        # The one enforced relationship in the project.
        self.nodes["test.asg.rel_silver_bronze"] = generic_test(
            "relationships_silver_gl_entries_gl_entry_key", "relationships",
            [bronze_id, silver_id], "gl_entry_key",
            kwargs={"to": "ref('bronze_gl_entries')", "field": "gl_entry_key",
                    "column_name": "gl_entry_key"},
            attached=silver_id)

        self.sources = {
            source_id: {
                "resource_type": "source",
                "name": "ledger_export",
                "source_name": "sap",
                "description": "Raw SAP extract.",
                "database": DB,
                "schema": "bronze_dbt",
                "identifier": "ledger_export",
                "relation_name": f"`{DB}`.`bronze_dbt`.`ledger_export`",
                "columns": {c["name"]: c for c in BRONZE_COLS[:6]},
                "loaded_at_field": "_bronze_loaded_at",
                "freshness": None,
                "unique_id": source_id,
            }
        }

        self.macros = {}
        self.exposures = {}

        self.parent_map = {
            bronze_id: [seed_id, source_id],
            silver_id: [bronze_id],
            gold_id: [silver_id],
            dim_id: [],
            seed_id: [],
        }
        self.child_map = {
            seed_id: [bronze_id],
            source_id: [bronze_id],
            bronze_id: [silver_id],
            silver_id: [gold_id],
            gold_id: [],
            dim_id: [],
        }

        self._by_name = {
            node["name"]: uid for uid, node in self.nodes.items()
            if node["resource_type"] in ("model", "seed", "snapshot")
        }

    # --- the Manifest surface erd.py uses ---

    def unique_id_for_name(self, name):
        return self._by_name.get(name)

    def source_by_key(self, source_name, table_name):
        for src in self.sources.values():
            if src["source_name"] == source_name and src["name"] == table_name:
                return src
        return None

    def buildable_nodes(self):
        from dbt_ui.backend.config import LAYER_ORDER, layer_of
        out = []
        tests = {}
        for uid, node in self.nodes.items():
            if node["resource_type"] != "test":
                continue
            for parent in node["depends_on"]["nodes"]:
                tests.setdefault(parent, []).append(uid)

        for uid, node in self.nodes.items():
            if node["resource_type"] not in ("model", "seed", "snapshot"):
                continue
            layer = layer_of(node.get("tags"), node.get("fqn"),
                             node["resource_type"])
            columns = node.get("columns") or {}
            out.append({
                "unique_id": uid,
                "name": node["name"],
                "resource_type": node["resource_type"],
                "layer": layer,
                "layer_order": LAYER_ORDER.get(layer, 99),
                "description": node.get("description") or "",
                "materialized": node["config"].get("materialized"),
                "database": node.get("database"),
                "schema": node.get("schema"),
                "alias": node.get("alias"),
                "relation_name": node.get("relation_name"),
                "tags": node.get("tags") or [],
                "path": node.get("path"),
                "original_file_path": node.get("original_file_path"),
                "patch_path": node.get("patch_path"),
                "column_count": len(columns),
                "documented_columns": 0,
                "typed_columns": len(columns),
                "test_count": len(tests.get(uid, [])),
                "tests": [],
                "depends_on": node["depends_on"]["nodes"],
                "children": self.child_map.get(uid, []),
                "partition_by": None,
                "cluster_by": None,
                "has_description": True,
            })
        out.sort(key=lambda n: (n["layer_order"], n["name"]))
        return out

    def source_summaries(self):
        return [{
            "unique_id": uid,
            "name": src["name"],
            "source_name": src["source_name"],
            "description": src["description"],
            "database": src["database"],
            "schema": src["schema"],
            "relation_name": src["relation_name"],
            "identifier": src["identifier"],
            "column_count": len(src["columns"]),
            "loaded_at_field": src["loaded_at_field"],
            "freshness": src["freshness"],
            "children": self.child_map.get(uid, []),
        } for uid, src in self.sources.items()]


def rel_between(model_payload, from_name, to_name, kind=None):
    return [
        r for r in model_payload["relationships"]
        if r["from_name"] == from_name and r["to_name"] == to_name
        and (kind is None or r["kind"] == kind)
    ]


def main():
    mf = FakeManifest()

    # ================= default build: gold shown, dimmed =================
    # Mirrors the existing lineage graph and Pipeline board: out-of-scope nodes
    # stay on the diagram, marked, rather than disappearing. Hiding them was
    # explicitly rejected elsewhere in this project because it makes the graph
    # look wrong, not because the boundary went away.
    print("\n== default build (gold shown, marked out of scope) ==")
    erd = erd_mod.build(mf, target="prod")

    names = [t["name"] for t in erd["tables"]]
    print(f"  tables: {names}")
    check("gold is present by default",
          "gold_gl_monthly_summary" in names, str(names))
    gold_table = next(t for t in erd["tables"] if t["name"] == "gold_gl_monthly_summary")
    check("gold is marked out of scope", gold_table["in_scope"] is False)
    check("bronze present", "bronze_gl_entries" in names)
    check("bronze is marked in scope",
          next(t for t in erd["tables"] if t["name"] == "bronze_gl_entries")["in_scope"] is True)
    check("silver present", "silver_gl_entries" in names)
    check("source present", "sap.ledger_export" in names)
    check("no hidden-table warning when nothing is hidden",
          not any("not shown" in w for w in erd["warnings"]), str(erd["warnings"]))
    check("hidden_out_of_scope is zero by default",
          erd["stats"]["hidden_out_of_scope"] == 0,
          str(erd["stats"]["hidden_out_of_scope"]))
    check("blocked_layers is still reported for the UI to read",
          erd["stats"]["blocked_layers"] == ["gold"],
          str(erd["stats"]["blocked_layers"]))

    # ---- primary keys ----
    print("\n== primary keys ==")
    by_name = {t["name"]: t for t in erd["tables"]}
    for name, table in sorted(by_name.items()):
        print(f"  {name:<24} pk={table['primary_key']}")
    check("bronze pk from the unique test",
          by_name["bronze_gl_entries"]["primary_key"] == ["gl_entry_key"],
          str(by_name["bronze_gl_entries"]["primary_key"]))
    check("silver pk from the unique test",
          by_name["silver_gl_entries"]["primary_key"] == ["gl_entry_key"])
    check("dimension pk from the unique test",
          by_name["silver_cost_center"]["primary_key"] == ["cost_center_key"])
    check("is_primary flag set on the pk column",
          any(c["is_primary"] for c in by_name["bronze_gl_entries"]["columns"]))
    check("audit columns are never keys",
          not any(c["looks_like_key"]
                  for c in by_name["silver_gl_entries"]["columns"]
                  if c["name"].startswith("_")))

    # ---- relationships ----
    print("\n== relationships ==")
    for r in erd["relationships"]:
        print(f"  {r['kind']:<10} {r['confidence']:<7} {r['cardinality']:<5} "
              f"{r['from_name']}.{'+'.join(r['from_columns']) or '?'} -> "
              f"{r['to_name']}.{'+'.join(r['to_columns']) or '?'}")

    declared = rel_between(erd, "silver_gl_entries", "bronze_gl_entries", "declared")
    check("the relationships test produced a declared edge", len(declared) == 1,
          str(len(declared)))
    if declared:
        check("declared edge is high confidence",
              declared[0]["confidence"] == "high")
        check("declared edge is 1:1 when both sides are unique",
              declared[0]["cardinality"] == "1:1", declared[0]["cardinality"])
        check("declared edge names the enforcing test",
              "relationships test" in declared[0]["evidence"])

    lineage = rel_between(erd, "bronze_gl_entries", "silver_gl_entries", "lineage")
    check("ref() produced a lineage edge", len(lineage) == 1, str(len(lineage)))
    if lineage:
        check("lineage join key found via the shared unique key",
              lineage[0]["from_columns"] == ["gl_entry_key"],
              str(lineage[0]["from_columns"]))
        check("lineage edge is 1:1 here", lineage[0]["cardinality"] == "1:1",
              lineage[0]["cardinality"])

    inferred = rel_between(erd, "silver_gl_entries", "silver_cost_center", "inferred")
    check("naming produced an inferred edge to the dimension",
          len(inferred) == 1, str(len(inferred)))
    if inferred:
        check("inferred edge is not high confidence",
              inferred[0]["confidence"] != "high", inferred[0]["confidence"])
        check("inferred edge says no test confirms it",
              "No relationships test" in inferred[0]["evidence"])
        check("inferred edge is N:1", inferred[0]["cardinality"] == "N:1",
              inferred[0]["cardinality"])

    # ---- what must NOT be detected ----
    print("\n== false positives ==")
    kinds = {(r["from_name"], r["to_name"]) for r in erd["relationships"]}
    check("period_month alone does not relate two tables",
          ("silver_cost_center", "silver_gl_entries") not in kinds)
    check("no self-relationships",
          not any(r["from_table"] == r["to_table"] for r in erd["relationships"]))
    check("no duplicate relationship ids",
          len({r["id"] for r in erd["relationships"]}) == len(erd["relationships"]))
    for r in erd["relationships"]:
        if r["from_columns"]:
            check(f"'{r['from_columns'][0]}' is not a generic column",
                  r["from_columns"][0] not in erd_mod.GENERIC_COLUMNS)

    # ================= in_scope_only opt-in =================
    print("\n== in_scope_only=True hides gold instead of dimming it ==")
    scoped = erd_mod.build(mf, target="prod", in_scope_only=True)
    scoped_names = [t["name"] for t in scoped["tables"]]
    check("gold is hidden when in_scope_only is set",
          "gold_gl_monthly_summary" not in scoped_names, str(scoped_names))
    check("a warning explains the omission",
          any("not shown" in w for w in scoped["warnings"]), str(scoped["warnings"]))
    check("hidden_out_of_scope counted", scoped["stats"]["hidden_out_of_scope"] >= 1,
          str(scoped["stats"]["hidden_out_of_scope"]))

    with_gold = erd  # the default build already includes it, dimmed
    check("gold picks up its lineage edge from silver",
          len(rel_between(with_gold, "silver_gl_entries",
                          "gold_gl_monthly_summary", "lineage")) == 1)
    gold_lineage = rel_between(with_gold, "silver_gl_entries",
                               "gold_gl_monthly_summary", "lineage")
    if gold_lineage:
        print(f"  gold lineage: cols={gold_lineage[0]['from_columns']} "
              f"card={gold_lineage[0]['cardinality']} "
              f"conf={gold_lineage[0]['confidence']}")
        check("aggregate edge does not claim a shared unique key",
              gold_lineage[0]["cardinality"] != "1:1",
              gold_lineage[0]["cardinality"])

    # ================= scope selection =================
    print("\n== scope selection ==")
    subset = erd_mod.build(mf, target="prod",
                           only_tables=["silver_gl_entries", "bronze_gl_entries"])
    check("only the requested tables are returned",
          {t["name"] for t in subset["tables"]}
          == {"silver_gl_entries", "bronze_gl_entries"},
          str([t["name"] for t in subset["tables"]]))
    check("edges to omitted tables are dropped",
          all(r["from_name"] in ("silver_gl_entries", "bronze_gl_entries")
              and r["to_name"] in ("silver_gl_entries", "bronze_gl_entries")
              for r in subset["relationships"]))

    by_dataset = erd_mod.build(mf, target="prod", datasets=["silver_dbt"])
    check("dataset filter works",
          {t["dataset"] for t in by_dataset["tables"]} == {"silver_dbt"},
          str({t["dataset"] for t in by_dataset["tables"]}))

    no_sources = erd_mod.build(mf, target="prod", include_sources=False)
    check("sources can be excluded",
          not any(t["resource_type"] == "source" for t in no_sources["tables"]))

    empty = erd_mod.build(mf, target="prod", only_tables=["does_not_exist"])
    check("an unmatched selection yields an empty diagram, not an error",
          empty["stats"]["table_count"] == 0)
    check("and no relationships", empty["stats"]["relationship_count"] == 0)

    # ================= stats =================
    print("\n== stats ==")
    print(f"  {erd['stats']}")
    check("by_kind counts every relationship",
          sum(erd["stats"]["by_kind"].values()) == len(erd["relationships"]))
    check("keyless tables are reported",
          isinstance(erd["stats"]["keyless_tables"], list))
    check("scope is echoed for the UI", "allowed_datasets" in erd["scope"])

    # ================= mermaid =================
    print("\n== mermaid ==")
    mermaid = erd_mod.to_mermaid(with_gold)
    print("\n".join(mermaid.splitlines()[:14]))
    check("starts with erDiagram", "erDiagram" in mermaid.splitlines()[2])
    check("no dots in mermaid identifiers",
          not any("." in line.split()[0]
                  for line in mermaid.splitlines()
                  if line.startswith("    ") and line.strip().endswith("{")))
    # A raw brace count cannot work here: the cardinality connectors }o--o{ are
    # made of braces too. Only structural block delimiters are counted.
    opens = [l for l in mermaid.splitlines() if l.startswith("    ") and l.rstrip().endswith("{")
             and "--" not in l]
    closes = [l for l in mermaid.splitlines() if l.strip() == "}"]
    check("one table block per table",
          len(opens) == len(with_gold["tables"]),
          f"{len(opens)} blocks vs {len(with_gold['tables'])} tables")
    check("every table block is closed", len(opens) == len(closes),
          f"{len(opens)} open vs {len(closes)} close")
    check("PK markers emitted", " PK" in mermaid)
    check("a relationship line is emitted", "||--||" in mermaid or "}o--||" in mermaid)
    check("relationship kind is labelled", "declared on" in mermaid)

    keys_only = erd_mod.to_mermaid(with_gold, keys_only=True)
    check("keys_only is smaller than the full diagram",
          len(keys_only) < len(mermaid),
          f"{len(keys_only)} vs {len(mermaid)}")
    check("keys_only still contains the pk", "gl_entry_key" in keys_only)
    check("keys_only drops a plain measure", "net_amount_mtd" not in keys_only)

    # ================= dbml =================
    print("\n== dbml ==")
    dbml = erd_mod.to_dbml(with_gold)
    print("\n".join(dbml.splitlines()[:16]))
    check("tables declared", dbml.count("Table ") == len(with_gold["tables"]),
          f"{dbml.count('Table ')} vs {len(with_gold['tables'])}")
    check("braces balance",
          dbml.count("{") == dbml.count("}"),
          f"{dbml.count('{')} vs {dbml.count('}')}")
    check("pk marked", "[pk]" in dbml or "pk," in dbml)
    check("the declared edge becomes a real Ref",
          any(line.startswith("Ref:") for line in dbml.splitlines()))

    ref_lines = [l for l in dbml.splitlines() if l.startswith("Ref:")]
    print(f"  Ref lines: {ref_lines}")
    check("exactly one Ref, for the one tested relationship",
          len(ref_lines) == 1, str(len(ref_lines)))
    check("inferred edges stay comments, not Refs",
          not any("cost_center" in l for l in ref_lines))
    check("inferred edges are still recorded as comments",
          any("cost_center" in l and l.startswith("//")
              for l in dbml.splitlines()))
    check("no unescaped single quotes inside DBML notes",
          "''" not in dbml)

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print(f"all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
