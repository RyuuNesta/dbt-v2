/* ==========================================================================
   schema.js - the Documentation page.

   One screen, two independent choices, rather than the old three "engine"
   cards that were really the same machine relabelled:

     Source       where the columns come from
                    - Model          a dbt model, types read from its table
                    - Query          a SELECT, dry-run for its output columns
                    - Existing table a dataset.table dbt does not build

     Descriptions who writes the prose
                    - Pattern   deterministic local rules (free, offline)
                    - AI        Gemini
                    - None      schema only, TODO placeholders to fill by hand

   The two are orthogonal: any source pairs with any description engine. The
   only thing the source decides is the *output shape*:

     Model / Query    -> a models: schema YAML (a model contract)
     Existing table   -> a sources: block (declares the table to dbt), and it
                         is the only source that offers the register-with-dbt
                         step (dbt parse + dbt docs generate) that makes dbt
                         actually track and document the table.

   dbt itself never writes descriptions - it has no engine for that - so the
   prose always comes from Pattern or AI. The "dbt" value in the Existing-table
   path is the sources: declaration plus the dbt commands that ingest it.
   ========================================================================== */

import {
  api, can, clear, copy, download, el, num, pct, state, toast,
} from '../core.js';
import {
  callout, codeBlock, columnContract, emptyState, loading,
  layerChip, schemaTable, sqlEditor, tabs, typeBadge,
} from '../components.js';
import { watchJob } from '../jobs.js';

export const meta = {
  title: 'Documentation',
  subtitle: 'Document a model, a query, or an existing table - descriptions by pattern rules or AI',
};

/* Remembered across navigations so the page reopens where you left it. */
const remembered = { source: 'model', engine: 'pattern' };

export function render(navigate, params = {}) {
  return documentation(navigate, params);
}

/* ======================================================================
   the one unified view
   ====================================================================== */

