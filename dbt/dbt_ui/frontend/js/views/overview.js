/* ==========================================================================
   overview.js - project health at a glance.
   ========================================================================== */

import { ago, api, el, layerLabel, num, pct, secs, state } from '../core.js';
import { callout, emptyState, kv, layerChip, statCard } from '../components.js';
import { help } from '../prefs.js';
import { openModel } from './drawer.js';

export const meta = {
  title: 'Overview',
  subtitle: 'How the project is doing right now',
};

export function render(navigate) {
  const stats = state.stats;
  const boot = state.boot;

  if (!stats) {
    return el(
      'div.panel',
      emptyState(
        'No manifest yet',
        'dbt has not parsed this project on this machine. Refresh the manifest to build target/manifest.json, which is what every screen here reads from.',
        el(
          'button.btn.btn-primary',
          { onclick: () => navigate('runs', { autorun: 'parse' }) },
          '⟳ Refresh manifest',
        ),
      ),
    );
  }

  return el(
    'div',
    help(
      'A "model" is one table or view built from a SQL file. A "check" is an ' +
      'automated rule that must hold true, like "this column is never empty". ' +
      'Coverage tells you how much of the project has been described in ' +
      'writing, which is what makes it usable by someone who did not build it.',
    ),
    warningsBlock(boot),
    statsBlock(stats),
    el('div.grid.grid-2.mt', layersPanel(stats, navigate), lastRunPanel(state.lastRun, navigate)),
    el('div.grid.grid-2.mt', healthPanel(stats, navigate), quickStartPanel(navigate)),
  );
}

/* ------------------------------------------------------------- warnings --- */

function warningsBlock(boot) {
  const notes = [];
  const target = (boot?.targets || []).find((entry) => entry.name === state.target);

  /* The most important warning on the page: if this is set, every model
     reference is pointing at a different environment than the one selected. */
  if (state.targetMismatch) {
    notes.push(
      callout(
        'Model references point at a different environment',
        `${state.targetMismatch.message} ${state.targetMismatch.fix}`,
        'err',
        el(
          'button.btn.btn-tiny.mt',
          { onclick: () => document.getElementById('btn-refresh-manifest')?.click() },
          '⟳ Reload project now',
        ),
      ),
    );
  }

  for (const warning of target?.warnings || []) {
    notes.push(callout(`Environment '${target.name}'`, warning, 'warn'));
  }

  if (!boot?.docs_available) {
    notes.push(
      callout(
        'dbt docs not generated',
        'Run "Generate docs" in the Run Console to publish the browsable catalog and lineage site.',
        'info',
      ),
    );
  }

  if (!notes.length) return null;
  return el('div.grid.mb', { style: { gap: '10px' } }, ...notes);
}

/* ---------------------------------------------------------------- stats --- */

function statsBlock(stats) {
  const docKind = stats.doc_coverage >= 90 ? 'ok' : stats.doc_coverage >= 60 ? 'warn' : 'err';

  return el(
    'div.grid.grid-4',
    statCard(num(stats.model_count), 'Tables & views', `plus ${num(stats.seed_count)} reference file${stats.seed_count === 1 ? '' : 's'}`),
    statCard(num(stats.test_count), 'Automated checks', testNote(stats)),
    statCard(pct(stats.doc_coverage), 'Columns described', `${num(stats.documented_columns)} of ${num(stats.total_columns)}`, { kind: docKind }),
    statCard(pct(stats.type_coverage), 'Types recorded', 'so nobody has to guess a format', {
      kind: stats.type_coverage >= 90 ? 'ok' : 'warn',
    }),
  );
}

function testNote(stats) {
  const untested = stats.untested_models?.length || 0;
  return untested ? `${untested} model${untested === 1 ? '' : 's'} untested` : 'every model tested';
}

/* --------------------------------------------------------------- layers --- */

function layersPanel(stats, navigate) {
  const layers = state.boot?.layers || [];

  return el(
    'div.panel',
    el(
      'div.panel-head',
      el('h3', 'Medallion layers'),
      el('button.btn.btn-tiny', { onclick: () => navigate('pipeline') }, 'Open pipeline →'),
    ),
    el(
      'div.panel-body',
      el(
        'div.grid',
        { style: { gap: '11px' } },
        ...layers.map((layer) => {
          const count = stats.by_layer?.[layer.key] || 0;
          return el(
            'div',
            el(
              'div.row.between',
              el('div.row', { style: { gap: '8px' } }, layerChip(layer.key), el('span.small.faint', layer.materialization)),
              el('span.mono.small', `${count} model${count === 1 ? '' : 's'}`),
            ),
            el('p.tiny.faint', { style: { margin: '4px 0 0', lineHeight: '1.5' } }, layer.blurb),
          );
        }),
        (stats.by_layer?.other || 0) > 0
          ? el(
              'div.row.between',
              el('div.row', { style: { gap: '8px' } }, el('span.chip.other', 'Other'), el('span.small.faint', 'unlayered')),
              el('span.mono.small', `${stats.by_layer.other} models`),
            )
          : null,
      ),
    ),
  );
}

/* ------------------------------------------------------------- last run --- */

