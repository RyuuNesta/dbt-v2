/* ==========================================================================
   createtable.js - a BigQuery-console-style "Create table" dialog.

   This is the workbench's table builder. It mirrors the shape of BigQuery's
   own dialog (Source, Destination, Schema, Partitioning) but keeps only the
   parts a CREATE TABLE statement can express from here:

     - Source          Empty table, or the query currently in the workbench.
     - Destination     Project / Dataset / Table. Prefilled from the target.
     - Schema          Column name + type + mode, or edit-as-text. Empty-table
                       only; a query result carries its own schema.
     - Partitioning    None, or PARTITION BY a column.
     - Clustering      Optional CLUSTER BY columns.

   Deliberately dropped, because they need ingestion or object types this UI
   does not do: Google Cloud Storage / Upload / Drive sources, external and
   Iceberg tables, the Browse picker, and ingestion-time partitioning.

   The dialog assembles a single CREATE TABLE statement and hands it back to the
   caller to run through the normal workbench execute path, so the read-only
   policy, scope guard and spend cap all still apply.
   ========================================================================== */

import { clear, el, state, toast } from './core.js';
import { callout, codeBlock, modal } from './components.js';

const BQ_TYPES = [
  'STRING', 'INT64', 'FLOAT64', 'NUMERIC', 'BIGNUMERIC', 'BOOL',
  'DATE', 'DATETIME', 'TIME', 'TIMESTAMP', 'BYTES', 'JSON', 'GEOGRAPHY',
];
const MODES = ['NULLABLE', 'REQUIRED', 'REPEATED'];

/** Quote an identifier part only if it needs it. */
function backtickRelation(project, dataset, table) {
  return `\`${project}.${dataset}.${table}\``;
}

/** A single SELECT/WITH check so we know a query source is wrappable. */
function looksLikeQuery(sql) {
  const first = String(sql || '')
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/--[^\n]*/g, ' ')
    .trim()
    .replace(/^\(+/, '')
    .split(/\s+/)[0]
    .toLowerCase();
  return first === 'select' || first === 'with';
}

/**
 * Open the Create table dialog.
 *
 * @param {object}   opts
 * @param {string}   opts.currentSql   the SQL in the workbench editor, if any
 * @param {function} opts.onSubmit     called with the assembled CREATE TABLE SQL
 */
