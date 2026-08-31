# ASG Data Platform — Data Team Guide

Everything in one place: what dbt is, how this project is laid out, and how to
use dbt Studio.

Written for data engineers and data analysts. No prior dbt experience assumed.

---

## Contents

1. [What dbt is](#1-what-dbt-is)
2. [dbt Cloud vs dbt Core](#2-dbt-cloud-vs-dbt-core)
3. [How this project is built](#3-how-this-project-is-built)
4. [Getting started](#4-getting-started)
5. [The UI, page by page](#5-the-ui-page-by-page)
6. [The two documentation engines](#6-the-two-documentation-engines)
7. [What is inside the generated documentation](#7-what-is-inside-the-generated-documentation)
8. [Guardrails](#8-guardrails)
9. [Troubleshooting](#9-troubleshooting)
10. [Reference](#10-reference)

---

## 1. What dbt is

dbt (data build tool) is how you turn SQL `SELECT` statements into a managed,
tested, documented set of tables and views in your warehouse.

The core idea: **you write a SELECT, dbt handles the rest.** You never write
`CREATE TABLE`, `DROP`, or `INSERT`. You write one `.sql` file per table
containing only a query, and dbt works out the DDL, the dependency order, and
the rebuild.

### The four things dbt gives you

**1. Dependency management through `ref()`**

Instead of hardcoding a table name:

```sql
-- without dbt: brittle, environment-specific
select * from `data-analytics-asg`.`silver_dbt`.`stg__ticket_trans`
```

you write:

```sql
-- with dbt: portable, and dbt now knows this model depends on that one
select * from {{ ref('stg__ticket_trans') }}
```

dbt reads every `ref()` in the project, builds a dependency graph (a DAG), and
runs your models in the correct order automatically. Change the target from dev
to prod and every `ref()` repoints itself — the SQL never changes.

**2. Testing**

You declare expectations in YAML and dbt turns them into SQL that must return
zero rows:

```yaml
columns:
  - name: gl_entry_key
    data_tests:
      - unique
      - not_null
```

If a duplicate ever appears, the build fails instead of quietly
double-counting downstream.

**3. Documentation as code**

Descriptions and column types live in YAML next to the SQL, in version control,
reviewed in the same pull request as the logic. They cannot drift out of date
the way a wiki page does.

**4. Environments**

The same code runs against a developer sandbox or against production, decided
by a target at run time. Nobody edits SQL to promote a change.

### What dbt does *not* do

- **It does not move data.** dbt transforms data that is already in BigQuery. If
  data needs to land in BigQuery first, that is a different tool (Debezium,
  Fivetran, an external table over Sheets, a manual load).
- **It does not schedule itself.** Something has to invoke `dbt build` on a
  cadence — an orchestrator, Cloud Scheduler, a CI pipeline.
- **It is not a BI tool.** It prepares the tables your BI layer reads.

---

## 2. dbt Cloud vs dbt Core

Same transformation engine underneath. The difference is everything around it.

| | dbt Cloud (what we used) | dbt Core (what we use now) |
| --- | --- | --- |
| Cost | Per-seat subscription | Free, open source |
| Where it runs | Google's/dbt Labs' servers | Your machine or your orchestrator |
| Interface | Web IDE, scheduler, docs site, logs | Command line |
| Scheduling | Built in | You provide it |
| Credentials | Managed in the Cloud UI | `profiles.yml` |

The migration saves the subscription cost. What you lose is the interface: with
dbt Core alone, everything happens through terminal commands.

**dbt Studio — the UI in `dbt_ui/` — exists to replace that lost interface**, and
adds the documentation and profiling features dbt Cloud never had.

---

## 3. How this project is built

### The medallion architecture

Data flows through three layers, each with one job. Never skip a layer, and never
put one layer's job in another.

```
  seeds/CSV ──► BRONZE ──► SILVER ──► GOLD ──► BI
                 raw       cleaned    business
```

**Bronze — raw landing zone.**
One row in, one row out. No filtering, no deduplication, no business logic. Light
type casting and audit columns only.

Why it matters: because bronze is a faithful copy, you can **replay history**
after a business rule changes. If bronze filtered or reshaped anything, that
original data is gone forever.

**Silver — cleaned and conformed.**
Deduplication, null handling, trimming, type discipline, derived categories, and
quality flags. Rows are **never dropped** for quality reasons — problems are
flagged on the row so they stay visible.

**Gold — business-facing.**
Aggregates and facts. The grain is explicit and stable. Measures are ready for
BI without further work.

### Where each layer physically lands

`macros/generate_schema_name.sql` routes writes based on the target, so a
developer cannot overwrite a production table:

| Target | bronze goes to | silver goes to | gold goes to | Use |
| --- | --- | --- | --- | --- |
| `dev` | `dbt_dev_bronze` | `dbt_dev_silver` | `dbt_dev_gold` | daily work |
| `test` | `dbt_ci_bronze` | `dbt_ci_silver` | `dbt_ci_gold` | CI |
| `prod` | `bronze_dbt` | `silver_dbt` | `gold_dbt` | orchestrator only |

`prod` writes the exact dataset names the business already reads. Every other
target writes to a prefixed sandbox.

> The gold column is listed for completeness, but **dbt Studio never reads or
> writes gold on any target.** Gold is built by the orchestrator from the command
> line. See [Guardrails](#8-guardrails).

### File layout

```
dbt/
  dbt_project.yml          project config, layer materializations, seed types
  profiles.yml             BigQuery connections (oauth, no secrets — safe to commit)
  packages.yml             dbt_utils 1.4.1, codegen 0.14.1

  models/
    bronze/
      bronze_gl_entries.sql        the model (a SELECT)
      _bronze__models.yml          its documentation and tests
    silver/
      silver_gl_entries.sql
      _silver__models.yml
    gold/
      gold_gl_monthly_summary.sql
      _gold__models.yml
    examples/                      starter smoke-test models

  seeds/
    gl_entries.csv                 synthetic GL data, safe to run anywhere
    _seeds__gl_entries.yml         its column types and tests

  macros/
    generate_schema_name.sql       target-aware dataset routing
    asg_helpers.sql                surrogate keys, audit columns, money casts

  dbt_ui/                          the UI (dbt never parses this folder)
  target/                          generated artifacts — do not edit
```

### The convention for each layer

Observed from the existing dbt Cloud output and preserved here:

| | Bronze | Silver | Gold |
| --- | --- | --- | --- |
| Materialization | table | view | table |
| Naming | `bronze_*` | `stg__*` / `silver_*` | `fact_*`, `kpi_*`, `dim_*` |
| Audit column | `_bronze_loaded_at` | `_silver_loaded_at` | `_gold_loaded_at` |
| Quality flags | none | `_is_*`, `_has_*` | rolled-up counters |

---

## 4. Getting started

### One-time setup

**1. Authenticate to BigQuery**

```powershell
gcloud auth application-default login
```

This expires periodically. When the connection pill in the UI turns red saying
*"Reauthentication is needed"*, run it again and **restart the server**.

**2. Install dbt packages**

```powershell
cd C:\Users\ryunu\Documents\work\dbt
dbt deps --profiles-dir .
```

**3. Optional — AI documentation**

```powershell
pip install google-genai
```

Then get a free key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
and paste it into the UI. A personal Google account is fine; see
[section 6](#6-the-two-documentation-engines).

### Starting the UI

```powershell
cd C:\Users\ryunu\Documents\work\dbt
python dbt_ui\serve.py
```

Opens `http://localhost:8777`. Or double-click `dbt_ui\start_dbt_ui.bat`.

**Important:** only run one server at a time. If the port is taken, it silently
moves to the next free one and your browser keeps talking to the old process, so
your changes appear not to work. Check with:

```powershell
Get-Process python          # should show one entry
taskkill /F /IM python.exe  # clears all of them
```

### Verifying without the UI

```powershell
python dbt_ui\serve.py --check    # environment report, then exits
dbt debug --profiles-dir .        # tests the BigQuery connection
dbt build --profiles-dir . --target dev   # full pipeline: seed, run, test
```

A healthy `dbt build` ends with `PASS=46 WARN=0 ERROR=0 SKIP=0`.

> The command may print a `quota project` warning from Google's auth library and
> report exit code 1 even on success. It is harmless. To silence it:
> `gcloud auth application-default set-quota-project data-analytics-asg`

---

## 5. The UI, page by page

Press `1`–`7` to switch pages. The header is always visible.

### The header

| Control | What it does |
| --- | --- |
| **Target** dropdown | Switches `dev` / `test` / `prod`. Drives everything: queries, profiling, dbt commands. Selecting `prod` asks for confirmation twice. |
| **Refresh manifest** | Runs `dbt parse`, rebuilding `target/manifest.json`. **Click this after editing any `.sql` or `.yml` file**, otherwise the UI shows stale information. |
| **Connection pill** | Green = BigQuery reachable. Red = credentials expired. Click to retest. |
| **Dataset scope** (sidebar) | The datasets this instance is allowed to read. See [section 8](#8-guardrails). |

---

### Page 1 — Overview

**Purpose:** project health in one screen.

**Shows:** model and test counts; documentation coverage; typed-column coverage;
the medallion layer breakdown; the result of the last dbt run with its slowest
nodes; a health panel listing models missing a description or tests.

**Use it when:**
- Starting the day: *did last night's build pass?*
- Before a pull request: *is coverage still 100%?*
- Onboarding someone: the whole project's shape on one screen.

---

### Page 2 — Pipeline

**Purpose:** the medallion architecture as a visual board.

Four columns — Seed, Bronze, Silver, Gold — with each model as a card showing its
materialization, column count, test count, and whether it is documented. Cards
outside the permitted dataset scope are dimmed and marked *out of scope*.

**Use it when:**
- Understanding flow: *what feeds gold?*
- Spotting gaps: *bronze exists but no silver for this entity.*
- Navigating: click any card to open the **model inspector**.

#### The model inspector (side panel)

Opens from any model card, anywhere in the app. Six tabs:

| Tab | Contents |
| --- | --- |
| **Overview** | Description, relation, partition/cluster config, upstream and downstream links, quick actions |
| **Columns** | Documented columns with name, `data_type`, description. Button to copy the whole contract |
| **SQL** | The raw Jinja you wrote, and the compiled GoogleSQL dbt generated |
| **Tests** | Every test attached, its type, column, and severity |
| **Data** | Live preview of the first 100 rows from BigQuery |
| **Physical** | Row count, size in bytes, partitioning, clustering, created and modified dates — read from the table definition. Warns if a large table has no partition column |

---

### Page 3 — Workbench

**Purpose:** query the warehouse **through dbt**, not through BigQuery directly.

This is the single most useful page for analysts. You write:

```sql
select
    company_code,
    period_month,
    sum(debit_amount)  as debit,
    sum(credit_amount) as credit,
    count(*)           as entries

from {{ ref('silver_gl_entries') }}

group by 1, 2
order by 1, 2
```

You never type a dataset name, and a typo in a model name is caught before a
single byte is scanned.

> **Switching environment requires reloading the project.** `ref()` is resolved
> from `manifest.json`, and dbt freezes the physical dataset into that file when
> it parses. Changing the dropdown alone would leave every reference pointing at
> the previous environment. The UI therefore re-parses automatically when you
> switch, and the Overview page shows a red banner if the two ever drift apart.

**Keyboard:**

| Shortcut | Action |
| --- | --- |
| `Ctrl+Enter` | Run — executes and returns rows (costs bytes) |
| `Ctrl+Shift+Enter` | **Validate — free.** BigQuery plans the query and returns the exact output columns and types without executing anything. Zero bytes billed. |
| `Ctrl+Space` | Column autocomplete. Reads the `ref()`s in your SQL and suggests their real column names with types |
| Type 2+ characters | Autocomplete for model and source names |
| `Tab` | Inserts two spaces |

**Result tabs:**

| Tab | Contents |
| --- | --- |
| **Results** | The grid, with type badges per column. Export as CSV or copy |
| **Columns & types** | The `name` + `data_type` contract for the result, ready to paste into a schema file |
| **Compiled SQL** | Exactly what was sent to BigQuery after `ref()` resolution |
| **Lineage** | Which relation each `ref()` resolved to |

**Validate is the feature to build a habit around.** It tells you the output
schema and the bytes the query *would* scan, for free, before you spend anything.

---

### Page 4 — Documentation

**Purpose:** generate the column contract and descriptions for any model or query.

On entry you choose an engine — **AI** or **Pattern**. Covered in detail in
[section 6](#6-the-two-documentation-engines).

**Two input modes:**

- **From a model** — reads the live table definition, so types are the real ones.
  The model must have been built at least once.
- **From a query** — dry-runs your SQL to read its output schema. Nothing
  executes, nothing is billed.

**Options:**

| Option | Effect |
| --- | --- |
| Profile the data | One aggregate pass measuring nulls, cardinality, ranges. Makes descriptions and test suggestions evidence-based. Costs a scan |
| Suggest tests | Only proposes a test the profile actually justifies |
| Include descriptions | Turn off for a bare type contract |
| Send sample values to Gemini | **AI only, off by default.** See [section 6](#privacy-what-leaves-your-machine) |

**Output tabs:** Contract (a table), `name + data_type` (the bare list), Full
schema YAML (ready to commit), Markdown (for a PR or Confluence).

You can write the YAML straight into the project. An existing file is backed up
to `.bak` first. **Then click Refresh manifest** so dbt picks it up.

---

### Page 5 — Silver Advisor

**Purpose:** turn a bronze table into concrete silver work, backed by measurement.

The bronze-to-silver step is where most judgment lives. This page replaces a blank
file with a reasoned starting point.

**How it works:**

1. Pick a bronze (or silver) model
2. It profiles every column in one pass: null rates, cardinality, ranges, blank
   strings, sign distribution, constant and unique detection
3. It infers the business key and **verifies it with a real `GROUP BY`** — it does
   not guess whether duplicates exist, it checks
4. It emits recommendations across nine categories, each carrying the measurement
   that triggered it and a confidence of high / medium / low

**The nine categories:**

| Category | Example recommendation |
| --- | --- |
| Deduplication | *"15 keys repeat, 15 surplus rows — deduplicate with `row_number()`"* |
| Null handling | *"`vendor_customer` is 33% null — flag it rather than coalescing to a fake default"* |
| Type cast | *"`amount_local` is FLOAT64 — cast to NUMERIC so totals do not drift"* |
| Standardisation | *"`currency` is compared and joined on — upper-case it once here"* |
| Categorisation | *"`document_type` has 3 values — map to labels with an explicit `else 'Unmapped'`"* |
| Aggregation | *"This is the gold grain this table supports; sum these measures by these dimensions"* |
| Quality flag | *"Stamp `_has_sign_conflict` rather than silently picking a winner"* |
| Pruning | *"`fiscal_year` is constant across all rows — omit it downstream"* |
| Testing | *"`gl_entry_key` is unique today — lock it in with a `unique` test"* |

**Then:**

- Uncheck anything you disagree with (or use *High confidence only*)
- Open the **Generated silver model** tab
- It produces runnable SQL with the evidence as comments
- Write it into the project, refresh the manifest, build it

**The generated model is a first draft for review, not a finished model.** It
calls project macros, so dbt must compile it — you cannot paste it into the
Workbench.

---

### Page 6 — Run Console

**Purpose:** run dbt with live streaming output. Replaces the terminal.

| Command | What it does |
| --- | --- |
| `build` | Seed, run, and test in dependency order. **The default for most work** |
| `run` | Models only |
| `test` | Tests only, against what is already built |
| `seed` | Load the CSVs in `seeds/` |
| `parse` | Rebuild the manifest. Same as Refresh manifest |
| `compile` | Render SQL without touching the warehouse |
| `debug` | Check profile, credentials, connection |
| `deps` | Install `packages.yml` |
| `docs` | Generate the browsable catalog and lineage site |
| `source freshness` | Check how stale sources are |

`--select` and `--exclude` accept the same syntax as the CLI:

| Selector | Meaning |
| --- | --- |
| `silver_gl_entries` | just that model |
| `silver_gl_entries+` | that model and everything downstream |
| `+silver_gl_entries` | that model and everything upstream |
| `tag:bronze` | every model tagged bronze |
| `path:models/silver` | everything in that folder |

Output streams line by line, colour-coded. One run at a time, because dbt writes
to a shared `target/` directory. Runs can be cancelled. Session history is kept,
and a run started on one page stays visible from every other page.

---

### Page 7 — Catalog

**Purpose:** find things, and see how they connect.

**Models tab** — searchable table of every model with layer, materialization,
column count, test count, doc status, and quick actions (query it, document it,
build it).

**Lineage tab** — a dependency graph laid out by medallion layer. Click a node to
inspect it; hover to highlight its edges.

**Sources tab** — declared sources and their freshness config.

**Warehouse tab** — read-only browser of the datasets you are permitted to see.
Useful for *"what exists in BigQuery that is not in dbt yet?"*

---

## 6. The two documentation engines

Both produce **the same artifact** — a dbt schema YAML block with name,
`data_type`, description, and justified tests. Only the prose differs, which is
what makes them comparable.

| | AI (Gemini) | Pattern (rules) |
| --- | --- | --- |
| Written by | A language model | Deterministic name-matching |
| Needs | A free API key | Nothing |
| Network | Yes | No |
| Reproducible | No — wording varies per run | Yes — identical every run |
| Infers business meaning | Yes | Only what is hardcoded |
| Recognises SAP field names | Yes, and reliably | Only the ones in its table |
| Coverage | Every column | Only what its rules match |
| Failure mode | **Confidently wrong** | Honest `TODO` |
| Flags uncertainty as | `Unclear: …` | `TODO …` |

### Real comparison

Both engines, same ad-hoc query, actual captured output:

```sql
select company_code,
       date_trunc(posting_date, month)      as period_month,
       sum(amount_local)                    as total_amount,
       count(distinct vendor_customer)      as counterparties,
       countif(debit_credit = 'C')          as credit_lines
from {{ ref('bronze_gl_entries') }}
group by 1, 2
```

**Pattern engine — 3 of 5 described, 2 honest TODOs:**

```yaml
- name: company_code
  data_type: int64
  description: Company code identifying the legal entity.
- name: period_month
  data_type: date
  description: First day of the reporting month.
- name: total_amount
  data_type: numeric
  description: Monetary amount.
- name: counterparties
  data_type: int64
  description: TODO describe counterparties. Numeric measure.
- name: credit_lines
  data_type: int64
  description: TODO describe credit lines. Numeric measure.
```

**AI engine — 5 of 5 described, much richer:**

> `company_code` — Identifies the specific operating subsidiary or legal entity
> within the group responsible for the aggregated monthly metrics (SAP BUKRS).
>
> `total_amount` — The total aggregated transaction value in Indonesian Rupiah
> (IDR) accumulated by the entity during the month.
>
> `credit_lines` — The total count of active credit facilities or loan
> arrangements utilized by the company during the given month.

**That last one is wrong.** `credit_lines` is `countif(debit_credit = 'C')` — the
number of accounting lines flagged as credit postings. It has nothing to do with
credit facilities or loans. The model invented a plausible business meaning and
stated it confidently, without an `Unclear:` prefix.

**This is the tradeoff.** The AI covers every column and writes better prose. It
is also occasionally, invisibly wrong. The pattern engine never lies to you, it
just leaves gaps.

**Practical advice:** use AI for the first pass, then read every line. It is much
faster to correct one wrong description than to write seventeen from scratch.

### Where the AI genuinely excels

On a well-named table it reads the profile and produces material a rules engine
never could. Actual output for `bronze_gl_entries`:

> **Table:** Raw landing zone containing unverified general ledger entries
> directly ingested from source accounting systems. Serves as an immutable audit
> layer preserving historical financial transactions prior to downstream cleaning
> and aggregation.
>
> `fiscal_year` — Financial year in which the transaction is officially posted
> (SAP GJAHR), **currently fixed to 2026.**
>
> `currency` — Three-character transaction currency code, **currently populated
> exclusively with IDR.**
>
> `vendor_customer` — Identifier for the vendor or customer subledger account,
> **populated for two-thirds of entries.**

The bold parts come from the profile, not the column name. It correctly
identified nine SAP field codes (BELNR, BUKRS, GJAHR, BUDAT, BLART, HKONT, KOSTL,
SHKZG, SGTXT, DMBTR) and wrote a table-level summary. Cost: **one request, 2,148
tokens in, 492 out.**

### Model options

All on the free tier. Because every column of a table goes in a **single batched
request**, one table costs one request — so even the smallest quota is generous.

| Model | Free quota | Notes |
| --- | --- | --- |
| **Gemini 3.6 Flash** *(default)* | 500/day, 15/min | Current generation. Available to all API keys |
| Gemini 2.5 Flash | 250/day, 10/min | Older. **Not available to newly created keys** |
| Gemini 2.5 Pro | 100/day, 5/min | Best at unfamiliar schemas |
| Gemini 2.5 Flash-Lite | 1,000/day, 15/min | Fastest, shortest output. Good quota fallback |

If a quota is hit, the UI says so and offers a one-click retry on Flash-Lite. The
Pattern engine keeps working regardless.

### Setup, and why it is free

Get a key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
**A personal Google account is fine.** No credit card, no GCP admin.

The Gemini key has nothing to do with your BigQuery project:

| | BigQuery | Gemini |
| --- | --- | --- |
| Purpose | Reads your warehouse | Writes descriptions |
| Auth | Your work `gcloud` login | A separate API key |
| Project | `data-analytics-asg` | Irrelevant |

Free tier means free. There is no billing attached, so nothing can be charged.
When a quota runs out the request simply fails.

> **Why not Vertex AI through our own GCP project?** It was tested and returns
> `PERMISSION_DENIED` for `aiplatform.endpoints.predict` on `data-analytics-asg`
> — it needs an admin grant. It also bills per token. The free key avoids both.

The key is stored in `dbt_ui/.runtime/ai.json`, which is gitignored, and is never
returned to the browser except as a masked prefix. `GEMINI_API_KEY` in the
environment overrides it.

### Privacy: what leaves your machine

This matters, so it is explicit.

**Always sent to Google (structure only):** column names, data types, medallion
layer, row count, null percentages, distinct counts, and the boolean flags
`is_unique` / `is_constant` / `all_null`.

**Only sent if you enable "Send sample values" (off by default):** observed
min/max per column, and the most frequent values.

That second group is **real data from your tables.** For `document_type` it is
harmless (`SA`, `KR`, `DR`). For `amount_local` it is actual figures from your
ledger. Leave it off unless you have decided that is acceptable for the table in
front of you.

---

## 7. What is inside the generated documentation

### The four output formats

**1. Bare contract** — the `name` + `data_type` list, nothing else. Hand this to
whoever is building the next layer:

```yaml
- name: gl_entry_key
  data_type: string
- name: document_number
  data_type: int64
- name: posting_date
  data_type: date
- name: amount_local
  data_type: numeric
- name: _bronze_loaded_at
  data_type: timestamp
```

**2. Full schema YAML** — commit-ready, with descriptions and tests:

```yaml
version: 2

models:
  - name: bronze_gl_entries
    description: >
      Raw landing zone for general ledger entries. One row in, one row out - no
      filtering, no deduplication, no business logic.
    config:
      materialized: table
    columns:
      - name: gl_entry_key
        data_type: string
        description: >
          Unique 32-character primary hash key generated to uniquely identify
          each general ledger line item.
        data_tests:
          - unique
          - not_null

      - name: document_type
        data_type: string
        description: >
          Two-character code classifying the type of accounting document
          (SAP BLART).
        data_tests:
          - not_null
          - accepted_values:
              arguments:
                values: [DR, KR, SA]
```

**3. Markdown table** — for a pull request or Confluence:

| Column | Type | Null % | Distinct | Description |
| --- | --- | --- | --- | --- |
| `gl_entry_key` | string | 0% | 15 | Deterministic surrogate key… |
| `amount_local` | numeric | 0% | 15 | Signed amount in local currency… |

**4. On-screen contract table** — sortable, with profile columns when profiling
is on.

### Field by field

| Field | Where it comes from | Notes |
| --- | --- | --- |
| `name` | The warehouse | Nested `STRUCT` fields appear as dotted paths, e.g. `address.city` |
| `data_type` | The warehouse | Lower-cased GoogleSQL spelling |
| `description` | AI, existing YAML, or pattern rules | Precedence below |
| `data_tests` | The profile | Only tests the data justifies |
| `config.materialized` | The manifest | `table`, `view`, `incremental` |

**Description precedence.** The UI labels which one won:

1. **AI** — if the AI engine ran
2. **existing** — a description already committed in the project YAML
3. **pattern** — matched a naming rule
4. **fallback** — nothing matched, emitted as `TODO describe …`

This is why running the Pattern engine on `bronze_gl_entries` showed all 17
columns as `existing`: the project already documents them, and committed prose is
never silently overwritten.

### Data types you will see

Read from BigQuery and normalised to GoogleSQL spelling. **BigQuery's REST API
still returns legacy names; the UI converts them**, so you get the spelling you
would actually write in DDL:

| Legacy (raw API) | What the UI emits |
| --- | --- |
| `INTEGER` | `int64` |
| `FLOAT` | `float64` |
| `BOOLEAN` | `bool` |
| `RECORD` | `struct` |
| `NUMERIC` | `numeric` |
| `DATE` / `TIMESTAMP` | `date` / `timestamp` |
| any `REPEATED` field | `array<type>` |

If you ever see `integer` in a YAML file, it was hand-written, not generated here.

### Which tests get suggested, and why

Never a blanket set — each is earned by a measurement:

| Test | Condition |
| --- | --- |
| `not_null` | The column had **zero** nulls in the profile |
| `unique` | Every value distinct **and** the name looks like a key (`*_key`, `*_id`, `*_number`) |
| `accepted_values` | Text column, 10 or fewer distinct values, under 50% of rows. Observed values are filled in for you |

> `accepted_values` on a numeric column needs `quote: false`, or BigQuery rejects
> it with *"No matching signature for operator IN"*. The generator adds it
> automatically. This was a real bug found by running the pipeline.

### Profile statistics

With *Profile the data* on, one aggregate pass produces per column:

| Statistic | Use |
| --- | --- |
| `null_count` / `null_pct` | Drives null-handling advice and `not_null` |
| `distinct_count` / `distinct_pct` | Identifies keys and code lists |
| `min` / `max` | Value range; character length for text |
| `blank_count` | Empty strings, which behave differently from NULL |
| `negative_count` | Mixed-sign measures needing a debit/credit split |
| `is_unique`, `is_constant`, `is_all_null` | Key detection and pruning |

Above 50,000 rows it samples and says so. Percentages are estimates at that
point, not a census.

---

## 8. Guardrails

Deliberate limits. Worth knowing before you hit one.

### Dataset scope

The UI may only read the bronze and silver layers — in **every** environment, not
just the selected one:

```
bronze_dbt        silver_dbt          (production)
dbt_dev_bronze    dbt_dev_silver      (dev sandbox)
dbt_ci_bronze     dbt_ci_silver       (CI sandbox)
```

The list covers all environments deliberately. The restriction is about
*layers*, not environments, and scoping it to the selected target caused a false
refusal: with the project loaded for dev and the dropdown on prod, `ref()`
pointed at `dbt_dev_silver` while the allowlist only held `silver_dbt`, so a
legitimate query was rejected. Widening it keeps the layer boundary exactly as
strict while removing a failure that had nothing to do with the boundary.

Everything else is refused — gold, seeds, and all 46 other datasets including
`GOLD`, `SILVER`, `ASG_DATALAKE`, and every `VS_*` set.

Enforced by **two independent checks**:

1. **Syntactic**, before any network call. Every dataset your SQL names must be
   allowed. Catches `select * from gold_dbt.x`. Costs nothing — the statement
   never leaves your machine.
2. **Semantic**, via a free dry run. Every *physical* table BigQuery would read
   must be allowed. Catches a permitted view that selects from a forbidden
   dataset — the text never names it, but the data would still reach your screen.

Out-of-scope models still appear in listings, dimmed and marked, because hiding
them would make the lineage look wrong. The warehouse browser lists only
permitted datasets, so it cannot be used to enumerate the rest of the project.

Change the boundary with `DBT_UI_ALLOWED_DATASETS=a,b,c`, then restart.

### Build scope — gold is never written from the UI

The dataset allowlist governs **reads**, and it cannot police dbt itself: `dbt
build` runs as a subprocess issuing its own SQL, which never passes through the
guard. So there is a second, independent control.

**Every dbt command the UI issues has `--exclude tag:gold` appended**,
unconditionally, merged with any exclusion you type. There is no code path in
the UI that builds gold. Verified: a full `dbt build` from the Run Console
reports `PASS=41` rather than `PASS=46`, the gold model and its four tests
simply absent, and the string `dbt_dev_gold` never appears in the log.

Selectors that explicitly target gold are refused with a 403 rather than
silently resolving to nothing:

| You ask for | Result |
| --- | --- |
| `--select tag:gold` | refused |
| `--select path:models/gold` | refused |
| `--select gold_gl_monthly_summary` | refused, by tag lookup in the manifest |
| `--select gold_gl_monthly_summary+` | refused |
| `--select tag:bronze` | allowed, with `--exclude tag:gold` still appended |

In the UI this shows up as a **read-only** badge on the Gold column of the
Pipeline board with its build button removed, no *Build this model* action in the
inspector for a gold model, and a notice in the Run Console.

`deps`, `debug` and `clean` are untouched, since they do not select nodes.

Change with `DBT_UI_BLOCKED_LAYERS=gold,something_else`, or set it empty to
disable the restriction.

> **The orchestrator is deliberately unaffected.** Production still needs gold
> built, and that happens from the command line, not from here.

### What these guardrails are and are not

> **They prevent accidents, not determined access.** Your gcloud credentials
> still hold whatever BigQuery grants your account has. Anyone with a terminal
> can run `bq query` or `dbt build --select tag:gold` directly and bypass all of
> this. For a boundary that cannot be bypassed you need IAM — ask your GCP admin
> to scope the account's dataset grants.
>
> **The seed remains buildable.** `gl_entries` lands in `dbt_dev_seeds`, which is
> outside the read scope, but bronze depends on it. Blocking it would break the
> pipeline, so it is built but not readable from the UI.

### Read-only workbench

`INSERT`, `UPDATE`, `DELETE`, `MERGE`, `CREATE`, `DROP`, `ALTER`, `TRUNCATE`,
`GRANT` and friends are refused. Changes to data or schema belong in a model or
seed so they go through review and land in the DAG. Comments and CTEs are
stripped before the check, so a legitimate query starting with a comment works.

### Spend cap

Every query runs with `maximum_bytes_billed` at 20 GiB. BigQuery **refuses the
job** rather than running it, so a careless `select *` on a huge table costs
nothing. Raise it with `DBT_UI_MAX_BYTES_BILLED`.

### Row cap

Previews are wrapped in a subquery with a `LIMIT`, default 200. Wrapping rather
than appending, so it cannot break a query that has its own `ORDER BY` or `LIMIT`.

### File writes

Confined to the project directory, restricted to `.sql .yml .yaml .md .csv`, and
blocked inside `target/`, `dbt_packages/`, `logs/` and `.git/`. Overwrites leave a
`.bak`.

### dbt commands

An allow-list — the browser sends a verb, never a command line. Selector strings
are validated before being passed as argv, so `a; drop table b` is refused.

### No authentication

The server binds to `127.0.0.1` and is meant for one person's machine. It can
query BigQuery with your credentials. **Do not bind it to `0.0.0.0`** without an
authenticating proxy in front.

---

## 9. Troubleshooting

**Changes to the code do not appear**

Almost always multiple servers running. The newest could not bind the port, moved
to another, and your browser is talking to the old one.

```powershell
Get-Process python          # more than one? that's the problem
taskkill /F /IM python.exe
python dbt_ui\serve.py
```

Then `Ctrl+F5`.

**"Reauthentication is needed"**

```powershell
gcloud auth application-default login
```

Then **restart the server** — it caches the BigQuery client at startup.

**"No target/manifest.json"**

Click **Refresh manifest**, or run `dbt parse --profiles-dir .`.

**Edited a file but the UI shows the old version**

Click **Refresh manifest**. The UI reads `target/manifest.json`, which only
updates when dbt parses.

**"was not found in location"**

A region mismatch. Every production dataset in `data-analytics-asg` is in
`asia-southeast2`, and BigQuery cannot join across regions. Check the target's
`location` in `profiles.yml`.

> The legacy `data_analytics_asg_test` profile is pinned to `US` and only works
> for self-contained seed runs. Prefer `--target test` on the main profile.

**"That model is not available to this key"**

Google retired an older Gemini model for new keys. Pick **Gemini 3.6 Flash**.

**"400 INVALID_ARGUMENT" from Gemini**

A parameter the chosen model does not support. Already handled for the shipped
models; if a new one misbehaves, switch models.

**"Free-tier quota reached"**

Wait for the daily reset, click the one-click retry on Flash-Lite, or use the
Pattern engine.

**"has no relation yet. Build it first"**

Types are read from the live table, so the model must exist. Build it once.

**A model shows "out of scope"**

It lives outside the permitted datasets. Expected for gold, seeds and the example
models. See [section 8](#dataset-scope).

---

## 10. Reference

### Commands

```powershell
# UI
python dbt_ui\serve.py                    # start
python dbt_ui\serve.py --check            # environment report, then exit
python dbt_ui\serve.py --port 9000        # different port
python dbt_ui\serve.py --no-browser       # do not open a browser

# dbt (all need --profiles-dir . from the project root)
dbt deps --profiles-dir .
dbt debug --profiles-dir .
dbt parse --profiles-dir .
dbt build --profiles-dir . --target dev
dbt build --profiles-dir . --target dev --select silver_gl_entries+
dbt test  --profiles-dir . --select tag:silver
dbt docs generate --profiles-dir . --static
```

### Environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `DBT_UI_HOST` | `127.0.0.1` | Interface to bind |
| `DBT_UI_PORT` | `8777` | Port; next free one used if taken |
| `DBT_UI_ALLOWED_DATASETS` | bronze + silver | Comma-separated dataset allowlist (reads) |
| `DBT_UI_BLOCKED_LAYERS` | `gold` | Layers the UI may never build. Empty disables it |
| `DBT_UI_MAX_BYTES_BILLED` | `21474836480` | Per-query spend cap in bytes |
| `DBT_UI_VERBOSE` | unset | Log every HTTP request |
| `GEMINI_API_KEY` | unset | Overrides the stored key |

### Backend modules

| File | Responsibility |
| --- | --- |
| `config.py` | Project discovery, targets, layers, dataset allowlist |
| `manifest.py` | Reader over `target/manifest.json` |
| `warehouse.py` | BigQuery via the dbt profile; dry-run type introspection; scope enforcement |
| `sql_scope.py` | Extracts table references from SQL for the syntactic guard |
| `jinja_sql.py` | `ref()` / `source()` resolution, read-only policy |
| `typing_map.py` | `INTEGER` → `int64` and friends |
| `profiling.py` | Single-pass column profiling |
| `recommend.py` | Silver Advisor rules |
| `codegen.py` | Schema YAML, pattern documentation, silver generation |
| `ai_docs.py` | Gemini documentation, key storage, quota mapping |
| `runner.py` | dbt subprocesses, job registry, log buffers |
| `api.py` | JSON routes |
| `server.py` | stdlib HTTP server |

### Dependencies

Nothing beyond what dbt already installs, except `google-genai` for AI
documentation. No Node, no build step, no bundler. Verified on dbt-core 1.12.3,
dbt-bigquery 1.12.0, Python 3.14.

### Glossary

| Term | Meaning |
| --- | --- |
| **model** | One `.sql` file containing a `SELECT`. Becomes one table or view |
| **seed** | A CSV in `seeds/`, version-controlled, loaded by `dbt seed` |
| **source** | A table dbt reads but does not create, declared in YAML |
| **materialization** | How a model is built: `table`, `view`, `incremental` |
| **target** | A named connection profile: `dev`, `test`, `prod` |
| **manifest** | `target/manifest.json` — dbt's compiled description of the project |
| **DAG** | The dependency graph dbt derives from your `ref()` calls |
| **relation** | A fully qualified `project.dataset.table` |
| **grain** | What one row of a table represents |
| **dry run** | BigQuery plans a query without executing it. Free, and returns the output schema |