function documentation(navigate, params = {}) {
  const host = el('div');
  const output = el('div.mt');
  const canWrite = can('can_write_files');
  const canRunDbt = can('can_run_dbt');

  const target = (state.boot?.targets || []).find((t) => t.name === state.target)
    || (state.boot?.targets || [])[0] || {};
  const exampleDataset = (state.boot?.scope?.allowed_datasets || ['bronze_dbt'])[0];

  let aiStatus = state.boot?.ai || {};
  let aiModel = aiStatus.default_model || 'gemini-2.5-flash';

  /* Source + engine, restored from last visit or a deep link. */
  let source = params.relation ? 'table' : params.sql ? 'query' : (remembered.source || 'model');
  let engine = remembered.engine || 'pattern';
  let selected = params.model || state.scratch.schemaModel || firstModel();

  /* ---------------------------------------------------- source: model --- */

  const modelList = el('div.scroll-list');
  function paintModelList() {
    clear(modelList);
    const models = state.models || [];
    const inScope = models.filter((m) => m.in_scope !== false);
    const blocked = models.filter((m) => m.in_scope === false);
    for (const model of [...inScope, ...blocked]) {
      const outOfScope = model.in_scope === false;
      modelList.append(el('button', {
        class: `list-btn${model.name === selected ? ' sel' : ''}${outOfScope ? ' out-of-scope' : ''}`,
        dataset: { name: model.name.toLowerCase() },
        title: outOfScope ? `Outside the permitted scope (dataset ${model.dataset})` : model.name,
        onclick: () => {
          if (outOfScope) {
            toast(`${model.name} is outside the permitted dataset scope.`, {
              kind: 'warn',
              detail: `It lives in '${model.dataset}'. Permitted: ${(state.scope?.allowed_datasets || []).join(', ')}.`,
            });
            return;
          }
          selected = model.name;
          state.scratch.schemaModel = model.name;
          paintModelList();
          generate();
        },
      },
        layerChip(model.layer),
        el('span.lb-name', model.name),
        el('span.lb-meta',
          outOfScope ? el('span.chip.err', 'out of scope')
            : model.has_description ? el('span.chip.ok', `${model.documented_columns}/${model.column_count}`)
            : el('span.chip.warn', 'undoc'))));
    }
  }

  /* ---------------------------------------------------- source: query --- */

  const sqlEditorInstance = sqlEditor({
    value: params.sql || `select *\nfrom {{ ref('${selected || 'your_model'}') }}\n`,
    placeholder: "select ... from {{ ref('model') }}",
  });
  const nameInput = el('input.input', { value: 'my_new_model', placeholder: 'model name' });

  /* --------------------------------------------- source: existing table --- */

  const relInput = el('input.input', {
    placeholder: `${exampleDataset}.your_table`,
    'aria-label': 'Fully-qualified table to declare',
    value: params.relation || '',
    autocomplete: 'off',
    spellcheck: 'false',
    style: { fontFamily: 'var(--mono)' },
  });
  const acMenu = el('div.autocomplete', { hidden: true });
  const relWrap = el('div.ac-input-wrap', relInput, acMenu);
  const tableCache = new Map();
  let acRows = [];
  let acSel = -1;

  const allowedDatasets = () => (state.boot?.scope?.allowed_datasets || []).map(String);

  async function tablesIn(dataset) {
    const key = dataset.toLowerCase();
    if (tableCache.has(key)) return tableCache.get(key);
    try {
      const payload = await api.autocompleteSchema(dataset);
      const names = (payload.tables || []).map((t) => t.table);
      tableCache.set(key, names);
      return names;
    } catch {
      tableCache.set(key, []);
      return [];
    }
  }
  function closeAc() { acMenu.hidden = true; acRows = []; acSel = -1; }
  function renderAc(items, onpick) {
    clear(acMenu);
    acRows = items;
    if (!items.length) { closeAc(); return; }
    items.forEach((item, i) => {
      acMenu.append(el('div', {
        class: `ac-item${i === acSel ? ' sel' : ''}`,
        role: 'option',
        onmousedown: (e) => { e.preventDefault(); onpick(item); },
        onmouseenter: () => {
          acSel = i;
          for (const c of acMenu.children) c.classList.remove('sel');
          acMenu.children[i]?.classList.add('sel');
        },
      },
        el('span.ac-kind', { 'aria-hidden': 'true' }, item.kind === 'dataset' ? '⊞' : '▤'),
        el('span.ac-name', item.label),
        item.meta ? el('span.ac-meta', item.meta) : null));
    });
    acSel = 0;
    acMenu.children[0]?.classList.add('sel');
    acMenu.hidden = false;
  }
  async function refreshAc() {
    const value = relInput.value;
    const dotAt = value.indexOf('.');
    if (dotAt === -1) {
      const frag = value.trim().toLowerCase();
      renderAc(
        allowedDatasets()
          .filter((d) => !frag || d.toLowerCase().includes(frag))
          .map((d) => ({ kind: 'dataset', label: d, insert: `${d}.`, meta: 'dataset' })),
        (item) => { relInput.value = item.insert; relInput.focus(); refreshAc(); });
      return;
    }
    const dataset = value.slice(0, dotAt).trim();
    const partial = value.slice(dotAt + 1).trim().toLowerCase();
    if (!allowedDatasets().some((d) => d.toLowerCase() === dataset.toLowerCase())) { closeAc(); return; }
    const names = await tablesIn(dataset);
    const matches = names
      .filter((n) => !partial || n.toLowerCase().includes(partial))
      .slice(0, 40)
      .map((n) => ({ kind: 'table', label: n, insert: `${dataset}.${n}`, meta: 'table' }));
    if (!matches.length && !names.length) {
      renderAc([{ kind: 'table', label: 'no tables available', insert: null, meta: 'BigQuery unreachable' }],
        () => closeAc());
      return;
    }
    renderAc(matches, (item) => { if (!item.insert) return; relInput.value = item.insert; closeAc(); relInput.focus(); });
  }
  relInput.addEventListener('input', refreshAc);
  relInput.addEventListener('focus', refreshAc);
  relInput.addEventListener('blur', () => setTimeout(closeAc, 120));
  relInput.addEventListener('keydown', (e) => {
    if (!acMenu.hidden && acRows.length) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        acSel = (acSel + (e.key === 'ArrowDown' ? 1 : -1) + acRows.length) % acRows.length;
        for (const c of acMenu.children) c.classList.remove('sel');
        acMenu.children[acSel]?.classList.add('sel');
        acMenu.children[acSel]?.scrollIntoView({ block: 'nearest' });
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        const item = acRows[acSel];
        if (item && item.insert) {
          e.preventDefault();
          relInput.value = item.insert;
          if (item.kind === 'table') closeAc(); else refreshAc();
          return;
        }
      }
      if (e.key === 'Escape') { closeAc(); return; }
    }
    if (e.key === 'Enter') { e.preventDefault(); generate(); }
  });

  /* ------------------------------------------------------- selectors --- */

  const sourceSelect = el('select.select', { 'aria-label': 'Source' },
    el('option', { value: 'model' }, 'A dbt model'),
    el('option', { value: 'query' }, 'A query (SELECT)'),
    el('option', { value: 'table' }, 'An existing table (declare as source)'));
  sourceSelect.value = source;
  sourceSelect.addEventListener('change', () => {
    source = sourceSelect.value;
    remembered.source = source;
    paintSourceInput();
    paintOptions();
  });

  const engineSelect = el('select.select', { 'aria-label': 'Descriptions' },
    el('option', { value: 'pattern' }, 'Pattern descriptions (free)'),
    el('option', { value: 'ai' }, 'AI descriptions (Gemini)'),
    el('option', { value: 'none' }, 'No descriptions (schema only)'));
  engineSelect.value = engine;
  engineSelect.addEventListener('change', () => {
    engine = engineSelect.value;
    remembered.engine = engine;
    paintOptions();
  });

  /* ------------------------------------------------------- options --- */

  const profileToggle = checkbox('Profile the data', false,
    'Reads null rates, cardinality and ranges so descriptions and tests are '
    + 'evidence-based. Costs a small BigQuery scan; leave off for a free, '
    + 'metadata-only draft.');
  const testsToggle = checkbox('Suggest tests', true, 'Only proposes a test the data justifies.');
  const samplesToggle = checkbox('Send sample values to Gemini', false,
    'Off: only structure leaves your machine. On: also sends observed min/max '
    + 'and frequent values. Improves AI descriptions; decide if acceptable.');

  const sourceInputHost = el('div');
  const optionsHost = el('div');
  const aiSettingsHost = el('div');

  function paintSourceInput() {
    if (source === 'model') {
      clear(sourceInputHost).append(
        el('div.field', el('label', 'Model'), el('div.mt', modelList)),
        el('p.tiny.faint', { style: { lineHeight: '1.5', marginTop: '6px' } },
          'Types come from the live table, so the model must have been built at least once.'));
      paintModelList();
    } else if (source === 'query') {
      clear(sourceInputHost).append(
        el('div.field', el('label', 'Model name for the YAML'), nameInput),
        el('div.field.mt', el('label', 'Query'), sqlEditorInstance.node),
        el('p.tiny.faint', { style: { lineHeight: '1.5' } },
          'Planned with a dry run to read its output columns. Nothing is executed or billed.'));
    } else {
      clear(sourceInputHost).append(
        el('div.field', el('label', 'Table to declare as a dbt source'),
          el('p.tiny.faint', { style: { margin: '0 0 5px' } },
            'Type dataset.table, e.g. ',
            el('code', `${exampleDataset}.bronze_workspace_analytics_combined`),
            '. Autocomplete suggests datasets, then tables after the dot.'),
          relWrap),
        el('p.tiny.faint', { style: { lineHeight: '1.5' } },
          'This is the only source that produces a dbt sources: block - the '
          + 'declaration that makes dbt aware of a table it did not build.'));
    }
  }

  function paintOptions() {
    const isAi = engine === 'ai';
    const showProfile = engine !== 'none' || testsToggle.checked;
    clear(optionsHost).append(
      el('div.stat-label.mb', 'Options'),
      el('div.grid', { style: { gap: '7px' } },
        profileToggle.node,
        testsToggle.node,
        isAi ? samplesToggle.node : null),
    );
    clear(aiSettingsHost);
    if (isAi) aiSettingsHost.append(paintAiSettings());
    paintGenerateBtn();
  }

  /* ------------------------------------------------------- AI settings --- */

  function paintAiSettings() {
    if (!aiStatus.sdk_available) {
      return callout('The google-genai package is unavailable', aiStatus.sdk_error || '', 'err');
    }
    if (!aiStatus.configured) return keySetupPanel();

    const modelSelect = el('select.select');
    for (const option of aiStatus.models || []) {
      modelSelect.append(el('option',
        { value: option.id, selected: option.id === aiModel },
        `${option.label}${option.recommended ? ' (recommended)' : ''}`));
    }
    modelSelect.addEventListener('change', () => { aiModel = modelSelect.value; });
    return el('div.mt',
      el('div.field', el('label', 'Gemini model'), modelSelect),
      el('div.row.between.mt',
        el('span.tiny.faint', `key ${aiStatus.key_masked} · ${aiStatus.key_source}`),
        el('button.btn.btn-tiny', { onclick: () => { clear(aiSettingsHost).append(keySetupPanel(true)); } }, 'Change key')));
  }

  function keySetupPanel(isChange = false) {
    const input = el('input.input', { type: 'password', placeholder: 'AIza…', autocomplete: 'off', spellcheck: 'false' });
    const status = el('div');
    async function save() {
      const key = input.value.trim();
      if (!key) { toast('Paste the key first.', { kind: 'warn' }); return; }
      clear(status).append(loading('Saving…'));
      try {
        const result = await api.saveAiKey(key);
        aiStatus = result.status; state.boot.ai = result.status;
        toast('Key saved. AI documentation is ready.', { kind: 'ok' });
        clear(aiSettingsHost).append(paintAiSettings());
      } catch (error) {
        clear(status).append(callout('Could not save the key', error.message, 'err'));
      }
    }
    return el('div.mt',
      callout(isChange ? 'Replace the Gemini API key' : 'AI needs a free Gemini key', '', 'info',
        el('p.small', { style: { margin: '4px 0 0', lineHeight: '1.6' } },
          'Get one free at ',
          el('a', { href: 'https://aistudio.google.com/apikey', target: '_blank', rel: 'noopener' }, 'aistudio.google.com/apikey'),
          ' - no credit card, no GCP admin.')),
      el('div.field.mt', el('label', 'Gemini API key'), input,
        el('span.hint', 'Stored in dbt_ui/.runtime/ai.json (gitignored).')),
      el('div.row.mt', { style: { gap: '7px' } },
        el('button.btn.btn-primary', { onclick: save }, 'Save key'),
        isChange ? el('button.btn', { onclick: () => { clear(aiSettingsHost).append(paintAiSettings()); } }, 'Cancel') : null),
      el('div.mt', status));
  }

  /* ------------------------------------------------------- generate --- */

  const generateBtn = el('button.btn.btn-primary.btn-block');
  function paintGenerateBtn() {
    clear(generateBtn);
    generateBtn.textContent = engine === 'ai' ? '✦ Generate with Gemini'
      : source === 'table' ? '⚙ Read table & draft source'
      : '⚙ Generate documentation';
    generateBtn.onclick = () => generate();
  }

  async function generate() {
    if (engine === 'ai' && !aiStatus.configured) {
      clear(output).append(el('div.panel', el('div.panel-body',
        callout('Add the API key first', 'Set a Gemini key in the panel on the left, or pick Pattern / None.', 'warn'))));
      return;
    }

    const includeDescriptions = engine !== 'none';
    const body = {
      include_tests: testsToggle.checked,
      include_descriptions: includeDescriptions,
      engine: engine === 'ai' ? 'ai' : 'pattern',
    };
    if (engine === 'ai') { body.ai_model = aiModel; body.send_sample_values = samplesToggle.checked; }

    let call;
    if (source === 'model') {
      if (!selected) { toast('Pick a model first.', { kind: 'warn' }); return; }
      body.model = selected;
      body.profile = profileToggle.checked;
      call = api.generateSchema(body);
    } else if (source === 'query') {
      const sql = sqlEditorInstance.value.trim();
      if (!sql) { toast('Write a query first.', { kind: 'warn' }); return; }
      body.sql = sql;
      body.name = nameInput.value.trim() || 'my_new_model';
      call = api.generateSchema(body);
    } else {
      const relation = relInput.value.trim();
      if (!relation) { toast(`Type a table, e.g. ${exampleDataset}.my_table`, { kind: 'warn' }); return; }
      body.relation = relation;
      body.profile = profileToggle.checked;
      call = api.generateSource(body);
    }

    generateBtn.disabled = true;
    clear(output).append(el('div.panel', loading(
      source === 'table' ? 'Reading the table schema from BigQuery…'
        : engine === 'ai' ? `Asking ${aiModel} to write the descriptions…`
        : profileToggle.checked ? 'Reading types and profiling…' : 'Reading column types…')));

    try {
      const payload = await call;
      if (source === 'table') paintOutput(payload, { kind: 'source' });
      else paintOutput(payload, { kind: 'model' });
    } catch (error) {
      paintFailure(error);
    } finally {
      generateBtn.disabled = false;
    }
  }

  function paintFailure(error) {
    const kind = error.payload?.kind;
    clear(output).append(el('div.panel', el('div.panel-body',
      callout(
        kind === 'quota' ? 'Free-tier quota reached'
          : error.status === 404 ? 'Not found'
          : 'Could not generate the documentation',
        error.message, 'err',
        el('div',
          error.detail ? el('pre.code-block', error.detail) : null,
          source === 'model' && !kind
            ? el('div.mt',
                el('span.small.faint', 'If the model was never built there is no table to read. '),
                el('button.btn.btn-tiny', { onclick: () => navigate('runs', { select: selected, autorun: 'run' }) }, `⚡ Build ${selected}`))
            : source === 'table'
            ? el('p.tiny.faint', { style: { marginTop: '8px', lineHeight: '1.6' } },
                'Give it as dataset.table. Must be in: ' + (state.boot?.scope?.allowed_datasets || []).join(', '))
            : null)))));
  }

  /* ======================================================================
     the editable proposal - shared by every source
     ====================================================================== */

  function paintOutput(payload, { kind }) {
    const isSource = kind === 'source';
    const columns = payload.columns || [];
    const review = payload.stats?.needs_review || [];
    const profiled = columns.some((c) => c.profile);
    const ai = payload.ai;
    const contract = columnContract(columns);
    const defaultPath = isSource
      ? (payload.suggested_path || 'models/_sources.yml')
      : (payload.suggested_path || `models/_${payload.name}.yml`);

    let rebuildTimer = null;
    const yamlSlots = [];
    const saveState = el('span.tiny.faint.save-hint');
    let lastWrittenPath = null;   // enables the register-with-dbt step for sources
    const setStatus = (label, detail = '') => { saveState.textContent = label; saveState.title = detail || label; };
    const registerYaml = (fn) => { yamlSlots.push(fn); fn(); };

    async function rebuildNow() {
      try {
        const descriptions = Object.fromEntries(columns.map((c) => [c.name, c.description || '']));
        const profiles = Object.fromEntries(columns.filter((c) => c.profile).map((c) => [c.name, c.profile]));
        const result = isSource
          ? await api.rebuildSource({
              source_name: payload.source_name, database: payload.database,
              schema: payload.schema, table: payload.name,
              columns, descriptions, include_tests: testsToggle.checked, profiles,
              include_descriptions: engine !== 'none',
            })
          : await api.rebuildSchema({
              name: payload.name, columns, descriptions,
              resource_type: payload.table?.resource_type || 'model',
              include_tests: testsToggle.checked, include_descriptions: engine !== 'none', profiles,
            });
        payload.yaml = result.yaml;
        payload.markdown = result.markdown;
        if (result.stats) payload.stats = result.stats;
        for (const fn of yamlSlots) fn();
      } catch (error) {
        toast('Could not rebuild the YAML from your edits', { kind: 'err', detail: error.message });
      }
    }
    const scheduleRebuild = () => { clearTimeout(rebuildTimer); rebuildTimer = setTimeout(rebuildNow, 400); };
    const flushEdits = async () => { clearTimeout(rebuildTimer); await rebuildNow(); };

    function editableTable() {
      const rows = columns.map((column) => {
        const profile = column.profile;
        const cell = el('div', {
          class: 'doc-cell', contenteditable: 'plaintext-only', role: 'textbox',
          'aria-multiline': 'true', 'aria-label': `Description for ${column.name}`, spellcheck: 'true',
        });
        cell.textContent = column.description || '';
        cell.addEventListener('input', () => {
          if (cell.querySelector('*')) cell.textContent = cell.textContent;
          column.description = cell.textContent.replace(/\s+/g, ' ').trim();
          cell.classList.add('is-dirty');
          setStatus('edited…', 'Rebuilding the YAML from your edits');
          scheduleRebuild();
        });
        cell.addEventListener('blur', () => cell.classList.remove('is-dirty'));
        return el('tr',
          el('td.mono', column.name),
          el('td', typeBadge(column.data_type, column.category)),
          el('td.small.faint', column.mode === 'REQUIRED' ? el('span.chip.info', 'required') : (column.mode || 'NULLABLE').toLowerCase()),
          profiled ? el('td.num', profile?.null_pct == null ? '-' : pct(profile.null_pct)) : null,
          profiled ? el('td.num', num(profile?.distinct_count)) : null,
          profiled ? el('td.small.mono.faint', profile?.min == null ? '-'
            : el('span.cell-clip', { title: `${profile.min} … ${profile.max}` }, `${profile.min} … ${profile.max}`)) : null,
          el('td', { style: { minWidth: '26ch' } }, cell));
      });
      return el('div.table-wrap', { style: { maxHeight: '56vh' } },
        el('table.data.compact.doc-table',
          el('thead', el('tr',
            el('th', 'Column'), el('th', 'data_type'), el('th', 'Mode'),
            profiled ? el('th', 'Null %') : null,
            profiled ? el('th', 'Distinct') : null,
            profiled ? el('th', 'Range') : null,
            el('th', 'Description'))),
          el('tbody', ...rows)));
    }

    /* ---- register with dbt (sources only) ---- */
    const registerHost = el('div.mt');
    function paintRegister() {
      clear(registerHost);
      if (!isSource || !lastWrittenPath) return;
      const btn = el('button.btn.btn-tiny.btn-primary', {
        disabled: !canRunDbt,
        title: canRunDbt ? '' : 'Only a Manager can run dbt commands',
        onclick: () => registerWithDbt(),
      }, '↻ Register with dbt (parse + docs)');
      registerHost.append(callout(
        'Saved. Now register it with dbt',
        'dbt does not know about this table until it re-parses. Registering runs '
        + 'dbt parse (so the source is recognised) then dbt docs generate (so it '
        + 'appears in the docs site with lineage). Both are free.',
        'ok',
        el('div.row.wrap.mt', { style: { gap: '7px' } },
          btn,
          el('a.btn.btn-tiny', { href: '/dbt-docs', target: '_blank', rel: 'noopener' }, 'Open dbt docs ↗'),
          canRunDbt ? null : el('span.tiny.faint', { style: { alignSelf: 'center' } },
            'A Manager must run it.'))));
    }
    async function registerWithDbt() {
      const dismiss = toast('dbt parse…', { kind: 'info', timeout: 60000 });
      try {
        const { job } = await api.dbtRun({ command: 'parse' });
        watchJob(job.id, {
          onDone: async (parseJob) => {
            if (!(parseJob?.status === 'success' || parseJob?.exit_code === 0)) {
              dismiss();
              toast('dbt parse failed - check the Run Console.', { kind: 'warn' });
              return;
            }
            const gen = await api.dbtRun({ command: 'docs' });
            watchJob(gen.job.id, {
              onDone: (docsJob) => {
                dismiss();
                const ok = docsJob?.status === 'success' || docsJob?.exit_code === 0;
                toast(ok ? 'Registered. dbt now documents this table.'
                  : 'dbt docs generate had errors - check the Run Console.',
                  { kind: ok ? 'ok' : 'warn' });
              },
            });
          },
        });
      } catch (error) {
        dismiss();
        toast(error.status === 403 ? 'Only a Manager can run dbt' : 'Could not register',
          { kind: 'err', detail: error.message });
      }
    }

    /* ---- header chips + tabs ---- */
    const engineChip = ai ? el('span.chip.info', ai.model_label)
      : engine === 'none' ? el('span.chip', 'no descriptions')
      : el('span.chip', 'pattern rules');

    const contractTab = {
      label: isSource ? 'Source' : 'Contract',
      count: columns.length,
      render: () => el('div',
        el('div.panel-body',
          el('div.row.wrap.mb', { style: { gap: '6px' } },
            el('span.chip.info', isSource ? payload.reference : payload.name),
            el('span.chip', `${columns.length} columns`),
            engine !== 'none' ? el('span.chip.ok', `${payload.stats.documented} documented`) : null,
            review.length ? el('span.chip.warn', `${review.length} need review`) : null,
            profiled ? el('span.chip', 'profiled') : null,
            engineChip),
          ai ? aiUsageNote(ai) : null,
          engine === 'none'
            ? callout('Schema only', 'Descriptions are left as TODO for a human to fill in. Edit them below or after saving.', 'info')
            : review.length
              ? callout(`${review.length} need a human`,
                  `Not confident about: ${review.join(', ')}. Marked in the YAML.`, 'warn')
              : callout('Every column has a description', 'Click any description to edit it, then Save.', 'ok'),
          el('div.row.wrap.mt', { style: { gap: '7px' } },
            el('button.btn.btn-tiny', { onclick: () => download(`${payload.name}_documentation.csv`, documentationCsv(payload.name, columns), 'text/csv') }, '↓ CSV'),
            el('button.btn.btn-tiny', { onclick: () => copy(documentationCsv(payload.name, columns), 'Copied as CSV') }, '⧉ Copy CSV'))),
        editableTable(),
        registerHost),
    };

    const bareTab = {
      label: 'name + data_type',
      render: () => el('div.panel-body',
        callout('The bare contract', 'Just names and data types, to hand to the next layer.', 'info'),
        el('div.row.wrap.mt.mb', { style: { gap: '7px' } },
          el('button.btn.btn-tiny', { onclick: () => copy(contract, 'Copied') }, '⧉ Copy'),
          el('button.btn.btn-tiny', { onclick: () => download(`${payload.name}_columns.yml`, contract, 'text/yaml') }, '↓ Download')),
        codeBlock(contract, { language: 'yaml', title: `${columns.length} columns` })),
    };

    const yamlTab = {
      label: isSource ? 'sources: YAML' : 'Full schema YAML',
      render: () => {
        const panel = el('div.panel-body');
        registerYaml(() => clear(panel).append(
          el('p.small.faint', { style: { marginTop: 0 } },
            isSource ? 'The dbt source declaration. Save it below, then Register with dbt.'
              : 'The dbt schema block. Save it below into your models folder.'),
          codeBlock(payload.yaml, { language: 'yaml', tall: true })));
        return panel;
      },
    };

    const mdTab = {
      label: 'Markdown',
      render: () => {
        const body = el('pre.code-block.tall');
        registerYaml(() => { body.textContent = payload.markdown; });
        return el('div.panel-body',
          el('p.small.faint', { style: { marginTop: 0 } }, 'For a PR description or a wiki page.'),
          el('div.row.wrap.mb', { style: { gap: '7px' } },
            el('button.btn.btn-tiny', { onclick: () => copy(payload.markdown, 'Copied') }, '⧉ Copy'),
            el('button.btn.btn-tiny', { onclick: () => download(`${payload.name}.md`, payload.markdown, 'text/markdown') }, '↓ Download')),
          body);
      },
    };

    const view = tabs(isSource ? [contractTab, yamlTab, mdTab] : [contractTab, bareTab, yamlTab, mdTab]);

    /* ---- save + download bar ---- */
    const pathInput = el('input.input.input-tiny', { value: defaultPath, title: 'Path to write', style: { width: '24ch' } });
    const saveBtn = el('button.btn.btn-primary.btn-tiny', {
      disabled: !canWrite, title: canWrite ? '' : 'Only a Manager can write files',
      onclick: () => doSave(isSource ? 'append' : 'overwrite'),
    }, '⤓ Save');

    async function doSave(mode) {
      const path = pathInput.value.trim();
      if (!path) { toast('Give the file a path.', { kind: 'warn' }); return; }
      setStatus('saving…'); saveBtn.disabled = true;
      try {
        await flushEdits();
        const result = await api.writeFile(path, payload.yaml, mode);
        setStatus(result.backup ? 'saved · backup kept' : 'saved',
          result.backup ? `Wrote ${result.written}\nBackup: ${result.backup}` : `Wrote ${result.written}`);
        toast(`Wrote ${result.written}`, { kind: 'ok',
          detail: isSource ? 'Now register it with dbt below.' : 'Refresh the manifest so dbt picks it up.' });
        lastWrittenPath = result.written;
        paintRegister();
      } catch (error) {
        setStatus('save failed', error.message);
        toast('Could not save', { kind: 'err', detail: error.message });
      } finally {
        saveBtn.disabled = false;
      }
    }

    function downloadAs(format) {
      const n = payload.name;
      if (format === 'csv') download(`${n}_documentation.csv`, documentationCsv(n, columns), 'text/csv');
      else if (format === 'yaml') download(`${n}${isSource ? '_source' : ''}.yml`, payload.yaml, 'text/yaml');
      else if (format === 'markdown') download(`${n}.md`, payload.markdown, 'text/markdown');
      else if (format === 'json') {
        const doc = { model: n, columns: columns.map((c) => ({ name: c.name, data_type: String(c.data_type || '').toLowerCase(), description: c.description || '' })) };
        download(`${n}.json`, JSON.stringify(doc, null, 2), 'application/json');
      }
    }
    const downloadList = el('div.download-menu', { hidden: true },
      ...[['yaml', 'YAML (.yml)'], ['csv', 'CSV (.csv)'], ['markdown', 'Markdown (.md)'], ['json', 'JSON (.json)']].map(
        ([k, label]) => el('button.download-item', { type: 'button', onclick: () => { downloadList.hidden = true; downloadAs(k); } }, label)));
    const downloadBtn = el('button.btn.btn-tiny.btn-icon', {
      type: 'button', title: 'Download', 'aria-label': 'Download', 'aria-haspopup': 'menu',
      onclick: (e) => {
        e.stopPropagation();
        const show = downloadList.hidden;
        downloadList.hidden = !show;
        if (show) setTimeout(() => document.addEventListener('click', () => { downloadList.hidden = true; }, { once: true }), 0);
      },
    }, downloadIconSvg());
    const downloadMenu = el('div.download-wrap', downloadBtn, downloadList);

    const actionBar = el('div.output-actions',
      saveState, downloadMenu, pathInput,
      el('button.btn.btn-tiny', { disabled: !canWrite, onclick: () => doSave(isSource ? 'overwrite' : 'append') },
        isSource ? '⤓ Overwrite' : '+ Append'),
      saveBtn);

    const tabsWrap = view.node;
    const bar = tabsWrap.querySelector('.tabs');
    const panels = tabsWrap.querySelector('.tab-panels');
    clear(tabsWrap).append(el('div.output-head-row', bar, actionBar), panels);
    clear(output).append(el('div.panel', tabsWrap));
  }

  function aiUsageNote(ai) {
    const usage = ai.usage || {};
    return el('p.tiny.faint', { style: { margin: '0 0 10px', lineHeight: '1.5' } },
      `${ai.model_label} · ${usage.requests || 1} request${(usage.requests || 1) === 1 ? '' : 's'} · `
      + `${num(usage.prompt_tokens)} tokens in, ${num(usage.output_tokens)} out`
      + (ai.missing?.length ? ` · ${ai.missing.length} column(s) fell back to pattern rules` : ''));
  }

  /* ------------------------------------------------------- assemble --- */

  paintSourceInput();
  paintOptions();

  host.append(
    el('div.split',
      el('div.panel',
        el('div.panel-head', el('h3', 'Documentation')),
        el('div.panel-body',
          el('div.field.mb', el('label', 'Source'), sourceSelect),
          el('div.field.mb', el('label', 'Descriptions'), engineSelect),
          sourceInputHost,
          aiSettingsHost,
          el('div.mt'),
          optionsHost,
          el('div.mt', generateBtn),
          el('p.tiny.faint', { style: { marginBottom: 0, lineHeight: '1.6' } },
            'dbt has no description engine of its own, so the prose is written '
            + 'by the Pattern rules or Gemini. "An existing table" produces a dbt '
            + 'sources: block and lets dbt register and document it.'))),
      output),
  );

  clear(output).append(el('div.panel', emptyState(
    'Pick a source, then generate',
    'Choose a model, a query, or an existing table on the left. You get the real '
    + 'column types, drafted descriptions, and an editable YAML ready to commit.')));

  /* Auto-run when we arrived with something already selected. */
  if (source === 'model' && selected && (engine !== 'ai' || aiStatus.configured)) generate();
  else if (source === 'query' && params.sql) generate();
  else if (source === 'table' && params.relation) generate();

  return host;
}