export function openCreateTable({ currentSql = '', onSubmit } = {}) {
  const targets = state.boot?.targets || [];
  const target = targets.find((t) => t.name === state.target) || targets[0] || {};

  const hasQuery = looksLikeQuery(currentSql);

  /* ----------------------------------------------------------- source --- */

  const sourceSelect = el(
    'select.select',
    el('option', { value: 'empty' }, 'Empty table'),
    el('option', { value: 'query', disabled: !hasQuery },
       hasQuery ? 'Query result (the SQL in the workbench)' : 'Query result (write a SELECT first)'),
  );
  sourceSelect.value = hasQuery ? 'query' : 'empty';

  /* ------------------------------------------------------ destination --- */

  const projectInput = field('Project *', target.project || '');
  const datasetInput = field('Dataset *', target.dataset || '');
  const tableInput = field('Table *', '', 'Letters, numbers and underscores.');

  /* ----------------------------------------------------------- schema --- */

  const schemaHost = el('div');
  const fieldRows = [];

  function addFieldRow(name = '', type = 'STRING', mode = 'NULLABLE') {
    const nameInput = el('input.input.input-tiny', { value: name, placeholder: 'field name' });
    const typeSelect = el('select.select.input-tiny',
      ...BQ_TYPES.map((t) => el('option', { value: t, selected: t === type }, t)));
    const modeSelect = el('select.select.input-tiny',
      ...MODES.map((m) => el('option', { value: m, selected: m === mode }, m)));
    const removeBtn = el('button.btn.btn-tiny.btn-ghost', {
      'aria-label': 'Remove field',
      onclick: () => {
        const idx = fieldRows.indexOf(row);
        if (idx >= 0) fieldRows.splice(idx, 1);
        row.node.remove();
      },
    }, '✕');

    const node = el('div.ct-field-row',
      nameInput, typeSelect, modeSelect, removeBtn);
    const row = { node, get name() { return nameInput.value.trim(); },
                  get type() { return typeSelect.value; },
                  get mode() { return modeSelect.value; } };
    fieldRows.push(row);
    return row;
  }

  const fieldsHost = el('div.ct-fields');
  const textArea = el('textarea.editor', {
    rows: 6, spellcheck: 'false', placeholder: 'name:STRING, amount:NUMERIC, posted:DATE',
  });
  let editAsText = false;

  const editAsTextToggle = el('label.switch',
    (() => {
      const cb = el('input', { type: 'checkbox' });
      cb.addEventListener('change', () => {
        editAsText = cb.checked;
        paintSchema();
      });
      return cb;
    })(),
    el('span', 'Edit as text'));

  const addFieldBtn = el('button.btn.btn-tiny', {
    onclick: () => { fieldsHost.append(addFieldRow().node); },
  }, '＋ Add field');

  function paintSchema() {
    clear(schemaHost);
    if (sourceSelect.value === 'query') {
      schemaHost.append(el('p.small.faint',
        'The schema comes from the query result, so there is nothing to define here.'));
      return;
    }

    schemaHost.append(el('div.row.between.mb',
      el('div.row', { style: { gap: '8px', alignItems: 'center' } }, editAsTextToggle),
      editAsText ? null : addFieldBtn));

    if (editAsText) {
      schemaHost.append(
        el('p.tiny.faint', { style: { margin: '0 0 6px' } },
          'One field per line or comma-separated, as name:TYPE (add :REQUIRED to force it).'),
        textArea);
    } else {
      if (!fieldRows.length) { fieldsHost.append(addFieldRow('id', 'INT64').node); }
      clear(fieldsHost);
      for (const row of fieldRows) fieldsHost.append(row.node);
      schemaHost.append(
        el('div.ct-field-head', el('span', 'Field name'), el('span', 'Type'), el('span', 'Mode'), el('span')),
        fieldsHost);
    }
  }

  /* ---------------------------------------------------- partitioning --- */

  const partNone = radio('ct-part', 'none', 'No partitioning', true);
  const partCol = radio('ct-part', 'column', 'Partition by a column');
  const partColInput = el('input.input.input-tiny',
    { placeholder: 'column to PARTITION BY', disabled: true, style: { marginLeft: '24px', marginTop: '6px', width: '30ch' } });

  for (const r of [partNone.input, partCol.input]) {
    r.addEventListener('change', () => { partColInput.disabled = !partCol.input.checked; });
  }

  const clusterInput = field('Cluster by (optional)', '',
    'Comma-separated column list for CLUSTER BY.');

  /* --------------------------------------------------------- preview --- */

  const previewHost = el('div.mt');

  function collectSchemaFields() {
    if (editAsText) {
      return textArea.value
        .split(/[\n,]+/)
        .map((chunk) => chunk.trim())
        .filter(Boolean)
        .map((chunk) => {
          const [name, type = 'STRING', mode = 'NULLABLE'] = chunk.split(':').map((s) => s.trim());
          return { name, type: type.toUpperCase(), mode: mode.toUpperCase() };
        });
    }
    return fieldRows
      .filter((r) => r.name)
      .map((r) => ({ name: r.name, type: r.type, mode: r.mode }));
  }

  function fieldDdl(f) {
    if (f.mode === 'REPEATED') return `${f.name} ARRAY<${f.type}>`;
    const notNull = f.mode === 'REQUIRED' ? ' NOT NULL' : '';
    return `${f.name} ${f.type}${notNull}`;
  }

  /** Build the CREATE TABLE statement, or throw a friendly Error. */
  function buildSql() {
    const project = projectInput.value.trim();
    const dataset = datasetInput.value.trim();
    const tableName = tableInput.value.trim();

    if (!project || !dataset || !tableName) {
      throw new Error('Project, dataset and table name are all required.');
    }
    if (!/^[A-Za-z0-9_]+$/.test(tableName)) {
      throw new Error('Table name may use letters, numbers and underscores only.');
    }

    const relation = backtickRelation(project, dataset, tableName);
    const clauses = [];

    if (partCol.input.checked && partColInput.value.trim()) {
      clauses.push(`partition by ${partColInput.value.trim()}`);
    }
    const cluster = clusterInput.value.trim();
    if (cluster) {
      clauses.push(`cluster by ${cluster.split(',').map((c) => c.trim()).filter(Boolean).join(', ')}`);
    }
    const tail = clauses.length ? `\n${clauses.join('\n')}` : '';

    if (sourceSelect.value === 'query') {
      if (!looksLikeQuery(currentSql)) {
        throw new Error('The workbench does not hold a SELECT to build the table from.');
      }
      const body = currentSql.trim().replace(/;\s*$/, '');
      return `create or replace table ${relation}${tail}\nas\n${body}\n`;
    }

    const fields = collectSchemaFields();
    if (!fields.length) {
      throw new Error('Add at least one field, or switch the source to a query.');
    }
    const badly = fields.find((f) => !/^[A-Za-z0-9_]+$/.test(f.name));
    if (badly) throw new Error(`"${badly.name}" is not a valid column name.`);

    const cols = fields.map((f) => `  ${fieldDdl(f)}`).join(',\n');
    return `create or replace table ${relation} (\n${cols}\n)${tail};\n`;
  }

  function refreshPreview() {
    clear(previewHost);
    try {
      const sql = buildSql();
      previewHost.append(
        el('div.stat-label.mb', 'Statement to run'),
        codeBlock(sql, { language: 'sql' }));
    } catch (error) {
      previewHost.append(callout('Not ready yet', error.message, 'info'));
    }
  }

  /* Re-preview and re-paint schema on any change. */
  sourceSelect.addEventListener('change', () => { paintSchema(); refreshPreview(); });
  for (const node of [projectInput, datasetInput, tableInput, partColInput, clusterInput, textArea]) {
    node.addEventListener('input', refreshPreview);
  }
  partNone.input.addEventListener('change', refreshPreview);
  partCol.input.addEventListener('change', refreshPreview);
  // Field-row edits: delegate so added rows are covered too.
  const scheduleFromFields = () => refreshPreview();
  fieldsHost.addEventListener('input', scheduleFromFields);
  fieldsHost.addEventListener('change', scheduleFromFields);

  /* ---------------------------------------------------------- assemble -- */

  const createBtn = el('button.btn.btn-primary', {
    onclick: () => {
      let sql;
      try { sql = buildSql(); }
      catch (error) { toast(error.message, { kind: 'warn' }); return; }
      dialog.close();
      onSubmit?.(sql);
    },
  }, 'Create table');

  const cancelBtn = el('button.btn.btn-ghost', { onclick: () => dialog.close() }, 'Cancel');

  const body = el('div.create-table',
    section('Source',
      labeledField('Create table from', sourceSelect)),
    section('Destination',
      projectInput.wrap, datasetInput.wrap, tableInput.wrap),
    section('Schema', schemaHost),
    section('Partitioning settings',
      partNone.node, partCol.node, partColInput),
    section('Clustering', clusterInput.wrap),
    previewHost,
    el('div.row.mt', { style: { gap: '8px' } }, createBtn, cancelBtn),
  );

  const dialog = modal({ title: 'Create table', body, width: '720px' });

  paintSchema();
  refreshPreview();
  return dialog;
}

/* ----------------------------------------------------------- helpers --- */

function section(title, ...children) {
  return el('section.ct-section',
    el('h3.ct-section-title', title),
    ...children);
}

function labeledField(label, control) {
  return el('div.field.mb', el('label', label), control);
}

/** A text input with a label and optional hint; returns the input with .wrap. */
function field(label, value = '', hint = '') {
  const input = el('input.input', { value });
  const wrap = el('div.field.mb',
    el('label', label),
    input,
    hint ? el('p.tiny.faint', { style: { margin: '3px 0 0' } }, hint) : null);
  input.wrap = wrap;
  return input;
}

function radio(name, value, label, checked = false) {
  const input = el('input', { type: 'radio', name, value });
  if (checked) input.checked = true;
  const node = el('label.ct-radio', input, el('span', label));
  return { node, input };
}
