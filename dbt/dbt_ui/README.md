# dbt Studio

A local web UI for this dbt Core project on BigQuery. Built for the data team to
do day-to-day dbt work without a terminal, and to make the medallion layers the
organising idea rather than a naming convention.

## Start it

```powershell
# from the project root (the folder with dbt_project.yml)
python dbt_ui/serve.py
```

Or double-click `dbt_ui/start_dbt_ui.bat`.

It opens `http://localhost:8777`. Bound to loopback only.

```powershell
python dbt_ui/serve.py --check        # verify the environment, then exit
python dbt_ui/serve.py --port 9000    # different port
python dbt_ui/serve.py --no-browser   # do not open a browser
```

## Requirements

Nothing beyond what dbt already needs. No `pip install`, no Node, no build step.
The server uses Python's standard library and the frontend is plain ES modules,
so it runs on a locked-down corporate machine.

Verified against dbt-core 1.12.3, dbt-bigquery 1.12.0, Python 3.14.

Authentication is whatever `profiles.yml` says. For `method: oauth` that is your
gcloud login:

```powershell
gcloud auth application-default login
```

## What each screen does

**Overview** — model and test counts, documentation coverage, the outcome of the
last run, and a list of models missing a description or tests.

**Pipeline** — Seed → Bronze → Silver → Gold as columns. Click any model to open
the inspector: columns, SQL, tests, a data preview, and the physical table
(partitioning, clustering, row count, size).

**Workbench** — write SQL against dbt, not against BigQuery:

```sql
select company_code, sum(debit_amount) as debit
from {{ ref('silver_gl_entries') }}
group by 1
```

`ref()` and `source()` resolve from the manifest, so the same statement works on
dev and prod and a typo in a model name is caught before anything is scanned.
`Ctrl+Enter` runs. `Ctrl+Shift+Enter` validates: BigQuery plans the query and
returns the output columns and types without executing it, which costs nothing.

Every result carries a **Columns & types** tab giving the contract in the shape a
dbt schema file wants:

```yaml
- name: company_code
  data_type: int64
- name: debit
  data_type: numeric
```

**Documentation** — point at a model or paste a query and get the full schema
YAML: name, `data_type`, a description, and only the generic tests the data
justifies. Types are read from the warehouse, so they are the real ones.
Optionally profile the data first, which makes the descriptions and test
suggestions evidence-based. You can write the result straight into
`models/<layer>/_<layer>__models.yml`; an existing file is backed up to `.bak`
first.

On entry you pick which engine writes the prose. Both produce the same shape of
file, so they are directly comparable:

| | AI | Pattern |
| --- | --- | --- |
| Written by | Gemini | Deterministic name-matching rules |
| Needs | a free API key | nothing |
| Network | yes | no |
| Same output every run | no | yes |
| Understands SAP field names, infers business meaning | yes | only what is hardcoded |
| Flags uncertainty as | `Unclear: …` | `TODO …` |

### AI documentation

