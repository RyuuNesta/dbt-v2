/* ==========================================================================
   advisor.js - bronze to silver recommendations, across one table or many.

   Flow: choose the tables you care about, profile them, read the
   recommendations, uncheck what you disagree with, preview how the silver model
   would be built, then generate it and write it into the project.

   Every recommendation shows the measurement that produced it. That is the
   whole point: this is not a template, it is a reading of the actual data.

   Decisions worth knowing:

   - The table chooser is the shared tablePicker, the same component the
     Warehouse tab uses. It carries row counts and modification dates, which are
     exactly the context you want when deciding what to clean next, and it
     persists the selection so the page reopens where you left it.

   - Tables are analysed one after another, not in parallel. Each analysis is a
     real aggregate scan plus a group-by; firing six at once would multiply the
     warehouse load and the spend for no gain in wall-clock time that matters
     here. Results paint as they arrive so the wait is legible.

   - One table's failure never sinks the batch. A relation that has not been
     built yet is a normal state, so it is recorded against that table and the
     run carries on.

   - Recommendation ids are unique per analysis, not per project
     (`pruning:fiscal_year` is what a second table would call it too), so the
     accepted set is kept per table rather than in one flat set.
   ========================================================================== */

import {
  api, bytes, clear, copy, download, el, ms, num, pct, state, toast,
} from '../core.js';
import {
  CATEGORY_LABELS, callout, codeBlock, confidenceChip, emptyState, layerChip,
  loading, schemaTable, tabs,
} from '../components.js';
import { tablePicker } from '../picker.js';

export const meta = {
  title: 'Cleanup Advisor',
  subtitle: 'Profile the tables you choose and get concrete silver transformations',
};

/** Layers this page can usefully analyse. Gold is never offered. */
const ANALYSABLE_LAYERS = ['seed', 'bronze', 'silver'];

