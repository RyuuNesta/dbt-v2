/* ==========================================================================
   components.js - reusable UI pieces shared across views.
   ========================================================================== */

import {
  $, api, bytes, clear, copy, download, el, layerLabel, ms, num, pct,
  plainRelation, shortRelation, state, toCsv,
} from './core.js';
import { highlight, rank } from './fuzzy.js';
/* Aliased: this module already exports its own CATEGORY_LABELS for the Silver
   Advisor's recommendation categories, which is a different vocabulary. */
import {
  CATEGORY_LABELS as AC_LABELS, CATEGORY_ORDER as AC_ORDER,
  DBT_ITEMS, FUNCTION_ITEMS, KEYWORD_ITEMS, expandSnippet,
} from './sqlcatalog.js';

/* A single glyph per category, so a row's type is readable at a glance without
   relying on colour alone. */
const KIND_GLYPH = {
  column: '▣',
  table: '⊞',
  macro: '{}',
  function: 'ƒ',
  keyword: 'K',
};

/* ------------------------------------------------------------ highlight --- */

const SQL_KEYWORDS = new Set(
  `select from where group by order having limit offset with as on join inner left right
   full outer cross union all distinct case when then else end and or not null is in
   between like exists asc desc over partition rows range unbounded preceding following
   current row cast safe_cast if ifnull coalesce nullif count sum avg min max countif
   array_agg struct unnest date_trunc extract timestamp date datetime time interval
   qualify window except replace using cross tablesample recursive`
    .split(/\s+/)
    .filter(Boolean),
);

const escapeHtml = (text) =>
  String(text).replace(/[&<>]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[ch]));

/**
 * Lightweight SQL highlighter.
 *
 * Tokenising in one pass with a single alternation regex keeps strings and
 * comments from being re-scanned for keywords, which is where naive
 * highlighters produce mangled output.
 */
