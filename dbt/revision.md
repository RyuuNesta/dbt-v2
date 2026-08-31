# dbt UI Project — Feature Revision Request

You are helping me revise my custom dbt UI (a web-based interface for managing dbt Core projects on BigQuery). Below are 8 feature changes I need implemented. For each, provide:

1. Implementation approach (architecture, components, libraries)

2. Key code changes (files to modify/create, pseudocode or actual code)

3. UX flow description

4. Edge cases to handle

---

## Context

- **Stack**: [INSERT YOUR STACK — e.g., React/Next.js frontend, Python/Flask backend, BigQuery as warehouse]

- **Current dbt version**: dbt Core (open-source)

- **GCP Project**: data-analytics-asg

- **BigQuery Location**: asia-southeast2

- **Datasets**: dbt_dev (development), dbt_testing (testing)

---

## Feature 1: Inline Editable Documentation

**Current state**: Documentation is static/read-only in the UI.

**Desired state**: Users can click on any model/column description field in the documentation view and edit it directly inline (contenteditable or input field).

**Requirements**:

- Edit triggers on click/double-click on description fields

- Changes persist to the corresponding `schema.yml` file in the dbt project

- Visual indicator showing "edited" vs "saved" state

- Validation: prevent empty descriptions if field was previously populated

---

## Feature 2: Documentation Autosave + Download

**Current state**: No autosave; documentation edits may be lost.

**Desired state**:

- Autosave every time a document is generated

- Save state indicator (e.g., "Saved ✓" / "Saving..." / "Unsaved changes")

- "Download Documentation" button that exports as:

  - `.yml` (native dbt schema format)

  - `.json` (structured export)

  - `.md` (human-readable markdown)

**Requirements**:

