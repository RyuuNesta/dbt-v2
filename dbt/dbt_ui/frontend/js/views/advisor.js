/* ==========================================================================
   advisor.js - bronze to silver recommendations.

   Flow: pick a bronze relation, profile it, read the recommendations, uncheck
   what you disagree with, generate the silver model, write it into the project.

   Every recommendation shows the measurement that produced it. That is the
   whole point: this is not a template, it is a reading of the actual data.
   ========================================================================== */

import {
  api, bytes, clear, copy, download, el, ms, num, pct, plainRelation, state, toast,
} from '../core.js';
import {
  CATEGORY_LABELS, callout, codeBlock, confidenceChip, emptyState, layerChip,
  loading, schemaTable, tabs, typeBadge,
} from '../components.js';

export const meta = {
  title: 'Silver Advisor',
  subtitle: 'Profile a bronze relation and get concrete silver transformations',
};

export function render(navigate, params = {}) {
  const host = el('div');
  const output = el('div.mt');

  let selected = params.model || state.scratch.advisorModel || firstBronze();
  let analysis = null;
  const accepted = new Set();

  const analyseBtn = el('button.btn.btn-primary.btn-block', { onclick: () => analyse() }, '✦ Analyse and recommend');

  /* ------------------------------------------------------------ picker --- */

  const list = el('div.scroll-list');

  function paintList() {
    clear(list);
    const candidates = (state.models || []).filter((model) =>
      ['seed', 'bronze', 'silver'].includes(model.layer),
    );
    const pool = candidates.length ? candidates : state.models || [];
    const inScope = pool.filter((m) => m.in_scope !== false);
    const blocked = pool.filter((m) => m.in_scope === false);

    for (const model of [...inScope, ...blocked]) {
      const outOfScope = model.in_scope === false;
      list.append(
        el(
          'button',
          {
            class: `list-btn${model.name === selected ? ' sel' : ''}${outOfScope ? ' out-of-scope' : ''}`,
            title: outOfScope
              ? `Outside the permitted scope (dataset ${model.dataset})`
              : model.name,
            onclick: () => {
              if (outOfScope) {
                toast(`${model.name} is outside the permitted dataset scope.`, {
                  kind: 'warn',
                  detail:
                    `It lives in '${model.dataset}'. This instance may only ` +
                    `profile: ${(state.scope?.allowed_datasets || []).join(', ')}.`,
                });
                return;
              }
              selected = model.name;
              state.scratch.advisorModel = model.name;
              paintList();
              analyse();
            },
          },
          layerChip(model.layer),
          el('span.lb-name', model.name),
          el(
            'span.lb-meta',
            outOfScope
              ? el('span.chip.err', 'out of scope')
              : el('span.chip', `${model.column_count} cols`),
          ),
        ),
      );
    }
  }

  /* ----------------------------------------------------------- analyse --- */

  async function analyse() {
    if (!selected) {
      toast('Pick a relation to analyse.', { kind: 'warn' });
      return;
    }

    analyseBtn.disabled = true;
    clear(output).append(
      el(
        'div.panel',
        loading(`Profiling ${selected}: measuring nulls, cardinality, ranges and duplicate keys…`),
      ),
    );

    try {
      analysis = await api.analyse({ model: selected });
      accepted.clear();
      for (const rec of analysis.recommendations) {
        if (rec.default_applied) accepted.add(rec.id);
      }
      paintOutput();
    } catch (error) {
      clear(output).append(
        el(
          'div.panel',
          el(
            'div.panel-body',
            callout(
              'Could not analyse this relation',
              error.message,
              'err',
              el(
                'div',
                error.detail ? el('pre.code-block', error.detail) : null,
                el(
                  'div.mt',
                  el('span.small.faint', 'A relation has to exist in the warehouse before it can be profiled. '),
                  el(
                    'button.btn.btn-tiny',
                    { onclick: () => navigate('runs', { select: selected, autorun: 'run' }) },
                    `⚡ Build ${selected}`,
                  ),
                ),
              ),
            ),
          ),
        ),
      );
    } finally {
      analyseBtn.disabled = false;
    }
  }

  /* ------------------------------------------------------------ output --- */

  function paintOutput() {
    const profile = analysis.profile;
    const summary = analysis.summary;

    const view = tabs([
      {
        label: 'Recommendations',
        count: summary.total,
        render: () => recommendationsPanel(),
      },
      {
        label: 'Profile',
        count: profile.columns.length,
        render: () => profilePanel(profile, analysis),
      },
      {
        label: 'Generated silver model',
        render: () => el('div.panel-body', loading('Building the model…')),
      },
    ]);

    clear(output).append(
      el(
        'div',
        summaryPanel(),
        el('div.panel.mt', view.node),
      ),
    );

    view.select(0);

    /* Generate lazily so a big profile does not wait on codegen. */
    let generated = false;
    const originalSelect = view.select;
    view.select = (index) => {
      originalSelect(index);
      if (index === 2 && !generated) {
        generated = true;
        buildSilver(view);
      }
    };
  }

  function summaryPanel() {
    const profile = analysis.profile;
    const dup = analysis.duplicate_check;
    const plan = analysis.plan;

    return el(
      'div.panel',
      el(
        'div.panel-head',
        el('h3', `${selected} → ${analysis.suggested_model_name}`),
        el(
          'div.row',
          { style: { gap: '6px' } },
          el('span.chip.ok', `${analysis.summary.high_confidence} high confidence`),
          el('span.chip', `${analysis.summary.total} total`),
        ),
      ),
      el(
        'div.panel-body',
        el(
          'div.grid.grid-4.mb',
          fact(num(profile.row_count), 'Rows profiled', profile.sampled ? `sampled from ${num(profile.declared_row_count)}` : 'full table'),
          fact(num(profile.columns.length), 'Columns'),
          fact(
            dup?.checked ? (dup.is_unique ? 'unique' : `${num(dup.duplicated_keys)} dup`) : '-',
            'Key check',
            plan.key_columns?.join(', ') || 'no key found',
            dup?.checked ? (dup.is_unique ? 'ok' : 'warn') : '',
          ),
          fact(bytes(profile.bytes_processed), 'Scanned', ms(profile.duration_ms)),
        ),
        profile.sampled
          ? callout(
              'Profiled from a sample',
              `The table has ${num(profile.declared_row_count)} rows, so ${num(profile.row_count)} were sampled. Percentages are estimates, not a census.`,
              'warn',
            )
          : null,
        el(
          'div.grid.grid-3.mt',
          planBlock('Business key', plan.key_columns, 'Deduplicate and test uniqueness on this.'),
          planBlock('Suggested gold grain', plan.grain_columns, 'Group by these when aggregating.'),
          planBlock('Measures', plan.measure_columns, 'Numeric columns worth summing.'),
        ),
      ),
    );
  }

  function planBlock(title, columns, hint) {
    return el(
      'div',
      el('div.stat-label.mb', title),
      columns?.length
        ? el('div.row.wrap', { style: { gap: '5px' } }, ...columns.map((name) => el('code.chip', name)))
        : el('span.small.faint', 'none detected'),
      el('p.tiny.faint', { style: { margin: '5px 0 0', lineHeight: '1.45' } }, hint),
    );
  }

  /* -------------------------------------------------- recommendations --- */

  function recommendationsPanel() {
    const groups = new Map();
    for (const rec of analysis.recommendations) {
      if (!groups.has(rec.category)) groups.set(rec.category, []);
      groups.get(rec.category).push(rec);
    }

    const body = el('div.panel-body');

    body.append(
      el(
        'div.row.wrap.mb',
        { style: { gap: '7px' } },
        el('button.btn.btn-tiny', { onclick: () => setAll(true) }, 'Select all'),
        el('button.btn.btn-tiny', { onclick: () => setAll(false) }, 'Clear all'),
        el('button.btn.btn-tiny', { onclick: () => setHighOnly() }, 'High confidence only'),
        el('div.spacer'),
        el('span.small.faint', { dataset: { role: 'accepted-count' } }, `${accepted.size} selected`),
      ),
    );

    for (const [category, recs] of [...groups].sort((a, b) => b[1].length - a[1].length)) {
      body.append(
        el(
          'div.mt',
          el(
            'div.row.mb',
            { style: { gap: '8px' } },
            el('span.chip.info', CATEGORY_LABELS[category] || category),
            el('span.small.faint', `${recs.length} item${recs.length === 1 ? '' : 's'}`),
          ),
          el('div.grid', { style: { gap: '8px' } }, ...recs.map(recCard)),
        ),
      );
    }

    return body;
  }

  function recCard(rec) {
    const box = el('input', { type: 'checkbox' });
    box.checked = accepted.has(rec.id);
    box.addEventListener('change', () => {
      if (box.checked) accepted.add(rec.id);
      else accepted.delete(rec.id);
      updateCount();
    });

    return el(
      'div.rec',
      { dataset: { confidence: rec.confidence, recId: rec.id } },
      el('div.rec-check', box),
      el(
        'div.rec-main',
        el('div.rec-title', rec.title),
        el('div.rec-detail', rec.detail),
        el('div.rec-evidence', rec.evidence),
        rec.sql_hint ? el('div.rec-hint', rec.sql_hint) : null,
        el(
          'div.rec-tags',
          confidenceChip(rec.confidence),
          ...(rec.columns || []).map((name) => el('code.chip', name)),
        ),
      ),
    );
  }

  function setAll(value) {
    accepted.clear();
    if (value) for (const rec of analysis.recommendations) accepted.add(rec.id);
    syncBoxes();
  }

  function setHighOnly() {
    accepted.clear();
    for (const rec of analysis.recommendations) {
      if (rec.confidence === 'high') accepted.add(rec.id);
    }
    syncBoxes();
  }

  function syncBoxes() {
    for (const card of document.querySelectorAll('.rec[data-rec-id]')) {
      const box = card.querySelector('input[type=checkbox]');
      if (box) box.checked = accepted.has(card.dataset.recId);
    }
    updateCount();
  }

  function updateCount() {
    const badge = document.querySelector('[data-role="accepted-count"]');
    if (badge) badge.textContent = `${accepted.size} selected`;
  }

  /* --------------------------------------------------- generated model --- */

  async function buildSilver(view) {
    try {
      const payload = await api.generateSilver({
        model: selected,
        accepted_ids: [...accepted],
        materialized: 'view',
      });
      view.setPanel(2, silverPanel(payload, view));
    } catch (error) {
      view.setPanel(
        2,
        el('div.panel-body', callout('Could not generate the model', error.message, 'err',
          error.detail ? el('pre.code-block', error.detail) : null)),
      );
    }
  }

  function silverPanel(payload, view) {
    const pathInput = el('input.input', { value: payload.path });
    const status = el('div');

    async function write(mode) {
      clear(status).append(loading(`Writing ${pathInput.value}…`));
      try {
        const result = await api.writeFile(pathInput.value.trim(), payload.sql, mode);
        clear(status).append(
          callout(
            `Wrote ${result.written}`,
            [result.backup ? `previous version saved as ${result.backup}` : null, result.note]
              .filter(Boolean)
              .join(' · '),
            'ok',
            el(
              'div.row.wrap.mt',
              { style: { gap: '6px' } },
              el('button.btn.btn-tiny', { onclick: () => navigate('runs', { autorun: 'parse' }) }, '⟳ Refresh manifest'),
              el(
                'button.btn.btn-tiny',
                { onclick: () => navigate('runs', { select: payload.model_name, autorun: 'run' }) },
                `⚡ Build ${payload.model_name}`,
              ),
            ),
          ),
        );
        toast(`Wrote ${result.written}`, { kind: 'ok' });
      } catch (error) {
        clear(status).append(callout('Write failed', error.message, 'err'));
      }
    }

    return el(
      'div.panel-body',
      callout(
        'Generated from your selections, meant to be reviewed',
        `${payload.applied.length} recommendation${payload.applied.length === 1 ? '' : 's'} applied, ${payload.skipped.length} skipped. Comments in the SQL record the measurement behind each transformation. Read it before merging.`,
        'warn',
      ),
      el(
        'div.row.wrap.mt.mb',
        { style: { gap: '7px' } },
        el('button.btn.btn-tiny', { onclick: () => copy(payload.sql, 'Model copied') }, '⧉ Copy'),
        el('button.btn.btn-tiny', { onclick: () => download(`${payload.model_name}.sql`, payload.sql) }, '↓ Download'),
        el(
          'button.btn.btn-tiny',
          {
            onclick: () => {
              view.setPanel(2, el('div.panel-body', loading('Regenerating…')));
              buildSilver(view);
            },
          },
          '↻ Regenerate from current selection',
        ),
      ),
      el(
        'p.tiny.faint',
        { style: { lineHeight: '1.5' } },
        'The model calls project macros, so dbt has to compile it. Write it, refresh the manifest, then build.',
      ),
      payload.dropped_columns?.length
        ? el(
            'div.mb',
            callout(
              `${payload.dropped_columns.length} column(s) omitted`,
              `${payload.dropped_columns.join(', ')} carried no information (constant or all null). They stay in bronze for fidelity.`,
              'info',
            ),
          )
        : null,
      codeBlock(payload.sql, { tall: true, title: payload.path }),
      el(
        'div.mt',
        el('div.stat-label.mb', 'Write into the project'),
        el(
          'div.row.wrap',
          { style: { gap: '7px' } },
          pathInput,
          el('button.btn', { onclick: () => write('overwrite') }, '⤓ Write model'),
        ),
        el('div.mt', status),
      ),
    );
  }

  /* ------------------------------------------------------------ assemble --- */

  paintList();

  host.append(
    el(
      'div.split',
      el(
        'div.panel',
        el('div.panel-head', el('h3', 'Choose a relation')),
        el(
          'div.panel-body',
          el(
            'p.small.faint',
            { style: { marginTop: 0, lineHeight: '1.55' } },
            'Pick the bronze model you want to promote. Silver models can also be analysed, to check what gold should aggregate.',
          ),
          list,
          el('div.mt', analyseBtn),
        ),
      ),
      output,
    ),
  );

  clear(output).append(
    el(
      'div.panel',
      emptyState(
        'Nothing analysed yet',
        'Choose a relation and run the analysis. Every recommendation is derived from a measurement taken against the real table, with the evidence attached.',
      ),
    ),
  );

  if (selected) analyse();

  return host;
}