export function highlightSql(sql) {
  const pattern =
    /(--[^\n]*|\/\*[\s\S]*?\*\/)|(\{\{[\s\S]*?\}\}|\{%[\s\S]*?%\})|('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")|(`[^`]*`)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)/g;

  let out = '';
  let last = 0;
  let match;

  while ((match = pattern.exec(sql)) !== null) {
    out += escapeHtml(sql.slice(last, match.index));
    const [token, comment, jinja, str, ident, digits, word] = match;

    if (comment) out += `<span class="tk-com">${escapeHtml(comment)}</span>`;
    else if (jinja) out += `<span class="tk-jinja">${escapeHtml(jinja)}</span>`;
    else if (str) out += `<span class="tk-str">${escapeHtml(str)}</span>`;
    else if (ident) out += `<span class="tk-key">${escapeHtml(ident)}</span>`;
    else if (digits) out += `<span class="tk-num">${escapeHtml(digits)}</span>`;
    else if (word) {
      const cls = SQL_KEYWORDS.has(word.toLowerCase()) ? 'tk-kw' : null;
      out += cls ? `<span class="${cls}">${escapeHtml(word)}</span>` : escapeHtml(word);
    } else out += escapeHtml(token);

    last = pattern.lastIndex;
  }

  return out + escapeHtml(sql.slice(last));
}

/** YAML highlighter: keys, strings, comments, numbers. */
export function highlightYaml(yaml) {
  return String(yaml)
    .split('\n')
    .map((line) => {
      const comment = line.match(/^(.*?)(#.*)$/);
      const body = comment ? comment[1] : line;
      const trail = comment ? `<span class="tk-com">${escapeHtml(comment[2])}</span>` : '';

      const keyed = body.replace(
        /^(\s*(?:-\s+)?)([A-Za-z_][\w.-]*)(:)/,
        (_, indent, key, colon) =>
          `${escapeHtml(indent)}<span class="tk-key">${escapeHtml(key)}</span><span class="tk-punc">${colon}</span>`,
      );

      const valued = keyed.replace(
        /('[^']*'|"[^"]*")|(\b\d+(?:\.\d+)?\b)|(\btrue\b|\bfalse\b|\bnull\b)/g,
        (token, str, digits, literal) => {
          if (str) return `<span class="tk-str">${escapeHtml(str)}</span>`;
          if (digits) return `<span class="tk-num">${digits}</span>`;
          if (literal) return `<span class="tk-kw">${literal}</span>`;
          return token;
        },
      );

      return valued + trail;
    })
    .join('\n');
}

/* ----------------------------------------------------------- code block --- */

export function codeBlock(text, { language = 'sql', tall = false, actions = [], title = '' } = {}) {
  const pre = el(`pre.code-block${tall ? '.tall' : ''}`);
  const rendered = language === 'yaml' ? highlightYaml(text || '') : highlightSql(text || '');
  pre.innerHTML = rendered;

  const buttons = [
    el('button.btn.btn-tiny', { onclick: () => copy(text, 'Copied to clipboard') }, '⧉ Copy'),
    ...actions,
  ];

  return el(
    'div',
    el(
      'div.row.between.mb',
      el('span.small.faint', title || `${(text || '').split('\n').length} lines`),
      el('div.row', { style: { gap: '6px' } }, buttons),
    ),
    pre,
  );
}

/* ---------------------------------------------------------------- chips --- */

export function layerChip(layer) {
  return el(`span.chip.${layer || 'other'}`, layerLabel(layer));
}

export function typeBadge(dataType, category) {
  const kind = category || 'other';
  return el(`span.type-badge.${kind}`, String(dataType || '').toLowerCase());
}

export function materializationChip(value) {
  const map = { table: 'info', view: 'ok', seed: 'other', incremental: 'warn' };
  return el(`span.chip.${map[value] || 'other'}`, value || 'view');
}

/* ------------------------------------------------------------ result grid --- */

/**
 * Render a query result.
 *
 * Rows are capped in the DOM because a browser table with tens of thousands of
 * nodes becomes unresponsive; the cap is stated in the footer rather than being
 * silent.
 */
const DOM_ROW_CAP = 500;

export function resultGrid(result, { filename = 'query_result.csv' } = {}) {
  const columns = result.columns || [];
  const rows = result.rows || [];

  if (!columns.length) {
    return el(
      'div.empty',
      el('h3', 'No columns returned'),
      el('p', 'The statement executed but produced no result schema.'),
    );
  }

  const shown = rows.slice(0, DOM_ROW_CAP);

  const table = el(
    'table.data.compact',
    el(
      'thead',
      el(
        'tr',
        el('th.rownum', '#'),
        ...columns.map((column) =>
          el(
            'th',
            el('div', { style: { display: 'flex', alignItems: 'center', gap: '6px' } },
              el('span', column.name),
              typeBadge(column.data_type, column.category),
            ),
          ),
        ),
      ),
    ),
    el(
      'tbody',
      ...shown.map((row, index) =>
        el(
          'tr',
          el('td.rownum', index + 1),
          ...row.map((value, columnIndex) => cell(value, columns[columnIndex])),
        ),
      ),
    ),
  );

  const footer = el(
    'div.row.between',
    { style: { padding: '9px 13px', borderTop: '1px solid var(--line-soft)' } },
    el(
      'span.small.faint',
      [
        `${num(rows.length)} row${rows.length === 1 ? '' : 's'}`,
        shown.length < rows.length ? `${num(shown.length)} shown` : null,
        result.truncated ? `capped at ${num(rows.length)}` : null,
        result.dry_run ? 'dry run, nothing executed' : null,
        result.bytes_billed ? `${bytes(result.bytes_billed)} billed` : null,
        result.bytes_processed ? `${bytes(result.bytes_processed)} scanned` : null,
        result.cache_hit ? 'cache hit' : null,
        result.duration_ms ? ms(result.duration_ms) : null,
      ]
        .filter(Boolean)
        .join(' · '),
    ),
    el(
      'div.row',
      { style: { gap: '6px' } },
      el(
        'button.btn.btn-tiny',
        { onclick: () => download(filename, toCsv(columns, rows), 'text/csv') },
        '↓ CSV',
      ),
      el(
        'button.btn.btn-tiny',
        { onclick: () => copy(toCsv(columns, rows), 'Result copied as CSV') },
        '⧉ Copy',
      ),
    ),
  );

  return el('div', el('div.table-wrap', { style: { maxHeight: '52vh' } }, table), footer);
}

function cell(value, column) {
  if (value === null || value === undefined) return el('td.null', 'null');

  const category = column?.category;
  if (category === 'numeric') {
    return el('td.num', typeof value === 'number' ? num(value) : String(value));
  }
  if (category === 'boolean') {
    return el('td.mono', { style: { color: value ? 'var(--ok)' : 'var(--text-faint)' } }, String(value));
  }
  if (category === 'temporal') return el('td.mono', String(value));
  if (category === 'struct' || category === 'array') {
    return el('td.mono', el('span.cell-clip', JSON.stringify(value)));
  }

  const text = String(value);
  return el('td', text.length > 46 ? el('span.cell-clip', { title: text }, text) : text);
}

/* --------------------------------------------------------- schema table --- */

/**
 * The column contract table: name + data_type, plus documentation and profile
 * stats when they are available. This is the artifact the requirement is about,
 * so it also offers the exact YAML fragment.
 */
export function schemaTable(columns, { showProfile = false, showDescription = true } = {}) {
  const table = el(
    'table.data.compact',
    el(
      'thead',
      el(
        'tr',
        el('th', 'Column'),
        el('th', 'data_type'),
        el('th', 'Mode'),
        showProfile ? el('th', 'Null %') : null,
        showProfile ? el('th', 'Distinct') : null,
        showProfile ? el('th', 'Range') : null,
        showDescription ? el('th', 'Description') : null,
      ),
    ),
    el(
      'tbody',
      ...columns.map((column) => {
        const profile = column.profile || (showProfile ? column : null);
        const nullPct = profile?.null_pct;
        const description = column.description || '';

        return el(
          'tr',
          el('td.mono', column.name),
          el('td', typeBadge(column.data_type, column.category)),
          el(
            'td.small.faint',
            column.mode === 'REQUIRED' ? el('span.chip.info', 'required') : (column.mode || 'NULLABLE').toLowerCase(),
          ),
          showProfile
            ? el(
                'td.num',
                nullPct === undefined || nullPct === null
                  ? '-'
                  : el(
                      'span',
                      { style: { color: nullPct > 40 ? 'var(--warn)' : nullPct > 0 ? 'var(--text)' : 'var(--ok)' } },
                      pct(nullPct),
                    ),
              )
            : null,
          showProfile ? el('td.num', num(profile?.distinct_count)) : null,
          showProfile
            ? el(
                'td.small.mono.faint',
                profile?.min === null || profile?.min === undefined
                  ? '-'
                  : el('span.cell-clip', { title: `${profile.min} … ${profile.max}` }, `${profile.min} … ${profile.max}`),
              )
            : null,
          showDescription
            ? el(
                'td.small',
                { style: { maxWidth: '42ch' } },
                description
                  ? el(
                      'span',
                      { style: { color: column.needs_review ? 'var(--warn)' : 'var(--text-dim)' } },
                      description,
                    )
                  : el('span.faint', 'not documented'),
              )
            : null,
        );
      }),
    ),
  );

  return el('div.table-wrap', { style: { maxHeight: '56vh' } }, table);
}

/** The bare `- name: x / data_type: y` list the requirement asks for. */
export function columnContract(columns) {
  return columns
    .map((column) => `- name: ${column.name}\n  data_type: ${String(column.data_type).toLowerCase()}`)
    .join('\n');
}

/* ----------------------------------------------------------------- tabs --- */

let _tabGroupSeq = 0;

/**
 * Tab set with the ARIA wiring screen readers need.
 *
 * Only the selected tab is in the tab order; Left/Right move between them. That
 * is the documented tab pattern, and it means Tab jumps straight into the panel
 * content rather than walking through every tab first.
 */
export function tabs(definitions, { initial = 0, onChange } = {}) {
  const buttons = [];
  const panels = [];
  const group = `tabs${++_tabGroupSeq}`;

  const bar = el('div.tabs', { role: 'tablist' });
  const host = el('div.tab-panels');

  definitions.forEach((definition, index) => {
    const tabId = `${group}-tab${index}`;
    const panelId = `${group}-panel${index}`;

    const button = el(
      'button.tab',
      {
        id: tabId,
        role: 'tab',
        type: 'button',
        'aria-controls': panelId,
        'aria-selected': 'false',
        tabindex: '-1',
        onclick: () => select(index),
        onkeydown: (event) => {
          const delta =
            event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
          if (delta) {
            event.preventDefault();
            select((index + delta + definitions.length) % definitions.length,
                   { focus: true });
          } else if (event.key === 'Home') {
            event.preventDefault();
            select(0, { focus: true });
          } else if (event.key === 'End') {
            event.preventDefault();
            select(definitions.length - 1, { focus: true });
          }
        },
      },
      definition.label,
      definition.count !== undefined && definition.count !== null
        ? el('span.count', num(definition.count))
        : null,
    );

    const panel = el('div.tab-panel', {
      id: panelId,
      role: 'tabpanel',
      'aria-labelledby': tabId,
      tabindex: '0',
    });
    if (definition.render) panel.append(definition.render());

    buttons.push(button);
    panels.push(panel);
    bar.append(button);
    host.append(panel);
  });

  function select(index, { focus = false } = {}) {
    buttons.forEach((button, i) => {
      const on = i === index;
      button.classList.toggle('active', on);
      button.setAttribute('aria-selected', on ? 'true' : 'false');
      button.tabIndex = on ? 0 : -1;
    });
    panels.forEach((panel, i) => panel.classList.toggle('active', i === index));
    if (focus) buttons[index].focus();
    onChange?.(index, definitions[index]);
  }

  select(initial);

  return {
    node: el('div', bar, host),
    select,
    setPanel(index, content) {
      clear(panels[index]).append(content);
    },
    setCount(index, count) {
      const badge = buttons[index].querySelector('.count');
      if (badge) badge.textContent = num(count);
      else if (count !== undefined) buttons[index].append(el('span.count', num(count)));
    },
  };
}

/* ------------------------------------------------------------- sql editor --- */

/**
 * Textarea-based SQL editor with intelligent autocomplete:
 *
 *   - Type 2+ chars         -> suggests matching ref('model') and source()
 *   - Ctrl+Space            -> column autocomplete from the models referenced
 *                              in the current SQL (like BigQuery's editor)
 *   - Ctrl+Enter            -> run
 *   - Ctrl+Shift+Enter      -> validate (free)
 *
 * Column suggestions are resolved from the manifest: we parse every
 * {{ ref('...') }} and {{ source('...') }} in the text, look up their columns,
 * and present them in the dropdown with the type badge and source model label.
 *
 * A textarea rather than a contenteditable surface: native undo, native
 * selection, native accessibility, and it cannot desync a highlight overlay
 * from the text.
 */

/* ========================================================================
   Autocomplete data sources.

   Three tiers, cheapest first:
     1. the catalog fetched once at editor construction (manifest-derived,
        no warehouse call)
     2. per-model column detail from the manifest
     3. INFORMATION_SCHEMA for relations dbt does not manage

   Everything is cached for the session, because this runs on keystrokes.
   ======================================================================== */

const _columnCache = new Map();   // model name -> [{name, data_type}]
const _schemaCache = new Map();   // dataset    -> {table -> [{name, data_type}]}
let _catalog = null;              // shared across every editor instance
let _catalogPromise = null;

async function _loadCatalog() {
  if (_catalog) return _catalog;
  if (_catalogPromise) return _catalogPromise;

  _catalogPromise = (async () => {
    try {
      const payload = await api.autocompleteCatalog();
      _catalog = payload;

      /* The catalog already carries documented columns, so seed the per-model
         cache and avoid a second request for the common case. */
      for (const model of payload.models || []) {
        if (model.columns?.length) {
          _columnCache.set(model.name, model.columns);
        }
      }
      return payload;
    } catch {
      _catalog = { models: [], sources: [], macros: [], datasets: [] };
      return _catalog;
    }
  })();

  return _catalogPromise;
}

/** Columns for a dbt model, from the manifest. */
async function _modelColumns(modelName) {
  if (_columnCache.has(modelName)) return _columnCache.get(modelName);
  try {
    const payload = await api.autocompleteColumns(modelName);
    const columns = payload.columns || [];
    _columnCache.set(modelName, columns);
    return columns;
  } catch {
    _columnCache.set(modelName, []);
    return [];
  }
}

/** Every table+column in a dataset, from INFORMATION_SCHEMA. */
async function _datasetSchema(dataset) {
  const key = dataset.toLowerCase();
  if (_schemaCache.has(key)) return _schemaCache.get(key);

  try {
    const payload = await api.autocompleteSchema(dataset);
    const byTable = new Map();
    for (const table of payload.tables || []) {
      byTable.set(table.table.toLowerCase(), table);
    }
    _schemaCache.set(key, byTable);
    return byTable;
  } catch {
    _schemaCache.set(key, new Map());
    return new Map();
  }
}

/** ref('name') occurrences, in the order they appear. */
function _extractRefs(sql) {
  const names = [];
  const pattern = /\{\{\s*ref\(\s*['"]([^'"]+)['"]\s*\)\s*\}\}/g;
  let match;
  while ((match = pattern.exec(sql)) !== null) {
    if (!names.includes(match[1])) names.push(match[1]);
  }
  return names;
}

/** source('a','b') occurrences as "a.b". */
function _extractSources(sql) {
  const keys = [];
  const pattern = /\{\{\s*source\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]\s*\)\s*\}\}/g;
  let match;
  while ((match = pattern.exec(sql)) !== null) {
    const key = `${match[1]}.${match[2]}`;
    if (!keys.includes(key)) keys.push(key);
  }
  return keys;
}

/**
 * Fully-qualified relations written directly in the SQL.
 * These are the ones the manifest knows nothing about, so they drive the
 * INFORMATION_SCHEMA lookup.
 */
function _extractRawRelations(sql) {
  const found = [];
  const pattern =
    /\b(?:from|join)\s+`?([A-Za-z0-9_-]+)`?\s*\.\s*`?([A-Za-z0-9_]+)`?\s*(?:\.\s*`?([A-Za-z0-9_]+)`?)?/gi;
  let match;
  while ((match = pattern.exec(sql)) !== null) {
    const [, a, b, c] = match;
    /* three parts => project.dataset.table, two parts => dataset.table */
    const dataset = c ? b : a;
    const table = c || b;
    if (dataset && table) found.push({ dataset, table });
  }

  /* Also catch the single-backtick form: `project.dataset.table` */
  const single = /\b(?:from|join)\s+`([A-Za-z0-9_-]+)\.([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)`/gi;
  while ((match = single.exec(sql)) !== null) {
    found.push({ dataset: match[2], table: match[3] });
  }

  return found;
}

export function sqlEditor({ value = '', onRun, onChange, placeholder = '' } = {}) {
  const textarea = el('textarea.editor', {
    spellcheck: 'false',
    autocapitalize: 'off',
    autocomplete: 'off',
    placeholder,
    rows: 12,
    'aria-label': 'SQL editor',
  });
  textarea.value = value;

  const menu = el('div.autocomplete', { hidden: true });
  const wrap = el('div.editor-wrap', textarea, menu);

  /* `rows` is the flat, navigable list. Header rows are interleaved for
     display but skipped by the arrow keys. */
  let rows = [];
  let selected = -1;
  let replaceFrom = null;   // caret offset the accepted text replaces from

  _loadCatalog();           // warm the cache while the user reads the screen

  function closeMenu() {
    menu.hidden = true;
    rows = [];
    selected = -1;
    replaceFrom = null;
  }

  const openItems = () => rows.filter((row) => row.kind !== 'header');

  /** The identifier fragment immediately before the caret. */
  function currentWord() {
    const upto = textarea.value.slice(0, textarea.selectionStart);
    const match = upto.match(/([A-Za-z0-9_$]+)$/);
    return match ? match[1] : '';
  }

  /**
   * The `alias.` or `table.` prefix before the caret, when the user has just
   * typed a dot. This is what makes `t.` offer only t's columns.
   */
  function dotContext() {
    const upto = textarea.value.slice(0, textarea.selectionStart);
    const match = upto.match(/([A-Za-z0-9_]+)\s*\.\s*([A-Za-z0-9_]*)$/);
    return match ? { qualifier: match[1], partial: match[2] } : null;
  }

  /* ------------------------------------------------------ candidates --- */

  /**
   * Every column reachable from the current statement.
   * Manifest first for dbt models, INFORMATION_SCHEMA for anything else.
   */
  async function columnCandidates() {
    const sql = textarea.value;
    const out = [];

    const refNames = _extractRefs(sql);
    const modelColumnLists = await Promise.all(refNames.map(_modelColumns));
    refNames.forEach((modelName, index) => {
      for (const column of modelColumnLists[index]) {
        out.push({
          kind: 'item',
          category: 'column',
          label: column.name,
          insert: column.name,
          dataType: column.data_type || column.data_type_yaml || '',
          meta: modelName,
          detail: column.description || '',
          owner: modelName,
        });
      }
    });

    /* Relations written out in full are not in the manifest, so ask the
       warehouse. One query per dataset, cached for the session. */
    const raw = _extractRawRelations(sql);
    const datasets = [...new Set(raw.map((r) => r.dataset.toLowerCase()))];
    const schemas = await Promise.all(datasets.map(_datasetSchema));
    const byDataset = new Map(datasets.map((name, i) => [name, schemas[i]]));

    for (const { dataset, table } of raw) {
      const tables = byDataset.get(dataset.toLowerCase());
      const entry = tables?.get(table.toLowerCase());
      if (!entry) continue;
      for (const column of entry.columns || []) {
        out.push({
          kind: 'item',
          category: 'column',
          label: column.name,
          insert: column.name,
          dataType: column.data_type_yaml || '',
          meta: table,
          owner: table,
        });
      }
    }

    /* De-duplicate on name, keeping the first owner but noting the clash so
       the user can see a column exists on more than one side of a join. */
    const seen = new Map();
    for (const item of out) {
      const existing = seen.get(item.label);
      if (!existing) {
        seen.set(item.label, item);
      } else if (existing.meta !== item.meta) {
        existing.meta = `${existing.meta}, ${item.meta}`;
        existing.ambiguous = true;
      }
    }
    return [...seen.values()];
  }

  /** Models and sources, offered as a ref()/source() call. */
  function tableCandidates() {
    const models = (_catalog?.models || state.models || []).map((model) => ({
      kind: 'item',
      category: 'table',
      label: model.name,
      snippet: `{{ ref('${model.name}') }}`,
      meta: model.in_scope === false ? 'out of scope' : shortRelation(model.relation || model.relation_name),
      blocked: model.in_scope === false,
      layer: model.layer,
    }));

    const sources = (_catalog?.sources || state.sources || []).map((entry) => {
      const [sourceName, tableName] = entry.key.split('.');
      return {
        kind: 'item',
        category: 'table',
        label: entry.key,
        snippet: `{{ source('${sourceName}', '${tableName}') }}`,
        meta: 'source',
      };
    });

    return [...models, ...sources];
  }

  function macroCandidates() {
    const projectMacros = (_catalog?.macros || []).map((name) => ({
      kind: 'item',
      category: 'macro',
      label: name,
      snippet: `{{ ${name}() }}`,
      meta: 'project macro',
    }));
    return [...DBT_ITEMS, ...projectMacros].map((item) => ({ kind: 'item', ...item }));
  }

  /* --------------------------------------------------------- opening --- */

  /**
   * Build, rank and show the dropdown.
   *
   * `trigger` shapes which categories are offered:
   *   manual  Ctrl+Space, everything
   *   dot     after a `.`, columns of that qualifier only
   *   typing  as you type, everything but only past 2 characters
   */
  async function openMenu(trigger = 'typing') {
    const dot = dotContext();
    const word = dot ? dot.partial : currentWord();

    if (trigger === 'typing' && !dot && word.length < 2) return closeMenu();

    let candidates = [];

    if (dot) {
      /* `alias.` - restrict to that relation's columns where we can resolve it,
         otherwise fall back to every reachable column. */
      const all = await columnCandidates();
      const qualifier = dot.qualifier.toLowerCase();
      const aliasMap = _aliasMap(textarea.value);
      const targetOwner = (aliasMap.get(qualifier) || qualifier).toLowerCase();

      const scoped = all.filter((item) => {
        const owner = String(item.owner || '').toLowerCase();
        return owner === targetOwner || owner.endsWith(`_${targetOwner}`);
      });
      candidates = scoped.length ? scoped : all;
    } else {
      const columns = await columnCandidates();
      candidates = [
        ...columns,
        ...tableCandidates(),
        ...macroCandidates(),
        ...FUNCTION_ITEMS.map((item) => ({ kind: 'item', ...item })),
        ...KEYWORD_ITEMS.map((item) => ({ kind: 'item', ...item })),
      ];
    }

    /* Rank per category, so keywords cannot crowd out the columns you asked
       for, then concatenate in a fixed, predictable order. */
    const grouped = [];
    for (const category of AC_ORDER) {
      const pool = candidates.filter((item) => item.category === category);
      if (!pool.length) continue;

      const ranked = rank(word, pool, {
        key: (item) => item.label,
        limit: category === 'column' ? 14 : category === 'keyword' ? 6 : 10,
      });
      if (!ranked.length) continue;

      grouped.push({ kind: 'header', category, label: AC_LABELS[category] });
      grouped.push(...ranked);
    }

    rows = grouped;
    if (!openItems().length) return closeMenu();

    replaceFrom = textarea.selectionStart - word.length;
    selected = rows.findIndex((row) => row.kind !== 'header');
    render();
  }

  /** Map `alias -> relation name` from FROM/JOIN clauses, for the dot trigger. */
  function _aliasMap(sql) {
    const map = new Map();
    const pattern =
      /\b(?:from|join)\s+(?:\{\{\s*ref\(\s*['"]([^'"]+)['"]\s*\)\s*\}\}|`?[\w-]+`?(?:\s*\.\s*`?(\w+)`?){0,2})\s+(?:as\s+)?(\w+)/gi;
    let match;
    while ((match = pattern.exec(sql)) !== null) {
      const relation = match[1] || match[2];
      const alias = match[3];
      if (relation && alias && !['on', 'using', 'where', 'group', 'order', 'left',
        'right', 'inner', 'full', 'cross', 'join'].includes(alias.toLowerCase())) {
        map.set(alias.toLowerCase(), relation);
      }
    }
    return map;
  }

  /* -------------------------------------------------------- rendering --- */

  function render() {
    clear(menu);

    rows.forEach((row, index) => {
      if (row.kind === 'header') {
        menu.append(el('div.ac-header', { role: 'presentation' }, row.label));
        return;
      }

      const node = el(
        'div',
        {
          class: `ac-item${index === selected ? ' sel' : ''}`,
          role: 'option',
          'aria-selected': index === selected ? 'true' : 'false',
          onmousedown: (event) => {
            event.preventDefault();
            accept(index);
          },
          onmouseenter: () => {
            selected = index;
            for (const child of menu.children) child.classList?.remove('sel');
            node.classList.add('sel');
          },
        },
        row.category === 'column' && row.dataType
          ? el('span.type-badge', { style: { marginRight: '5px', fontSize: '10px' } },
              String(row.dataType).toLowerCase())
          : el('span.ac-kind', { 'aria-hidden': 'true' }, KIND_GLYPH[row.category] || '·'),
        el('span.ac-name', highlight(row.label, row._positions)),
        row.ambiguous ? el('span.chip.warn.ac-flag', 'ambiguous') : null,
        el('span.ac-meta', row.detail || row.meta || ''),
      );

      menu.append(node);
    });

    /* Position under the caret's line. Approximated from the row index because
       a textarea gives no caret coordinates; good enough and costs nothing. */
    const before = textarea.value.slice(0, textarea.selectionStart);
    const lines = before.split('\n');
    const lineHeight = 21;
    const top = Math.min(lines.length * lineHeight + 14, Math.max(40, textarea.clientHeight - 40));
    const column = lines[lines.length - 1].length;

    menu.style.top = `${top}px`;
    menu.style.left = `${Math.min(18 + column * 7.1, Math.max(18, textarea.clientWidth - 300))}px`;
    menu.hidden = false;

    const active = menu.querySelector('.ac-item.sel');
    active?.scrollIntoView({ block: 'nearest' });
  }

  function move(delta) {
    const items = openItems();
    if (!items.length) return;

    const currentItemIndex = items.indexOf(rows[selected]);
    const nextItemIndex = (currentItemIndex + delta + items.length) % items.length;
    selected = rows.indexOf(items[nextItemIndex]);
    render();
  }

  function accept(index) {
    const row = rows[index];
    if (!row || row.kind === 'header') return;

    const start = replaceFrom ?? textarea.selectionStart;
    const end = textarea.selectionStart;

    let insertText = row.insert ?? row.label;
    let caretOffset = insertText.length;
    let selectLength = 0;

    if (row.snippet) {
      const expanded = expandSnippet(row.snippet);
      insertText = expanded.text;
      caretOffset = expanded.caret;
      selectLength = expanded.selectionLength;
    }

    textarea.value =
      textarea.value.slice(0, start) + insertText + textarea.value.slice(end);

    const caret = start + caretOffset;
    textarea.setSelectionRange(caret, caret + selectLength);

    closeMenu();
    textarea.focus();
    onChange?.(textarea.value);
  }

  /* ----------------------------------------------------------- events --- */

  textarea.addEventListener('input', (event) => {
    onChange?.(textarea.value);

    /* A dot is a strong signal that a column is wanted, so trigger on it
       regardless of how few characters follow. */
    if (event.data === '.') {
      openMenu('dot');
      return;
    }
    if (!menu.hidden || currentWord().length >= 2) {
      openMenu('typing');
      return;
    }
    closeMenu();
  });

  textarea.addEventListener('blur', () => setTimeout(closeMenu, 130));

  textarea.addEventListener('keydown', (event) => {
    /* Ctrl+Space: offer everything, whatever has been typed. */
    if (event.code === 'Space' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      openMenu('manual');
      return;
    }

    if (!menu.hidden && openItems().length) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        move(1);
        return;
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault();
        move(-1);
        return;
      }
      if (event.key === 'PageDown' || event.key === 'PageUp') {
        event.preventDefault();
        move(event.key === 'PageDown' ? 5 : -5);
        return;
      }
      if (event.key === 'Tab' || event.key === 'Enter') {
        event.preventDefault();
        accept(selected);
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        closeMenu();
        return;
      }
    }

    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      /* The view also listens on document for this shortcut, so without
         stopping propagation the query would run twice. */
      event.stopPropagation();
      onRun?.(textarea.value, { dryRun: event.shiftKey });
      return;
    }

    /* Tab inserts two spaces instead of leaving the field. */
    if (event.key === 'Tab' && menu.hidden) {
      event.preventDefault();
      const { selectionStart: start, selectionEnd: end } = textarea;
      textarea.value = `${textarea.value.slice(0, start)}  ${textarea.value.slice(end)}`;
      textarea.setSelectionRange(start + 2, start + 2);
      onChange?.(textarea.value);
    }
  });

  return {
    node: wrap,
    get value() {
      return textarea.value;
    },
    set value(next) {
      textarea.value = next;
      onChange?.(next);
    },
    focus: () => textarea.focus(),
    insert(text) {
      const { selectionStart: start, selectionEnd: end } = textarea;
      textarea.value = textarea.value.slice(0, start) + text + textarea.value.slice(end);
      const caret = start + text.length;
      textarea.setSelectionRange(caret, caret);
      textarea.focus();
      onChange?.(textarea.value);
    },
  };
}