- Debounced autosave (don't fire on every keystroke)

- Conflict detection if underlying file changed externally

- Download preserves the full schema.yml structure (not just edited fields)

---

## Feature 3: Workbench SQL Editor — CTRL+SPACE Autocomplete

**Current state**: SQL editor in the workbench has no intelligent autocomplete.

**Desired state**: Pressing `CTRL+SPACE` triggers an autocomplete dropdown (similar to BigQuery's console) showing:

- Table names (from connected datasets)

- Column names (context-aware, based on referenced tables)

- SQL keywords and functions

- dbt-specific syntax (ref(), source(), macros)

**Requirements**:

- Use schema metadata from BigQuery `INFORMATION\_SCHEMA` to populate table/column suggestions

- Fuzzy matching on typed characters

- Categorized suggestions (tables, columns, functions, macros)

- Keyboard navigation (arrow keys + Enter to select)

- Trigger: `CTRL+SPACE` for manual invoke, optional auto-trigger after `.` or typing 3+ characters

---

## Feature 4: Cleanup Advisor — Table-Level Selection

**Current state**: Cleanup Advisor analyzes the entire dataset at once.

**Desired state**: Users can select individual tables (not the whole dataset) to run cleanup analysis on.

**Requirements**:

- Multi-select table picker (checkboxes or searchable dropdown)

- "Select All" / "Deselect All" options

- Show table row counts and last modified date in the selector for context

- Analysis results scoped only to selected tables

- Persist last selection for convenience

---

## Feature 5: Silver Model Generation — Transparency + Scheduling

**Current state**: Silver model generation happens without explaining the transformation logic.

**Desired state**:

1. **Before generation**: Show a preview/explanation of HOW the silver model will be created:

   - Source table(s) being queried

   - Transformations applied (filtering, joins, column selection, type casting, deduplication, etc.)

   - Resulting schema (columns, types)

   - Estimated row count (if feasible)

2. **Scheduling**: Allow users to schedule automated silver model regeneration:

   - Cron-based or interval-based (daily, weekly, custom)

   - Trigger conditions (e.g., "when source data changes")

   - Execution log/history

**Requirements**:

- Preview is read-only but with an "Approve & Generate" button

- User can modify the proposed logic before approving

- Scheduling uses a job queue (e.g., Celery, Cloud Scheduler, or cron)

- Email/notification on scheduled run completion or failure

---

## Feature 6: Replace Environment Selector with "Save View" (BigQuery-style)

**Current state**: There is an environment selector (dev/staging/prod).

**Desired state**: Remove the environment selector entirely. Replace with a "Save View" mechanism:

- User can save a query result as a **view** within an existing dataset

- A dropdown lists all existing datasets in the project — user selects where to save

- **NEVER** create a new dataset — only save views into existing datasets

- Saved views are accessible from the dropdown for future reference

**Requirements**:

- "Save View" button in the workbench after running a query

- Modal/dialog: "Save as view" → Name field + Dataset dropdown (existing only)

- Dataset dropdown populated from BigQuery API `datasets.list`)

- Prevent duplicate view names within same dataset (show warning)

- Option to overwrite existing view with confirmation

- NO "Create new dataset" option anywhere in this flow

---

## Feature 7: Build & Test — Explanations + dbt Flow Guide

**Current state (what already exists in the UI)**:

The Commands panel is divided into 3 sections:

### BUILD section (existing):

| Command | Badge | Current Description |

|---------|-------|-------------------|

| `dbt build` | ⚡ writes | "Seed, run and test in dependency order. The default for most work." |

| `dbt run` | ▶ writes | "Models only, no seeds and no tests." |

| `dbt test` | ✓ (no writes) | "Tests only, against whatever is already built." |

| `dbt seed` | ↓ writes | "Load the CSV files in seeds/." |

### INSPECT section (existing):

| Command | Current Description |

|---------|-------------------|

| `dbt parse` | "Rebuild target/manifest.json. Every screen here reads from it." |

| `dbt compile` | "Render SQL without touching the warehouse." |

| `dbt debug` | "Check the profile, credentials and connection." |

| `dbt deps` | "Install the packages in packages.yml." |

| `dbt docs` | "Generate the browsable catalog and lineage site." |

| `dbt source` | "Check source freshness." |

### SELECTION section (existing):

- `--SELECT` field with placeholder: "e.g. silver_gl_entries+ or tag:bronze"

- `--EXCLUDE` field (optional)

- `--full-refresh` checkbox

- Helper text: "Same syntax as the CLI: model name, tag:bronze, +downstream, path:models/silver"

- Info notice: "The gold layer is never built from here — dbt runs from this UI always exclude: tag:gold"

---

**What's missing / Desired enhancements**:

1. **Expandable "Learn More" per command** — Each command box should have a clickable expand icon (▼ or ℹ️) that reveals:

   - **What it does** in more detail (not just 1-line summary)

   - **When to use it** (use-case guidance)

   - **What it affects** (which tables/models get touched, what gets written to the warehouse)

   - **Common pitfalls** (e.g., "running `dbt run` alone won't catch test failures")

   - **Example usage** with `--select` flags

2. **"How dbt Works" flow guide** — A dedicated section or modal accessible from the Commands panel that explains the **end-to-end dbt execution flow**:

   - **Visual flow diagram** showing the order of operations:

     ```

     dbt deps → dbt seed → dbt run → dbt test → dbt docs generate

     ```

   - Explanation of **what happens at each stage**:

     - `deps`: Pulls external packages (e.g., dbt_utils) defined in packages.yml

     - `seed`: Loads static CSV reference data into the warehouse

     - `run`: Executes SQL models (CREATE TABLE / CREATE VIEW) in dependency order

     - `test`: Validates data quality assertions (unique, not_null, relationships, custom)

     - `docs`: Generates metadata catalog from manifest

   - **Dependency order explained**: Why dbt builds bronze → silver → gold in sequence

   - **Incremental vs Full Refresh**: When each is triggered and what `--full-refresh` does

   - **What happens on failure**: Partial runs, which models get skipped, how to resume

   - **The "writes" badge explained**: Which commands mutate the warehouse vs. read-only

3. **Selection syntax cheat sheet** — Expand the existing helper text into a quick-reference:

   | Syntax | Meaning | Example |

   |--------|---------|---------|

   | `model\_name` | Run a single model | `silver\_gl\_entries` |

   | `model\_name+` | Model + all downstream | `silver\_gl\_entries+` |

   | `+model\_name` | All upstream + model | `+gold\_profit\_loss` |

   | `tag:tagname` | All models with tag | `tag:bronze` |

   | `path:folder` | All models in path | `path:models/silver` |

   | `model1 model2` | Multiple models (space-separated) | `silver\_gl silver\_ap` |

4. **Context-aware highlighting** — When a command is running or has completed:

   - Show which step in the flow the user is currently on

   - Color-code: green (success), red (failed), grey (skipped), blue (running)

   - Show execution time per step

**Requirements**:

- Expandable sections should be collapsed by default (keep the clean current layout)

- Flow guide accessible via a "How does this work?" button at the top of the Commands panel

- All explanations must be accurate to dbt Core behavior (not dbt Cloud)

- Mobile-responsive (expandable sections work on smaller screens)

- The existing `tag:gold` exclusion notice must remain prominent

---

## Feature 8: ERD (Entity Relationship Diagram) Generator

**Current state**: No visual representation of table relationships exists in the UI.

**Desired state**: Users can generate an interactive ERD that visually maps out relationships between tables/models in their dbt project and BigQuery datasets.

**Requirements**:

- **Auto-detection of relationships**:

  - Parse dbt `schema.yml` and `ref()` / `source()` usage in model SQL files to infer relationships

  - Detect foreign key patterns from column naming conventions (e.g., `\_id` suffixes, matching column names across tables)

  - Use BigQuery `INFORMATION\_SCHEMA` to pull any defined constraints

- **Visual ERD rendering**:

  - Interactive, zoomable, pannable canvas (e.g., using D3.js, Mermaid.js, or React Flow)

  - Tables shown as nodes with column names and data types

  - Relationships shown as connecting lines with cardinality labels (1:1, 1:N, N:N)

  - Color-coding by layer (bronze/staging, silver, gold) or by dataset

- **User interactions**:

  - Click on a table node to see full column details, row count, and description

  - Drag nodes to rearrange layout

  - Filter/search to focus on specific tables or relationships

  - Toggle column visibility (show all columns vs. keys only)

  - Highlight upstream/downstream lineage for a selected model

- **Export options**:

  - Export as PNG / SVG image

  - Export as PDF

  - Export as Mermaid.js markdown (for embedding in documentation)

  - Export as DBML (database markup language) for dbdiagram.io compatibility

- **Scope selection**:

  - Generate ERD for entire project, a specific dataset, or a custom selection of tables

  - Option to include/exclude staging models

- **Auto-refresh**: ERD updates automatically when models or schema definitions change

---

## Output Format

For each feature, respond with:

 

 

Feature N: [Title]
Architecture:

Component diagram or description of how frontend/backend pieces connect
Which existing files are modified vs. new files created
Third-party libraries or APIs introduced
Implementation Steps:

[Step with file paths and key code]
[Step with file paths and key code]
... (Ordered from foundational to UI — backend first, then frontend)
Key Code (most critical file or component): [Full code block — not pseudocode — for the primary new file or modified section]

Supporting Code (secondary files if needed): [Additional code blocks for API routes, utility functions, configs, etc.]

UX Flow: User journey as a numbered sequence:

User does [action] →
UI shows [response] →
User confirms/selects [option] →
System executes [process] →

 

 

Testing Suggestions:

[Test case 1: what to verify and expected outcome]
[Test case 2: adversarial or boundary input]
[Test case 3: failure/recovery scenario]

---

## Global Technical Constraints

Apply these across ALL features:
- **No new datasets** may ever be created in BigQuery from the UI
- **tag:gold is always excluded** from any dbt run triggered by this UI
- **BigQuery location**: `asia-southeast2` — all API calls must respect this
- **GCP Project**: `data-analytics-asg`
- **Existing datasets**: `dbt\_dev`, `dbt\_testing` (and any others already in the project)
- **dbt Core** (not dbt Cloud) — no references to dbt Cloud APIs or features
- **Authentication**: Use existing GCP service account / application default credentials

---

## Priority Order

Implement in this order (dependencies and value):
1. Feature 3 (Autocomplete) — highest daily-use impact
2. Feature 7 (Build & Test explanations) — low effort, high clarity
3. Feature 1 + 2 (Documentation editing + autosave) — paired features
4. Feature 4 (Cleanup Advisor scoping)
5. Feature 6 (Save View replacing env selector)
6. Feature 5 (Silver model transparency + scheduling)
7. Feature 8 (ERD Generator) — most complex, highest architectural scope