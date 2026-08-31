/* ==========================================================================
   catalog.js - searchable model list, lineage graph, and warehouse browser.
   ========================================================================== */

import {
  api, bytes, clear, copy, el, layerRank, num, plainRelation, shortRelation,
  state, toast,
} from '../core.js';
import {
  callout, emptyState, layerChip, loading, materializationChip, tabs,
} from '../components.js';
import { tablePicker } from '../picker.js';
import { openModel } from './drawer.js';

export const meta = {
  title: 'Catalog',
  subtitle: 'Every model, the lineage graph, and what is actually in BigQuery',
};

export function render(navigate) {
  const view = tabs([
    { label: 'Models', count: (state.models || []).length, render: () => modelsPanel(navigate) },
    { label: 'Lineage', render: () => el('div.panel-body', loading('Building the graph…')) },
    { label: 'Sources', count: (state.sources || []).length, render: () => sourcesPanel() },
    { label: 'Warehouse', render: () => el('div.panel-body', loading('Reading table metadata…')) },
  ], {
    onChange: (index) => {
      if (index === 1) loadGraph(view);
      if (index === 3) loadWarehouse(view, navigate);
    },
  });

  return el('div.panel', view.node);
}

/* --------------------------------------------------------------- models --- */

function modelsPanel(navigate) {
  const rows = el('tbody');

  const search = el('input.input', {
    type: 'search',
    placeholder: 'Search name or description…',
    style: { maxWidth: '300px' },
    oninput: (event) => paint(event.target.value),
  });

  function paint(term = '') {
    const needle = term.trim().toLowerCase();
    const models = (state.models || []).filter(
      (model) =>
        !needle ||
        model.name.toLowerCase().includes(needle) ||
        (model.description || '').toLowerCase().includes(needle),
    );

    clear(rows);
    if (!models.length) {
      rows.append(el('tr', el('td', { colspan: '8' }, el('div.empty', el('p', 'Nothing matches.')))));
      return;
    }

    for (const model of models) {
      const outOfScope = model.in_scope === false;
      rows.append(
        el(
          outOfScope ? 'tr.out-of-scope' : 'tr',
          el('td', layerChip(model.layer)),
          el(
            'td',
            el(
              'button.btn.btn-tiny.btn-ghost',
              {
                onclick: () => {
                  if (outOfScope) {
                    toast(`${model.name} is outside the permitted dataset scope.`, {
                      kind: 'warn',
                      detail: `Dataset '${model.dataset}' is not readable by this instance.`,
                    });
                    return;
                  }
                  openModel(model.name);
                },
                style: { fontFamily: 'var(--mono)' },
                title: outOfScope ? `Out of scope (${model.dataset})` : model.name,
              },
              model.name,
            ),
            outOfScope ? el('span.chip.err', { style: { marginLeft: '6px' } }, 'out of scope') : null,
          ),
          el('td', materializationChip(model.materialized)),
          el('td.num', num(model.column_count)),
          el(
            'td.num',
            model.test_count
              ? el('span.chip.ok', model.test_count)
              : el('span.chip.err', '0'),
          ),
          el(
            'td',
            model.has_description
              ? el('span.chip.ok', `${model.documented_columns}/${model.column_count}`)
              : el('span.chip.warn', 'undocumented'),
          ),
          el('td.mono.small.faint', shortRelation(model.relation_name)),
          el(
            'td',
            el(
              'div.row',
              { style: { gap: '4px' } },
              el(
                'button.btn.btn-tiny',
                {
                  title: 'Query in workbench',
                  onclick: () =>
                    navigate('workbench', {
                      sql: `select *\nfrom {{ ref('${model.name}') }}\nlimit 100`,
                    }),
                },
                '▶',
              ),
              el(
                'button.btn.btn-tiny',
                { title: 'Documentation', onclick: () => navigate('schema', { model: model.name }) },
                '☰',
              ),
              el(
                'button.btn.btn-tiny',
                { title: 'Build this model', onclick: () => navigate('runs', { select: model.name }) },
                '⚡',
              ),
            ),
          ),
        ),
      );
    }
  }

  paint();

  return el(
    'div',
    el('div.panel-body', el('div.row.between', search, el('span.small.faint', `${(state.models || []).length} nodes`))),
    el(
      'div.table-wrap',
      { style: { maxHeight: '62vh' } },
      el(
        'table.data',
        el(
          'thead',
          el(
            'tr',
            el('th', 'Layer'),
            el('th', 'Name'),
            el('th', 'Materialized'),
            el('th', 'Cols'),
            el('th', 'Tests'),
            el('th', 'Docs'),
            el('th', 'Relation'),
            el('th', ''),
          ),
        ),
        rows,
      ),
    ),
  );
}

