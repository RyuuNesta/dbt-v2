/* ==========================================================================
   dbtdocs.js - reference content for the Build & Test screen.

   Kept as data, separate from the view, so the wording can be reviewed and
   corrected without touching rendering logic.

   Everything here describes dbt Core behaviour. Where Core and Cloud differ
   the Core behaviour is what is documented, because that is what this UI runs.
   ========================================================================== */

/* --------------------------------------------------------------------------
   Per-command detail, revealed by the expander on each command card.
   -------------------------------------------------------------------------- */

export const COMMAND_DETAIL = {
  build: {
    what:
      'Runs seeds, models, snapshots and tests together in one pass, ordered by ' +
      'the dependency graph. Crucially, a model\'s tests run immediately after ' +
      'that model is built, not at the end.',
    when:
      'The default for almost all work. Use it unless you have a specific ' +
      'reason to run only part of the pipeline.',
    affects:
      'Creates or replaces every selected model in the warehouse, reloads ' +
      'selected seeds, and writes test failure rows to the test_failures ' +
      'dataset when store_failures is on.',
    pitfalls: [
      'Because tests are interleaved, a failing test stops the models that ' +
      'depend on it from building at all. That is the point, but it means a ' +
      'partial warehouse state is normal after a failure.',
      'Seeds are fully replaced, not appended. A seed run overwrites whatever ' +
      'was there.',
    ],
    examples: [
      ['dbt build', 'Everything the UI is permitted to build'],
      ['dbt build --select tag:bronze', 'Just the bronze layer and its tests'],
      ['dbt build --select silver_gl_entries+', 'That model and everything downstream of it'],
    ],
  },

  run: {
    what:
      'Executes model SQL only. Each model becomes CREATE OR REPLACE VIEW, ' +
      'CREATE OR REPLACE TABLE, or an incremental MERGE, depending on its ' +
      'materialization. Independent models run in parallel up to the thread ' +
      'count in profiles.yml.',
    when:
      'When seeds are already loaded and you only want to rebuild model logic, ' +
      'and you intend to run tests separately.',
    affects:
      'Creates or replaces the selected models. Does not touch seeds. Does not ' +
      'evaluate a single test.',
    pitfalls: [
      'A green dbt run means the SQL executed, not that the data is correct. ' +
      'Nothing is validated. Prefer dbt build unless you have a reason not to.',
      'A model whose upstream failed is skipped, so a "success" summary can ' +
      'still hide skipped nodes. Read the counts, not just the exit code.',
    ],
    examples: [
      ['dbt run --select silver_gl_entries', 'One model'],
      ['dbt run --select +gold_summary', 'That model and everything it depends on'],
      ['dbt run --full-refresh --select tag:silver', 'Rebuild incrementals from scratch'],
    ],
  },

  test: {
    what:
      'Compiles every selected test into a SELECT that is expected to return ' +
      'zero rows, runs it, and reports any row it finds as a failure. Covers ' +
      'the generic tests declared in YAML (unique, not_null, accepted_values, ' +
      'relationships) and any custom test in tests/.',
    when:
      'To re-validate data without rebuilding anything, for example after an ' +
      'upstream load landed new rows.',
    affects:
      'Read-only against your tables. The only thing it can write is the ' +
      'failing rows, into the test_failures dataset, when store_failures is on.',
    pitfalls: [
      'Tests run against whatever is currently in the warehouse. If your models ' +
      'are stale, you are testing stale data.',
      'A test with severity: warn reports but does not fail the run. Check the ' +
      'warning count, not only the exit code.',
    ],
    examples: [
      ['dbt test', 'Every test in scope'],
      ['dbt test --select silver_gl_entries', 'Only that model\'s tests'],
      ['dbt test --select test_type:generic', 'Only the YAML-declared tests'],
    ],
  },

  seed: {
    what:
      'Loads each CSV in seeds/ into a table, using the column types declared ' +
      'in dbt_project.yml or the seed\'s YAML. The table is fully replaced ' +
      'on every run.',
    when:
      'After editing a CSV, or on a fresh environment before the first build.',
    affects:
      'Replaces the seed tables. In this project that is gl_entries in the ' +
      'seeds dataset.',
    pitfalls: [
      'Without explicit column_types, BigQuery infers them, and money columns ' +
      'become FLOAT64 and lose cent-level precision. This project declares ' +
      'types for exactly that reason.',
      'Seeds are for small, version-controlled reference data. They are not a ' +
      'data loading mechanism for anything of size.',
    ],
    examples: [
      ['dbt seed', 'Load every seed'],
      ['dbt seed --select gl_entries', 'One seed file'],
    ],
  },

  parse: {
    what:
      'Reads every model, YAML file and macro and writes target/manifest.json, ' +
      'dbt\'s compiled description of the project. Needs no warehouse ' +
      'connection.',
    when:
      'After editing any .sql or .yml file. Every screen in this UI reads the ' +
      'manifest, so nothing you changed appears until this has run.',
    affects:
      'Only files under target/. Touches nothing in BigQuery.',
    pitfalls: [
      'This is what the Reload project button in the header runs.',
      'The manifest freezes each model\'s physical dataset at parse time, so ' +
      'switching environment requires a re-parse to repoint references.',
    ],
    examples: [['dbt parse', 'Rebuild the manifest']],
  },

  compile: {
    what:
      'Renders every model\'s Jinja into the final SQL and writes it to ' +
      'target/compiled/, without executing any of it. This is how you see what ' +
      'ref(), source() and your macros actually expand to.',
    when:
      'To debug a macro, or to read the real SQL before committing to running it.',
    affects:
      'Writes files under target/. Creates and replaces nothing.',
    pitfalls: [
      'It still opens a warehouse connection, because some macros introspect ' +
      'relations while compiling. Compiling is not entirely offline.',
      'Compiled SQL is not validated by BigQuery. Use Validate in the Workbench ' +
      'for that, which is free.',
    ],
    examples: [
      ['dbt compile', 'Compile everything'],
      ['dbt compile --select silver_gl_entries', 'One model'],
    ],
  },

  debug: {
    what:
      'Checks that dbt_project.yml and profiles.yml are valid, that required ' +
      'dependencies are present, and that it can actually open a connection ' +
      'and run a trivial query.',
    when:
      'First thing when anything looks like a credentials or connection problem.',
    affects: 'Nothing. Purely diagnostic.',
    pitfalls: [
      'It validates the connection for the selected target only. Another ' +
      'target can still be misconfigured.',
    ],
    examples: [['dbt debug', 'Check config and connectivity']],
  },

  deps: {
    what:
      'Downloads the packages listed in packages.yml into dbt_packages/ and ' +
      'writes the resolved versions to package-lock.yml.',
    when:
      'On a fresh clone, and after changing packages.yml.',
    affects: 'Only dbt_packages/ and package-lock.yml on disk.',
    pitfalls: [
      'Needs outbound access to hub.getdbt.com. Behind a strict proxy this is ' +
      'usually the first thing to fail.',
      'Deleting dbt_packages/ without re-running deps breaks every model that ' +
      'calls a package macro.',
    ],
    examples: [['dbt deps', 'Install or update packages']],
  },

  docs: {
    what:
      'Builds target/catalog.json by querying INFORMATION_SCHEMA for every ' +
      'relation, combines it with the manifest, and produces a browsable site. ' +
      'This UI passes --static so the whole site lands in one HTML file.',
    when:
      'After documentation changes, when you want the standard dbt catalogue ' +
      'and lineage view.',
    affects:
      'Writes files under target/. Reads table metadata from BigQuery, which is ' +
      'metadata-only and does not scan table data.',
    pitfalls: [
      'A relation that has never been built has no catalogue entry, so its ' +
      'columns show as empty.',
    ],
    examples: [['dbt docs generate --static', 'Build the bundled site']],
  },

  source: {
    what:
      'For each source with a loaded_at_field and a freshness block, finds the ' +
      'newest row and compares its age against the warn_after and error_after ' +
      'thresholds. Writes target/sources.json.',
    when:
      'To confirm upstream data is actually arriving before you trust a build.',
    affects: 'Read-only. One lightweight query per source.',
    pitfalls: [
      'Silently does nothing useful unless loaded_at_field and freshness are ' +
      'configured. This project declares no sources yet, so there is nothing ' +
      'to check.',
    ],
    examples: [['dbt source freshness', 'Check every configured source']],
  },
};

