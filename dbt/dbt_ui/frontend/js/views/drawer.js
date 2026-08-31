/* ==========================================================================
   drawer.js - the model inspector, shared by Pipeline, Catalog and Overview.
   ========================================================================== */

import {
  $, api, bytes, clear, el, num, pct, plainRelation, reportError, state,
} from '../core.js';
import {
  callout, codeBlock, columnContract, kv, layerChip, loading,
  materializationChip, relationLine, resultGrid, schemaTable, tabs, typeBadge,
} from '../components.js';
import { copy } from '../core.js';

let onNavigate = () => {};

/** The element that opened the drawer, so focus can be handed back. */
let lastTrigger = null;

export function wireDrawer(navigate) {
  onNavigate = navigate;
  $('#drawer-close').addEventListener('click', closeDrawer);
  $('#drawer-backdrop').addEventListener('click', closeDrawer);

  document.addEventListener('keydown', (event) => {
    const drawer = $('#drawer');
    if (drawer.hidden) return;

    if (event.key === 'Escape') {
      closeDrawer();
      return;
    }

    /* Focus trap. A dialog that lets Tab wander into the page behind it is
       unusable with a keyboard or a screen reader: you cannot tell you have
       left, and you cannot get back. */
    if (event.key === 'Tab') {
      const focusable = [...drawer.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]),' +
        'select:not([disabled]), textarea:not([disabled]),' +
        'summary, [tabindex]:not([tabindex="-1"])',
      )].filter((node) => node.offsetParent !== null);

      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];

      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });
}

export function closeDrawer() {
  $('#drawer').hidden = true;
  $('#drawer-backdrop').hidden = true;

  /* Hand focus back where it came from, so the keyboard position is not lost. */
  if (lastTrigger && document.body.contains(lastTrigger)) {
    lastTrigger.focus();
  }
  lastTrigger = null;
}

export async function openModel(name) {
  const drawer = $('#drawer');
  const body = $('#drawer-body');

  /* Remember what opened us, so Escape returns the keyboard to that spot. */
  if (document.activeElement && document.activeElement !== document.body) {
    lastTrigger = document.activeElement;
  }

  $('#drawer-title').textContent = name;
  $('#drawer-sub').textContent = 'loading…';
  clear(body).append(loading('Reading the project…'));
  drawer.hidden = false;
  $('#drawer-backdrop').hidden = false;

  /* Move focus into the dialog so a keyboard user is actually inside it. */
  $('#drawer-close').focus();

  let model;
  try {
    ({ model } = await api.model(name));
  } catch (error) {
    clear(body).append(
      el('div.panel-body', callout('Could not load this model', error.message, 'err')),
    );
    return;
  }

  $('#drawer-sub').textContent = [
    model.resource_type,
    model.materialized,
    `${model.column_count} columns`,
    `${model.test_count} tests`,
  ].join(' · ');

  render(body, model);
}

function render(body, model) {
  const view = tabs(
    [
      { label: 'Overview', render: () => overviewPanel(model) },
      { label: 'Columns', count: model.column_count, render: () => columnsPanel(model) },
      { label: 'SQL', render: () => sqlPanel(model) },
      { label: 'Tests', count: model.test_count, render: () => testsPanel(model) },
      { label: 'Data', render: () => el('div.panel-body', loading('Fetching a preview…')) },
      { label: 'Physical', render: () => el('div.panel-body', loading('Reading the table…')) },
    ],
    {
      onChange: (index) => {
        if (index === 4) loadPreview(view, model);
        if (index === 5) loadPhysical(view, model);
      },
    },
  );

  clear(body).append(view.node);
}

/* ------------------------------------------------------------- overview --- */

function overviewPanel(model) {
  const docCoverage = model.column_count
    ? Math.round((model.documented_columns / model.column_count) * 100)
    : 0;

  return el(
    'div.panel-body',
    el(
      'div.row.wrap.mb',
      { style: { gap: '6px' } },
      layerChip(model.layer),
      materializationChip(model.materialized),
      ...(model.tags || []).map((tag) => el('span.chip', tag)),
      model.test_count === 0 && model.resource_type === 'model'
        ? el('span.chip.warn', 'no tests')
        : null,
    ),

    model.description
      ? el('p.muted', { style: { marginTop: 0, lineHeight: '1.65' } }, model.description)
      : callout(
          'Not documented',
          'Add a description in the schema YAML. The Documentation page can draft one from the live column types.',
          'warn',
        ),

    el('div.mt'),
    kv([
      ['Relation', relationLine(model.relation_name)],
      ['Dataset', `${model.database}.${model.schema}`],
      ['File', model.original_file_path],
      ['Documented', `${model.documented_columns} of ${model.column_count} columns (${docCoverage}%)`],
      ['Typed', `${model.typed_columns} of ${model.column_count} columns`],
      [
        'Partition',
        model.partition_by
          ? `${model.partition_by.field} (${model.partition_by.granularity || model.partition_by.data_type})`
          : null,
      ],
      ['Cluster', (model.cluster_by || []).join(', ') || null],
    ]),

    lineageBlock('Upstream', model.parents),
    lineageBlock('Downstream', model.child_nodes),

    el(
      'div.row.wrap.mt',
      { style: { gap: '7px' } },
      el(
        'button.btn.btn-primary',
        {
          onclick: () => {
            closeDrawer();
            onNavigate('workbench', { sql: `select *\nfrom {{ ref('${model.name}') }}\nlimit 100` });
          },
        },
        '▶ Query in workbench',
      ),
      el(
        'button.btn',
        {
          onclick: () => {
            closeDrawer();
            onNavigate('schema', { model: model.name });
          },
        },
        '☰ Documentation',
      ),
      model.layer === 'bronze' || model.layer === 'seed'
        ? el(
            'button.btn',
            {
              onclick: () => {
                closeDrawer();
                onNavigate('advisor', { model: model.name });
              },
            },
            '✦ Silver advice',
          )
        : null,
      (state.scope?.blocked_layers || []).includes(model.layer)
        ? el(
            'span.chip.err',
            { title: `This UI never builds the ${model.layer} layer` },
            `${model.layer} is read-only here`,
          )
        : el(
            'button.btn',
            {
              onclick: () => {
                closeDrawer();
                onNavigate('runs', { select: model.name });
              },
            },
            '⚡ Build this model',
          ),
    ),
  );
}

