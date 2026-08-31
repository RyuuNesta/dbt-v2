/* ==========================================================================
   app.js - boot, routing, header wiring.
   ========================================================================== */

import {
  $, $$, api, clear, el, num, reportError, secs, state, toast,
} from './core.js';
import { callout, emptyState, loading } from './components.js';
import { watchActive, watchJob } from './jobs.js';
import * as prefs from './prefs.js';
import { wireDrawer } from './views/drawer.js';

import * as overview from './views/overview.js';
import * as pipeline from './views/pipeline.js';
import * as workbench from './views/workbench.js';
import * as schema from './views/schema.js';
import * as advisor from './views/advisor.js';
import * as runs from './views/runs.js';
import * as catalog from './views/catalog.js';
import * as erd from './views/erd.js';
import * as schedule from './views/schedule.js';

const VIEWS = { overview, pipeline, workbench, schema, advisor, runs, catalog, erd, schedule };
const ORDER = ['overview', 'pipeline', 'workbench', 'schema', 'advisor', 'runs',
               'catalog', 'erd', 'schedule'];

let current = 'overview';

/* ------------------------------------------------------------------ boot --- */

async function boot() {
  const main = $('#main');
  clear(main).append(loading('Reading the dbt project…'));

  /* Appearance and help mode before the first paint, so there is no flash of
     the wrong theme. */
  prefs.init();

  wireDrawer(navigate);
  wireHeader();
  wireKeyboard();

  try {
    state.boot = await api.bootstrap();
  } catch (error) {
    clear(main).append(
      el(
        'div.panel',
        el(
          'div.panel-body',
          callout(
            'Cannot reach the dbt Studio backend',
            error.message,
            'err',
            el('pre.code-block', error.detail || 'Restart the server:\n  python dbt_ui/serve.py'),
          ),
        ),
      ),
    );
    return;
  }

  state.target = state.boot.default_target;
  state.stats = state.boot.stats;
  state.lastRun = state.boot.last_run;
  state.activeJob = state.boot.active_job;
  state.docsAvailable = state.boot.docs_available;
  state.scope = state.boot.scope;

  paintBrand();
  paintTargets();
  paintLegend();

  if (state.boot.manifest_error) {
    toast(state.boot.manifest_error, { kind: 'warn', timeout: 9000 });
  }

  await loadProjectData();

  const initial = location.hash.replace('#', '');
  navigate(ORDER.includes(initial) ? initial : 'overview');

  checkConnection();
  watchActive({
    onUpdate: paintRunDock,
    onDone: async () => {
      await loadProjectData();
      if (current === 'overview' || current === 'pipeline' || current === 'catalog') {
        navigate(current);
      }
    },
  });
}

/** Manifest-derived data every view reads from. */
async function loadProjectData() {
  try {
    const [models, refs, sources] = await Promise.all([
      api.models().catch(() => ({ models: [] })),
      api.refs().catch(() => ({ refs: [], sources: [] })),
      api.sources().catch(() => ({ sources: [] })),
    ]);
    state.models = models.models || [];
    state.refs = refs.refs || [];
    state.sources = refs.sources || [];
    state.sourceDetails = sources.sources || [];
    if (models.scope) {
      state.scope = models.scope;
      paintScope();
    }
    state.targetMismatch = models.target_mismatch || null;
    state.manifestTarget = models.manifest_target || null;
    paintMismatchPill();
  } catch (error) {
    reportError(error, 'Loading the project');
  }
}

/* --------------------------------------------------------------- routing --- */

export function navigate(name, params = {}) {
  if (!VIEWS[name]) name = 'overview';
  current = name;

  for (const button of $$('.nav-item')) {
    const on = button.dataset.view === name;
    button.classList.toggle('active', on);
    /* aria-current tells a screen reader which section it is in. */
    if (on) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  }

  const view = VIEWS[name];
  $('#view-title').textContent = view.meta?.title || name;
  $('#view-sub').textContent = view.meta?.subtitle || '';

  if (location.hash !== `#${name}`) {
    history.replaceState(null, '', `#${name}`);
  }

  const main = $('#main');
  clear(main);

  try {
    main.append(view.render(navigate, params));
  } catch (error) {
    main.append(
      el(
        'div.panel',
        el(
          'div.panel-body',
          callout(
            'This screen failed to render',
            String(error?.message || error),
            'err',
            el('pre.code-block', error?.stack || ''),
          ),
        ),
      ),
    );
  }

  main.scrollTop = 0;
}