/* --------------------------------------------------------- log console --- */

export function logConsole({ tall = false } = {}) {
  const node = el(`div.console${tall ? '.tall' : ''}`, { role: 'log', 'aria-live': 'polite' });
  let pinned = true;

  node.addEventListener('scroll', () => {
    pinned = node.scrollTop + node.clientHeight >= node.scrollHeight - 40;
  });

  return {
    node,
    append(lines) {
      if (!lines?.length) return;
      const fragment = document.createDocumentFragment();
      for (const line of lines) {
        fragment.append(el(`span.log-line.${line.level || 'plain'}`, `${line.text}\n`));
      }
      node.append(fragment);
      if (pinned) node.scrollTop = node.scrollHeight;
    },
    clear() {
      clear(node);
      pinned = true;
    },
    get empty() {
      return !node.firstChild;
    },
  };
}

/* ---------------------------------------------------------------- modal --- */

/**
 * A dismissible dialog appended to <body>.
 *
 * Separate from the model drawer, which is bound to a specific record. This is
 * for transient content like the dbt flow guide.
 *
 * Carries the same accessibility contract as the drawer: focus moves in on
 * open, Tab is trapped inside, Escape closes, and focus returns to whatever
 * opened it.
 */
export function modal({ title, subtitle = '', body, width = '820px',
                        returnFocusTo = null } = {}) {
  /* Prefer an explicit return target. Falling back to activeElement is only a
     convenience: it is <body> when the dialog was opened programmatically
     rather than by a real click, and focusing <body> on close would silently
     drop the keyboard position. */
  const active = document.activeElement;
  const trigger =
    returnFocusTo ||
    (active && active !== document.body && active !== document.documentElement
      ? active
      : null);

  const closeButton = el('button.btn.btn-icon', {
    'aria-label': 'Close dialog',
    onclick: () => close(),
  }, '✕');

  const panel = el(
    'div.modal',
    {
      role: 'dialog',
      'aria-modal': 'true',
      'aria-label': title,
      style: { maxWidth: width },
    },
    el(
      'header.modal-head',
      el('div', el('h2', title), subtitle ? el('p.muted.small', subtitle) : null),
      closeButton,
    ),
    el('div.modal-body', body),
  );

  const backdrop = el('div.modal-backdrop', { onclick: () => close() });
  const host = el('div.modal-host', backdrop, panel);

  function onKeydown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
      return;
    }
    if (event.key !== 'Tab') return;

    const focusable = [...panel.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]),' +
      'select:not([disabled]), textarea:not([disabled]), summary,' +
      '[tabindex]:not([tabindex="-1"])',
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

  function close() {
    document.removeEventListener('keydown', onKeydown, true);
    host.remove();
    if (trigger && document.body.contains(trigger)) trigger.focus();
  }

  document.addEventListener('keydown', onKeydown, true);
  document.body.append(host);
  closeButton.focus();

  return { close, node: panel };
}

