/* ==========================================================================
   workbench.js - query the warehouse through dbt.

   The point of this screen: an analyst writes ref('model') and never touches a
   dataset name. Switching target in the header repoints every ref with no edit
   to the SQL, so the same statement is safe to run against dev or prod.
   ========================================================================== */

import {
  api, bytes, clear, copy, download, el, ms, num, plainRelation, reportError,
  shortRelation, state, toast,
} from '../core.js';
import {
  callout, codeBlock, columnContract, emptyState, layerChip, loading,
  resultGrid, schemaTable, sqlEditor, tabs, typeBadge,
} from '../components.js';
import { openSaveAsModel } from '../savemodel.js';
import { openCreateTable } from '../createtable.js';

export const meta = {
  title: 'Workbench',
  subtitle: 'Explore your data with SQL. Ctrl+Enter to run, Ctrl+Shift+Enter to check for free.',
};

const STORAGE_KEY = 'dbtstudio.workbench.sql';

const STARTER = `-- Query through dbt: ref() resolves to the right dataset for the
-- selected target, so this statement is portable across dev and prod.
--
--   Ctrl+Enter        run (capped preview)
--   Ctrl+Shift+Enter  validate only - returns columns and types, costs nothing

select
    company_code,
    period_month,
    account_group,
    sum(debit_amount)  as debit,
    sum(credit_amount) as credit,
    count(*)           as entries

from {{ ref('silver_gl_entries') }}

group by 1, 2, 3
order by 1, 2, 3
`;