/* ---------------------------------------------------------------- header --- */

function paintBrand() {
  const project = state.boot.project;
  $('#brand-project').textContent = project.name;
  $('#brand-project').title = project.dir;
}

function paintTargets() {
  const select = $('#target-select');
  clear(select);

  for (const target of state.boot.targets) {
    select.append(
      el(
        'option',
        { value: target.name, selected: target.name === state.target },
        `${target.name} · ${target.dataset}`,
      ),
    );
  }

  select.addEventListener('change', () => {
    const previous = state.target;
    state.target = select.value;
    const target = state.boot.targets.find((entry) => entry.name === state.target);

    if (state.target === 'prod') {
      const readable = (state.scope?.allowed_datasets || []).join(', ');
      const blocked = (state.scope?.blocked_layers || []).join(', ');
      const ok = window.confirm(
        'Switching to the prod target.\n\n' +
          `Queries will read PRODUCTION data (${readable}).\n\n` +
          'A dbt run from this UI will overwrite the production tables for the ' +
          'bronze and silver layers.' +
          (blocked ? `\n\nThe ${blocked} layer stays excluded and is never built from here.` : '') +
          '\n\nThis is the orchestrator\'s target, not a laptop\'s. Continue?',
      );
      if (!ok) {
        state.target = previous;
        select.value = previous;
        return;
      }
    }

    toast(`Environment is now ${state.target} (${target?.project}.${target?.dataset})`, {
      kind: state.target === 'prod' ? 'warn' : 'ok',
    });

    for (const warning of target?.warnings || []) {
      toast(`Environment '${state.target}'`, { kind: 'warn', detail: warning, timeout: 11000 });
    }

    checkConnection();

    /* Model references are frozen into the manifest when dbt parses, so simply
       changing this dropdown would leave every ref() pointing at the previous
       environment's datasets. Re-parse so the switch is real rather than
       cosmetic. */
    if (state.target !== previous) reparseForTarget(state.target);
    else navigate(current);
  });
}

async function reparseForTarget(target) {
  const dismiss = toast(`Reloading the project for ${target}…`, {
    kind: 'info',
    timeout: 60000,
  });
  try {
    const { job } = await api.refreshManifest();
    await new Promise((resolve) => {
      watchJob(job.id, {
        onDone: async () => {
          dismiss();
          await loadProjectData();
          const mf = state.boot?.targets?.find((t) => t.name === target);
          toast(`Project reloaded. References now point at ${mf?.dataset || target}.`, {
            kind: 'ok',
          });
          navigate(current);
          resolve();
        },
      });
    });
  } catch (error) {
    dismiss();
    if (error.busy) {
      toast('A run is in progress, so the project was not reloaded.', {
        kind: 'warn',
        detail:
          'Model references still point at the previous environment until you ' +
          'click Reload project.',
      });
    } else {
      reportError(error, 'Reloading the project');
    }
    navigate(current);
  }
}

function paintLegend() {
  const host = $('#layer-legend');
  clear(host);
  const byLayer = state.stats?.by_layer || {};

  for (const layer of state.boot.layers) {
    host.append(
      el(
        'div.legend-row',
        el('span.legend-swatch', { style: { background: `var(--${layer.key})` } }),
        el('span', layer.label),
        el('span.legend-count', String(byLayer[layer.key] || 0)),
      ),
    );
  }

  const docsLink = $('#docs-link');
  docsLink.hidden = !state.docsAvailable;

  paintScope();
}

/**
 * Mark the environment selector when the manifest was built for another target.
 * Colour alone is not enough, so the title carries the explanation too.
 */
function paintMismatchPill() {
  const select = $('#target-select');
  if (!select) return;

  const mismatch = state.targetMismatch;
  select.setAttribute('aria-invalid', mismatch ? 'true' : 'false');
  select.title = mismatch
    ? `${mismatch.message} ${mismatch.fix}`
    : 'Chooses which BigQuery environment every query and build uses.';
}

