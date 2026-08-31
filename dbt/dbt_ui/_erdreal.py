"""
Run erd.build() against the real target/manifest.json for this project.

Uses the real Manifest class from backend/manifest.py, not the fake one in
_erdtest.py, so any mismatch between what erd.py expects and what dbt actually
emits in 1.12.3 shows up here. Needs no BigQuery access: counts and constraints
default off.

Run:  python dbt_ui\_erdreal.py
"""
import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dbt_ui.backend import erd as erd_mod, manifest as manifest_mod  # noqa: E402


def main():
    mf = manifest_mod.load(force=True)
    print(f"loaded manifest: {len(mf.nodes)} nodes, {len(mf.sources)} sources")

    erd = erd_mod.build(mf, target="dev")

    print(f"\ntables: {len(erd['tables'])}")
    for table in erd["tables"]:
        print(f"  {table['layer']:<8} {table['name']:<32} "
              f"pk={table['primary_key']!s:<20} cols={table['column_count']}")

    print(f"\nrelationships: {len(erd['relationships'])}")
    for r in erd["relationships"]:
        print(f"  {r['kind']:<10} {r['confidence']:<7} {r['cardinality']:<5} "
              f"{r['from_name']}.{'+'.join(r['from_columns']) or '?'} -> "
              f"{r['to_name']}.{'+'.join(r['to_columns']) or '?'}")

    print(f"\nstats: {erd['stats']}")
    print(f"warnings: {erd['warnings']}")

    print("\n-- with gold included --")
    with_gold = erd_mod.build(mf, target="dev", in_scope_only=False)
    print(f"tables: {[t['name'] for t in with_gold['tables']]}")
    gold = next((t for t in with_gold["tables"] if t["layer"] == "gold"), None)
    print(f"gold table in_scope: {gold['in_scope'] if gold else 'NOT FOUND'}")

    print("\n-- mermaid (first 40 lines) --")
    mermaid = erd_mod.to_mermaid(with_gold)
    print("\n".join(mermaid.splitlines()[:40]))

    out_dir = pathlib.Path(__file__).resolve().parent
    (out_dir / "_erd_real_output.mmd").write_text(mermaid, encoding="utf-8")
    (out_dir / "_erd_real_output.dbml").write_text(erd_mod.to_dbml(with_gold), encoding="utf-8")
    (out_dir / "_erd_real_output.json").write_text(
        json.dumps(erd, indent=2, default=str), encoding="utf-8"
    )
    print(f"\nwrote _erd_real_output.mmd / .dbml / .json to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