function lastRunPanel(lastRun, navigate) {
  if (!lastRun) {
    return el(
      'div.panel',
      el('div.panel-head', el('h3', 'Last run')),
      emptyState('Nothing has run yet', 'Build the project to populate run history.',
        el('button.btn.btn-primary', { onclick: () => navigate('runs', { autorun: 'build' }) }, '⚡ Build now')),
    );
  }

  const counts = lastRun.counts || {};
  const failed = (counts.error || 0) + (counts.fail || 0) + (counts.runtime_error || 0);
  const slowest = (lastRun.results || []).slice(0, 5);

  return el(
    'div.panel',
    el(
      'div.panel-head',
      el('h3', 'Last run'),
      el('span.muted.small', `${lastRun.args?.which || 'run'} · ${ago(lastRun.generated_at)}`),
    ),
    el(
      'div.panel-body',
      el(
        'div.row.wrap.mb',
        { style: { gap: '6px' } },
        ...Object.entries(counts).map(([status, count]) =>
          el(`span.chip.${statusKind(status)}`, `${count} ${status}`),
        ),
        el('span.chip', `${secs(lastRun.elapsed_time)} elapsed`),
        lastRun.args?.target ? el('span.chip.info', `target ${lastRun.args.target}`) : null,
      ),
      failed
        ? callout(`${failed} node${failed === 1 ? '' : 's'} did not succeed`, 'Open the Run Console for the full log.', 'err')
        : callout('All nodes succeeded', '', 'ok'),
      slowest.length
        ? el(
            'div.mt',
            el('div.stat-label.mb', 'Slowest nodes'),
            el(
              'table.data.compact',
              el(
                'tbody',
                ...slowest.map((result) =>
                  el(
                    'tr',
                    el('td.mono.small', result.name),
                    el('td', el(`span.chip.${statusKind(result.status)}`, result.status)),
                    el('td.num.small', `${result.execution_time}s`),
                    el(
                      'td.num.small.faint',
                      result.rows_affected !== null && result.rows_affected !== undefined
                        ? `${num(result.rows_affected)} rows`
                        : '',
                    ),
                  ),
                ),
              ),
            ),
          )
        : null,
    ),
  );
}

function statusKind(status) {
  const text = String(status).toLowerCase();
  if (['success', 'pass'].includes(text)) return 'ok';
  if (['error', 'fail', 'runtime error'].includes(text)) return 'err';
  if (['warn', 'skipped', 'skip'].includes(text)) return 'warn';
  return 'other';
}

/* --------------------------------------------------------------- health --- */

function healthPanel(stats, navigate) {
  const undocumented = stats.undocumented_models || [];
  const untested = stats.untested_models || [];
  const items = [];

  if (undocumented.length) {
    items.push(
      issueRow(
        `${undocumented.length} model${undocumented.length === 1 ? '' : 's'} without a description`,
        undocumented,
        'warn',
        (name) => navigate('schema', { model: name }),
      ),
    );
  }
  if (untested.length) {
    items.push(
      issueRow(
        `${untested.length} model${untested.length === 1 ? '' : 's'} without tests`,
        untested,
        'err',
        (name) => openModel(name),
      ),
    );
  }

  return el(
    'div.panel',
    el('div.panel-head', el('h3', 'Health'), el('span.muted.small', 'from the manifest')),
    items.length
      ? el('div.panel-body', el('div.grid', { style: { gap: '13px' } }, ...items))
      : el('div.panel-body', callout('Nothing outstanding', 'Every model has a description and at least one test.', 'ok')),
  );
}

function issueRow(title, names, kind, onClick) {
  return el(
    'div',
    el('div.row.mb', { style: { gap: '8px' } }, el(`span.chip.${kind}`, names.length), el('span.small', title)),
    el(
      'div.row.wrap',
      { style: { gap: '5px' } },
      ...names.slice(0, 12).map((name) => el('button.btn.btn-tiny', { onclick: () => onClick(name) }, name)),
      names.length > 12 ? el('span.tiny.faint', `+${names.length - 12} more`) : null,
    ),
  );
}

/* ----------------------------------------------------------- quick start --- */

function quickStartPanel(navigate) {
  const steps = [
    ['Query through dbt', 'Write SQL with ref() and never hardcode a dataset.', 'workbench'],
    ['Get the column contract', 'name + data_type for any model or ad-hoc query.', 'schema'],
    ['Plan the silver layer', 'Profile bronze and get concrete transformation advice.', 'advisor'],
    ['Build and test', 'Run dbt with live logs, no terminal needed.', 'runs'],
  ];

  return el(
    'div.panel',
    el('div.panel-head', el('h3', 'Where to start')),
    el(
      'div.panel-body',
      el(
        'div.grid',
        { style: { gap: '9px' } },
        ...steps.map(([title, body, view], index) =>
          el(
            'button.model-card',
            { onclick: () => navigate(view) },
            el(
              'div.row',
              { style: { gap: '9px' } },
              el('span.chip.info', index + 1),
              el('span', { style: { fontWeight: '500' } }, title),
            ),
            el('span.model-card-desc', body),
          ),
        ),
      ),
    ),
  );
}