/* ------------------------------------------------------------- key/value --- */

export function kv(pairs) {
  const list = el('dl.kv');
  for (const [key, value] of pairs) {
    if (value === null || value === undefined || value === '') continue;
    list.append(el('dt', key), el('dd', value));
  }
  return list;
}

/* ---------------------------------------------------------------- misc --- */

export function statCard(value, label, note, { kind = '' } = {}) {
  return el(
    'div.panel',
    el(
      'div.panel-body',
      el(
        'div.stat',
        el('span.stat-value', { style: kind ? { color: `var(--${kind})` } : null }, value),
        el('span.stat-label', label),
        note ? el('span.stat-note', note) : null,
      ),
    ),
  );
}

export function emptyState(title, body, action) {
  return el('div.empty', el('h3', title), el('p', body), action ? el('div.mt', action) : null);
}

export function loading(message = 'Loading…') {
  return el('div.loading', el('span.spinner'), el('span', message));
}

export function callout(title, body, kind = 'info', extra) {
  const icons = { info: 'i', warn: '!', err: '✕', ok: '✓' };
  return el(
    `div.callout.${kind}`,
    el('span.callout-ico', icons[kind] || 'i'),
    el('div', el('strong', title), body ? el('span', body) : null, extra ? el('div.mt', extra) : null),
  );
}

export function relationLine(relation) {
  return el(
    'div.row',
    { style: { gap: '6px' } },
    el('code.small.faint', plainRelation(relation)),
    el(
      'button.btn.btn-tiny.btn-ghost',
      { onclick: () => copy(plainRelation(relation), 'Relation copied'), title: 'Copy relation' },
      '⧉',
    ),
  );
}

export function confidenceChip(confidence) {
  const map = { high: 'ok', medium: 'warn', low: 'other' };
  return el(`span.chip.${map[confidence] || 'other'}`, confidence);
}

export const CATEGORY_LABELS = {
  deduplication: 'Deduplication',
  null_handling: 'Null handling',
  type_cast: 'Type cast',
  standardisation: 'Standardisation',
  categorisation: 'Categorisation',
  aggregation: 'Aggregation',
  quality_flag: 'Quality flag',
  pruning: 'Pruning',
  partitioning: 'Partitioning',
  testing: 'Testing',
};