/* --------------------------------------------------------------------------
   The end-to-end flow, for the "How does this work?" guide.
   -------------------------------------------------------------------------- */

export const FLOW_STAGES = [
  {
    command: 'deps',
    label: 'deps',
    title: 'Install packages',
    body:
      'Pulls the packages in packages.yml (here: dbt_utils and codegen) into ' +
      'dbt_packages/. Run once per clone, then only when packages.yml changes.',
    writes: false,
    optional: true,
  },
  {
    command: 'seed',
    label: 'seed',
    title: 'Load reference CSVs',
    body:
      'Replaces the seed tables from the CSVs in seeds/. Small, ' +
      'version-controlled reference data only.',
    writes: true,
    optional: true,
  },
  {
    command: 'run',
    label: 'run',
    title: 'Build the models',
    body:
      'Executes each model\'s SELECT as CREATE OR REPLACE VIEW / TABLE, or an ' +
      'incremental MERGE, walking the dependency graph so upstream always ' +
      'lands before downstream.',
    writes: true,
    optional: false,
  },
  {
    command: 'test',
    label: 'test',
    title: 'Validate the data',
    body:
      'Runs every assertion as a query that must return no rows. Any row ' +
      'returned is a failure.',
    writes: false,
    optional: false,
  },
  {
    command: 'docs',
    label: 'docs',
    title: 'Publish the catalogue',
    body:
      'Reads column metadata for every relation and bundles it with the ' +
      'manifest into a browsable site.',
    writes: false,
    optional: true,
  },
];

