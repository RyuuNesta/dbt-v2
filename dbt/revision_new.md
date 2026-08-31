dbt UI Project — Feature Revision Request (v2)
You are helping me revise my custom dbt UI (a web-based interface for managing dbt Core projects on BigQuery). Below are 8 feature changes. Some are net-new, some are completions of partially built work. For each, provide implementation guidance scoped to the exact stack, constraints, and current state described below.

Context
Stack (zero external dependencies — this is non-negotiable)
Frontend Vanilla ES modules, no framework, no build step dbt_ui/frontend/{index.html, css/app.css, js/**}
Backend Python 3.14 stdlib http.server (ThreadingHTTPServer) dbt_ui/backend/{api,server,warehouse,manifest,runner,...}.py
Warehouse BigQuery via google-cloud-bigquery, credentials from profiles.yml
Deps Zero beyond what dbt-core installs, plus google-genai for AI docs
OS Windows (username: ryunu)

No React, no Flask, no Celery, no npm, no pip installs allowed. This runs on a locked-down corporate machine with no pip approval process.

No D3.js, no Mermaid.js, no React Flow. All frontend rendering is hand-rolled vanilla JS + SVG + CSS.

Libraries available: only what ships with Python 3.14 stdlib, dbt-core's transitive deps, and google-cloud-bigquery.

GCP Project & Warehouse
GCP Project: data-analytics-asg

BigQuery Location: asia-southeast2

Permitted datasets (the only datasets the UI may read/write):

bronze_dbt, silver_dbt (production bronze/silver layers)
dbt_dev_bronze, dbt_dev_silver (development)
dbt_ci_bronze, dbt_ci_silver (CI/testing)
Legacy dataset: dbt_testing — US-region only, used for self-contained seed runs, not part of the UI's active scope

Architectural Invariants
Workbench is read-only — blocks CREATE, DROP, INSERT, UPDATE, DELETE, MERGE. Only SELECT queries are permitted.

tag:gold is always excluded — every dbt run from this UI appends --exclude tag:gold

No new datasets may be created from the UI — ever

dbt Core only — no references to dbt Cloud APIs, IDE, or features

Feature Status Audit
#	Feature	Current State	What's Done	What's Missing
1	Inline editable docs	Not started	Guarded file writer exists in backend	Everything frontend
2	Autosave + download	~70% done	Download works (.yml, .md, JSON via API)	Autosave, save-state indicator, conflict detection
3	Ctrl+Space autocomplete	~60% done	Ctrl+Space works, pulls columns from refs, arrow-key nav	Fuzzy matching (currently substring), SQL keywords/functions, category grouping, auto-trigger after ., INFORMATION_SCHEMA fallback for non-dbt tables
4	Cleanup Advisor scoping	Partial	Currently single-model (not whole-dataset as originally assumed)	Multi-select table picker with row counts, dates
5	Silver model transparency + scheduling	Not started	—	Transparency preview + scheduling (resolved below)
6	Save View / env selector	Resolved	Env selector exists and works (re-parses on switch)	Replaced with "Save as dbt model" (resolved below)
7	Build & Test explanations	~25% done	Commands panel, selectors, tag:gold notice exist	All four enhancements (expandable detail, flow guide, cheat sheet, live highlighting)
8	ERD generator	Partial	Lineage graph exists in hand-rolled SVG	Full ERD with relationships, interactivity, exports
IMPORTANT: For partially completed features, do NOT rewrite existing working code. Extend it. Reference the existing file paths and patterns.

Feature 1: Inline Editable Documentation
Current state: Documentation is static/read-only in the UI. The backend already has a guarded file writer.

Desired state: Users can click on any model/column description field in the documentation view and edit it directly inline.

Requirements:

Edit triggers on click/double-click on description fields

Changes persist to the corresponding schema.yml file via the existing guarded file writer

Visual indicator showing "edited" vs "saved" state

Validation: prevent empty descriptions if field was previously populated

Implementation: vanilla JS contenteditable or dynamically swapped <input> elements — no framework components

Feature 2: Documentation Autosave + Download (Completion)
Current state: Download is ~70% done (.yml, .md, JSON export via API). No autosave exists.

What needs to be added:

Autosave every N seconds (configurable, default 30s) when edits are detected

Save state indicator in the UI (e.g., "Saved ✓" / "Saving..." / "Unsaved changes")

Conflict detection: if the underlying schema.yml file changed externally (e.g., git pull), warn before overwriting

Requirements:

Debounced autosave using vanilla JS setTimeout / clearTimeout — no lodash

Conflict detection via file mtime comparison (backend returns Last-Modified, frontend tracks it)

Extend the existing download API, don't replace it

Feature 3: Workbench SQL Autocomplete (Completion)
Current state: ~60% done. Ctrl+Space works, pulls column names from ref() usage in the current SQL, arrow-key navigation works. Uses substring matching.

What needs to be added:

Fuzzy matching — replace substring matching with a scoring algorithm (e.g., subsequence match with bonus for consecutive chars, prefix match)

SQL keywords & functions — static catalogue of BigQuery SQL keywords SELECT, FROM, WHERE, GROUP BY, etc.) and functions COALESCE, DATE_TRUNC, SAFE_CAST, etc.)

Category grouping — suggestions grouped by type: Tables | Columns | Keywords | Functions | dbt Macros, with visual section headers in the dropdown

Auto-trigger after . — typing dataset. or table. automatically opens the dropdown with relevant completions

INFORMATION_SCHEMA fallback — for tables not managed by dbt, query INFORMATION_SCHEMA.COLUMNS to get column suggestions

Requirements:

Extend the existing autocomplete JS module — don't rebuild from scratch

Fuzzy scoring must be fast enough for 500+ suggestions without lag

INFORMATION_SCHEMA queries should be cached (per session, per dataset) to avoid repeated BigQuery calls

Backend endpoint to proxy INFORMATION_SCHEMA queries if one doesn't already exist

Feature 4: Cleanup Advisor — Multi-Select Table Picker
Current state: Currently operates on a single model at a time (not the whole dataset as originally assumed).

Desired state: Users can select multiple tables to run cleanup analysis on simultaneously.

Requirements:

Searchable multi-select picker (checkboxes in a dropdown with a filter input)

"Select All" / "Deselect All" buttons

Each table row shows: table name, row count, last modified date

Analysis results scoped to selected tables only

Persist last selection in localStorage for convenience

All built with vanilla JS — no select2, no chosen.js

Feature 5: Silver Model Generation — Transparency + Scheduling
Current state: Not started.

Part A: Transparency Preview (build now)
Before generating a silver model, show a preview panel explaining:

Source table(s) being queried

Transformations applied (filtering, joins, column selection, type casting, deduplication, etc.)

Resulting schema (columns, types)

Estimated row count (if feasible via COUNT(*) dry-run)

Requirements:

Preview is read-only with an "Approve & Generate" button

User can modify the proposed logic before approving

Renders in a modal or slide-out panel

Part B: Scheduling (resolved — Windows Task Scheduler)
Instead of Celery or Cloud Scheduler, generate a Windows Task Scheduler entry:

UI presents a schedule form (daily, weekly, or custom cron expression)

Backend generates and executes a schtasks /create command targeting:
dbt build --select tag:silver --exclude tag:gold --project-dir <project_path> --profiles-dir <profiles_path>

Task runs at the OS level — independent of the UI server process

No email notifications — instead, write a JSON log file schedule_runs.json) that the UI reads and displays as an execution history panel

UI shows: scheduled tasks list, next run time, last run status, execution log

Requirements:

schtasks command generated via Python subprocess.run()

Log file written by wrapping the dbt command in a small Python script that captures stdout/stderr and appends to the JSON log

UI can list, enable/disable, and delete scheduled tasks via schtasks /query, /change, /delete

Feature 6: Replace Environment Selector with "Save as dbt Model" (Revised)
Current state: Environment selector exists and works (re-parses on switch — just fixed today).

IMPORTANT REVISION: The original request to "save views into existing datasets" has been rejected because it violates:

The read-only workbench invariant (blocks CREATE)

Production dataset protection (writes to bronze_dbtsilver_dbt bypass the DAG)

The principle that all warehouse objects should be tested and reviewable

Revised desired state: Replace the environment selector with a "Save as dbt Model" mechanism:

User writes a SELECT query in the workbench

Clicks "Save as Model" button

Modal: enter model name + select target folder models/bronze/, models/silver/, models/staging/)

Backend writes a .sql file into the selected folder with the query content

Optionally generates a stub entry in the corresponding schema.yml

User then builds the model via the Commands panel dbt build --select <model_name>)

Requirements:

File is written via the existing guarded file writer

Model name validation: lowercase, underscores only, no duplicates

Target folder dropdown populated by scanning the dbt project's models/ directory

Generated .sql file includes a header comment with author, timestamp, source query

The environment selector is removed from the UI entirely

NEVER executes CREATE VIEW/TABLE directly — only writes a file

Feature 7: Build & Test — Explanations + dbt Flow Guide (Completion)
Current state (~25% done):
The Commands panel already exists with 3 sections:

BUILD section (existing):
Command	Badge	Current Description
dbt build	⚡ writes	"Seed, run and test in dependency order. The default for most work."
dbt run	▶ writes	"Models only, no seeds and no tests."
dbt test	✓ (no writes)	"Tests only, against whatever is already built."
dbt seed	↓ writes	"Load the CSV files in seeds/."
INSPECT section (existing):
Command	Current Description
dbt parse	"Rebuild target/manifest.json. Every screen here reads from it."
dbt compile	"Render SQL without touching the warehouse."
dbt debug	"Check the profile, credentials and connection."
dbt deps	"Install the packages in packages.yml."
dbt docs	"Generate the browsable catalog and lineage site."
dbt source	"Check source freshness."
SELECTION section (existing):
--SELECT field with placeholder: "e.g. silver_gl_entries+ or tag:bronze"

--EXCLUDE field (optional)

--full-refresh checkbox

Helper text: "Same syntax as the CLI: model name, tag:bronze, +downstream, path:models/silver"

Info notice: "The gold layer is never built from here — dbt runs from this UI always exclude: tag:gold"

What needs to be added (4 enhancements):
Enhancement 1: Expandable "Learn More" per command
Each command box gets a clickable expand icon (▼ or ℹ️) that reveals:

What it does in more detail (beyond the existing 1-line summary)

When to use it (use-case guidance, e.g., "Use dbt run when you've already seeded and just want to rebuild models")

What it affects (which tables/models get touched, what gets written to the warehouse)

Common pitfalls (e.g., dbt run alone won't catch test failures — use dbt build instead")

Example usage with --select flags (e.g., dbt build --select tag:bronze --exclude tag:gold)

Enhancement 2: "How dbt Works" flow guide
A dedicated modal or section accessible via a "How does this work?" button at the top of the Commands panel:

Visual flow diagram (hand-rolled SVG or styled HTML) showing the execution order:
dbt deps → dbt seed → dbt run → dbt test → dbt docs generate

Stage-by-stage explanation:

deps: Pulls external packages (e.g., dbt_utils) from packages.yml

seed: Loads static CSV reference data into the warehouse

run: Executes SQL models (CREATE TABLE / VIEW) in dependency order

test: Validates data quality assertions (unique, not_null, relationships, custom)

docs: Generates metadata catalog from manifest.json

Dependency order: Why dbt builds bronze → silver → gold in sequence

Incremental vs Full Refresh: When each is triggered and what --full-refresh does

What happens on failure: Partial runs, which models get skipped, how to resume

The "writes" badge explained: Which commands mutate the warehouse vs. read-only inspect commands

Enhancement 3: Selection syntax cheat sheet
Expand the existing helper text below the --SELECT field into a toggleable quick-reference table:

Syntax	Meaning	Example
model_name	Run a single model	silver_gl_entries
model_name+	Model + all downstream dependents	silver_gl_entries+
+model_name	All upstream dependencies + model	+gold_profit_loss
tag:tagname	All models with a specific tag	tag:bronze
path:folder	All models in a directory	path:models/silver
model1 model2	Multiple models (space-separated)	silver_gl silver_ap
+model_name+	Full upstream + model + full downstream	+silver_gl_entries+
Enhancement 4: Context-aware execution highlighting
When a command is running or has completed:

Highlight which step in the flow the user is currently on

Color-code command boxes: green (success), red (failed), grey (skipped), blue/pulse (running)

Show execution time per step

Show model count (e.g., "12/15 models passed")

Requirements:

Expandable sections collapsed by default (preserve the current clean layout)

Flow guide opens as a modal via a "How does this work?" link/button

All explanations accurate to dbt Core behavior (not dbt Cloud)

Cheat sheet is toggleable (hidden by default, shown on click)

The existing tag:gold exclusion notice must remain prominent

All rendering in vanilla JS + CSS — no external libraries

Feature 8: ERD (Entity Relationship Diagram) Generator
Current state: A lineage graph exists using hand-rolled SVG. ERD is a new addition.

Desired state: Users can generate an interactive ERD showing table relationships across their dbt project and BigQuery datasets.

Relationship Detection
Parse dbt schema.yml and ref() / source() usage in model SQL files

Detect foreign key patterns from column naming conventions (e.g., _id suffixes, matching column names across tables)

Use BigQuery INFORMATION_SCHEMA.KEY_COLUMN_USAGE / TABLE_CONSTRAINTS if available

Visual ERD Rendering (hand-rolled, zero deps)
Canvas: Hand-rolled SVG with vanilla JS pan (mousedown+mousemove) and zoom (wheel event → transform scale)

Table nodes: SVG <rect> + <text> showing table name, column names, data types

Relationship lines: SVG <path> or <line> with cardinality labels (1:1, 1:N, N:N)

Color-coding: By layer — bronze (copper/orange), silver (grey), gold (yellow), staging (muted)

Reuse patterns from the existing lineage graph SVG code where possible

User Interactions
Click a table node → side panel with full column details, row count, description

Drag nodes to rearrange layout (mousedown on node → mousemove → update position)

Search/filter bar to focus on specific tables

Toggle: show all columns vs. keys only

Highlight upstream/downstream lineage for a selected model (reuse existing lineage logic)

Export Options (all zero-dependency)
SVG — serialize the SVG DOM element → download as .svg

PNG — SVG → <canvas> drawImage → canvas.toDataURL('image/png') → download

PDF — window.print() scoped to ERD container via print-specific CSS @media print)

Mermaid.js markdown — generate text syntax from the relationship data (for embedding in docs)

DBML — generate Database Markup Language text (for dbdiagram.io import)

Scope Selection
Generate ERD for: entire project / specific dataset / custom table selection

Option to include/exclude staging models

Reuse the multi-select picker from Feature 4 if built first

Auto-refresh
ERD regenerates when manifest.json changes (watch via polling or after any dbt command completes)
Output Format
For each feature, respond with:
Feature N: [Title]
Architecture:
How frontend and backend pieces connect
Which EXISTING files are modified vs. new files created
Note: no external libraries — vanilla JS + Python stdlib only
Implementation Steps:
[Step with specific file paths in dbt_ui/frontend/ or dbt_ui/backend/ and key code]
[Step with specific file paths and key code]
... (Ordered: backend changes first, then frontend)
Key Code (most critical new or modified file): [Full code block — not pseudocode — using vanilla JS or Python stdlib only]
Supporting Code (secondary files if needed): [Additional code blocks for API endpoints, utility functions, CSS, etc.]
UX Flow: User journey as a numbered sequence:
User does [action] →
UI shows [response] →
User confirms/selects [option] →
System executes [process] →
UI displays [result/feedback]
Edge Cases:
Table
Scenario
How to Handle
[Edge case 1] [Handling strategy]
[Edge case 2] [Handling strategy]
[Edge case 3] [Handling strategy]
View more
Testing Suggestions:
[Test case 1: what to verify and expected outcome]
[Test case 2: adversarial or boundary input]
[Test case 3: failure/recovery scenario]

Global Technical Constraints
Apply these to EVERY feature:

Constraint	Detail
Zero dependencies	No npm, no pip installs, no external JS/CSS libraries. Vanilla JS + Python 3.14 stdlib + google-cloud-bigquery only
No new datasets	The UI may never create a BigQuery dataset
tag:gold excluded	Every dbt run appends --exclude tag:gold
Workbench is read-only	Only SELECT queries — CREATE/DROP/INSERT/UPDATE/DELETE/MERGE are blocked
BigQuery location	asia-southeast2 for all API calls
GCP Project	data-analytics-asg
Permitted datasets	bronze_dbt, silver_dbt, dbt_dev_bronze, dbt_dev_silver, dbt_ci_bronze, dbt_ci_silver
OS	Windows (username: ryunu)
dbt Core only	No references to dbt Cloud APIs or features
File structure	Frontend: dbt_ui/frontend/{index.html, css/app.css, js/**} · Backend: dbt_ui/backend/{api,server,warehouse,manifest,runner,...}.py
Priority Order
Implement in this order (accounting for dependencies and current completion state):

Priority	Feature	Rationale
1	Feature 3 — Autocomplete completion	~60% done, highest daily-use impact, finish first
2	Feature 7 — Build & Test explanations	~25% done, low effort to complete, high clarity gain
3	Features 1 + 2 — Docs editing + autosave	Paired features, Feature 2 download already 70% done
4	Feature 4 — Cleanup Advisor multi-select	Self-contained, picker pattern reused by Feature 8
5	Feature 6 — "Save as dbt Model"	Replaces env selector, moderate scope
6	Feature 5 — Silver transparency + Task Scheduler	Two-part: transparency is straightforward, scheduling needs OS integration
7	Feature 8 — ERD Generator	Most complex, benefits from Feature 4's picker and existing lineage SVG
