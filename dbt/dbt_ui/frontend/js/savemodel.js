/* ==========================================================================
   savemodel.js - "Save as dbt model" dialog for the workbench.

   The whole point is that this does not write to BigQuery. An exploratory query
   becomes a *file* in the working copy, which then gets reviewed and built like
   any other model. Materialising a view straight into bronze_dbt would create an
   object with no model behind it: absent from the DAG, untested, not recreated
   by dbt build, and invisible in lineage.

   The dialog is preview-first because the interesting step - rewriting hardcoded
   table names into ref() - is a judgement call. A rewrite matched on table name
   alone is usually right and occasionally not, so it is shown before it lands
   rather than discovered later in a diff.
   ========================================================================== */

import { api, clear, copy, el, num, state, toast } from './core.js';
import { callout, codeBlock, loading, modal } from './components.js';

/** Wait this long after a keystroke before re-scaffolding. */
const PREVIEW_DEBOUNCE_MS = 400;

/**
 * @param {object}   options
 * @param {string}   options.sql        the workbench SQL
 * @param {Element}  options.trigger    button to restore focus to on close
 * @param {function} options.onSaved    (result) => void
 * @param {function} options.navigate
 */
export function openSaveAsModel({ sql, trigger, onSaved, navigate } = {}) {
  const previewHost = el('div');
  const statusHost = el('div');

  let scaffolded = null;
  let debounceTimer = null;
  let inFlight = false;
  let dialog = null;

  /* ------------------------------------------------------------- inputs --- */

  const nameInput = el('input.input', {
    placeholder: 'silver_customer_summary',
    autocomplete: 'off',
    spellcheck: 'false',
    oninput: () => schedule(),
  });

  const layerSelect = el('select.select', { onchange: () => schedule() });
  const materializedSelect = el('select.select', { onchange: () => schedule() });

  const descriptionInput = el('textarea.textarea', {
    rows: '2',
    placeholder: 'What this model is for. Goes into the file header.',
    oninput: () => schedule(),
  });

  const rewriteToggle = el('input', { type: 'checkbox' });
  rewriteToggle.checked = true;
  rewriteToggle.addEventListener('change', () => schedule());

  const saveButton = el(
    'button.btn.btn-primary',
    { disabled: true, onclick: () => save('overwrite') },
    'Save model file',
  );

  /* Populated from the first scaffold response so the options can never drift
     from what the backend will actually accept. */
  let optionsFilled = false;

  function fillOptions(payload) {
    if (optionsFilled) return;
    optionsFilled = true;

    clear(layerSelect);
    for (const layer of payload.allowed_layers || []) {
      layerSelect.append(
        el('option', { value: layer, selected: layer === payload.layer }, layer),
      );
    }

    clear(materializedSelect);
    for (const kind of payload.materializations || []) {
      materializedSelect.append(el('option', { value: kind }, kind));
    }
    materializedSelect.value = payload.materialized;
  }

  /* ---------------------------------------------------------- scaffold --- */

  function schedule() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(refresh, PREVIEW_DEBOUNCE_MS);
  }

  async function refresh() {
    if (inFlight) return;
    inFlight = true;
    saveButton.disabled = true;

    try {
      const payload = await api.scaffoldModel({
        sql,
        name: nameInput.value,
        layer: layerSelect.value || 'silver',
        materialized: materializedSelect.value || '',
        description: descriptionInput.value,
        rewrite: rewriteToggle.checked,
      });

      scaffolded = payload;
      fillOptions(payload);
      paintPreview(payload);
      saveButton.disabled = !payload.ok;
      saveButton.textContent = payload.exists
        ? 'Overwrite model file'
        : 'Save model file';
    } catch (error) {
      scaffolded = null;
      saveButton.disabled = true;
      clear(previewHost).append(
        callout('Cannot build a model from this', error.message, 'err',
          error.detail ? el('pre.code-block', error.detail) : null),
      );
    } finally {
      inFlight = false;
    }
  }

  /* ----------------------------------------------------------- preview --- */

  function paintPreview(payload) {
    const blocks = [];

    if (payload.errors?.length) {
      blocks.push(
        callout(
          payload.errors.length === 1 ? 'Fix this first' : 'Fix these first',
          '',
          'err',
          el('ul', { style: { margin: '4px 0 0', paddingLeft: '18px', lineHeight: '1.6' } },
            ...payload.errors.map((message) => el('li.small', message))),
        ),
      );
    }

    /* The rewrite table is the reason this dialog exists, so it goes above the
       file preview rather than below it. */
    if (payload.replacements?.length) {
      blocks.push(
        el(
          'div.mb',
          el('div.stat-label.mb', `Rewritten to ref() and source()`),
          el(
            'div.table-wrap',
            { style: { maxHeight: '150px' } },
            el(
              'table.data.compact',
              el('thead', el('tr',
                el('th', 'Was'), el('th', 'Now'), el('th', 'Matched on'))),
              el(
                'tbody',
                ...payload.replacements.map((entry) =>
                  el(
                    'tr',
                    el('td.mono.small', entry.literal),
                    el('td.mono.small', entry.expression),
                    el(
                      'td',
                      entry.confidence === 'table'
                        ? el('span.chip.warn.tiny',
                             { title: 'The dataset did not match, only the table name' },
                             'table name')
                        : el('span.chip.ok.tiny', 'full path'),
                    ),
                  )),
              ),
            ),
          ),
        ),
      );
    }

    if (payload.warnings?.length) {
      blocks.push(
        callout(
          payload.warnings.length === 1
            ? 'One thing to check'
            : `${payload.warnings.length} things to check`,
          '',
          'warn',
          el('ul', { style: { margin: '4px 0 0', paddingLeft: '18px', lineHeight: '1.6' } },
            ...payload.warnings.map((message) => el('li.small', message))),
        ),
      );
    }

    /* Undeclared tables are the common case in this warehouse, so hand over the
       exact YAML rather than stopping at "declare a source". */
    if (payload.source_stub) {
      blocks.push(
        el(
          'details.mb',
          el('summary.small', { style: { cursor: 'pointer' } },
             `Source declarations for ${payload.unresolved.length} undeclared `
             + `table${payload.unresolved.length === 1 ? '' : 's'}`),
          el(
            'div.mt',
            el('p.tiny.faint', { style: { marginTop: 0, lineHeight: '1.55' } },
              'Add this to a sources YAML file, then change the hardcoded names in '
              + 'the model to source() calls. Until you do, the model builds but '
              + 'the lineage graph will not show where its data comes from.'),
            el(
              'div.row.mb',
              { style: { gap: '6px' } },
              el('button.btn.btn-tiny',
                 { onclick: () => copy(payload.source_stub, 'Source YAML copied') },
                 '⧉ Copy'),
            ),
            codeBlock(payload.source_stub, { language: 'yaml' }),
          ),
        ),
      );
    }

    blocks.push(
      el(
        'div',
        el(
          'div.row.wrap.between.mb',
          el('div.stat-label', 'File to be written'),
          el(
            'div.row.wrap',
            { style: { gap: '6px' } },
            el('code.chip', payload.path),
            payload.exists
              ? el('span.chip.warn', 'already exists')
              : el('span.chip.ok', 'new file'),
            payload.uses_config_block
              ? el('span.chip.info', `config: ${payload.materialized}`)
              : el('span.chip', `${payload.materialized} by project default`),
          ),
        ),
        codeBlock(payload.content, { language: 'sql', tall: true }),
      ),
    );

    if (payload.exists) {
      blocks.push(
        callout(
          'A file is already at that path',
          'Saving will overwrite it. The previous version is kept alongside it '
          + 'as a .bak, but if that file is tracked in git you should check the '
          + 'diff rather than rely on the backup.',
          'warn',
        ),
      );
    }

    clear(previewHost).append(...blocks.filter(Boolean));
  }

  /* -------------------------------------------------------------- save --- */

  async function save(mode) {
    if (!scaffolded?.ok) return;

    saveButton.disabled = true;
    clear(statusHost).append(loading(`Writing ${scaffolded.path}…`));

    try {
      const result = await api.writeFile(scaffolded.path, scaffolded.content, mode);

      clear(statusHost).append(
        callout(
          `Wrote ${result.written}`,
          [
            `${num(result.bytes)} bytes`,
            result.backup ? `previous version kept as ${result.backup}` : null,
          ].filter(Boolean).join(' · '),
          'ok',
          el(
            'div',
            el('p.small.muted', { style: { lineHeight: '1.6' } },
              'dbt has not seen this file yet. Parse the project to register it, '
              + 'then build it to create the table or view.'),
            el(
              'div.row.wrap.mt',
              { style: { gap: '7px' } },
              el(
                'button.btn.btn-tiny.btn-primary',
                {
                  onclick: () => {
                    dialog?.close();
                    navigate?.('runs', { autorun: 'parse' });
                  },
                },
                '⟳ Parse the project',
              ),
              el(
                'button.btn.btn-tiny',
                {
                  onclick: () => {
                    dialog?.close();
                    navigate?.('runs', { select: scaffolded.name });
                  },
                },
                `⚡ Build ${scaffolded.name}`,
              ),
              el(
                'button.btn.btn-tiny',
                {
                  onclick: () => {
                    dialog?.close();
                    navigate?.('schema', { engine: 'pattern', model: scaffolded.name });
                  },
                },
                '☰ Document it',
              ),
            ),
            el('p.tiny.faint', { style: { marginTop: '9px', lineHeight: '1.5' } },
              'Building will not work until the project is parsed, because dbt '
              + 'resolves the name from the manifest.'),
          ),
        ),
      );

      toast(`Saved ${scaffolded.path}`, { kind: 'ok' });
      onSaved?.({ ...result, name: scaffolded.name, path: scaffolded.path });
    } catch (error) {
      clear(statusHost).append(
        callout('Could not write the file', error.message, 'err',
          error.detail ? el('pre.code-block', error.detail) : null),
      );
      saveButton.disabled = false;
    }
  }

  /* ---------------------------------------------------------- assemble --- */

  const body = el(
    'div',
    callout(
      'This writes a file, not a table',
      'The model goes into your working copy so it can be reviewed and built '
      + 'like any other. Nothing is created in BigQuery until you build it, and '
      + 'the target you build with decides which dataset it lands in.',
      'info',
    ),
    el(
      'div.grid.grid-2.mt',
      el(
        'div.field',
        el('label', { for: 'sm-name' }, 'Model name'),
        nameInput,
        el('span.hint', 'Becomes the filename and the ref() other models use.'),
      ),
      el(
        'div.field',
        el('label', 'Layer'),
        layerSelect,
        el('span.hint', 'Decides the folder, the tag, and the dataset it builds into.'),
      ),
    ),
    el(
      'div.grid.grid-2.mt',
      el(
        'div.field',
        el('label', 'Materialization'),
        materializedSelect,
        el('span.hint', 'A config block is only written when this differs from the project default.'),
      ),
      el(
        'div.field',
        el('label', 'Description'),
        descriptionInput,
      ),
    ),
    el(
      'div.mt',
      el('label.switch', rewriteToggle,
         el('span', 'Rewrite table names as ref() and source()')),
      el('p.tiny.faint', { style: { margin: '3px 0 0 22px', lineHeight: '1.5' } },
        'Strongly recommended. A hardcoded dataset pins the model to one '
        + 'environment and hides the dependency from the lineage graph.'),
    ),
    el('div.mt', previewHost),
    el('div.mt', statusHost),
    el(
      'div.row.wrap.mt',
      { style: { gap: '7px' } },
      saveButton,
      el('button.btn', { onclick: () => dialog?.close() }, 'Cancel'),
    ),
  );

  nameInput.id = 'sm-name';

  dialog = modal({
    title: 'Save as dbt model',
    subtitle: 'Turn this query into a model file you can review, test and build',
    body,
    width: '900px',
    returnFocusTo: trigger || null,
  });

  /* Seed the name from the target, then scaffold immediately so the preview is
     never blank on open. */
  nameInput.value = suggestName();
  refresh();
  nameInput.focus();
  nameInput.select?.();

  return dialog;
}

/**
 * A starting name derived from what the query reads.
 *
 * Not clever on purpose: a wrong guess the user has to clear is worse than an
 * obviously-placeholder one they expect to replace.
 */
function suggestName() {
  const layer = 'silver';
  const first = (state.models || []).find((model) => model.in_scope !== false);
  const stem = first?.name?.replace(/^(bronze|silver|gold|stg|raw)_/, '') || 'query';
  return `${layer}_${stem}_summary`.slice(0, 60);
}