export const FLOW_NOTES = [
  {
    title: 'dbt build does the middle three at once',
    body:
      'The five stages above are the mental model, but you rarely run them ' +
      'separately. dbt build interleaves seed, run and test in a single ' +
      'dependency-ordered pass, so each model is tested the moment it is ' +
      'built rather than at the end. That is why a failing test can stop a ' +
      'downstream model from being created at all, which is exactly what you ' +
      'want: bad data does not propagate.',
  },
  {
    title: 'Why bronze, then silver, then gold',
    body:
      'dbt does not know about layers. It derives the order purely from your ' +
      'ref() calls: silver refs bronze, gold refs silver, so the graph forces ' +
      'that sequence. The layer names are a convention that makes the graph ' +
      'legible; the dependency is what actually schedules the work. Independent ' +
      'branches run in parallel up to the thread count in profiles.yml.',
  },
  {
    title: 'Incremental versus full refresh',
    body:
      'A view or table model is fully rebuilt on every run, so --full-refresh ' +
      'changes nothing for them. It only matters for incremental models, where ' +
      'the normal behaviour is to merge in new rows and --full-refresh drops ' +
      'and recreates from scratch. Use it after changing an incremental ' +
      'model\'s logic, otherwise old rows keep the old shape.',
  },
  {
    title: 'What happens when something fails',
    body:
      'The failed node is marked error and everything downstream of it is ' +
      'skipped. Unrelated branches carry on, so one failure does not abandon ' +
      'the whole run. dbt exits non-zero and the summary line reports ' +
      'PASS / WARN / ERROR / SKIP counts. Fix the cause, then resume with ' +
      '--select <failed_model>+ to rebuild just that model and its dependents ' +
      'rather than starting over.',
  },
  {
    title: 'What the writes badge means',
    body:
      'Commands marked writes create or replace objects in BigQuery: build, ' +
      'run, seed and snapshot. Everything else is read-only or touches only ' +
      'local files under target/. parse and deps do not connect to the ' +
      'warehouse at all.',
  },
];

/* --------------------------------------------------------------------------
   Selection syntax, for the cheat sheet under the --select field.
   -------------------------------------------------------------------------- */

export const SELECTOR_SYNTAX = [
  ['model_name', 'One model on its own', 'silver_gl_entries'],
  ['model_name+', 'The model plus everything downstream of it', 'silver_gl_entries+'],
  ['+model_name', 'Everything upstream plus the model', '+gold_gl_monthly_summary'],
  ['+model_name+', 'Full upstream, the model, and full downstream', '+silver_gl_entries+'],
  ['2+model_name', 'Only two levels of upstream', '2+silver_gl_entries'],
  ['model_name+2', 'Only two levels of downstream', 'bronze_gl_entries+2'],
  ['tag:name', 'Everything carrying that tag', 'tag:bronze'],
  ['path:folder', 'Everything under that directory', 'path:models/silver'],
  ['a b', 'Union: a space means both', 'bronze_gl_entries silver_gl_entries'],
  ['@model_name', 'The model, its descendants, and their other parents too', '@bronze_gl_entries'],
  ['config.materialized:x', 'Everything with that materialization', 'config.materialized:incremental'],
  ['result:error', 'Whatever errored last run. Needs a previous run to compare against', 'result:error'],
];

/* --------------------------------------------------------------------------
   Status vocabulary shared by the command cards and the flow diagram.
   -------------------------------------------------------------------------- */

export const RUN_STATE = {
  running:   { label: 'running',   kind: 'info',  glyph: '●' },
  success:   { label: 'succeeded', kind: 'ok',    glyph: '✓' },
  failed:    { label: 'failed',    kind: 'err',   glyph: '✕' },
  cancelled: { label: 'cancelled', kind: 'warn',  glyph: '⊘' },
  skipped:   { label: 'skipped',   kind: 'other', glyph: '–' },
};