/* ---------------------------------------------------------------- utils --- */

function checkbox(label, checked, hint) {
  const input = el('input', { type: 'checkbox' });
  input.checked = checked;
  const node = el('div',
    el('label.switch', input, el('span', label)),
    hint ? el('p.tiny.faint', { style: { margin: '2px 0 0 22px', lineHeight: '1.45' } }, hint) : null);
  return { node, get checked() { return input.checked; } };
}

/**
 * The documentation as CSV, RFC 4180 quoted so a comma in a description cannot
 * shift the columns.
 */
function documentationCsv(modelName, columns) {
  const cell = (value) => {
    const text = String(value ?? '');
    return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const lines = [['model', 'column', 'data_type', 'description'].join(',')];
  for (const column of columns) {
    lines.push([cell(modelName), cell(column.name),
      cell(String(column.data_type || '').toLowerCase()), cell(column.description || '')].join(','));
  }
  return `${lines.join('\n')}\n`;
}

/* The "arrow into a tray" download glyph. */
function downloadIconSvg() {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '15'); svg.setAttribute('height', '15');
  svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2.2');
  svg.setAttribute('stroke-linecap', 'round'); svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  const a = document.createElementNS(NS, 'path');
  a.setAttribute('d', 'M12 3v11m0 0l-4-4m4 4l4-4');
  const t = document.createElementNS(NS, 'path');
  t.setAttribute('d', 'M5 15v3a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-3');
  svg.append(a, t);
  return svg;
}

function firstModel() {
  const usable = (state.models || []).filter((model) => model.in_scope !== false);
  const bronze = usable.find((model) => model.layer === 'bronze');
  return bronze?.name || usable[0]?.name || '';
}
