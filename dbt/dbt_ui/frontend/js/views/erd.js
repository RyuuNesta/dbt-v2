/* ==========================================================================
   erd.js - Entity Relationship Diagram, hand-rolled SVG, zero dependencies.

   The lineage graph on the Catalog page answers "what builds what". This page
   answers "how do these tables join, and how sure are we". Same medallion
   colour palette (imported from catalog.js so the two cannot drift), different
   question, different interactions: pan, zoom, drag, search, and five export
   formats, none of which touch a third-party library.

   Layout: a force-free layered placement, columns by medallion layer exactly
   like the lineage graph, then a handful of relaxation passes that nudge each
   node towards the vertical centre of the nodes it connects to. This is not a
   real force-directed layout - there is no repulsion, no simulation loop, no
   physics - it is a cheap approximation that keeps connected tables roughly
   level with each other, which is what actually makes a join diagram readable.
   Positions are cached per diagram key, so relaxation runs once and dragging a
   node afterwards persists for the session.

   Rendering rebuilds the whole SVG on every state change rather than patching
   it incrementally. At the table counts this project has (dozens, not
   thousands), a full rebuild is imperceptible and immensely simpler than
   tracking incremental diffs against pan/zoom/filter state.
   ========================================================================== */

import {
  $, api, clear, copy, download, el, num, reportError, state, toast,
} from '../core.js';
import {
  callout, emptyState, loading, modal, tabs,
} from '../components.js';
import { tablePicker } from '../picker.js';
import { LAYER_COLOURS } from './catalog.js';

export const meta = {
  title: 'ERD',
  subtitle: 'How your tables relate, detected from tests, refs and naming',
};

const NS = 'http://www.w3.org/2000/svg';

const NODE_W = 230;
const HEADER_H = 26;
const ROW_H = 18;
const MAX_ROWS_SHOWN = 8;
const COL_GAP_X = 150;
const ROW_GAP_Y = 40;
const PAD = 40;

const ZOOM_MIN = 0.25;
const ZOOM_MAX = 2.5;
const ZOOM_STEP = 1.18;

const KIND_LABEL = { declared: 'tested', lineage: 'ref()', inferred: 'naming', constraint: 'BigQuery' };
const KIND_DASH = { declared: '0', lineage: '0', inferred: '5 4', constraint: '2 3' };