export function render(navigate, params = {}) {
  const initial =
    params.sql ||
    state.scratch.workbenchSql ||
    localStorage.getItem(STORAGE_KEY) ||
    defaultStarter();

  const host = el('div');
  const resultHost = el('div.panel.mt');
  const statusBar = el('div.row.wrap', { style: { gap: '7px', minHeight: '24px' } });

  const editor = sqlEditor({
    value: initial,
    placeholder: "select * from {{ ref('your_model') }}",
    /* The editor forwards the shift state so Ctrl+Shift+Enter validates. */
    onRun: (_sql, options = {}) => execute({ dryRun: Boolean(options.dryRun) }),
    onChange: (value) => {
      state.scratch.workbenchSql = value;
      localStorage.setItem(STORAGE_KEY, value);
    },
  });

  const limitInput = el('input.input', {
    type: 'number',
    min: '1',
    max: String(state.boot?.settings?.max_preview_row_limit || 5000),
    value: String(state.boot?.settings?.preview_row_limit || 200),
    style: { width: '92px' },
  });

  const runBtn = el('button.btn.btn-primary', { onclick: () => execute() }, '▶ Run');
  const validateBtn = el('button.btn', { onclick: () => execute({ dryRun: true }) }, '✓ Validate (free)');
  const compileBtn = el('button.btn', { onclick: () => showCompiled() }, '⌗ Compiled SQL');

  /* Promoting an exploratory query into a committed model. Deliberately writes a
     file rather than a view: see savemodel.js for why. */
  const saveBtn = el(
    'button.btn',
    {
      title: 'Turn this query into a dbt model file',
      onclick: () => {
        const sql = editor.value.trim();
        if (!sql) {
          toast('Write a statement first.', { kind: 'warn' });
          return;
        }
        openSaveAsModel({ sql, trigger: saveBtn, navigate });
      },
    },
    '⤓ Save as model',
  );

  /* The write dataset for the current target, e.g. data-analytics-asg.dbt_dev.
     Used to scaffold a fully-qualified table name the way BigQuery's console
     does when you click "Save results > BigQuery table". */
  function targetRelationPrefix() {
    const target = (state.boot?.targets || []).find((t) => t.name === state.target)
      || (state.boot?.targets || [])[0];
    if (!target) return '';
    return `${target.project}.${target.dataset}`;
  }

  /* "Create table" — wrap the current SELECT in a CREATE OR REPLACE TABLE ... AS
     (a CTAS) and load it into the editor so the name can be edited before Run.
     This is the table equivalent of typing a CREATE statement in the BigQuery
     console; the read-only policy now permits CREATE TABLE and CREATE VIEW. */
  const createTableBtn = el(
    'button.btn',
    {
      title: 'Create a BigQuery table — empty or from the current query',
      onclick: () => openCreateTable({
        currentSql: editor.value,
        onSubmit: (sql) => {
          /* Put the assembled statement in the editor so it is visible and
             editable, then run it through the normal execute path. */
          editor.value = sql;
          execute();
        },
      }),
    },
    '⊞ Create table',
  );

  const createViewBtn = el(
    'button.btn',
    {
      title: 'Wrap this query in CREATE OR REPLACE VIEW … AS, ready to run',
      onclick: () => scaffoldDdl('view'),
    },
    '◫ Create view',
  );

  function scaffoldDdl(kind) {
    const sql = editor.value.trim();
    if (!sql) {
      toast('Write a SELECT first.', { kind: 'warn' });
      return;
    }
    const ok = jinja_sql_isSelect(sql);
    if (!ok) {
      toast('Start from a SELECT statement to wrap it.', {
        kind: 'warn',
        detail: 'The current statement does not look like a query to build a '
          + `${kind} from.`,
      });
      return;
    }

    const prefix = targetRelationPrefix();
    const placeholder = kind === 'table' ? 'new_table' : 'new_view';
    const relation = prefix ? `\`${prefix}.${placeholder}\`` : placeholder;
    const verb = kind === 'table'
      ? 'create or replace table'
      : 'create or replace view';

    editor.value = `${verb} ${relation} as\n${sql.replace(/;\s*$/, '')}\n`;

    /* Put the caret on the placeholder name so it can be renamed immediately. */
    const idx = editor.node.querySelector('textarea')?.value.indexOf(placeholder);
    const textarea = editor.node.querySelector('textarea');
    if (textarea && idx >= 0) {
      textarea.focus();
      textarea.setSelectionRange(idx, idx + placeholder.length);
    }

    toast(`Rename ${placeholder}, then press Run to create the ${kind}.`, {
      kind: 'info',
    });
  }

  /* A light client-side check: is the statement a plain read we can wrap? Kept
     simple on purpose - the backend policy is the real gate. */
  function jinja_sql_isSelect(sql) {
    const first = sql
      .replace(/\/\*[\s\S]*?\*\//g, ' ')
      .replace(/--[^\n]*/g, ' ')
      .trim()
      .replace(/^\(+/, '')
      .split(/\s+/)[0]
      .toLowerCase();
    return first === 'select' || first === 'with';
  }

  let busy = false;

  function setBusy(next) {
    busy = next;
    runBtn.disabled = next;
    validateBtn.disabled = next;
    compileBtn.disabled = next;
    saveBtn.disabled = next;
    createTableBtn.disabled = next;
    createViewBtn.disabled = next;
    runBtn.textContent = next ? 'Running…' : '▶ Run';
  }

  async function execute({ dryRun = false } = {}) {
    const sql = editor.value.trim();
    if (!sql) {
      toast('Nothing to run. Write a statement first.', { kind: 'warn' });
      return;
    }
    if (busy) return;

    setBusy(true);
    clear(statusBar).append(el('span.spinner'), el('span.small.faint', dryRun ? 'Validating…' : 'Querying BigQuery…'));
    clear(resultHost).append(loading(dryRun ? 'Planning the query…' : 'Running through dbt…'));

    try {
      const payload = dryRun
        ? await api.validate(sql)
        : await api.run(sql, Number(limitInput.value) || undefined);
      renderResult(payload, dryRun);
    } catch (error) {
      renderError(error);
    } finally {
      setBusy(false);
    }
  }

  function renderStatus(payload, dryRun) {
    const result = payload.result;
    const compiled = payload.compiled;

    clear(statusBar).append(
      el(`span.chip.${dryRun ? 'info' : 'ok'}`, dryRun ? 'validated, not executed' : 'executed'),
      el('span.chip', `${result.column_count} columns`),
      dryRun ? null : el('span.chip', `${num(result.total_rows)} rows`),
      el('span.chip', `${bytes(result.bytes_processed)} ${dryRun ? 'would scan' : 'scanned'}`),
      dryRun ? el('span.chip.ok', 'nothing billed') : el('span.chip', `${bytes(result.bytes_billed)} billed`),
      result.cache_hit ? el('span.chip.ok', 'cache hit') : null,
      el('span.chip', ms(result.duration_ms)),
      el('span.chip.info', `target ${result.target}`),
      ...(compiled.refs || []).map((name) => el('span.chip', `ref ${name}`)),
      ...(compiled.sources || []).map((key) => el('span.chip', `source ${key}`)),
    );
  }

  function renderResult(payload, dryRun) {
    renderStatus(payload, dryRun);

    const result = payload.result;

    /* A CREATE VIEW / CREATE TABLE returns no result set. Show that it worked
       and offer the SQL that ran, rather than an empty "no columns" grid. */
    if (payload.ddl && !dryRun) {
      const kind = payload.ddl_kind === 'table' ? 'table' : 'view';
      const label = kind === 'table' ? 'Table created' : 'View created';
      const body = kind === 'table'
        ? 'BigQuery ran the statement and the table now exists in the target '
          + 'dataset, populated with the query results. It is not part of the '
          + 'dbt DAG, so add a model if you want dbt to manage and rebuild it.'
        : 'BigQuery ran the statement and the view now exists in the target '
          + 'dataset. It is not part of the dbt DAG, so add a model if you want '
          + 'dbt to manage it.';
      clear(statusBar).append(
        el('span.chip.ok', `${kind} created`),
        el('span.chip', ms(result.duration_ms)),
        el('span.chip.info', `target ${result.target}`),
      );
      clear(resultHost).append(
        el(
          'div.panel-body',
          callout(label, body, 'ok'),
          el('div.mt',
            el('p.small.faint', { style: { marginTop: 0 } }, 'The statement that ran:'),
            codeBlock(result.executed_sql || payload.compiled.compiled_sql, { tall: false })),
        ),
      );
      return;
    }

    const contract = columnContract(result.columns);

    const view = tabs([
      {
        label: dryRun ? 'Rows (not fetched)' : 'Results',
        count: dryRun ? null : result.rows.length,
        render: () =>
          dryRun
            ? el(
                'div.panel-body',
                callout(
                  'Validation only',
                  'The statement was planned but not executed, so no rows were fetched and no bytes were billed. Press Run to fetch data.',
                  'info',
                ),
              )
            : resultGrid(result, { filename: 'workbench_result.csv' }),
      },
      {
        label: 'Columns & types',
        count: result.column_count,
        render: () => columnsPanel(result, contract, payload, navigate),
      },
      {
        label: 'Compiled SQL',
        render: () =>
          el(
            'div.panel-body',
            el('p.small.faint', { style: { marginTop: 0 } },
              'What dbt actually sent to BigQuery after resolving ref() and source().'),
            codeBlock(result.executed_sql || payload.compiled.compiled_sql, { tall: true }),
          ),
      },
      {
        label: 'Lineage',
        render: () => lineagePanel(payload.compiled),
      },
    ]);

    clear(resultHost).append(view.node);
  }

  function renderError(error) {
    clear(statusBar).append(el('span.chip.err', error.payload?.stage || 'failed'));

    const stage = error.payload?.stage;
    const title =
      stage === 'compile'
        ? 'Could not compile the Jinja'
        : stage === 'policy'
        ? 'Blocked by workbench policy'
        : stage === 'warehouse'
        ? 'BigQuery rejected the query'
        : 'Query failed';

    clear(resultHost).append(
      el(
        'div.panel-body',
        callout(
          title,
          error.message,
          'err',
          el(
            'div',
            error.detail ? el('pre.code-block', error.detail) : null,
            error.payload?.unknown_refs?.length
              ? el(
                  'div.mt',
                  el('span.small.faint', 'Models available in this project: '),
                  el(
                    'div.row.wrap.mt',
                    { style: { gap: '5px' } },
                    ...(state.refs || []).slice(0, 24).map((entry) =>
                      el(
                        'button.btn.btn-tiny',
                        { onclick: () => editor.insert(`{{ ref('${entry.name}') }}`) },
                        entry.name,
                      ),
                    ),
                  ),
                )
              : null,
            error.payload?.sql
              ? el(
                  'details.mt',
                  el('summary.small.faint', { style: { cursor: 'pointer' } }, 'SQL that was sent'),
                  el('div.mt', codeBlock(error.payload.sql, { tall: false })),
                )
              : null,
          ),
        ),
      ),
    );
  }

  async function showCompiled() {
    const sql = editor.value.trim();
    if (!sql) return;
    try {
      const payload = await api.compile(sql);
      clear(resultHost).append(
        el(
          'div.panel-body',
          el('div.row.wrap.mb', { style: { gap: '6px' } },
            el('span.chip.ok', 'compiled'),
            ...(payload.refs || []).map((name) => el('span.chip', `ref ${name}`)),
          ),
          codeBlock(payload.compiled_sql, { tall: true }),
        ),
      );
      clear(statusBar).append(el('span.chip.ok', 'compiled without running'));
    } catch (error) {
      renderError(error);
    }
  }

  /* -------------------------------------------------------- assembly --- */

  document.addEventListener('keydown', keyHandler);
  function keyHandler(event) {
    if (!host.isConnected) {
      document.removeEventListener('keydown', keyHandler);
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      execute({ dryRun: event.shiftKey });
    }
  }

  host.append(
    el(
      'div.split',
      refPanel(editor),
      el(
        'div',
        el(
          'div.panel',
          el(
            'div.panel-head',
            el('h3', 'Statement'),
            el(
              'div.row',
              { style: { gap: '7px' } },
              el('span.small.faint', 'row cap'),
              limitInput,
              compileBtn,
              createViewBtn,
              createTableBtn,
              saveBtn,
              validateBtn,
              runBtn,
            ),
          ),
          el('div.panel-body', editor.node, el('div.mt', statusBar)),
        ),
        resultHost,
      ),
    ),
  );

  clear(resultHost).append(
    emptyState(
      'Ready',
      'Press Run to execute, or Validate to get the output columns and their data types without spending anything.',
    ),
  );

  return host;
}

function defaultStarter() {
  const hasSilver = (state.refs || []).some((entry) => entry.name === 'silver_gl_entries');
  if (hasSilver) return STARTER;

  const first = (state.refs || [])[0];
  if (!first) return '-- No models in this project yet.\nselect 1 as ok\n';
  return `select *\nfrom {{ ref('${first.name}') }}\nlimit 100\n`;
}

/* ------------------------------------------------------------- ref panel --- */

function refPanel(editor) {
  const search = el('input.input', {
    type: 'search',
    placeholder: 'Find a model, table or dataset…',
    oninput: (event) => {
      const needle = event.target.value.trim().toLowerCase();
      /* Query every button so the nested warehouse-group tables are filtered
         too, not just the top-level model/source rows. */
      for (const row of list.querySelectorAll('.list-btn')) {
        row.hidden = Boolean(needle) && !(row.dataset.name || '').includes(needle);
      }
      /* Group headings hide when a filter is active, since they are not
         themselves matches and would otherwise float over empty groups. */
      for (const head of list.querySelectorAll('.ref-group-head, .ref-group-sub')) {
        head.hidden = Boolean(needle);
      }
    },
  });

  // Out-of-scope models sort last and are marked, because inserting a ref to
  // one produces a scope refusal rather than results.
  const ordered = [...(state.models || [])].sort(
    (a, b) =>
      Number(a.in_scope === false) - Number(b.in_scope === false) ||
      (a.layer_order ?? 9) - (b.layer_order ?? 9) ||
      a.name.localeCompare(b.name),
  );

  const list = el('div.scroll-list');
  for (const model of ordered) {
    const outOfScope = model.in_scope === false;
    list.append(
      el(
        'button',
        {
          class: `list-btn${outOfScope ? ' out-of-scope' : ''}`,
          dataset: { name: model.name.toLowerCase() },
          title: outOfScope
            ? `Out of scope: dataset '${model.dataset}' cannot be queried here`
            : `Insert ref('${model.name}')\n${plainRelation(model.relation_name)}`,
          onclick: () => {
            if (outOfScope) {
              toast(`${model.name} is outside the permitted dataset scope.`, {
                kind: 'warn',
                detail:
                  `Querying it would be refused. Permitted datasets: ` +
                  `${(state.scope?.allowed_datasets || []).join(', ')}.`,
              });
              return;
            }
            editor.insert(`{{ ref('${model.name}') }}`);
          },
        },
        layerChip(model.layer),
        el('span.lb-name', model.name),
        outOfScope ? el('span.lb-meta', el('span.chip.err', 'blocked')) : null,
      ),
    );
  }

  for (const source of state.sources || []) {
    const [sourceName, tableName] = source.key.split('.');
    list.append(
      el(
        'button.list-btn',
        {
          dataset: { name: source.key.toLowerCase() },
          title: `Insert source('${sourceName}', '${tableName}')`,
          onclick: () => editor.insert(`{{ source('${sourceName}', '${tableName}') }}`),
        },
        el('span.chip.source', 'Source'),
        el('span.lb-name', source.key),
      ),
    );
  }

  /* Accessible datasets and their tables, beyond the dbt models. These are the
     physical relations the current credentials can read within scope. Loaded
     asynchronously from the warehouse inventory so the panel paints instantly;
     the group is appended once the metadata arrives. Clicking inserts the fully
     qualified `project.dataset.table`, since these have no ref(). */
  const warehouseGroup = el('div');
  list.append(warehouseGroup);

  (async () => {
    let payload;
    try {
      payload = await api.inventory();
    } catch {
      return; // inventory is a bonus here; models/sources already listed.
    }

    const tables = payload.tables || [];
    if (!tables.length) return;

    /* Skip the ones already offered as a dbt model, to avoid a duplicate row
       that inserts a raw relation instead of ref(). */
    const modelTables = new Set(
      (state.models || [])
        .map((m) => String(m.relation_name || '').replace(/`/g, '').toLowerCase())
        .filter(Boolean),
    );

    warehouseGroup.append(
      el('div.ref-group-head', `Datasets & tables · ${payload.project || ''}`),
    );

    const byDataset = new Map();
    for (const table of tables) {
      if (!byDataset.has(table.dataset)) byDataset.set(table.dataset, []);
      byDataset.get(table.dataset).push(table);
    }

    for (const [dataset, rows] of [...byDataset.entries()].sort()) {
      warehouseGroup.append(el('div.ref-group-sub', dataset));
      for (const table of rows.sort((a, b) => a.table.localeCompare(b.table))) {
        const relation = String(table.relation || '').replace(/`/g, '');
        const bareRelation = table.relation
          || `${payload.project}.${table.dataset}.${table.table}`;
        if (modelTables.has(relation.toLowerCase())) continue;

        warehouseGroup.append(
          el('button.list-btn', {
            dataset: { name: `${dataset}.${table.table}`.toLowerCase() },
            title: `Insert ${bareRelation}`,
            onclick: () => editor.insert(bareRelation),
          },
          el('span.chip.tiny', table.is_view ? 'view' : 'table'),
          el('span.lb-name', table.table),
          el('span.lb-meta', el('span.tiny.faint', dataset)),
          ),
        );
      }
    }
  })();

  return el(
    'div.panel',
    el(
      'div.panel-head',
      el('h3', 'Insert a reference'),
      el('span.muted.small', 'models, sources & tables'),
    ),
    el(
      'div.panel-body',
      search,
      el('div.mt', list),
      el(
        'div.tiny.faint',
        { style: { marginBottom: 0, lineHeight: '1.6' } },
        el('div', el('kbd', 'Ctrl'), ' + ', el('kbd', 'Space'), ' — suggest columns, tables, functions and keywords'),
        el('div', 'Type ', el('kbd', '.'), ' after a table alias for its columns, or after a dataset for its tables'),
        el('div', 'Models insert ref(); accessible tables insert the full relation'),
      ),
    ),
  );
}

/* ---------------------------------------------------------- columns tab --- */

/**
 * The requirement in its most direct form: after a query, the exact column
 * names and their BigQuery data types, in the shape a dbt schema file wants.
 */
function columnsPanel(result, contract, payload, navigate) {
  return el(
    'div',
    el(
      'div.panel-body',
      callout(
        'Column contract for this result',
        'These are the real BigQuery types of the query output, normalised to GoogleSQL spelling. Paste the YAML straight into your schema file.',
        'info',
      ),
      el(
        'div.row.wrap.mt',
        { style: { gap: '7px' } },
        el('button.btn.btn-tiny', { onclick: () => copy(contract, 'Contract copied') }, '⧉ Copy YAML'),
        el(
          'button.btn.btn-tiny',
          { onclick: () => download('columns.yml', contract, 'text/yaml') },
          '↓ Download',
        ),
        el(
          'button.btn.btn-tiny',
          {
            onclick: () =>
              navigate('schema', { sql: payload.compiled.raw_sql, fromWorkbench: true }),
          },
          '☰ Full schema + docs →',
        ),
      ),
    ),
    schemaTable(result.columns, { showProfile: false, showDescription: false }),
    el(
      'div.panel-body',
      el('div.stat-label.mb', 'YAML'),
      codeBlock(contract, { language: 'yaml', title: `${result.column_count} columns` }),
    ),
  );
}

/* ---------------------------------------------------------- lineage tab --- */

function lineagePanel(compiled) {
  const refs = compiled.refs || [];
  const sources = compiled.sources || [];

  if (!refs.length && !sources.length) {
    return el(
      'div.panel-body',
      callout(
        'This query does not reference any dbt model',
        'It reads a literal or a hardcoded relation. Using ref() instead puts the query in the DAG and makes it portable across targets.',
        'warn',
      ),
    );
  }

  return el(
    'div.panel-body',
    el('p.small.faint', { style: { marginTop: 0 } }, 'Relations this statement resolved to:'),
    el(
      'table.data.compact',
      el('thead', el('tr', el('th', 'Reference'), el('th', 'Resolves to'))),
      el(
        'tbody',
        ...refs.map((name, index) =>
          el(
            'tr',
            el('td.mono', `ref('${name}')`),
            el('td.mono.small.faint', plainRelation(compiled.relations[index])),
          ),
        ),
        ...sources.map((key, index) =>
          el(
            'tr',
            el('td.mono', `source('${key.split('.').join("', '")}')`),
            el('td.mono.small.faint', plainRelation(compiled.relations[refs.length + index])),
          ),
        ),
      ),
    ),
  );
}