function lineageBlock(title, nodes) {
  if (!nodes?.length) return null;
  return el(
    'div.mt',
    el('div.stat-label.mb', title),
    el(
      'div.row.wrap',
      { style: { gap: '6px' } },
      ...nodes.map((node) =>
        el(
          'button.btn.btn-tiny',
          { onclick: () => openModel(node.name), title: plainRelation(node.relation_name || '') },
          layerChip(node.layer),
          el('span.mono', { style: { marginLeft: '4px' } }, node.name),
        ),
      ),
    ),
  );
}

/* -------------------------------------------------------------- columns --- */

function columnsPanel(model) {
  if (!model.columns?.length) {
    return el(
      'div.panel-body',
      callout(
        'No documented columns',
        'This model has no columns in its schema YAML. Generate a contract from the live relation on the Documentation page.',
        'warn',
      ),
    );
  }

  const contract = columnContract(model.columns);

  return el(
    'div',
    el(
      'div.panel-body',
      el(
        'div.row.between.mb',
        el('span.small.faint', `${model.columns.length} documented columns`),
        el(
          'button.btn.btn-tiny',
          { onclick: () => copy(contract, 'Column contract copied') },
          '⧉ Copy name + data_type',
        ),
      ),
    ),
    schemaTable(model.columns, { showProfile: false, showDescription: true }),
  );
}

/* ------------------------------------------------------------------ sql --- */

function sqlPanel(model) {
  return el(
    'div.panel-body',
    codeBlock(model.raw_code || '-- no raw SQL (seeds have none)', {
      tall: true,
      title: model.original_file_path,
    }),
    model.compiled_code
      ? el(
          'details.mt',
          el('summary.small.faint', { style: { cursor: 'pointer' } }, 'Compiled SQL'),
          el('div.mt', codeBlock(model.compiled_code, { tall: true })),
        )
      : null,
  );
}

/* ---------------------------------------------------------------- tests --- */

function testsPanel(model) {
  if (!model.tests?.length) {
    return el(
      'div.panel-body',
      callout(
        'No tests',
        'An untested model in the DAG is a silent failure waiting to happen. At minimum add not_null and unique on the key.',
        'warn',
      ),
    );
  }

  return el(
    'div.table-wrap',
    el(
      'table.data.compact',
      el('thead', el('tr', el('th', 'Test'), el('th', 'Column'), el('th', 'Severity'))),
      el(
        'tbody',
        ...model.tests.map((test) =>
          el(
            'tr',
            el('td.mono', test.test_type),
            el('td.mono.small', test.column || el('span.faint', 'model level')),
            el(
              'td',
              el(`span.chip.${test.severity === 'error' ? 'err' : 'warn'}`, test.severity),
            ),
          ),
        ),
      ),
    ),
  );
}

/* ----------------------------------------------------------------- data --- */

async function loadPreview(view, model) {
  try {
    const payload = await api.preview({ model: model.name, limit: 100 });
    view.setPanel(
      4,
      el(
        'div',
        el(
          'div.panel-body',
          el('span.small.faint', `Preview of ${plainRelation(payload.relation)}`),
        ),
        resultGrid(payload.result, { filename: `${model.name}.csv` }),
      ),
    );
  } catch (error) {
    view.setPanel(
      4,
      el(
        'div.panel-body',
        callout(
          'Could not preview this relation',
          error.message,
          'err',
          error.detail ? el('pre.code-block', error.detail) : null,
        ),
      ),
    );
  }
}

/* ------------------------------------------------------------- physical --- */

async function loadPhysical(view, model) {
  try {
    const table = await api.describe({ model: model.name });
    view.setPanel(
      5,
      el(
        'div',
        el(
          'div.panel-body',
          kv([
            ['Type', table.table_type],
            ['Rows', num(table.row_count)],
            ['Size', bytes(table.size_bytes)],
            ['Location', table.location],
            [
              'Partition',
              table.partitioning
                ? `${table.partitioning.field} · ${table.partitioning.granularity || table.partitioning.kind}`
                : 'none',
            ],
            ['Cluster', (table.clustering || []).join(', ') || 'none'],
            ['Created', table.created ? new Date(table.created).toLocaleString() : null],
            [
              'Last modified',
              table.last_modified ? new Date(table.last_modified).toLocaleString() : null,
            ],
          ]),
          !table.partitioning && table.row_count > 1_000_000
            ? el(
                'div.mt',
                callout(
                  'Unpartitioned and large',
                  `${num(table.row_count)} rows with no partition column. Every query scans the whole table. Add partition_by on a date column in the model config.`,
                  'warn',
                ),
              )
            : null,
        ),
        el('div.panel-body', el('div.stat-label.mb', 'Live warehouse schema')),
        schemaTable(table.columns, { showProfile: false, showDescription: true }),
      ),
    );
  } catch (error) {
    view.setPanel(
      5,
      el(
        'div.panel-body',
        callout(
          'Could not read the table definition',
          error.message,
          'err',
          error.detail ? el('pre.code-block', error.detail) : null,
        ),
      ),
    );
  }
}
