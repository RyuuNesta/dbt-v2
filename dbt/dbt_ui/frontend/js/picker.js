/* ==========================================================================
   picker.js - multi-select table picker over the physical warehouse inventory.

   Built as a standalone component because two features need it: the Warehouse
   tab uses it to browse and act on tables in bulk, and the diagram export will
   use it to choose what goes in a diagram. Nothing in here knows about either
   caller.

   Decisions worth knowing:

   - Selection is keyed by the qualified `dataset.table` name, not by array
     index, so it survives sorting, filtering and a reload where a table has
     been dropped or added.
   - "Select all" means all *shown*, never all *loaded*. Filtering to three rows
     and then selecting sixty-four would be a genuinely harmful surprise.
   - A view has no row count. It reports null from the backend and renders as
     "view" here, because printing "0 rows" next to a populated view is worse
     than printing nothing.
   - Selection persists to localStorage. Stale entries are pruned on load, since
     a saved selection can outlive the table it named.
   ========================================================================== */

import { ago, api, bytes, clear, el, num } from './core.js';
import { callout, layerChip, loading } from './components.js';
import { highlight, rank } from './fuzzy.js';

const STORAGE_PREFIX = 'dbtstudio.picker.';

/* Sorts offered in the header. `get` returns a primitive; nulls always sort
   last regardless of direction, because "unknown" is not "smallest". */
const SORTS = {
  name:     { label: 'Name',     get: (t) => t.qualified.toLowerCase(), numeric: false },
  rows:     { label: 'Rows',     get: (t) => t.row_count,               numeric: true },
  size:     { label: 'Size',     get: (t) => t.size_bytes,              numeric: true },
  modified: { label: 'Modified', get: (t) => t.last_modified,           numeric: true },
};

/**
 * @param {object}   options
 * @param {string}   options.storageKey   suffix under dbtstudio.picker.
 * @param {function} options.onChange     called with the selected rows
 * @param {function} [options.rowFilter]  restrict the universe up front
 * @param {string}   [options.title]
 * @param {boolean}  [options.autoLoad]
 */