/* ---------------------------------------------------------- profile tab --- */

function profilePanel(profile, analysis) {
  const flagged = profile.columns.filter(
    (column) => column.is_all_null || column.is_constant || column.null_pct > 40,
  );

  return el(
    'div',
    el(
      'div.panel-body',
      flagged.length
        ? callout(
            `${flagged.length} column${flagged.length === 1 ? '' : 's'} worth a look`,
            flagged
              .map((column) =>
                column.is_all_null
                  ? `${column.name} is entirely null`
                  : column.is_constant
                  ? `${column.name} never changes`
                  : `${column.name} is ${pct(column.null_pct)} null`,
              )
              .join('; '),
            'warn',
          )
        : callout('No structural problems found', 'No all-null or constant columns, and nothing heavily sparse.', 'ok'),
    ),
    schemaTable(profile.columns, { showProfile: true, showDescription: true }),
  );
}

/* ---------------------------------------------------------------- utils --- */

function fact(value, label, note, kind = '') {
  return el(
    'div.stat',
    el('span.stat-value', { style: kind ? { color: `var(--${kind})` } : null, class: 'stat-value' }, value),
    el('span.stat-label', label),
    note ? el('span.stat-note', note) : null,
  );
}

function firstBronze() {
  // Restricted to what the UI may actually profile, so the page does not open
  // on a scope refusal.
  const usable = (state.models || []).filter((model) => model.in_scope !== false);
  return (
    usable.find((model) => model.layer === 'bronze')?.name ||
    usable.find((model) => model.layer === 'silver')?.name ||
    usable[0]?.name ||
    ''
  );
}