/* -------------------------------------------------------------- lineage --- */

async function loadGraph(view) {
  let graph;
  try {
    graph = await api.graph();
  } catch (error) {
    view.setPanel(1, el('div.panel-body', callout('Could not load the graph', error.message, 'err')));
    return;
  }

  if (!graph.nodes?.length) {
    view.setPanel(1, emptyState('Nothing to draw', 'The manifest contains no models.'));
    return;
  }

  view.setPanel(1, el('div', el('div.panel-body', el('span.small.faint',
    `${graph.nodes.length} nodes, ${graph.edges.length} edges. Columns are medallion layers; click a node to inspect it.`)),
    el('div.lineage', drawGraph(graph))));
}

const LAYER_COLOURS = {
  source: '#6f7d8f',
  seed: '#7c8b9d',
  bronze: '#c8813c',
  silver: '#9fb3c8',
  gold: '#e0b040',
  other: '#4a5768',
};

/**
 * Layered DAG drawing.
 *
 * Nodes are placed in columns by medallion layer, which is both meaningful to
 * the team and avoids needing a real graph layout algorithm. Edges are cubic
 * beziers so crossings stay readable.
 */
function drawGraph(graph) {
  const NS = 'http://www.w3.org/2000/svg';
  const NODE_W = 190;
  const NODE_H = 34;
  const GAP_Y = 16;
  const GAP_X = 96;
  const PAD = 14;

  const columns = new Map();
  for (const node of graph.nodes) {
    const rank = layerRank(node.layer);
    if (!columns.has(rank)) columns.set(rank, []);
    columns.get(rank).push(node);
  }

  const ranks = [...columns.keys()].sort((a, b) => a - b);
  const positions = new Map();
  let maxRows = 0;

  ranks.forEach((rank, columnIndex) => {
    const nodes = columns.get(rank).sort((a, b) => a.name.localeCompare(b.name));
    maxRows = Math.max(maxRows, nodes.length);
    nodes.forEach((node, rowIndex) => {
      positions.set(node.id, {
        x: PAD + columnIndex * (NODE_W + GAP_X),
        y: PAD + rowIndex * (NODE_H + GAP_Y),
        node,
      });
    });
  });

  const width = PAD * 2 + ranks.length * NODE_W + (ranks.length - 1) * GAP_X;
  const height = PAD * 2 + maxRows * NODE_H + Math.max(0, maxRows - 1) * GAP_Y;

  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('width', String(width));
  svg.setAttribute('height', String(Math.max(height, 120)));
  svg.setAttribute('viewBox', `0 0 ${width} ${Math.max(height, 120)}`);

  /* column headers */
  ranks.forEach((rank, columnIndex) => {
    const label = columns.get(rank)[0]?.layer || 'other';
    const text = document.createElementNS(NS, 'text');
    text.setAttribute('x', String(PAD + columnIndex * (NODE_W + GAP_X)));
    text.setAttribute('y', '8');
    text.setAttribute('fill', LAYER_COLOURS[label] || '#64748b');
    text.setAttribute('font-size', '10');
    text.setAttribute('font-family', 'var(--mono)');
    text.textContent = label.toUpperCase();
    svg.append(text);
  });

  /* edges first so nodes sit on top */
  const edgeLayer = document.createElementNS(NS, 'g');
  for (const edge of graph.edges) {
    const from = positions.get(edge.source);
    const to = positions.get(edge.target);
    if (!from || !to) continue;

    const x1 = from.x + NODE_W;
    const y1 = from.y + NODE_H / 2 + 12;
    const x2 = to.x;
    const y2 = to.y + NODE_H / 2 + 12;
    const mid = (x1 + x2) / 2;

    const path = document.createElementNS(NS, 'path');
    path.setAttribute('d', `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`);
    path.setAttribute('class', 'ln-edge');
    path.dataset.from = edge.source;
    path.dataset.to = edge.target;
    edgeLayer.append(path);
  }
  svg.append(edgeLayer);

  /* nodes */
  for (const { x, y, node } of positions.values()) {
    const group = document.createElementNS(NS, 'g');
    group.setAttribute('class', 'ln-node');
    group.setAttribute('transform', `translate(${x}, ${y + 12})`);
    group.style.cursor = node.resource_type === 'source' ? 'default' : 'pointer';

    const rect = document.createElementNS(NS, 'rect');
    rect.setAttribute('width', String(NODE_W));
    rect.setAttribute('height', String(NODE_H));
    rect.setAttribute('rx', '6');
    rect.setAttribute('fill', LAYER_COLOURS[node.layer] || LAYER_COLOURS.other);
    group.append(rect);

    const label = document.createElementNS(NS, 'text');
    label.setAttribute('x', '10');
    label.setAttribute('y', '21');
    const maxChars = 24;
    label.textContent =
      node.name.length > maxChars ? `${node.name.slice(0, maxChars - 1)}…` : node.name;
    group.append(label);

    if (node.test_count) {
      const badge = document.createElementNS(NS, 'text');
      badge.setAttribute('x', String(NODE_W - 8));
      badge.setAttribute('y', '21');
      badge.setAttribute('text-anchor', 'end');
      badge.setAttribute('font-size', '9');
      badge.textContent = `${node.test_count}✓`;
      group.append(badge);
    }

    const title = document.createElementNS(NS, 'title');
    title.textContent = `${node.name}\n${node.layer} · ${node.materialized} · ${node.test_count} tests`;
    group.append(title);

    group.addEventListener('mouseenter', () => highlight(node.id, true));
    group.addEventListener('mouseleave', () => highlight(node.id, false));
    if (node.resource_type !== 'source') {
      group.addEventListener('click', () => openModel(node.name));
    }

    svg.append(group);
  }

  function highlight(id, on) {
    for (const path of edgeLayer.children) {
      if (path.dataset.from === id || path.dataset.to === id) {
        path.classList.toggle('hot', on);
      }
    }
  }

  return svg;
}