export function tablePicker({
  storageKey = 'default',
  onChange,
  rowFilter = null,
  title = 'Tables',
  autoLoad = true,
} = {}) {
  const KEY = STORAGE_PREFIX + storageKey;

  const host = el('div');
  const bodyHost = el('div');
  const headHost = el('div');

  /** Every row the backend gave us, after rowFilter. */
  let universe = [];
  /** Qualified names, as a Set for O(1) membership while rendering. */
  let selected = new Set(readStored());
  let sortKey = 'name';
  let sortDir = 'asc';
  let query = '';
  let showViews = true;
  let dbtOnly = false;
  let lastPayload = null;
  /** Index of the last checkbox toggled, for shift-click range select. */
  let anchor = null;

  /* ------------------------------------------------------- persistence --- */

  function readStored() {
    try {
      const raw = localStorage.getItem(KEY);
      const parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed.filter((v) => typeof v === 'string') : [];
    } catch {
      /* Corrupt or unavailable storage must not stop the picker rendering. */
      return [];
    }
  }

  function persist() {
    try {
      localStorage.setItem(KEY, JSON.stringify([...selected]));
    } catch {
      /* Private browsing, or the quota is full. Losing persistence is
         acceptable; losing the picker is not. */
    }
  }

  /* ------------------------------------------------------------- derive --- */

  /** The rows currently visible, after every filter and the active sort. */
  function visible() {
    let rows = universe;

    if (!showViews) rows = rows.filter((t) => !t.is_view);
    if (dbtOnly) rows = rows.filter((t) => t.managed_by_dbt);

    /* Fuzzy match rather than substring: `bwac` should find
       bronze_workspace_analytics_combined. limit is the full set because the
       picker is a list, not a suggestion dropdown. */
    if (query) {
      rows = rank(query, rows, { key: (t) => t.qualified, limit: rows.length });
    }

    const sort = SORTS[sortKey] || SORTS.name;
    const factor = sortDir === 'asc' ? 1 : -1;

    /* Ranked order is already the most relevant order, so a text query keeps it
       unless the user has explicitly chosen a different sort. */
    if (query && sortKey === 'name') return rows;

    return [...rows].sort((a, b) => {
      const left = sort.get(a);
      const right = sort.get(b);
      /* Nulls last in both directions: a view has no row count, and floating it
         to the top of "most rows" would be nonsense. */
      if (left === null || left === undefined) return 1;
      if (right === null || right === undefined) return -1;
      if (left < right) return -1 * factor;
      if (left > right) return 1 * factor;
      return a.qualified.localeCompare(b.qualified);
    });
  }

  function selectedRows() {
    return universe.filter((t) => selected.has(t.qualified));
  }

  function announce() {
    persist();
    paintHead();
    onChange?.(selectedRows());
  }

  /* --------------------------------------------------------------- head --- */

  function paintHead() {
    const shown = visible();
    const shownSelected = shown.filter((t) => selected.has(t.qualified)).length;
    const allShownSelected = shown.length > 0 && shownSelected === shown.length;

    clear(headHost).append(
      el(
        'div.row.wrap.between',
        { style: { gap: '10px' } },
        el(
          'div.row.wrap',
          { style: { gap: '6px' } },
          el(`span.chip${selected.size ? '.info' : ''}`,
             `${selected.size} selected`),
          el('span.chip', `${shown.length} shown`),
          universe.length !== shown.length
            ? el('span.chip.faint', `${universe.length} total`)
            : null,
        ),
        el(
          'div.row.wrap',
          { style: { gap: '6px' } },
          el(
            'button.btn.btn-tiny',
            {
              disabled: !shown.length,
              /* Says "shown" out loud so nobody can mistake it for everything. */
              onclick: () => {
                for (const table of shown) {
                  if (allShownSelected) selected.delete(table.qualified);
                  else selected.add(table.qualified);
                }
                paintBody();
                announce();
              },
            },
            allShownSelected ? `Deselect ${shown.length} shown` : `Select ${shown.length} shown`,
          ),
          el(
            'button.btn.btn-tiny.btn-ghost',
            {
              disabled: !selected.size,
              onclick: () => {
                selected.clear();
                paintBody();
                announce();
              },
            },
            'Clear all',
          ),
        ),
      ),
    );
  }

  /* ------------------------------------------------------------ filters --- */

  const search = el('input.input', {
    type: 'search',
    placeholder: 'Filter tables…',
    'aria-label': 'Filter tables by name',
    style: { maxWidth: '260px' },
    oninput: (event) => {
      query = event.target.value.trim();
      paintBody();
      paintHead();
    },
  });

  const sortSelect = el('select.select', {
    'aria-label': 'Sort tables by',
    style: { maxWidth: '150px' },
    onchange: (event) => {
      sortKey = event.target.value;
      /* Biggest and most-recent first are what people actually want from these
         columns, so flip the default direction for them. */
      sortDir = SORTS[sortKey].numeric ? 'desc' : 'asc';
      paintBody();
      paintHead();
    },
  });
  for (const [key, sort] of Object.entries(SORTS)) {
    sortSelect.append(el('option', { value: key, selected: key === sortKey }, sort.label));
  }

  const dirButton = el(
    'button.btn.btn-tiny',
    {
      title: 'Reverse the sort order',
      onclick: () => {
        sortDir = sortDir === 'asc' ? 'desc' : 'asc';
        dirButton.textContent = sortDir === 'asc' ? '↑ asc' : '↓ desc';
        paintBody();
      },
    },
    '↑ asc',
  );

  function toggleChip(label, initial, onToggle, hint) {
    const input = el('input', { type: 'checkbox' });
    input.checked = initial;
    input.addEventListener('change', () => {
      onToggle(input.checked);
      paintBody();
      paintHead();
    });
    return el('label.switch', { title: hint || '' }, input, el('span', label));
  }

  /* --------------------------------------------------------------- body --- */

  const bodyRows = el('tbody');

  function paintBody() {
    const shown = visible();
    clear(bodyRows);

    if (!shown.length) {
      bodyRows.append(
        el('tr', el('td', { colspan: '6' },
          el('div.empty', el('p', universe.length
            ? 'Nothing matches these filters.'
            : 'No tables found in the permitted datasets.')))),
      );
      return;
    }

    shown.forEach((table, index) => {
      const isSelected = selected.has(table.qualified);
      const box = el('input', {
        type: 'checkbox',
        'aria-label': `Select ${table.qualified}`,
      });
      box.checked = isSelected;

      const rowsCell = table.is_view
        ? el('span.faint.tiny', 'view')
        : el('span', num(table.row_count));

      const sizeCell = table.is_view
        ? el('span.faint.tiny', '—')
        : el('span', bytes(table.size_bytes));

      const row = el(
          isSelected ? 'tr.is-picked' : 'tr',
          el('td.pick-cell', box),
          el(
            'td',
            el(
              'div.pick-name',
              /* rank() already attached _positions to the rows it returned, so
                 highlighting the fuzzy match costs nothing extra. It makes it
                 obvious why a row survived a non-obvious query. */
              query && table._positions
                ? el('span.mono.small', highlight(table.qualified, table._positions))
                : el('span.mono.small', table.qualified),
            ),
            el(
              'div.row.wrap',
              { style: { gap: '4px', marginTop: '3px' } },
              table.managed_by_dbt
                ? layerChip(table.layer)
                : el('span.chip.tiny', { title: 'Not built by dbt' }, 'foreign'),
              table.is_view ? el('span.chip.tiny', 'view') : null,
              table.managed_by_dbt && table.test_count === 0
                ? el('span.chip.warn.tiny', { title: 'No tests' }, 'untested')
                : null,
            ),
          ),
          el('td.num', rowsCell),
          el('td.num', sizeCell),
          el(
            'td.small.faint',
            {
              /* last_modified is epoch milliseconds; ago() takes seconds. */
              title: table.last_modified
                ? new Date(table.last_modified).toLocaleString()
                : 'unknown',
            },
            table.last_modified ? ago(table.last_modified / 1000) : '—',
          ),
          el('td.small.faint', table.dataset),
      );

      box.addEventListener('click', (event) => {
        /* Shift-click selects the range since the last click, which is what
           makes picking 20 tables out of 64 bearable. */
        if (event.shiftKey && anchor !== null) {
          const [from, to] = anchor < index ? [anchor, index] : [index, anchor];
          const target = box.checked;
          for (let i = from; i <= to; i += 1) {
            if (target) selected.add(shown[i].qualified);
            else selected.delete(shown[i].qualified);
          }
          /* A range touches rows other than this one, so the whole body has to
             be repainted to show them. */
          paintBody();
        } else {
          if (box.checked) selected.add(table.qualified);
          else selected.delete(table.qualified);
          /* Toggle just this row rather than repainting the table. A full
             repaint on every tick would throw away focus and scroll position,
             which makes selecting several rows in a row miserable. */
          row.classList.toggle('is-picked', box.checked);
        }
        anchor = index;
        announce();
      });

      bodyRows.append(row);
    });
  }

  /* --------------------------------------------------------------- load --- */

  async function load(refresh = false) {
    clear(bodyHost).append(loading(refresh
      ? 'Re-reading table metadata…'
      : 'Reading row counts and sizes from BigQuery…'));
    clear(headHost);

    let payload;
    try {
      payload = await api.inventory(refresh);
    } catch (error) {
      clear(bodyHost).append(
        callout('Could not read the warehouse inventory', error.message, 'err',
          el('div',
            error.detail ? el('pre.code-block', error.detail) : null,
            el('button.btn.btn-tiny.mt', { onclick: () => load(true) }, 'Retry'))),
      );
      return;
    }

    lastPayload = payload;
    universe = (payload.tables || []).filter((t) => (rowFilter ? rowFilter(t) : true));

    /* A saved selection can name a table that has since been dropped, or one
       the current filter excludes. Prune rather than carry an invisible
       selection that silently affects whatever acts on it. */
    const live = new Set(universe.map((t) => t.qualified));
    const before = selected.size;
    selected = new Set([...selected].filter((name) => live.has(name)));
    const pruned = before - selected.size;
    if (pruned > 0) persist();

    clear(bodyHost).append(
      payload.error
        ? callout('Row counts unavailable', payload.error, 'warn',
            el('p.tiny.faint',
              'The table list is still accurate. Only the counts and sizes are missing.'))
        : null,
      pruned > 0
        ? callout(
            `${pruned} saved selection${pruned === 1 ? '' : 's'} no longer exist`,
            'Those tables have been dropped or renamed since you last used this list, so they have been removed from your selection.',
            'info')
        : null,
      el(
        'div.row.wrap.mb',
        { style: { gap: '8px', alignItems: 'center' } },
        search,
        el('div.row', { style: { gap: '5px' } }, sortSelect, dirButton),
        toggleChip('Views', showViews, (on) => { showViews = on; },
                   'Views have no row count'),
        toggleChip('Only dbt models', dbtOnly, (on) => { dbtOnly = on; },
                   'Hide tables dbt does not build'),
      ),
      el(
        'div.table-wrap.pick-table',
        { style: { maxHeight: '54vh' } },
        el(
          'table.data.compact',
          el('thead', el('tr',
            el('th', { style: { width: '1%' } }, ''),
            el('th', 'Table'),
            el('th.num', 'Rows'),
            el('th.num', 'Size'),
            el('th', 'Modified'),
            el('th', 'Dataset'))),
          bodyRows,
        ),
      ),
      el(
        'p.tiny.faint',
        { style: { margin: '9px 0 0', lineHeight: '1.55' } },
        `${payload.table_count} tables across ${(payload.datasets || []).join(', ')}. ` +
        `${payload.managed_count} built by dbt, ${payload.table_count - payload.managed_count} pre-existing. ` +
        'Row counts come from free table metadata, not a scan, so nothing is billed. ' +
        'Shift-click a checkbox to select a range.',
      ),
    );

    paintBody();
    paintHead();
    onChange?.(selectedRows());
  }

  /* ---------------------------------------------------------- assemble --- */

  host.append(
    el(
      'div.row.wrap.between.mb',
      { style: { gap: '10px' } },
      el('div.stat-label', title || 'Tables'),
      el(
        'div.row',
        { style: { gap: '6px' } },
        headHost,
        el('button.btn.btn-tiny.btn-ghost',
           { title: 'Re-read metadata from BigQuery', onclick: () => load(true) },
           '⟳'),
      ),
    ),
    bodyHost,
  );

  if (autoLoad) load();

  return {
    node: host,
    /** The selected rows, in the inventory's own order. */
    selected: () => selectedRows(),
    /** Just the qualified names. */
    selectedNames: () => [...selected],
    count: () => selected.size,
    setSelected(names) {
      selected = new Set(names || []);
      paintBody();
      announce();
    },
    reload: (refresh = true) => load(refresh),
    payload: () => lastPayload,
  };
}