export function render(navigate, params = {}) {
  const host = el('div');
  const progressHost = el('div');
  const output = el('div');

  /** model name -> { model, row, analysis, accepted:Set, error } */
  const sessions = new Map();
  /** Bumps on every new batch, so a superseded run cannot paint over a newer one. */
  let runToken = 0;
  let running = false;

  /* A deep link from the model drawer names a model, not a physical table. The
     picker is keyed by `dataset.table`, so the mapping has to wait until the
     inventory has loaded. */
  let pendingModel = params.model || state.scratch.advisorModel || '';
  let deepLinkResolved = false;

  const analyseBtn = el(
    'button.btn.btn-primary.btn-block',
    { onclick: () => analyseSelection() },
    '✦ Analyse and recommend',
  );

  /* ------------------------------------------------------------ chooser --- */

  const picker = tablePicker({
    storageKey: 'advisor',
    title: 'Tables to analyse',
    /* Show every in-scope physical table so any one can be ticked, not just the
       dbt-managed bronze/silver ones. Analysis still needs a ref() target, so a
       foreign table (no dbt model) is listed but flagged, and analyseSelection
       skips it with a clear message rather than silently dropping it. Views are
       included too - profiling a view is valid, it just reads the underlying
       tables. */
    rowFilter: () => true,
    onChange: (rows) => {
      paintAnalyseButton(rows);
      if (!deepLinkResolved) resolveDeepLink();
    },
  });

  function paintAnalyseButton(rows) {
    const count = rows.length;
    analyseBtn.disabled = running || count === 0;
    analyseBtn.textContent = count > 1
      ? `✦ Analyse ${count} tables`
      : '✦ Analyse and recommend';
  }

  /**
   * Turn a `?model=` deep link into a picker selection, once the inventory is
   * available to map it against. Runs at most once.
   */
  function resolveDeepLink() {
    const payload = picker.payload();
    if (!payload) return;

    deepLinkResolved = true;
    if (!pendingModel) return;

    const match = (payload.tables || []).find((table) => table.model === pendingModel);
    const wanted = pendingModel;
    pendingModel = '';

    if (!match) {
      toast(`${wanted} has no physical table to profile yet.`, {
        kind: 'warn',
        detail: 'Build it once, then refresh this list. Until then there is '
              + 'nothing in the warehouse to measure.',
      });
      return;
    }

    /* setSelected announces, which re-enters onChange - harmless now that
       deepLinkResolved is set. */
    picker.setSelected([match.qualified]);
    analyseSelection();
  }

  /* ------------------------------------------------------------ analyse --- */

  async function analyseSelection() {
    const chosen = picker.selected();

    if (!chosen.length) {
      toast('Tick at least one table to analyse.', { kind: 'warn' });
      return;
    }

    /* A table dbt builds carries a model name; a pre-existing ("foreign") one
       does not. Both are analysed and both can generate a silver model - the
       only difference is that a foreign table is read by its full relation name
       instead of ref(), which the generated SQL notes. */
    const foreign = chosen.filter((row) => !row.model);
    if (foreign.length) {
      toast(
        `${foreign.length} of the selected table${foreign.length === 1 ? ' is' : 's are'} not built by dbt.`,
        {
          kind: 'info',
          detail: 'They are analysed and can still generate a silver model; it '
            + 'reads the table by its full name instead of ref(), with a note on '
            + 'how to promote it to a dbt source.',
        },
      );
    }

    const token = ++runToken;
    running = true;
    sessions.clear();
    state.scratch.advisorModel = chosen.length === 1 ? (chosen[0].model || '') : '';
    paintAnalyseButton(chosen);

    /* A stable, unique key for every table, model or not. */
    const keyOf = (row) => row.model || row.qualified;
    const nameOf = (row) => row.model || row.qualified;

    clear(output);
    paintProgress(0, chosen.length, nameOf(chosen[0]));

    for (let index = 0; index < chosen.length; index += 1) {
      /* A newer batch has started; abandon this one rather than interleaving. */
      if (token !== runToken) return;

      const row = chosen[index];
      paintProgress(index, chosen.length, nameOf(row));

      const session = {
        key: keyOf(row),
        model: row.model || null,
        name: nameOf(row),
        foreign: !row.model,
        row,
        analysis: null,
        accepted: new Set(),
        error: null,
      };
      sessions.set(session.key, session);

      try {
        /* dbt table -> resolve by model name; foreign table -> by its physical
           relation. The backend accepts either. */
        const analysis = await api.analyse(
          row.model ? { model: row.model } : { relation: row.relation },
        );
        if (token !== runToken) return;
        session.analysis = analysis;
        for (const rec of analysis.recommendations || []) {
          if (rec.default_applied) session.accepted.add(rec.id);
        }
      } catch (error) {
        if (token !== runToken) return;
        session.error = error;
      }

      /* Paint after every table so a long batch shows its work. */
      paintOutput(navigate, sessions, chosen);
    }

    if (token !== runToken) return;

    running = false;
    clear(progressHost);
    paintAnalyseButton(picker.selected());
    paintOutput(navigate, sessions, chosen);

    const failed = [...sessions.values()].filter((s) => s.error);
    if (chosen.length > 1) {
      toast(
        `Analysed ${chosen.length - failed.length} of ${chosen.length} tables.`,
        {
          kind: failed.length ? 'warn' : 'ok',
          detail: failed.length
            ? `Could not profile: ${failed.map((s) => s.name).join(', ')}.`
            : '',
        },
      );
    }
  }

  function paintProgress(done, total, current) {
    if (total === 1) {
      clear(progressHost).append(
        el('div.panel', loading(
          `Profiling ${current}: measuring nulls, cardinality, ranges and duplicate keys…`,
        )),
      );
      return;
    }

    const fraction = total ? done / total : 0;
    clear(progressHost).append(
      el(
        'div.panel.batch-progress',
        el(
          'div.panel-body',
          el(
            'div.row.between.mb',
            el('span.small', `Profiling ${current}`),
            el('span.small.faint', `${done} of ${total} done`),
          ),
          el(
            'div.meter',
            { role: 'progressbar', 'aria-valuenow': String(done), 'aria-valuemin': '0', 'aria-valuemax': String(total) },
            el('div.meter-fill', { style: { width: `${Math.round(fraction * 100)}%` } }),
          ),
          el(
            'p.tiny.faint',
            { style: { margin: '9px 0 0', lineHeight: '1.55' } },
            'One table at a time: each analysis is a real aggregate scan plus a '
            + 'group-by to verify the key. Results appear as they finish.',
          ),
        ),
      ),
    );
  }

  /* ----------------------------------------------------------- assemble --- */

  host.append(
    el(
      'div.panel.mb',
      el(
        'div.panel-head',
        el('h3', 'Choose what to clean'),
        el('span.small.faint', 'any accessible table'),
      ),
      el(
        'div.panel-body',
        el(
          'p.small.faint',
          { style: { marginTop: 0, lineHeight: '1.55' } },
          'Tick the tables you want to promote. Row counts and modification '
          + 'dates come from free metadata, so choosing costs nothing. Silver '
          + 'tables can be analysed too, to check what gold should aggregate. '
          + 'Your selection is remembered.',
        ),
        picker.node,
        el('div.mt', analyseBtn),
      ),
    ),
    progressHost,
    output,
  );

  clear(output).append(
    el(
      'div.panel',
      emptyState(
        'Nothing analysed yet',
        'Choose one or more tables and run the analysis. Every recommendation is '
        + 'derived from a measurement taken against the real table, with the '
        + 'evidence attached.',
      ),
    ),
  );

  paintAnalyseButton([]);

  return host;

  /* ------------------------------------------------------------ output --- */

  function paintOutput(nav, all, chosen) {
    const list = chosen
      .map((row) => all.get(row.model || row.qualified))
      .filter(Boolean);

    if (!list.length) return;

    clear(output);

    if (list.length === 1) {
      output.append(deepDive(nav, list[0], { standalone: true }));
      return;
    }

    output.append(batchSummary(nav, list, chosen.length));
  }

  /* ------------------------------------------------- batch (many tables) --- */

  function batchSummary(nav, list, expected) {
    const ok = list.filter((session) => session.analysis);
    const failed = list.filter((session) => session.error);

    const totals = ok.reduce(
      (acc, session) => {
        acc.recs += session.analysis.summary.total;
        acc.high += session.analysis.summary.high_confidence;
        acc.rows += session.analysis.profile.row_count || 0;
        acc.columns += session.analysis.profile.columns.length;
        acc.scanned += session.analysis.profile.bytes_processed || 0;
        return acc;
      },
      { recs: 0, high: 0, rows: 0, columns: 0, scanned: 0 },
    );

    const rows = el('tbody');
    const detailHost = el('div.mt');

    for (const session of list) {
      const { analysis, error } = session;
      const label = session.name;

      const open = () => {
        clear(detailHost).append(deepDive(nav, session, { standalone: false }));
        detailHost.scrollIntoView({ behavior: 'smooth', block: 'start' });
      };

      rows.append(
        el(
          error ? 'tr.is-failed' : 'tr',
          el('td', layerChip(session.row.layer)),
          el(
            'td',
            el(
              'button.btn.btn-tiny.btn-ghost',
              {
                disabled: Boolean(error),
                style: { fontFamily: 'var(--mono)' },
                onclick: open,
              },
              label,
              session.foreign ? el('span.chip.tiny', { style: { marginLeft: '6px' } }, 'foreign') : null,
            ),
          ),
          el('td.num', analysis ? num(analysis.profile.row_count) : '—'),
          el('td.num', analysis ? num(analysis.profile.columns.length) : '—'),
          el(
            'td',
            analysis
              ? (analysis.duplicate_check?.checked
                  ? (analysis.duplicate_check.is_unique
                      ? el('span.chip.ok', 'unique')
                      : el('span.chip.warn', `${num(analysis.duplicate_check.duplicated_keys)} dup`))
                  : el('span.chip.faint', 'no key'))
              : el('span.chip.err', 'not profiled'),
          ),
          el('td.num', analysis ? num(analysis.summary.total) : '—'),
          el(
            'td.num',
            analysis
              ? (analysis.summary.high_confidence
                  ? el('span.chip.ok', String(analysis.summary.high_confidence))
                  : el('span.chip', '0'))
              : '—',
          ),
          el(
            'td',
            error
              ? el(
                  'span.small.faint',
                  { title: `${error.message}\n\n${error.detail || ''}`.trim() },
                  error.message.length > 46 ? `${error.message.slice(0, 45)}…` : error.message,
                )
              : el('button.btn.btn-tiny', { onclick: open }, 'Open →'),
          ),
        ),
      );
    }

    const node = el(
      'div',
      el(
        'div.panel',
        el(
          'div.panel-head',
          el('h3', `${list.length} of ${expected} tables analysed`),
          el(
            'div.row',
            { style: { gap: '6px' } },
            el('span.chip.ok', `${totals.high} high confidence`),
            el('span.chip', `${totals.recs} recommendations`),
          ),
        ),
        el(
          'div.panel-body',
          el(
            'div.grid.grid-4.mb',
            fact(num(ok.length), 'Tables profiled', failed.length ? `${failed.length} failed` : 'all succeeded', failed.length ? 'warn' : 'ok'),
            fact(num(totals.rows), 'Rows measured'),
            fact(num(totals.columns), 'Columns measured'),
            fact(bytes(totals.scanned), 'Scanned', 'billed once per table'),
          ),
          failed.length
            ? callout(
                `${failed.length} table${failed.length === 1 ? '' : 's'} could not be profiled`,
                failed.map((session) => `${session.model}: ${session.error.message}`).join(' · '),
                'warn',
                el(
                  'p.tiny.faint',
                  { style: { margin: '7px 0 0' } },
                  'A relation has to exist in the warehouse before it can be '
                  + 'measured. Build it once, then analyse again.',
                ),
              )
            : null,
          el(
            'div.table-wrap.mt',
            el(
              'table.data.compact',
              el(
                'thead',
                el(
                  'tr',
                  el('th', 'Layer'),
                  el('th', 'Table'),
                  el('th.num', 'Rows'),
                  el('th.num', 'Cols'),
                  el('th', 'Key'),
                  el('th.num', 'Recs'),
                  el('th.num', 'High'),
                  el('th', ''),
                ),
              ),
              rows,
            ),
          ),
          el(
            'p.tiny.faint',
            { style: { margin: '10px 0 0', lineHeight: '1.55' } },
            'Open a table to read its recommendations, adjust them, and generate '
            + 'its silver model. Each table is generated separately, because each '
            + 'one becomes its own model file.',
          ),
        ),
      ),
      detailHost,
    );

    return node;
  }

  /* ------------------------------------------------ single-table deep dive --- */

  function deepDive(nav, session, { standalone }) {
    if (session.error) {
      return el(
        'div.panel',
        el(
          'div.panel-body',
          callout(
            `Could not analyse ${session.name}`,
            session.error.message,
            'err',
            el(
              'div',
              session.error.detail ? el('pre.code-block', session.error.detail) : null,
              /* Only a dbt model can be built from here; a foreign table already
                 exists or the failure is a permission/scope issue. */
              session.model
                ? el(
                    'div.mt',
                    el('span.small.faint', 'A relation has to exist in the warehouse before it can be profiled. '),
                    el(
                      'button.btn.btn-tiny',
                      { onclick: () => nav('runs', { select: session.model, autorun: 'run' }) },
                      `⚡ Build ${session.model}`,
                    ),
                  )
                : null,
            ),
          ),
        ),
      );
    }

    if (!session.analysis) {
      return el('div.panel', loading(`Profiling ${session.name}…`));
    }

    const { analysis, accepted, model } = session;
    const foreign = session.foreign;
    const displayName = session.name;

    /* Built lazily so a big profile does not wait on codegen, and cached so
       flipping back to a tab does not re-request.

       This hangs off tabs()' onChange rather than wrapping the returned
       select(): the tab buttons close over tabs()' internal select, so a
       wrapper on the returned object only ever fires for programmatic calls and
       never for an actual click. */
    const built = new Set();
    let view = null;

    function onTab(index) {
      /* tabs() selects `initial` during construction, before `view` exists. */
      if (!view) return;
      if (index === 2 && !built.has(2)) { built.add(2); buildPreview(); }
      if (index === 3 && !built.has(3)) { built.add(3); buildSilver(); }
    }

    /* Both dbt-built and foreign tables get the full flow. The only difference
       is how the generated model names its input: a dbt model is ref()'d, a
       foreign table is read by its fully-qualified relation (with a note in the
       SQL on how to promote it to a dbt source). */
    const tabDefs = [
      { label: 'Recommendations', count: analysis.summary.total, render: () => recommendationsPanel() },
      { label: 'Profile', count: analysis.profile.columns.length, render: () => profilePanel(analysis.profile) },
      { label: 'How it will be built', render: () => el('div.panel-body', loading('Working out the plan…')) },
      { label: 'Generated silver model', render: () => el('div.panel-body', loading('Building the model…')) },
    ];
    view = tabs(tabDefs, { onChange: onTab });

    const node = el(
      'div',
      standalone ? null : el('div.deep-dive-mark', el('span.small.faint', `Detail · ${displayName}`)),
      summaryPanel(),
      el('div.panel.mt', view.node),
    );

    view.select(0);

    return node;

    /* ------------------------------------------------------- summary --- */

    function summaryPanel() {
      const profile = analysis.profile;
      const dup = analysis.duplicate_check;
      const plan = analysis.plan;

      return el(
        'div.panel',
        el(
          'div.panel-head',
          el('h3', foreign
            ? displayName
            : `${model} → ${analysis.suggested_model_name}`),
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
            fact(
              num(profile.row_count),
              'Rows profiled',
              profile.sampled ? `sampled from ${num(profile.declared_row_count)}` : 'full table',
            ),
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

    /* ----------------------------------------------- recommendations --- */

    function recommendationsPanel() {
      const groups = new Map();
      for (const rec of analysis.recommendations) {
        if (!groups.has(rec.category)) groups.set(rec.category, []);
        groups.get(rec.category).push(rec);
      }

      const body = el('div.panel-body');
      const countBadge = el('span.small.faint', `${accepted.size} selected`);

      body.append(
        el(
          'div.row.wrap.mb',
          { style: { gap: '7px' } },
          el('button.btn.btn-tiny', { onclick: () => setAll(true) }, 'Select all'),
          el('button.btn.btn-tiny', { onclick: () => setAll(false) }, 'Clear all'),
          el('button.btn.btn-tiny', { onclick: () => setHighOnly() }, 'High confidence only'),
          el('div.spacer'),
          countBadge,
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

      function recCard(rec) {
        const box = el('input', { type: 'checkbox' });
        box.checked = accepted.has(rec.id);
        box.addEventListener('change', () => {
          if (box.checked) accepted.add(rec.id);
          else accepted.delete(rec.id);
          invalidateDerived();
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
        /* Scoped to this panel: a batch view can hold several of these at once,
           and a document-wide query would rewrite another table's checkboxes. */
        for (const card of body.querySelectorAll('.rec[data-rec-id]')) {
          const box = card.querySelector('input[type=checkbox]');
          if (box) box.checked = accepted.has(card.dataset.recId);
        }
        invalidateDerived();
        updateCount();
      }

      function updateCount() {
        countBadge.textContent = `${accepted.size} selected`;
      }
    }

    /** The plan and the generated SQL both depend on the accepted set. */
    function invalidateDerived() {
      built.delete(2);
      built.delete(3);
      view.setPanel(2, el('div.panel-body', el(
        'div.stale-note',
        el('span.small.faint', 'Your selection changed. Open this tab to rebuild the plan.'),
      )));
      view.setPanel(3, el('div.panel-body', el(
        'div.stale-note',
        el('span.small.faint', 'Your selection changed. Open this tab to regenerate the model.'),
      )));
    }

    /* ----------------------------------------------- foreign-table note --- */

    /* --------------------------------------------------- build preview --- */

    /* How the backend should resolve the source: by model for a dbt table, by
       physical relation for a foreign one. Both endpoints accept either. */
    function sourceRef() {
      return model ? { model } : { relation: session.row.relation };
    }

    async function buildPreview() {
      try {
        const payload = await api.previewSilver({
          ...sourceRef(),
          accepted_ids: [...accepted],
          materialized: 'view',
        });
        view.setPanel(2, previewPanel(payload, () => {
          view.select(3);
        }));
      } catch (error) {
        view.setPanel(
          2,
          el('div.panel-body', callout('Could not work out the plan', error.message, 'err',
            error.detail ? el('pre.code-block', error.detail) : null)),
        );
      }
    }

    /**
     * The transparency preview: what is about to happen, and why, before any
     * SQL exists. Read-only by design - the way to change the outcome is to
     * change the recommendations, which is one click away.
     */
    function previewPanel(payload, onApprove) {
      const estimate = payload.row_estimate || {};

      return el(
        'div.panel-body',
        callout(
          'Nothing has been generated or written yet',
          'This is a reading of the accepted recommendations. Every number below '
          + 'comes from the profile that has already been taken, so opening this '
          + 'tab costs nothing and scans nothing.',
          'info',
        ),

        /* ---------- sources ---------- */
        el('div.stat-label.mt.mb', 'Reads from'),
        ...payload.sources.map((source) =>
          el(
            'div.plan-source',
            el(
              'div.row.wrap.between',
              el('code.mono.small', source.reference),
              el('span.small.faint', `${num(source.row_count)} rows`),
            ),
            el('div.tiny.faint.mono', source.relation),
            el('p.tiny.faint', { style: { margin: '5px 0 0', lineHeight: '1.5' } }, source.note),
          ),
        ),

        /* ---------- row estimate ---------- */
        el('div.stat-label.mt.mb', 'Estimated result'),
        el(
          'div.grid.grid-4.mb',
          fact(num(estimate.source_rows), 'Source rows'),
          fact(
            estimate.rows === null || estimate.rows === undefined ? 'unknown' : num(estimate.rows),
            'Output rows',
            estimate.exact ? 'exact' : 'estimate',
            estimate.exact ? 'ok' : 'warn',
          ),
          fact(
            estimate.removed === null || estimate.removed === undefined ? '—' : num(estimate.removed),
            'Rows removed',
            estimate.removed ? 'by deduplication' : 'none',
          ),
          fact(num(payload.column_count), 'Output columns',
               payload.dropped_columns.length ? `${payload.dropped_columns.length} omitted` : 'nothing omitted'),
        ),
        el('p.tiny.faint', { style: { margin: '0 0 4px', lineHeight: '1.55' } }, estimate.basis),

        /* ---------- steps ---------- */
        el('div.stat-label.mt.mb', `Transformations, in the order they apply (${payload.steps.length})`),
        el(
          'ol.plan-steps',
          ...payload.steps.map((step) =>
            el(
              'li.plan-step',
              { dataset: { kind: step.kind } },
              el('div.plan-step-title', step.title),
              el('div.plan-step-detail', step.detail),
              step.evidence
                ? el('div.plan-step-evidence', step.evidence)
                : null,
              step.sql ? el('code.plan-step-sql', step.sql) : null,
              step.columns.length
                ? el(
                    'div.row.wrap',
                    { style: { gap: '4px', marginTop: '6px' } },
                    ...step.columns.map((name) => el('code.chip.tiny', name)),
                  )
                : null,
            ),
          ),
        ),

        /* ---------- resulting schema ---------- */
        el('div.stat-label.mt.mb', `Resulting schema (${payload.column_count} columns)`),
        el(
          'div.table-wrap',
          { style: { maxHeight: '46vh' } },
          el(
            'table.data.compact',
            el('thead', el('tr',
              el('th', 'Column'),
              el('th', 'Type'),
              el('th', 'Origin'),
              el('th', 'How it is produced'))),
            el(
              'tbody',
              ...payload.columns.map((column) =>
                el(
                  'tr',
                  el('td.mono.small', column.name),
                  el('td', el('span.chip.tiny', column.data_type)),
                  el('td', el(`span.chip.tiny.origin-${column.origin}`, column.origin)),
                  el('td.small.faint', column.note || '—'),
                ),
              ),
            ),
          ),
        ),

        /* ---------- tests ---------- */
        payload.tests.length
          ? el(
              'div.mt',
              el('div.stat-label.mb', 'Tests worth declaring'),
              ...payload.tests.map((test) =>
                el(
                  'div.plan-test',
                  el('code.mono.small', test.column),
                  el('span.small', test.tests),
                  el('div.tiny.faint', test.why),
                ),
              ),
            )
          : null,

        payload.dropped_columns.length
          ? el(
              'div.mt',
              callout(
                `${payload.dropped_columns.length} column(s) will be omitted`,
                payload.dropped_columns.join(', '),
                'info',
              ),
            )
          : null,

        /* ---------- approve ---------- */
        el(
          'div.sticky-actions.mt',
          el(
            'div.row.wrap',
            { style: { gap: '8px', alignItems: 'center' } },
            el(
              'button.btn.btn-primary',
              { onclick: onApprove },
              '✓ Approve & generate the model',
            ),
            el(
              'button.btn.btn-tiny',
              { onclick: () => view.select(0) },
              '← Adjust the recommendations',
            ),
            el(
              'span.small.faint',
              'Generating writes nothing on its own. You choose the path and '
              + 'confirm the write on the next tab.',
            ),
          ),
        ),
      );
    }

    /* ----------------------------------------------------- generated --- */

    async function buildSilver() {
      try {
        const payload = await api.generateSilver({
          ...sourceRef(),
          accepted_ids: [...accepted],
          materialized: 'view',
        });
        view.setPanel(3, silverPanel(payload));
      } catch (error) {
        view.setPanel(
          3,
          el('div.panel-body', callout('Could not generate the model', error.message, 'err',
            error.detail ? el('pre.code-block', error.detail) : null)),
        );
      }
    }

    function silverPanel(payload) {
      const pathInput = el('input.input', { value: payload.path });
      const status = el('div');

      /* The generated model is a first draft. Editing it here rather than after
         the write means the file that lands in the project is the one that was
         actually reviewed, and the .bak dance is not needed to fix a typo. */
      let edited = null;
      let editing = false;
      const sqlHost = el('div');

      const currentSql = () => (edited === null ? payload.sql : edited);

      const editToggle = el(
        'button.btn.btn-tiny',
        {
          onclick: () => {
            editing = !editing;
            paintSql();
          },
        },
        '✎ Edit before writing',
      );

      function paintSql() {
        clear(sqlHost);
        editToggle.textContent = editing ? '✓ Done editing' : '✎ Edit before writing';

        if (!editing) {
          sqlHost.append(codeBlock(currentSql(), { tall: true, title: payload.path }));
          return;
        }

        const area = el('textarea.input.code-area', {
          spellcheck: 'false',
          'aria-label': 'Generated silver model SQL',
          oninput: (event) => { edited = event.target.value; },
        });
        area.value = currentSql();

        sqlHost.append(
          area,
          el(
            'div.row.wrap.mt',
            { style: { gap: '7px', alignItems: 'center' } },
            el(
              'button.btn.btn-tiny.btn-ghost',
              {
                disabled: edited === null,
                onclick: () => { edited = null; paintSql(); },
              },
              '↺ Revert to generated',
            ),
            el('span.tiny.faint',
               'Jinja is not validated here. dbt compiles it when you build.'),
          ),
        );
      }

      paintSql();

      async function write(mode) {
        clear(status).append(loading(`Writing ${pathInput.value}…`));
        try {
          const result = await api.writeFile(pathInput.value.trim(), currentSql(), mode);
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
                el('button.btn.btn-tiny', { onclick: () => nav('runs', { autorun: 'parse' }) }, '⟳ Refresh manifest'),
                el(
                  'button.btn.btn-tiny',
                  { onclick: () => nav('runs', { select: payload.model_name, autorun: 'run' }) },
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
          el('button.btn.btn-tiny', { onclick: () => copy(currentSql(), 'Model copied') }, '⧉ Copy'),
          el('button.btn.btn-tiny', { onclick: () => download(`${payload.model_name}.sql`, currentSql()) }, '↓ Download'),
          editToggle,
          el(
            'button.btn.btn-tiny',
            {
              onclick: () => {
                view.setPanel(3, el('div.panel-body', loading('Regenerating…')));
                buildSilver();
              },
            },
            '↻ Regenerate from current selection',
          ),
          el('button.btn.btn-tiny.btn-ghost', { onclick: () => view.select(2) }, '← Back to the plan'),
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
        sqlHost,
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
  }
}

/* ---------------------------------------------------------- profile tab --- */

function profilePanel(profile) {
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

/* --------------------------------------------------------- shared bits --- */

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

function fact(value, label, note, kind = '') {
  return el(
    'div.stat',
    el('span.stat-value', { style: kind ? { color: `var(--${kind})` } : null, class: 'stat-value' }, value),
    el('span.stat-label', label),
    note ? el('span.stat-note', note) : null,
  );
}