/* -------------------------------------------------------------- sources --- */

function sourcesPanel() {
  const sources = state.boot?.stats ? state.sourceDetails || [] : [];

  if (!sources.length) {
    return emptyState(
      'No sources declared',
      'This project reads from seeds and models only. Declaring the upstream BigQuery tables as sources in a models/*.yml file gives you freshness checks and puts the real origin in the lineage graph.',
    );
  }

  return el(
    'div.table-wrap',
    el(
      'table.data',
      el('thead', el('tr', el('th', 'Source'), el('th', 'Table'), el('th', 'Relation'), el('th', 'Cols'), el('th', 'Freshness'))),
      el(
        'tbody',
        ...sources.map((source) =>
          el(
            'tr',
            el('td.mono', source.source_name),
            el('td.mono', source.name),
            el('td.mono.small.faint', shortRelation(source.relation_name)),
            el('td.num', num(source.column_count)),
            el('td', source.freshness ? el('span.chip.ok', 'configured') : el('span.chip', 'none')),
          ),
        ),
      ),
    ),
  );
}

/* ------------------------------------------------------------ warehouse --- */

/*
 * The Warehouse tab is a multi-select inventory rather than a dataset drill-down.
 *
 * The old two-pane browser made you click a dataset, wait, then read a table
 * list with no row counts. That is the wrong shape for this warehouse: 62 of the
 * 64 in-scope tables are not built by dbt, so the common task is finding one
 * among many and doing something with a handful of them - not exploring one
 * dataset at a time. A flat, filterable, multi-select list with sizes answers
 * "which tables actually matter here" in one screen.
 */