Set-up is self-service and free: get a key at
[aistudio.google.com/apikey](https://aistudio.google.com/apikey), paste it into
the settings panel on the page. No credit card, and no GCP admin involvement.

The key is stored in `dbt_ui/.runtime/ai.json`, which is gitignored, and is
never sent back to the browser except as a masked prefix. Setting
`GEMINI_API_KEY` or `GOOGLE_API_KEY` in the environment overrides the stored
file.

Three models are offered. All are on the free tier, and because every column of
a table goes in a **single batched request**, even the smallest daily quota is
plenty:

| Model | Free quota | Notes |
| --- | --- | --- |
| Gemini 2.5 Flash *(default)* | 250 req/day, 10/min | Best balance. Thinking is switched off, so nothing is wasted on a structured-extraction task. |
| Gemini 2.5 Pro | 100 req/day, 5/min | Best at inferring meaning from an unfamiliar schema. |
| Gemini 2.5 Flash-Lite | 1,000 req/day, 15/min | Fastest, shortest descriptions. Good fallback when a quota is hit. |

The model is given measured facts, not just names: null rate, distinct count,
observed range, frequent values, and whether a column is constant or unique.
That is what lets it write something specific rather than paraphrasing the
column name back at you. It is also told to prefix anything it cannot infer with
`Unclear:` rather than invent a purpose.

**Vertex AI is not used.** Going through your gcloud login was tested and
returns `PERMISSION_DENIED` for `aiplatform.endpoints.predict` on
`data-analytics-asg`, and it bills per token. The free API key path avoids both
problems.

A language model is confidently wrong sometimes. Read the descriptions before
committing them.

**Silver Advisor** — the bronze-to-silver step. Profiles a bronze relation in one
pass (nulls, cardinality, ranges, blank strings, sign distribution), verifies the
candidate key with a real group-by, then recommends the silver work with the
measurement that triggered each item attached:

- deduplication, when a key actually repeats
- null handling and blank-string normalisation
- type casts, e.g. money in `FLOAT64` that should be `NUMERIC`
- standardisation (trim, case folding on codes)
- categorisation, including splitting a mixed-sign amount into debit and credit
- period columns derived from a date, so gold does not re-compute them
- the gold grain and measures to aggregate
- quality flags instead of silently coercing
- pruning columns that are constant or entirely null
- generic tests

Uncheck what you disagree with, then generate a runnable silver model. The SQL
carries the evidence as comments. It is a first draft for review, not a
finished model.

**Run Console** — `dbt build / run / test / seed / parse / compile / debug /
deps / docs / source freshness` with output streaming line by line. `--select`
and `--exclude` take the same strings as the CLI. One run at a time, because dbt
writes to a shared `target/` directory. Runs can be cancelled.

**Catalog** — searchable model list, a lineage graph laid out by medallion layer,
declared sources, and a read-only browser of the datasets your credentials can
see.

## Targets

The target selector in the header drives everything: queries, profiling, and dbt
commands. `macros/generate_schema_name.sql` routes writes by target:

| Target | Writes to | Use |
| --- | --- | --- |
| `dev` | `dbt_dev_bronze`, `dbt_dev_silver`, `dbt_dev_gold` | daily work |
| `test` | `dbt_ci_*` | CI and shared smoke tests |
| `prod` | `bronze_dbt`, `silver_dbt`, `gold_dbt` | orchestrator only |

A dev run cannot overwrite a production table. Selecting `prod`, or running a
writing command against it, asks for confirmation twice.

## Guardrails

These are deliberate, and worth knowing about before you hit one.

- **Read-only workbench.** `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `CREATE`,
  `DROP`, `ALTER`, `TRUNCATE`, `GRANT` and friends are refused. Changes to data
  or schema belong in a model or seed so they go through review and land in the
  DAG.
- **Spend cap.** Every query runs with `maximum_bytes_billed` set to 20 GiB.
  BigQuery refuses the job rather than running it, so an accidental `select *` on
  a huge table costs nothing. Raise it with `DBT_UI_MAX_BYTES_BILLED`.
- **Row cap.** Previews are wrapped in a subquery with a `LIMIT`, default 200.
- **File writes** are confined to the project directory, restricted to
  `.sql .yml .yaml .md .csv`, and blocked inside `target/`, `dbt_packages/`,
  `logs/` and `.git/`. Overwrites leave a `.bak`.
- **dbt commands** come from an allow-list; the browser sends a verb, never a
  command line. Selector strings are validated before being passed as argv.
- **No authentication.** The server binds to `127.0.0.1` and is meant for one
  person's machine. It can query and write to BigQuery with your credentials, so
  do not bind it to `0.0.0.0` without putting an authenticating proxy in front.

## Known rough edges

- **Project macros do not expand in the workbench.** `{{ asg_audit_columns() }}`
  and anything else from `macros/` needs dbt's own Jinja context. The workbench
  reports this clearly and points at `dbt compile`. Generated silver models
  contain macros for this reason, so build them with dbt rather than pasting them
  into the workbench.
- **Log streaming is polling**, not a socket, because the stdlib server has no
  websocket support. Cursor-based, so several viewers can follow one run and a
  refresh resumes cleanly.
- **BigQuery only.** The query, profiling and type layers are written against
  the BigQuery adapter.
- **Profiling large tables samples.** Above `profile_sample_rows` (50,000) the
  profile is taken from a sample and the UI says so. Percentages are estimates
  at that point.

## Environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `DBT_UI_HOST` | `127.0.0.1` | interface to bind |
| `DBT_UI_PORT` | `8777` | port; the next free one is used if taken |
| `DBT_UI_MAX_BYTES_BILLED` | `21474836480` | per-query spend cap in bytes |
| `DBT_UI_VERBOSE` | unset | log every HTTP request to stderr |
| `GEMINI_API_KEY` | unset | Gemini key for AI documentation; overrides the stored file |
| `GOOGLE_API_KEY` | unset | same, checked second |

## Layout

```
dbt_ui/
  serve.py              launcher
  start_dbt_ui.bat      double-click launcher
  backend/
    config.py           project discovery, targets, layer definitions
    manifest.py         reader over target/manifest.json
    warehouse.py        BigQuery via the dbt profile; dry-run type introspection
    jinja_sql.py        ref/source resolution, read-only policy
    typing_map.py       INTEGER -> INT64 and friends
    profiling.py        single-pass column profiling
    recommend.py        the Silver Advisor rules
    codegen.py          schema YAML, pattern documentation, silver generation
    ai_docs.py          Gemini-backed documentation, key storage, quota mapping
    runner.py           dbt subprocesses, job registry, log buffers
    api.py              JSON routes
    server.py           stdlib HTTP server
  frontend/
    index.html
    css/app.css
    js/                 core, components, jobs, app, views/
```

## Troubleshooting

**"No target/manifest.json"** — click *Refresh manifest* in the header. That runs
`dbt parse`.

**"was not found in location"** — a region mismatch. Every production dataset in
`data-analytics-asg` is in `asia-southeast2`; BigQuery cannot join across
regions. Check the target's `location` in `profiles.yml`. Note the legacy
`data_analytics_asg_test` profile is pinned to `US` and only works for
self-contained seed runs.

**Types read `integer` instead of `int64`** — they should not; the UI normalises
BigQuery's legacy REST type names. If you see a legacy name it came from a
hand-written YAML file, not from here.

**Quota project warning in gcloud output** — harmless, and filtered out of the
run log. To silence it at source:

```powershell
gcloud auth application-default set-quota-project data-analytics-asg
```

**A model has no types to read** — the relation has to exist before the
warehouse can report its schema. Build the model once, then generate.