/** The dataset allowlist, always visible so the boundary is never a surprise. */
function paintScope() {
  const box = $('#scope-box');
  const list = $('#scope-list');
  const scope = state.scope;

  if (!scope?.allowed_datasets?.length) {
    box.hidden = true;
    return;
  }

  clear(list);
  for (const dataset of scope.allowed_datasets) {
    list.append(el('code', dataset));
  }
  box.hidden = false;
  box.title =
    `${scope.summary}\n\nEverything else in the project is refused before any ` +
    `query is sent.${scope.overridden ? '\n\nSet via DBT_UI_ALLOWED_DATASETS.' : ''}`;
}

function wireHeader() {
  $('#conn-pill').addEventListener('click', () => checkConnection(true));

  $('#btn-refresh-manifest').addEventListener('click', async () => {
    const button = $('#btn-refresh-manifest');
    button.disabled = true;
    button.textContent = '⟳ Parsing…';
    try {
      await api.refreshManifest();
      toast('dbt parse started.', { kind: 'ok', timeout: 2500 });
    } catch (error) {
      if (error.busy) toast(error.message, { kind: 'warn' });
      else reportError(error, 'Refresh manifest');
    } finally {
      setTimeout(() => {
        button.disabled = false;
        button.textContent = '⟳ Refresh manifest';
      }, 1200);
    }
  });

  $('#run-dock-open').addEventListener('click', () => navigate('runs'));
  $('#run-dock-cancel').addEventListener('click', async () => {
    const job = state.activeJob;
    if (!job) return;
    try {
      await api.cancelJob(job.id);
      toast('Cancellation requested.', { kind: 'warn' });
    } catch (error) {
      toast(error.message, { kind: 'err' });
    }
  });

  for (const button of $$('.nav-item')) {
    button.addEventListener('click', () => navigate(button.dataset.view));
  }

  window.addEventListener('hashchange', () => {
    const name = location.hash.replace('#', '');
    if (ORDER.includes(name) && name !== current) navigate(name);
  });
}

function wireKeyboard() {
  document.addEventListener('keydown', (event) => {
    /* Never hijack typing. */
    const tag = document.activeElement?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;

    const index = Number(event.key);
    if (Number.isInteger(index) && index >= 1 && index <= ORDER.length) {
      event.preventDefault();
      navigate(ORDER[index - 1]);
    }
  });
}

/* ------------------------------------------------------------ connection --- */

async function checkConnection(verbose = false) {
  const dot = $('#conn-dot');
  const text = $('#conn-text');

  dot.className = 'dot busy';
  text.textContent = 'checking…';

  try {
    const result = await api.connection();
    if (result.ok) {
      dot.className = 'dot ok';
      text.textContent = `${result.dataset} @ ${result.location}`;
      $('#conn-pill').title =
        `${result.project}.${result.dataset}\nlocation ${result.location}\nauth ${result.method}\nround trip ${result.duration_ms} ms`;
      if (verbose) toast(`Connected in ${result.duration_ms} ms`, { kind: 'ok', timeout: 2500 });
    } else {
      dot.className = 'dot err';
      text.textContent = 'not connected';
      $('#conn-pill').title = result.error || 'Connection failed';
      toast('BigQuery connection failed', {
        kind: 'err',
        detail: `${result.error || ''}\n\n${result.detail || ''}`.trim(),
      });
    }
  } catch (error) {
    dot.className = 'dot err';
    text.textContent = 'unreachable';
    if (verbose) reportError(error, 'Connection test');
  }
}

/* -------------------------------------------------------------- run dock --- */

function paintRunDock(job) {
  const dock = $('#run-dock');
  if (!job || !job.is_active) {
    dock.hidden = true;
    return;
  }
  dock.hidden = current === 'runs';
  $('#run-dock-label').textContent = `dbt ${job.command} · ${secs(job.duration)} · ${num(job.line_count)} lines`;
}

/* ------------------------------------------------------------------ init --- */

boot();