export function render(navigate) {
  const host = el('div.erd-page');

  /* Session-scoped so re-entering the page does not re-ask, but a fresh load
     of the app starts from the defaults. */
  const options = {
    inScopeOnly: false,
    includeStaging: true,
    includeSources: true,
    counts: false,
    tables: [],
  };

  let erd = null;
  let query = '';
  let keysOnly = false;
  let focusId = null;              // upstream/downstream highlight anchor
  let selectedId = null;           // side panel subject
  /** Positions keyed by table id, persisted for the life of the page. */
  const positions = new Map();
  let laidOutFor = '';             // signature of the last table set laid out

  /* Pan/zoom state, applied as a single SVG transform on a <g> wrapper rather
     than to individual nodes, so it costs one attribute write per frame. */
  const view = { x: 0, y: 0, scale: 1 };

  const canvasHost = el('div.erd-canvas', { tabindex: '0' });
  const sideHost = el('div.erd-side');
  const statusHost = el('div.erd-status');
  const legendHost = el('div.erd-legend');

  /* ------------------------------------------------------------- scope --- */

  const scopePicker = tablePicker({
    storageKey: 'erd-scope',
    title: 'Custom table selection',
    autoLoad: false,
    onChange: (rows) => {
      options.tables = rows.map((row) => row.qualified);
      load();
    },
  });
  let scopeMode = 'all';   // all | custom

  function scopeControls() {
    const allBtn = el(
      'button.btn.btn-tiny',
      {
        class: scopeMode === 'all' ? 'btn active' : 'btn',
        onclick: () => {
          scopeMode = 'all';
          options.tables = [];
          paintToolbar();
          load();
        },
      },
      'Entire project',
    );
    const customBtn = el(
      'button.btn.btn-tiny',
      {
        class: scopeMode === 'custom' ? 'btn active' : 'btn',
        onclick: () => {
          scopeMode = 'custom';
          paintToolbar();
          openScopeModal();
        },
      },
      scopeMode === 'custom' && options.tables.length
        ? `Custom (${options.tables.length})`
        : 'Choose tables…',
    );
    return el('div.row', { style: { gap: '6px' } }, allBtn, customBtn);
  }

  function openScopeModal() {
    const dialog = modal({
      title: 'Choose which tables to diagram',
      subtitle: 'Reuses the same picker as the Warehouse tab and the Cleanup Advisor. Row counts help you tell a real table from a stray one.',
      width: '900px',
      body: scopePicker.node,
      returnFocusTo: document.activeElement,
    });
    if (!scopePicker.payload()) scopePicker.reload();
    else scopePicker.setSelected(options.tables);
    dialog.node.querySelector('.modal-body').append(
      el(
        'div.row.wrap.mt',
        { style: { gap: '8px' } },
        el('button.btn.btn-primary.btn-tiny', {
          onclick: () => { dialog.close(); load(); },
        }, `Diagram ${scopePicker.count()} table${scopePicker.count() === 1 ? '' : 's'}`),
      ),
    );
  }

  /* -------------------------------------------------------------- load --- */

  async function load() {
    clear(canvasHost).append(loading('Reading the manifest and detecting relationships…'));
    clear(sideHost);
    selectedId = null;

    try {
      erd = await api.erd({
        inScopeOnly: options.inScopeOnly,
        includeStaging: options.includeStaging,
        includeSources: options.includeSources,
        counts: options.counts,
        tables: scopeMode === 'custom' ? options.tables : [],
      });
    } catch (error) {
      reportError(error, 'Building the ERD');
      clear(canvasHost).append(
        el('div.panel', el('div.panel-body', callout('Could not build the diagram', error.message, 'err'))),
      );
      return;
    }

    if (!erd.tables.length) {
      clear(canvasHost).append(
        emptyState(
          'Nothing to draw',
          scopeMode === 'custom'
            ? 'None of the selected tables matched. Pick at least one.'
            : 'The manifest has no models, seeds or sources yet.',
        ),
      );
      clear(statusHost);
      return;
    }

    layoutIfNeeded();
    paintStatus();
    paintLegend();
    paintCanvas();
  }

  /* ------------------------------------------------------------ layout --- */

  /**
   * Column-by-layer placement, then a few relaxation passes.
   *
   * Re-run only when the table *set* changes (a stable sort of ids), not on
   * every render, so a node you dragged stays where you put it while you pan,
   * zoom, search or open the side panel.
   */
  function layoutIfNeeded() {
    const signature = erd.tables.map((t) => t.id).sort().join('|');
    if (signature === laidOutFor) {
      // Still seed any table the previous layout did not know about (a
      // widened scope), so nothing renders at (0, 0) on top of everything else.
      for (const table of erd.tables) {
        if (!positions.has(table.id)) positions.set(table.id, fallbackPosition(table));
      }
      return;
    }
    laidOutFor = signature;

    const byLayerOrder = new Map();
    for (const table of erd.tables) {
      const key = table.layer_order;
      if (!byLayerOrder.has(key)) byLayerOrder.set(key, []);
      byLayerOrder.get(key).push(table);
    }

    const columns = [...byLayerOrder.keys()].sort((a, b) => a - b);
    const next = new Map();

    columns.forEach((layerOrder, columnIndex) => {
      const tables = byLayerOrder.get(layerOrder).sort((a, b) => a.name.localeCompare(b.name));
      tables.forEach((table, rowIndex) => {
        next.set(table.id, {
          x: PAD + columnIndex * (NODE_W + COL_GAP_X),
          y: PAD + rowIndex * (nodeHeight(table) + ROW_GAP_Y),
        });
      });
    });

    // Relaxation: pull each node toward the mean y of what it connects to,
    // column by column left-to-right then right-to-left, a few times. No
    // repulsion term is needed because rows within a column never overlap by
    // construction - only their vertical order changes.
    const edgesByTable = new Map();
    for (const rel of erd.relationships) {
      if (!edgesByTable.has(rel.from_table)) edgesByTable.set(rel.from_table, []);
      if (!edgesByTable.has(rel.to_table)) edgesByTable.set(rel.to_table, []);
      edgesByTable.get(rel.from_table).push(rel.to_table);
      edgesByTable.get(rel.to_table).push(rel.from_table);
    }

    for (let pass = 0; pass < 4; pass += 1) {
      const order = pass % 2 === 0 ? columns : [...columns].reverse();
      for (const layerOrder of order) {
        const tables = byLayerOrder.get(layerOrder)
          .slice()
          .sort((a, b) => next.get(a.id).y - next.get(b.id).y);

        const wanted = tables.map((table) => {
          const neighbours = (edgesByTable.get(table.id) || [])
            .map((id) => next.get(id)?.y)
            .filter((y) => y !== undefined);
          const target = neighbours.length
            ? neighbours.reduce((a, b) => a + b, 0) / neighbours.length
            : next.get(table.id).y;
          return { table, target };
        });

        wanted.sort((a, b) => a.target - b.target);
        let cursor = PAD;
        for (const { table } of wanted) {
          next.set(table.id, { x: next.get(table.id).x, y: cursor });
          cursor += nodeHeight(table) + ROW_GAP_Y;
        }
      }
    }

    positions.clear();
    for (const [id, point] of next) positions.set(id, point);
  }

  function fallbackPosition(table) {
    const existing = [...positions.values()];
    const maxY = existing.length ? Math.max(...existing.map((p) => p.y)) : 0;
    return { x: PAD, y: maxY + nodeHeight(table) + ROW_GAP_Y };
  }

  function nodeHeight(table) {
    const shown = visibleColumns(table).length;
    return HEADER_H + Math.min(shown, MAX_ROWS_SHOWN) * ROW_H
      + (shown > MAX_ROWS_SHOWN ? ROW_H : 0) + 8;
  }

  function visibleColumns(table) {
    if (!keysOnly) return table.columns;
    return table.columns.filter((c) => c.is_primary || c.looks_like_key);
  }

  /* ------------------------------------------------------------- search --- */

  function matchesQuery(table) {
    if (!query) return true;
    const needle = query.toLowerCase();
    return table.name.toLowerCase().includes(needle)
      || table.columns.some((c) => c.name.toLowerCase().includes(needle));
  }

  /** Tables to actually draw: the diagram's own out-of-scope dimming stays,
      but a text search narrows what is drawn at all. */
  function drawnTables() {
    return erd.tables.filter(matchesQuery);
  }

  function lineageSet(anchorId) {
    if (!anchorId) return null;
    const upstream = new Set();
    const downstream = new Set();
    const parents = new Map();
    const children = new Map();

    for (const rel of erd.relationships) {
      if (!parents.has(rel.to_table)) parents.set(rel.to_table, []);
      parents.get(rel.to_table).push(rel.from_table);
      if (!children.has(rel.from_table)) children.set(rel.from_table, []);
      children.get(rel.from_table).push(rel.to_table);
    }

    const walk = (start, map, into) => {
      const stack = [start];
      while (stack.length) {
        const current = stack.pop();
        for (const next of map.get(current) || []) {
          if (into.has(next)) continue;
          into.add(next);
          stack.push(next);
        }
      }
    };
    walk(anchorId, parents, upstream);
    walk(anchorId, children, downstream);

    /* Every table on the highlighted path, including the anchor itself. An
       edge belongs to the highlight when both its ends are in this set - that
       is what "part of the lineage" means for an edge, as opposed to a node. */
    const reachable = new Set([anchorId, ...upstream, ...downstream]);

    return { upstream, downstream, reachable };
  }

  /* -------------------------------------------------------------- paint --- */

  function paintStatus() {
    const stats = erd.stats;
    clear(statusHost).append(
      el('span.chip', `${stats.table_count} tables`),
      el('span.chip', `${stats.relationship_count} relationships`),
      stats.by_kind.declared ? el('span.chip.ok', `${stats.by_kind.declared} tested`) : null,
      stats.by_kind.lineage ? el('span.chip.info', `${stats.by_kind.lineage} ref()`) : null,
      stats.by_kind.inferred ? el('span.chip.warn', `${stats.by_kind.inferred} naming-only`) : null,
      stats.by_kind.constraint ? el('span.chip.ok', `${stats.by_kind.constraint} BigQuery`) : null,
      stats.keyless_tables.length
        ? el('span.chip.err', { title: stats.keyless_tables.join(', ') },
             `${stats.keyless_tables.length} without a key`)
        : null,
    );
    if (erd.warnings?.length) {
      statusHost.append(
        el('span.small.faint', { style: { marginLeft: '6px' } }, erd.warnings.join(' ')),
      );
    }
  }

  function paintLegend() {
    const layers = [...new Set(erd.tables.map((t) => t.layer))];
    clear(legendHost);
    for (const layer of layers) {
      legendHost.append(
        el(
          'span.erd-legend-item',
          el('span.erd-legend-swatch', { style: { background: LAYER_COLOURS[layer] || LAYER_COLOURS.other } }),
          layer,
        ),
      );
    }
    legendHost.append(
      el('span.erd-legend-item.erd-legend-sep', '·'),
      el('span.erd-legend-item', el('span.erd-line-sample.solid'), 'tested / ref()'),
      el('span.erd-legend-item', el('span.erd-line-sample.dashed'), 'naming guess'),
    );
  }

  function paintCanvas() {
    const tables = drawnTables();
    const byId = new Map(tables.map((t) => [t.id, t]));
    const lineage = lineageSet(focusId);

    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('class', 'erd-svg');
    svg.setAttribute('width', '100%');
    svg.setAttribute('height', '100%');

    const world = document.createElementNS(NS, 'g');
    world.setAttribute('class', 'erd-world');
    svg.append(world);

    const edgeLayer = document.createElementNS(NS, 'g');
    const nodeLayer = document.createElementNS(NS, 'g');
    world.append(edgeLayer, nodeLayer);

    for (const rel of erd.relationships) {
      const fromTable = byId.get(rel.from_table);
      const toTable = byId.get(rel.to_table);
      if (!fromTable || !toTable) continue;
      edgeLayer.append(drawEdge(rel, fromTable, toTable, lineage));
    }

    for (const table of tables) {
      nodeLayer.append(drawNode(table, lineage));
    }

    /* Every paintCanvas() call rebuilds `world` from scratch - a fresh element
       with no transform of its own - because a full rebuild on every filter
       change is far simpler than diffing. The current pan/zoom has to be
       re-applied to *this* element explicitly rather than through the
       querySelector fallback in applyTransform(): that fallback only finds
       whatever `.erd-world` is attached to canvasHost right now, which on a
       second paint is still the *previous* element until the clear() below
       runs. Relying on it here silently reset the view to the origin on every
       search keystroke. */
    applyTransform(world);

    clear(canvasHost).append(svg);
    wirePanZoom(svg, world);
    centreOnFirstPaint(tables, world);
  }

  /**
   * Apply the current pan/zoom to a world element.
   *
   * `world` should be passed explicitly by anything that just created or
   * already holds a reference to it (paintCanvas, wirePanZoom, wireNodeDrag).
   * The DOM-lookup fallback exists only for callers with no reference at all
   * (zoomBy, the toolbar's zoom buttons), where the *currently attached*
   * world is unambiguously the right target.
   */
  function applyTransform(world) {
    const target = world || canvasHost.querySelector('.erd-world');
    if (target) {
      target.setAttribute('transform', `translate(${view.x} ${view.y}) scale(${view.scale})`);
    }
  }

  let hasCentred = false;
  function centreOnFirstPaint(tables, world) {
    if (hasCentred || !tables.length) return;
    hasCentred = true;
    const rect = canvasHost.getBoundingClientRect();
    const xs = tables.map((t) => positions.get(t.id)?.x || 0);
    const minX = Math.min(...xs);
    view.x = Math.max(24, rect.width / 6) - minX * view.scale;
    view.y = 40;
    applyTransform(world);
  }

  /* ----------------------------------------------------------- drawing --- */

  function drawNode(table, lineage) {
    const point = positions.get(table.id) || fallbackPosition(table);
    const columns = visibleColumns(table);
    const shown = columns.slice(0, MAX_ROWS_SHOWN);
    const height = nodeHeight(table);
    const colour = LAYER_COLOURS[table.layer] || LAYER_COLOURS.other;

    const dimmed = !table.in_scope || (lineage && !lineage.reachable.has(table.id));

    const group = document.createElementNS(NS, 'g');
    group.setAttribute('class', `erd-node${dimmed ? ' is-dimmed' : ''}${table.id === selectedId ? ' is-selected' : ''}`);
    group.setAttribute('transform', `translate(${point.x} ${point.y})`);
    group.dataset.tableId = table.id;

    const rect = document.createElementNS(NS, 'rect');
    rect.setAttribute('class', 'erd-node-box');
    rect.setAttribute('width', String(NODE_W));
    rect.setAttribute('height', String(height));
    rect.setAttribute('rx', '8');
    group.append(rect);

    const headerRect = document.createElementNS(NS, 'rect');
    headerRect.setAttribute('class', 'erd-node-header');
    headerRect.setAttribute('width', String(NODE_W));
    headerRect.setAttribute('height', String(HEADER_H));
    headerRect.setAttribute('rx', '8');
    headerRect.setAttribute('fill', colour);
    group.append(headerRect);
    // Square off the bottom corners of the header, since rx rounds all four.
    const patch = document.createElementNS(NS, 'rect');
    patch.setAttribute('width', String(NODE_W));
    patch.setAttribute('height', '8');
    patch.setAttribute('y', String(HEADER_H - 8));
    patch.setAttribute('fill', colour);
    group.append(patch);

    const title = document.createElementNS(NS, 'text');
    title.setAttribute('class', 'erd-node-title');
    title.setAttribute('x', '10');
    title.setAttribute('y', '17');
    title.textContent = table.name.length > 30 ? `${table.name.slice(0, 29)}…` : table.name;
    group.append(title);

    if (!table.in_scope) {
      const badge = document.createElementNS(NS, 'text');
      badge.setAttribute('class', 'erd-node-badge');
      badge.setAttribute('x', String(NODE_W - 8));
      badge.setAttribute('y', '17');
      badge.setAttribute('text-anchor', 'end');
      badge.textContent = 'out of scope';
      group.append(badge);
    }

    shown.forEach((column, index) => {
      const y = HEADER_H + index * ROW_H + 13;
      const row = document.createElementNS(NS, 'g');
      row.setAttribute('class', 'erd-col-row');

      if (query && column.name.toLowerCase().includes(query.toLowerCase())) {
        const hit = document.createElementNS(NS, 'rect');
        hit.setAttribute('x', '2');
        hit.setAttribute('y', String(HEADER_H + index * ROW_H + 2));
        hit.setAttribute('width', String(NODE_W - 4));
        hit.setAttribute('height', String(ROW_H - 2));
        hit.setAttribute('class', 'erd-col-hit');
        row.append(hit);
      }

      const marker = document.createElementNS(NS, 'text');
      marker.setAttribute('x', '10');
      marker.setAttribute('y', String(y));
      marker.setAttribute('class', 'erd-col-marker');
      marker.textContent = column.is_primary ? '🔑' : column.looks_like_key ? '↴' : '';
      row.append(marker);

      const name = document.createElementNS(NS, 'text');
      name.setAttribute('x', '26');
      name.setAttribute('y', String(y));
      name.setAttribute('class', `erd-col-name${column.is_primary ? ' is-pk' : ''}`);
      const maxChars = 20;
      name.textContent = column.name.length > maxChars
        ? `${column.name.slice(0, maxChars - 1)}…`
        : column.name;
      row.append(name);

      const type = document.createElementNS(NS, 'text');
      type.setAttribute('x', String(NODE_W - 10));
      type.setAttribute('y', String(y));
      type.setAttribute('text-anchor', 'end');
      type.setAttribute('class', 'erd-col-type');
      type.textContent = column.data_type || '';
      row.append(type);

      const tip = document.createElementNS(NS, 'title');
      tip.textContent = `${column.name}: ${column.data_type || 'unknown'}`
        + (column.description ? `\n${column.description}` : '')
        + (column.tests.length ? `\ntests: ${column.tests.join(', ')}` : '');
      row.append(tip);

      group.append(row);
    });

    if (columns.length > MAX_ROWS_SHOWN) {
      const more = document.createElementNS(NS, 'text');
      more.setAttribute('x', '10');
      more.setAttribute('y', String(HEADER_H + MAX_ROWS_SHOWN * ROW_H + 13));
      more.setAttribute('class', 'erd-col-more');
      more.textContent = `+${columns.length - MAX_ROWS_SHOWN} more`;
      group.append(more);
    }

    wireNodeDrag(group, table);
    group.addEventListener('click', (event) => {
      if (group.dataset.dragging === '1') return;
      event.stopPropagation();
      selectTable(table);
    });

    return group;
  }

  function drawEdge(rel, fromTable, toTable, lineage) {
    const from = positions.get(fromTable.id) || fallbackPosition(fromTable);
    const to = positions.get(toTable.id) || fallbackPosition(toTable);
    const fromH = nodeHeight(fromTable);
    const toH = nodeHeight(toTable);

    const leftToRight = from.x <= to.x;
    const x1 = leftToRight ? from.x + NODE_W : from.x;
    const x2 = leftToRight ? to.x : to.x + NODE_W;
    const y1 = from.y + Math.min(fromH / 2, HEADER_H + 8);
    const y2 = to.y + Math.min(toH / 2, HEADER_H + 8);
    const mid = (x1 + x2) / 2;

    const group = document.createElementNS(NS, 'g');
    group.setAttribute('class', `erd-edge erd-edge-${rel.kind}`);

    // Both ends have to be on the highlighted path for the edge itself to
    // light up - one end touching the anchor is not enough, or every edge
    // leaving a two-hop chain would look equally relevant.
    const hot = Boolean(lineage)
      && lineage.reachable.has(rel.from_table)
      && lineage.reachable.has(rel.to_table);
    if (hot) group.classList.add('is-hot');
    if (lineage && !hot) group.classList.add('is-dimmed');

    const path = document.createElementNS(NS, 'path');
    path.setAttribute('d', `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`);
    path.setAttribute('class', 'erd-edge-path');
    path.setAttribute('stroke-dasharray', KIND_DASH[rel.kind] || '0');
    group.append(path);

    if (rel.cardinality) {
      const label = document.createElementNS(NS, 'text');
      label.setAttribute('x', String(mid));
      label.setAttribute('y', String((y1 + y2) / 2 - 4));
      label.setAttribute('text-anchor', 'middle');
      label.setAttribute('class', 'erd-edge-label');
      label.textContent = rel.cardinality;
      group.append(label);
    }

    const tip = document.createElementNS(NS, 'title');
    tip.textContent = `${rel.from_name}.${rel.from_columns.join('+') || '?'} -> `
      + `${rel.to_name}.${rel.to_columns.join('+') || '?'}\n`
      + `${KIND_LABEL[rel.kind] || rel.kind}, ${rel.confidence} confidence\n${rel.evidence}`;
    group.append(tip);

    group.addEventListener('mouseenter', () => group.classList.add('is-hover'));
    group.addEventListener('mouseleave', () => group.classList.remove('is-hover'));

    return group;
  }

  /* --------------------------------------------------------- pan / zoom --- */

  /*
   * Both wirePanZoom and wireNodeDrag are called again on every repaint,
   * because paintCanvas rebuilds the whole SVG from scratch (including on
   * every search keystroke). A window-level mousemove/mouseup is needed so
   * dragging keeps tracking once the cursor leaves the small element that
   * started it - but registering one permanently on every call would leak: a
   * few dozen keystrokes would leave hundreds of dead listeners on `window`,
   * each one holding its detached SVG node alive.
   *
   * The fix is to only ever have window listeners attached while a drag is
   * actually in progress: added on mousedown, removed on the mouseup that
   * ends that same drag. At rest, window carries none of these.
   */
  function wirePanZoom(svg, world) {
    let last = null;

    function onMove(event) {
      view.x += event.clientX - last.x;
      view.y += event.clientY - last.y;
      last = { x: event.clientX, y: event.clientY };
      applyTransform(world);
    }
    function onUp() {
      canvasHost.classList.remove('is-panning');
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    }

    svg.addEventListener('mousedown', (event) => {
      if (event.target.closest('.erd-node')) return;
      last = { x: event.clientX, y: event.clientY };
      canvasHost.classList.add('is-panning');
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
    });

    svg.addEventListener('wheel', (event) => {
      event.preventDefault();
      const rect = canvasHost.getBoundingClientRect();
      const cursor = { x: event.clientX - rect.left, y: event.clientY - rect.top };
      const factor = event.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP;
      const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, view.scale * factor));
      const ratio = next / view.scale;

      // Zoom around the cursor: keep the world point under it fixed.
      view.x = cursor.x - (cursor.x - view.x) * ratio;
      view.y = cursor.y - (cursor.y - view.y) * ratio;
      view.scale = next;
      applyTransform(world);
    }, { passive: false });
  }

  function wireNodeDrag(group, table) {
    let start = null;
    let origin = null;

    function onMove(event) {
      const dx = (event.clientX - start.x) / view.scale;
      const dy = (event.clientY - start.y) / view.scale;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) group.dataset.dragging = '1';
      const point = { x: origin.x + dx, y: origin.y + dy };
      positions.set(table.id, point);
      group.setAttribute('transform', `translate(${point.x} ${point.y})`);
      redrawEdges();
    }
    function onUp() {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      // Cleared on a tick's delay so the click handler (which fires right
      // after mouseup) can still see that a drag just happened and skip
      // opening the side panel for what was actually a drag release.
      setTimeout(() => { group.dataset.dragging = '0'; }, 0);
    }

    group.addEventListener('mousedown', (event) => {
      event.stopPropagation();
      group.dataset.dragging = '0';
      start = { x: event.clientX, y: event.clientY };
      origin = { ...(positions.get(table.id) || fallbackPosition(table)) };
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
    });
  }

  /** Cheap: redraw only the edge layer rather than the whole SVG while dragging. */
  function redrawEdges() {
    const world = canvasHost.querySelector('.erd-world');
    if (!world) return;
    const edgeLayer = world.children[0];
    if (!edgeLayer) return;
    const tables = drawnTables();
    const byId = new Map(tables.map((t) => [t.id, t]));
    const lineage = lineageSet(focusId);
    clear(edgeLayer);
    for (const rel of erd.relationships) {
      const fromTable = byId.get(rel.from_table);
      const toTable = byId.get(rel.to_table);
      if (!fromTable || !toTable) continue;
      edgeLayer.append(drawEdge(rel, fromTable, toTable, lineage));
    }
  }

  function zoomBy(factor) {
    const rect = canvasHost.getBoundingClientRect();
    const cursor = { x: rect.width / 2, y: rect.height / 2 };
    const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, view.scale * factor));
    const ratio = next / view.scale;
    view.x = cursor.x - (cursor.x - view.x) * ratio;
    view.y = cursor.y - (cursor.y - view.y) * ratio;
    view.scale = next;
    applyTransform();
  }

  function resetView() {
    hasCentred = false;
    view.scale = 1;
    paintCanvas();
  }

  /* ------------------------------------------------------------- select --- */

  function selectTable(table) {
    selectedId = table.id;
    paintSide(table);
    paintCanvas();
  }

  function paintSide(table) {
    const upstream = erd.relationships.filter((r) => r.to_table === table.id);
    const downstream = erd.relationships.filter((r) => r.from_table === table.id);

    clear(sideHost).append(
      el(
        'div.erd-side-head',
        el('span.erd-legend-swatch', { style: { background: LAYER_COLOURS[table.layer] || LAYER_COLOURS.other } }),
        el('h3', table.name),
        el('button.btn.btn-icon.btn-ghost', { onclick: () => { selectedId = null; clear(sideHost); paintCanvas(); }, 'aria-label': 'Close' }, '✕'),
      ),
      table.in_scope === false ? callout('Outside the permitted dataset scope', `Dataset: ${table.dataset}`, 'warn') : null,
      table.description ? el('p.small.muted', table.description) : el('p.small.faint', 'Not documented.'),
      el(
        'div.row.wrap',
        { style: { gap: '6px', margin: '6px 0 10px' } },
        el('span.chip', table.resource_type),
        el('span.chip', table.materialized),
        table.primary_key.length
          ? el('span.chip.ok', `key: ${table.primary_key.join(', ')}`)
          : el('span.chip.err', 'no key detected'),
        table.row_count !== null && table.row_count !== undefined
          ? el('span.chip', `${num(table.row_count)} rows`)
          : null,
      ),
      el(
        'div.row.wrap',
        { style: { gap: '6px', marginBottom: '10px' } },
        el('button.btn.btn-tiny', {
          // Repaint the side panel too, not just the canvas: this button's own
          // label depends on focusId, and paintSide() only runs again here -
          // relying on the next unrelated re-render left it stuck on whichever
          // label it had when the panel first opened.
          onclick: () => {
            focusId = focusId === table.id ? null : table.id;
            paintCanvas();
            paintSide(table);
          },
        }, focusId === table.id ? '✕ Clear lineage highlight' : '⇄ Highlight upstream/downstream'),
        table.resource_type !== 'source'
          ? el('button.btn.btn-tiny', {
              onclick: () => navigate('workbench', { sql: `select *\nfrom {{ ref('${table.name}') }}\nlimit 100` }),
            }, '▶ Query')
          : null,
      ),
      el('div.stat-label.mb', `Columns (${table.columns.length})`),
      el(
        'div.table-wrap',
        { style: { maxHeight: '32vh' } },
        el(
          'table.data.compact',
          el('thead', el('tr', el('th', ''), el('th', 'Name'), el('th', 'Type'), el('th', 'Tests'))),
          el(
            'tbody',
            ...table.columns.map((column) =>
              el(
                'tr',
                el('td', column.is_primary ? '🔑' : column.looks_like_key ? '↴' : ''),
                el('td.mono.small', column.name),
                el('td', el('span.chip.tiny', column.data_type || '?')),
                el('td.small.faint', column.tests.join(', ') || '—'),
              ),
            ),
          ),
        ),
      ),
      (upstream.length || downstream.length)
        ? el(
            'div.mt',
            el('div.stat-label.mb', 'Relationships'),
            ...upstream.map((r) => relRow(r, 'from', () => focusOn(r.from_table))),
            ...downstream.map((r) => relRow(r, 'to', () => focusOn(r.to_table))),
          )
        : null,
    );
  }

  function focusOn(id) {
    const table = erd.tables.find((t) => t.id === id);
    if (table) selectTable(table);
  }

  function relRow(rel, direction, onOpen) {
    const otherName = direction === 'from' ? rel.from_name : rel.to_name;
    const columns = direction === 'from' ? rel.from_columns : rel.to_columns;
    return el(
      'div.erd-rel-row',
      el('span.chip.tiny', KIND_LABEL[rel.kind] || rel.kind),
      el('button.btn.btn-tiny.btn-ghost', { onclick: onOpen }, `${direction === 'from' ? '←' : '→'} ${otherName}`),
      columns.length ? el('code.tiny.faint', columns.join('+')) : null,
      el('span.tiny.faint', rel.cardinality || ''),
    );
  }

  /* ---------------------------------------------------------- exports --- */

  async function exportText(format) {
    try {
      const payload = await api.erdExport(format, {
        inScopeOnly: options.inScopeOnly,
        includeStaging: options.includeStaging,
        includeSources: options.includeSources,
        tables: scopeMode === 'custom' ? options.tables : [],
        keysOnly,
      });
      download(payload.filename, payload.content, 'text/plain');
      toast(`Downloaded ${payload.filename}`, { kind: 'ok' });
    } catch (error) {
      reportError(error, `Exporting ${format}`);
    }
  }

  function exportSvg() {
    const svg = canvasHost.querySelector('svg');
    if (!svg) return;
    const serialised = serialiseSvg(svg);
    download('erd.svg', serialised, 'image/svg+xml');
    toast('Downloaded erd.svg', { kind: 'ok' });
  }

  function serialiseSvg(svg) {
    const clone = svg.cloneNode(true);
    const bbox = computeBounds();
    clone.setAttribute('xmlns', NS);
    clone.setAttribute('width', String(bbox.width));
    clone.setAttribute('height', String(bbox.height));
    clone.setAttribute('viewBox', `${bbox.minX} ${bbox.minY} ${bbox.width} ${bbox.height}`);
    // Bake the current pan/zoom out and lay the world out at its natural
    // extent, so the exported file is not cropped to whatever the viewport
    // happened to show.
    const world = clone.querySelector('.erd-world');
    if (world) world.removeAttribute('transform');

    const style = document.createElementNS(NS, 'style');
    style.textContent = exportCss();
    clone.insertBefore(style, clone.firstChild);

    return `<?xml version="1.0" encoding="UTF-8"?>\n${new XMLSerializer().serializeToString(clone)}`;
  }

  /** Bounding box of every drawn node, in world coordinates. */
  function computeBounds() {
    const tables = drawnTables();
    if (!tables.length) return { minX: 0, minY: 0, width: 400, height: 300 };
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const table of tables) {
      const point = positions.get(table.id) || fallbackPosition(table);
      minX = Math.min(minX, point.x);
      minY = Math.min(minY, point.y);
      maxX = Math.max(maxX, point.x + NODE_W);
      maxY = Math.max(maxY, point.y + nodeHeight(table));
    }
    return {
      minX: minX - PAD, minY: minY - PAD,
      width: maxX - minX + PAD * 2, height: maxY - minY + PAD * 2,
    };
  }

  /** A self-contained stylesheet for the exported SVG - it will not carry the app's CSS. */
  function exportCss() {
    return `
      .erd-node-box { fill: #ffffff; stroke: #94a3b8; stroke-width: 1; }
      .erd-node-header { }
      .erd-node-title { font: bold 12px sans-serif; fill: #ffffff; }
      .erd-node-badge { font: 9px sans-serif; fill: #ffffff; opacity: .85; }
      .erd-col-name { font: 11px monospace; fill: #1e293b; }
      .erd-col-name.is-pk { font-weight: bold; }
      .erd-col-type { font: 10px monospace; fill: #64748b; }
      .erd-col-marker { font: 10px sans-serif; }
      .erd-col-more { font: 10px monospace; fill: #94a3b8; font-style: italic; }
      .erd-col-hit { fill: #fde68a; }
      .erd-edge-path { fill: none; stroke: #64748b; stroke-width: 1.4; }
      .erd-edge-label { font: 10px monospace; fill: #334155; }
      .is-dimmed { opacity: 0.35; }
    `;
  }

  function exportPng() {
    const svg = canvasHost.querySelector('svg');
    if (!svg) return;
    const bbox = computeBounds();
    const scale = 2; // export at 2x for a crisper raster
    const serialised = serialiseSvg(svg);
    const blob = new Blob([serialised], { type: 'image/svg+xml' });
    const url = URL.createObjectURL(blob);
    const image = new Image();
    image.onload = () => {
      const canvas = document.createElement('canvas');
      canvas.width = bbox.width * scale;
      canvas.height = bbox.height * scale;
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.scale(scale, scale);
      ctx.drawImage(image, 0, 0);
      URL.revokeObjectURL(url);
      canvas.toBlob((blob2) => {
        const link = el('a', { href: URL.createObjectURL(blob2), download: 'erd.png' });
        document.body.append(link);
        link.click();
        link.remove();
        toast('Downloaded erd.png', { kind: 'ok' });
      }, 'image/png');
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      toast('Could not rasterise the diagram. SVG export always works as a fallback.', { kind: 'err' });
    };
    image.src = url;
  }

  /**
   * PDF export: window.print() scoped to the diagram via @media print.
   *
   * The live SVG is sized at width/height: 100% of its container so pan/zoom
   * has a stable viewport to work against. @media print gives that container
   * `height: auto`, and a percentage height against an auto-height ancestor
   * resolves to nothing - the SVG would print as a blank page. So for the
   * duration of the print, the SVG is given explicit pixel dimensions and a
   * viewBox covering the whole diagram (not just whatever pan/zoom happens to
   * show), and the pan/zoom transform is suspended. Both are restored from
   * `afterprint`, which fires whether the user prints or cancels.
   */
  function exportPdf() {
    const svg = canvasHost.querySelector('svg');
    const world = canvasHost.querySelector('.erd-world');
    if (!svg || !world) {
      toast('Nothing to print yet.', { kind: 'warn' });
      return;
    }

    const bbox = computeBounds();
    const saved = {
      width: svg.getAttribute('width'),
      height: svg.getAttribute('height'),
      viewBox: svg.getAttribute('viewBox'),
      transform: world.getAttribute('transform'),
    };

    svg.setAttribute('width', String(bbox.width));
    svg.setAttribute('height', String(bbox.height));
    svg.setAttribute('viewBox', `${bbox.minX} ${bbox.minY} ${bbox.width} ${bbox.height}`);
    world.removeAttribute('transform');
    document.body.classList.add('erd-printing');

    const restore = () => {
      svg.setAttribute('width', saved.width);
      svg.setAttribute('height', saved.height);
      if (saved.viewBox) svg.setAttribute('viewBox', saved.viewBox);
      else svg.removeAttribute('viewBox');
      if (saved.transform) world.setAttribute('transform', saved.transform);
      document.body.classList.remove('erd-printing');
      window.removeEventListener('afterprint', restore);
    };
    window.addEventListener('afterprint', restore);
    window.print();
  }

  /* ------------------------------------------------------------ toolbar --- */

  const toolbarHost = el('div.erd-toolbar');

  function paintToolbar() {
    const searchInput = el('input.input', {
      type: 'search',
      placeholder: 'Search tables or columns…',
      value: query,
      style: { maxWidth: '220px' },
      oninput: (event) => { query = event.target.value.trim(); paintCanvas(); },
    });

    const keysToggle = el('label.switch', { title: 'Show only keys, hide plain columns' },
      el('input', {
        type: 'checkbox',
        checked: keysOnly,
        onchange: (event) => { keysOnly = event.target.checked; positions.clear(); laidOutFor = ''; load(); },
      }),
      el('span', 'Keys only'));

    const stagingToggle = el('label.switch', { title: 'Include stg_/staging_ models' },
      el('input', {
        type: 'checkbox',
        checked: options.includeStaging,
        onchange: (event) => { options.includeStaging = event.target.checked; load(); },
      }),
      el('span', 'Staging'));

    const sourcesToggle = el('label.switch', { title: 'Include declared sources' },
      el('input', {
        type: 'checkbox',
        checked: options.includeSources,
        onchange: (event) => { options.includeSources = event.target.checked; load(); },
      }),
      el('span', 'Sources'));

    const inScopeToggle = el('label.switch', { title: 'Hide everything outside bronze/silver instead of dimming it' },
      el('input', {
        type: 'checkbox',
        checked: options.inScopeOnly,
        onchange: (event) => { options.inScopeOnly = event.target.checked; load(); },
      }),
      el('span', 'In-scope only'));

    const countsToggle = el('label.switch', { title: 'Fetch row counts (one free metadata query)' },
      el('input', {
        type: 'checkbox',
        checked: options.counts,
        onchange: (event) => { options.counts = event.target.checked; load(); },
      }),
      el('span', 'Row counts'));

    const exportMenu = el(
      'div.erd-export-menu',
      el('button.btn.btn-tiny', { onclick: exportSvg }, '↓ SVG'),
      el('button.btn.btn-tiny', { onclick: exportPng }, '↓ PNG'),
      el('button.btn.btn-tiny', { onclick: exportPdf }, '↓ PDF'),
      el('button.btn.btn-tiny', { onclick: () => exportText('mermaid') }, '↓ Mermaid'),
      el('button.btn.btn-tiny', { onclick: () => exportText('dbml') }, '↓ DBML'),
    );

    clear(toolbarHost).append(
      el(
        'div.row.wrap.between',
        { style: { gap: '10px' } },
        el('div.row.wrap', { style: { gap: '8px', alignItems: 'center' } }, scopeControls(), searchInput),
        el('div.row.wrap', { style: { gap: '10px', alignItems: 'center' } },
           keysToggle, stagingToggle, sourcesToggle, inScopeToggle, countsToggle),
      ),
      el(
        'div.row.wrap.between.mt',
        { style: { gap: '10px' } },
        el(
          'div.row',
          { style: { gap: '5px' } },
          el('button.btn.btn-icon.btn-ghost', { title: 'Zoom in', onclick: () => zoomBy(ZOOM_STEP) }, '+'),
          el('button.btn.btn-icon.btn-ghost', { title: 'Zoom out', onclick: () => zoomBy(1 / ZOOM_STEP) }, '−'),
          el('button.btn.btn-tiny.btn-ghost', { title: 'Reset the view', onclick: resetView }, '⤢ Fit'),
        ),
        exportMenu,
      ),
    );
  }

  /* ---------------------------------------------------------- assemble --- */

  paintToolbar();

  host.append(
    el(
      'div.panel.erd-help',
      el(
        'div.panel-body',
        el(
          'p.small.faint',
          { style: { margin: 0, lineHeight: '1.55' } },
          'Solid lines are enforced: a dbt test or a ref() dependency. Dashed '
          + 'lines are a guess from column naming - add a relationships test '
          + 'to promote one into a solid line. Drag a table to rearrange it, '
          + 'scroll to zoom, click a table for details.',
        ),
      ),
    ),
    toolbarHost,
    legendHost,
    statusHost,
    el(
      'div.erd-layout.mt',
      canvasHost,
      sideHost,
    ),
  );

  clear(sideHost).append(
    el('div.erd-side-empty', el('p.small.faint', 'Click a table to see its columns and relationships.')),
  );

  load();

  return host;
}
