/* ==========================================================================
   pipeline.js - the medallion board.

   Bronze -> Silver -> Gold as columns, each model a card. This is the mental
   model the team already has, so it is the primary navigation surface.
   ========================================================================== */

import { api, el, num, reportError, state, toast } from '../core.js';
import {
  callout, emptyState, layerChip, materializationChip,
} from '../components.js';
import { help } from '../prefs.js';
import { openModel } from './drawer.js';

export const meta = {
  title: 'Pipeline',
  subtitle: 'Your data flow, raw on the left through to business-ready on the right',
};

export function render(navigate) {
  const layers = state.boot?.layers || [];
  const models = state.models || [];

  if (!models.length) {
    return el(
      'div.panel',
      emptyState(
        'No models found',
        'The manifest parsed but contains no models or seeds. Check that model-paths in dbt_project.yml points at your models directory.',
      ),
    );
  }

  const unlayered = models.filter((model) => !layers.some((layer) => layer.key === model.layer));

  return el(
    'div',
    help(
      'Each column is a stage. Data enters on the left as an untouched copy of ' +
      'the source, gets cleaned in the middle, and is summarised for reporting ' +
      'on the right. Click any card to see its columns, its SQL and a preview ' +
      'of the actual data.',
    ),
    toolbar(navigate),
    el(
      'div.board.mt',
      ...layers.map((layer) => column(layer, models.filter((model) => model.layer === layer.key), navigate)),
    ),
    unlayered.length
      ? el(
          'div.mt',
          el(
            'div.panel',
            el(
              'div.panel-head',
              el('h3', 'Unlayered models'),
              el('span.muted.small', `${unlayered.length} not tagged bronze, silver or gold`),
            ),
            el(
              'div.panel-body',
              callout(
                'These models sit outside the medallion layers',
                'Move them into models/bronze, models/silver or models/gold, or add a layer tag in dbt_project.yml, so the lineage tells the whole story.',
                'info',
              ),
              el(
                'div.row.wrap.mt',
                { style: { gap: '7px' } },
                ...unlayered.map((model) =>
                  el('button.btn.btn-tiny', { onclick: () => openModel(model.name) }, model.name),
                ),
              ),
            ),
          ),
        )
      : null,
  );
}

/* -------------------------------------------------------------- toolbar --- */

function toolbar(navigate) {
  const search = el('input.input', {
    type: 'search',
    placeholder: 'Filter models…',
    style: { maxWidth: '260px' },
    oninput: (event) => filterCards(event.target.value),
  });

  return el(
    'div.row.wrap',
    { style: { gap: '9px' } },
    search,
    el('div.spacer'),
    el(
      'button.btn',
      { onclick: () => navigate('runs', { autorun: 'build' }) },
      '⚡ Build everything',
    ),
    el('button.btn', { onclick: () => navigate('catalog') }, '⌗ Lineage graph'),
  );
}

function filterCards(term) {
  const needle = term.trim().toLowerCase();
  for (const card of document.querySelectorAll('.model-card[data-name]')) {
    const haystack = `${card.dataset.name} ${card.dataset.desc || ''}`.toLowerCase();
    card.hidden = Boolean(needle) && !haystack.includes(needle);
  }
  for (const column of document.querySelectorAll('.board-col')) {
    const cards = Array.from(column.querySelectorAll('.model-card[data-name]'));
    const visible = cards.filter((card) => !card.hidden).length;
    const badge = column.querySelector('[data-role="count"]');
    if (badge) badge.textContent = `${visible}`;
  }
}

/* --------------------------------------------------------------- column --- */

function column(layer, models, navigate) {
  return el(
    'section.board-col',
    { dataset: { layer: layer.key } },
    el(
      'header.board-col-head',
      el(
        'div.row.between',
        el(
          'div.row',
          { style: { gap: '8px' } },
          layerChip(layer.key),
          el('span.chip', { dataset: { role: 'count' } }, String(models.length)),
          blockedLayer(layer.key) ? el('span.chip.err', 'read-only') : null,
        ),
        blockedLayer(layer.key)
          ? null
          : el(
              'button.btn.btn-tiny.btn-ghost',
              {
                onclick: () => navigate('runs', { select: `tag:${layer.key}`, autorun: 'build' }),
                title: `dbt build --select tag:${layer.key}`,
              },
              '⚡',
            ),
      ),
      el('h3', layer.label),
      el('p', layer.blurb),
    ),
    el(
      'div.board-col-body',
      models.length
        ? models.map((model) => card(model, navigate))
        : el('p.tiny.faint', { style: { padding: '6px 2px' } }, emptyHint(layer.key)),
    ),
  );
}

/** True when this UI is not permitted to build the layer. */
function blockedLayer(layerKey) {
  return (state.scope?.blocked_layers || []).includes(layerKey);
}

function emptyHint(layerKey) {
  return {
    seed: 'No seeds. Drop a CSV in seeds/ for reference data.',
    bronze: 'No bronze models. Land raw sources here first, one row in one row out.',
    silver: 'No silver models. Use the Silver Advisor on a bronze model to scaffold one.',
    gold: 'No gold models. Aggregate silver into a business grain here.',
  }[layerKey] || 'Nothing here yet.';
}

function card(model, navigate) {
  const outOfScope = model.in_scope === false;

  /* One status chip, not four. Most-severe wins, and the rest is available in
     the inspector. Five competing badges on a card is noise, not information. */
  let status = null;
  if (outOfScope) {
    status = el('span.chip.err', 'read-only');
  } else if (model.test_count === 0 && model.resource_type === 'model') {
    status = el('span.chip.err', 'no checks');
  } else if (!model.has_description) {
    status = el('span.chip.warn', 'needs a description');
  }

  return el(
    'button',
    {
      class: `model-card${outOfScope ? ' out-of-scope' : ''}`,
      dataset: { name: model.name, desc: model.description || '' },
      onclick: () => {
        if (outOfScope) {
          toast(`${model.name} is outside the permitted dataset scope.`, {
            kind: 'warn',
            detail:
              `It lives in '${model.dataset}'. This instance may only read: ` +
              `${(state.scope?.allowed_datasets || []).join(', ')}.`,
          });
          return;
        }
        openModel(model.name);
      },
      title: outOfScope
        ? `Outside the permitted scope (dataset ${model.dataset})`
        : 'Open inspector',
    },
    el('span.model-card-name', model.name),
    el(
      'span.model-card-meta',
      el('span.chip', `${model.column_count} columns`),
      model.test_count
        ? el('span.chip.ok', `${model.test_count} check${model.test_count === 1 ? '' : 's'}`)
        : null,
      status,
    ),
    model.description ? el('span.model-card-desc', model.description) : null,
  );
}