function loadWarehouse(view, navigate) {
  const actionHost = el('div');

  const picker = tablePicker({
    storageKey: 'warehouse',
    title: 'Warehouse inventory',
    onChange: (rows) => paintActions(rows),
  });

  function paintActions(rows) {
    if (!rows.length) {
      clear(actionHost).append(
        el('p.tiny.faint', { style: { margin: 0, lineHeight: '1.55' } },
          'Tick one or more tables to copy their names, or open one to inspect its columns.'),
      );
      return;
    }

    const names = rows.map((row) => row.qualified);
    const managed = rows.filter((row) => row.managed_by_dbt);
    const single = rows.length === 1 ? rows[0] : null;

    clear(actionHost).append(
      el(
        'div.row.wrap',
        { style: { gap: '7px', alignItems: 'center' } },
        el('span.small.muted',
           `${rows.length} selected${managed.length ? ` · ${managed.length} built by dbt` : ''}`),

        el(
          'button.btn.btn-tiny',
          {
            onclick: () => copy(names.join('\n'), `${names.length} names copied`),
            title: 'One qualified name per line',
          },
          '⧉ Copy names',
        ),

        el(
          'button.btn.btn-tiny',
          {
            /* One statement per table, not a UNION. Selected tables rarely share
               a schema, so a union would just fail to compile - and even when it
               compiled it would answer a question nobody asked. A scratchpad of
               ready-to-run previews is what is actually wanted.

               Every one is capped, because a 5-million-row table is one careless
               Ctrl+Enter away otherwise. */
            onclick: () => {
              const sql = rows
                .map((row) => {
                  const size = row.is_view
                    ? 'view'
                    : `${num(row.row_count)} rows, ${bytes(row.size_bytes)}`;
                  return `-- ${row.qualified}  (${size})\nselect *\nfrom ${row.relation}\nlimit 100;`;
                })
                .join('\n\n');
              navigate('workbench', { sql });
            },
            title: 'Open a capped preview of each selected table in the workbench',
          },
          `▶ Preview ${rows.length} in workbench`,
        ),

        single
          ? el(
              'button.btn.btn-tiny',
              {
                onclick: () => {
                  if (single.managed_by_dbt && single.model) openModel(single.model);
                  else toast(`${single.qualified} is not built by dbt.`, {
                    kind: 'info',
                    detail: 'There is no model to open. Use the workbench to query it, '
                          + 'or the Documentation page to describe it.',
                  });
                },
              },
              single.managed_by_dbt ? '↗ Open model' : '↗ Details',
            )
          : null,

        managed.length
          ? el(
              'button.btn.btn-tiny',
              {
                onclick: () => {
                  /* Space-separated model names are a valid dbt selector, so the
                     Build page can act on exactly this set. Only dbt-managed
                     rows are offered: there is nothing to build for a foreign
                     table, and pretending otherwise would produce a run that
                     fails on a name dbt has never heard of. */
                  const selector = managed.map((row) => row.model).filter(Boolean).join(' ');
                  navigate('runs', { select: selector });
                },
                title: 'Build only the selected models',
              },
              `⚡ Build ${managed.length} model${managed.length === 1 ? '' : 's'}`,
            )
          : null,
      ),
    );
  }

  view.setPanel(
    3,
    el(
      'div.panel-body',
      callout(
        'Read-only inventory of the permitted datasets',
        'Row counts, sizes and modification times come from free table metadata, '
        + 'so opening this page costs nothing and scans nothing. Views have no row '
        + 'count, which is why they show "view" rather than zero.',
        'info',
      ),
      el('div.mt', picker.node),
      el('div.sticky-actions.mt', actionHost),
    ),
  );

  paintActions([]);
}
