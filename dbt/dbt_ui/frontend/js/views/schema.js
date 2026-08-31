/* ==========================================================================
   schema.js - the Documentation page.

   Three modes, chosen on entry:

     AI          Gemini writes the descriptions from the column names, types and
                 measured profile. Better prose, understands SAP conventions,
                 needs a free API key.
     Pattern     Deterministic name-matching rules. No network, no key, no
                 quota, identical output every run.
     Edit        Opens the descriptions already committed to the project's YAML
                 and lets you correct them in place.

   AI and Pattern both produce the same artifact: a dbt schema YAML block with
   name, data_type, description and the tests the data justifies. Only the prose
   differs, which is what makes them directly comparable. They generate a *new*
   block for you to review and write.

   Edit is the other half of the loop. Generated prose is a first draft, and the
   person who knows what a column really means is usually reading it a week
   later. Sending them back through a generator to fix one sentence would
   rewrite the whole file, so Edit patches just the description values and
   leaves comments, tests and key order untouched.
   ========================================================================== */

import {
  api, clear, copy, download, el, num, pct, plainRelation, state, toast,
} from '../core.js';
import {
  callout, codeBlock, columnContract, emptyState, layerChip, loading,
  schemaTable, sqlEditor, tabs,
} from '../components.js';
import { documentationEditor } from '../docedit.js';

export const meta = {
  title: 'Documentation',
  subtitle: 'Generate column contracts and descriptions, by AI or by pattern rules',
};

/* Remembered across navigations so you land back where you were working. */
let chosenEngine = null;

export function render(navigate, params = {}) {
  if (params.engine) chosenEngine = params.engine;

  if (!chosenEngine) return chooser(navigate);
  if (chosenEngine === 'edit') return editor(navigate, params);
  return generator(navigate, params, chosenEngine);
}

/* ======================================================================
   engine chooser
   ====================================================================== */

function chooser(navigate) {
  const ai = state.boot?.ai || {};
  const host = el('div');

  const cards = el(
    'div.grid.grid-2',
    engineCard({
      badge: 'AI',
      title: 'AI documentation',
      tagline: 'Gemini reads your schema and writes the descriptions',
      points: [
        'Understands source-system conventions, including SAP field names like BUKRS and DMBTR',
        'Uses the measured profile: null rates, cardinality, observed ranges, frequent values',
        'Flags its own uncertainty with "Unclear:" instead of inventing a purpose',
        'One request per table, so the free tier goes a long way',
      ],
      status: ai.configured
        ? el('span.chip.ok', `ready · ${ai.key_masked}`)
        : el('span.chip.warn', 'needs a free API key'),
      note: ai.configured
        ? `Key from ${ai.key_source}. Default model ${ai.default_model}.`
        : 'Free key from Google AI Studio, no credit card, no GCP admin needed.',
      cta: ai.configured ? 'Use AI documentation' : 'Set up AI documentation',
      accent: 'var(--accent)',
      onclick: () => {
        chosenEngine = 'ai';
        navigate('schema', { engine: 'ai' });
      },
    }),

    engineCard({
      badge: 'RULES',
      title: 'Pattern documentation',
      tagline: 'Deterministic rules built into dbt Studio',
      points: [
        'No network call, no API key, no quota, works offline',
        'Same input always produces the same output, so diffs stay clean',
        'Recognises the conventions in this project: audit columns, _is_/_has_ flags, period columns',
        'Marks anything it cannot infer as TODO for a human to finish',
      ],
      status: el('span.chip.ok', 'always available'),
      note: 'This is the engine that produced the descriptions currently in the project.',
      cta: 'Use pattern documentation',
      accent: 'var(--silver)',
      onclick: () => {
        chosenEngine = 'pattern';
        navigate('schema', { engine: 'pattern' });
      },
    }),
  );

  /* Counted so the edit card can say something useful about how much
     documentation is already committed rather than just "open the editor". */
  const documented = (state.models || []).filter(
    (model) => model.in_scope !== false && model.has_description,
  ).length;
  const inScopeTotal = (state.models || []).filter((m) => m.in_scope !== false).length;

  host.append(
    el(
      'div.panel.mb',
      el('div.panel-head', el('h3', 'Choose how descriptions get written')),
      el(
        'div.panel-body',
        el(
          'p.muted',
          { style: { marginTop: 0, lineHeight: '1.65' } },
          'Both engines read the real column types from BigQuery and emit the same schema YAML. ' +
            'The difference is who writes the prose. You can switch at any time, and generating ' +
            'with one does not overwrite what the other produced until you click write.',
        ),
      ),
    ),
    cards,
    el(
      'div.panel.mt',
      { style: { borderTop: '2px solid var(--info)' } },
      el(
        'div.panel-body',
        el(
          'div.row.wrap.between',
          { style: { gap: '14px' } },
          el(
            'div',
            { style: { flex: '1 1 420px', minWidth: 0 } },
            el(
              'div.row.mb',
              { style: { gap: '9px' } },
              el('span.chip.info', 'EDIT'),
              el('h3', { style: { fontSize: '15px' } }, 'Edit the documentation already committed'),
            ),
            el(
              'p.small.muted',
              { style: { margin: '0 0 8px', lineHeight: '1.65' } },
              'Open the descriptions that are in the project right now and correct them in ' +
                'place. Use this when a generated draft is nearly right, or when you know ' +
                'what a column actually means and want to say so properly.',
            ),
            el(
              'ul',
              { style: { margin: '0 0 10px', paddingLeft: '18px', lineHeight: '1.7' } },
              el('li.small.muted', 'Edits save themselves a few seconds after you stop typing'),
              el(
                'li.small.muted',
                'Only description text is rewritten - your comments, tests, config and ' +
                  'key order are left exactly as they are',
              ),
              el(
                'li.small.muted',
                'Refuses to save over a file that changed underneath you, so a git pull ' +
                  'cannot silently eat your work',
              ),
              el('li.small.muted', 'Download the result as YAML, JSON or Markdown'),
            ),
            el(
              'p.tiny.faint',
              { style: { margin: 0, lineHeight: '1.5' } },
              'This writes to the schema YAML files in your working copy. It does not touch ' +
                'BigQuery and it does not commit anything to git.',
            ),
          ),
          el(
            'div',
            { style: { flex: '0 0 auto', textAlign: 'right' } },
            el(
              'div.mb',
              inScopeTotal
                ? el(
                    `span.chip.${documented === inScopeTotal ? 'ok' : 'warn'}`,
                    `${documented} of ${inScopeTotal} models described`,
                  )
                : el('span.chip', 'no models in scope'),
            ),
            el(
              'button.btn.btn-primary',
              {
                onclick: () => {
                  chosenEngine = 'edit';
                  navigate('schema', { engine: 'edit' });
                },
              },
              'Open the editor',
            ),
          ),
        ),
      ),
    ),
  );

  return host;
}

function engineCard({ badge, title, tagline, points, status, note, cta, accent, onclick }) {
  return el(
    'div.panel',
    { style: { borderTop: `2px solid ${accent}` } },
    el(
      'div.panel-body',
      el(
        'div.row.between.mb',
        el(
          'div.row',
          { style: { gap: '9px' } },
          el('span.chip.info', badge),
          el('h3', { style: { fontSize: '15px' } }, title),
        ),
        status,
      ),
      el('p.small.muted', { style: { marginTop: 0 } }, tagline),
      el(
        'ul',
        { style: { margin: '12px 0', paddingLeft: '18px', lineHeight: '1.7' } },
        ...points.map((point) => el('li.small.muted', point)),
      ),
      el('p.tiny.faint', { style: { lineHeight: '1.5' } }, note),
      el('button.btn.btn-primary.btn-block.mt', { onclick }, cta),
    ),
  );
}

/* ======================================================================
   generator
   ====================================================================== */

function generator(navigate, params, engine) {
  const host = el('div');
  const output = el('div.mt');
  const isAi = engine === 'ai';

  let mode = params.sql ? 'sql' : 'model';
  let selected = params.model || state.scratch.schemaModel || firstModel();
  let aiStatus = state.boot?.ai || {};
  let aiModel = aiStatus.default_model || 'gemini-2.5-flash';

  const profileToggle = checkbox(
    'Profile the data',
    true,
    isAi
      ? 'Strongly recommended. The measurements are what let the model say something specific instead of paraphrasing the column name.'
      : 'Measures nulls, cardinality and ranges so the descriptions and test suggestions are evidence-based.',
  );
  const testsToggle = checkbox('Suggest tests', true, 'Only proposes a test the profile justifies.');
  const docsToggle = checkbox('Include descriptions', true, '');

  // Off by default. This is the only setting that sends real values from your
  // tables to Google rather than just structural statistics.
  const samplesToggle = checkbox(
    'Send sample values to Gemini',
    false,
    'Off: only structure leaves your machine (column names, types, null rates, distinct counts). ' +
      'On: also sends observed min/max and the most frequent values, which are real figures from your tables. ' +
      'It improves the descriptions, but decide whether that is acceptable for this data.',
  );

  const sqlEditorInstance = sqlEditor({
    value: params.sql || `select *\nfrom {{ ref('${selected || 'your_model'}') }}\n`,
    placeholder: "select ... from {{ ref('model') }}",
  });

  const nameInput = el('input.input', { value: 'my_new_model', placeholder: 'model name' });

  const generateBtn = el(
    'button.btn.btn-primary.btn-block',
    { onclick: () => generate() },
    isAi ? '✦ Generate with Gemini' : '⚙ Generate contract',
  );

  /* ------------------------------------------------------- header bar --- */

  const engineBar = el(
    'div.row.between.mb',
    el(
      'div.row',
      { style: { gap: '9px' } },
      el(`span.chip.${isAi ? 'info' : 'ok'}`, isAi ? 'AI · Gemini' : 'Pattern rules'),
      el(
        'span.small.faint',
        isAi
          ? 'Descriptions written by a language model from your schema and profile'
          : 'Descriptions from deterministic name-matching rules',
      ),
    ),
    el(
      'button.btn.btn-tiny',
      {
        onclick: () => {
          chosenEngine = null;
          navigate('schema');
        },
      },
      '← Switch engine',
    ),
  );

  /* ------------------------------------------------------ ai settings --- */

  const aiSettingsHost = el('div');

  function paintAiSettings() {
    if (!isAi) return clear(aiSettingsHost);

    if (!aiStatus.sdk_available) {
      return clear(aiSettingsHost).append(
        callout('The google-genai package is unavailable', aiStatus.sdk_error, 'err'),
      );
    }

    if (!aiStatus.configured) return clear(aiSettingsHost).append(keySetupPanel());

    const modelSelect = el('select.select');
    for (const option of aiStatus.models || []) {
      modelSelect.append(
        el(
          'option',
          { value: option.id, selected: option.id === aiModel },
          `${option.label}${option.recommended ? ' (recommended)' : ''}`,
        ),
      );
    }
    modelSelect.addEventListener('change', () => {
      aiModel = modelSelect.value;
      paintAiSettings();
    });

    const active = (aiStatus.models || []).find((option) => option.id === aiModel);

    clear(aiSettingsHost).append(
      el(
        'div',
        el('div.field', el('label', 'Model'), modelSelect),
        active
          ? el(
              'p.tiny.faint',
              { style: { margin: '5px 0 0', lineHeight: '1.5' } },
              `${active.blurb} Free tier: ${num(active.free_rpd)} requests/day, ${active.free_rpm}/min. ` +
                'One table costs one request.',
            )
          : null,
        el(
          'div.row.between.mt',
          el('span.tiny.faint', `key ${aiStatus.key_masked} · ${aiStatus.key_source}`),
          el('button.btn.btn-tiny', { onclick: () => showKeyEditor() }, 'Change key'),
        ),
      ),
    );
  }

  function showKeyEditor() {
    clear(aiSettingsHost).append(keySetupPanel(true));
  }

  function keySetupPanel(isChange = false) {
    const input = el('input.input', {
      type: 'password',
      placeholder: 'AIza…',
      autocomplete: 'off',
      spellcheck: 'false',
    });
    const status = el('div');

    async function save() {
      const key = input.value.trim();
      if (!key) {
        toast('Paste the key first.', { kind: 'warn' });
        return;
      }
      clear(status).append(loading('Saving…'));
      try {
        const result = await api.saveAiKey(key);
        aiStatus = result.status;
        state.boot.ai = result.status;
        toast('Key saved. AI documentation is ready.', { kind: 'ok' });
        paintAiSettings();
      } catch (error) {
        clear(status).append(
          callout('Could not save the key', error.message, 'err',
            error.detail ? el('pre.code-block', error.detail) : null),
        );
      }
    }

    async function clearKey() {
      try {
        const result = await api.clearAiKey();
        aiStatus = result.status;
        state.boot.ai = result.status;
        toast('Key removed.', { kind: 'ok' });
        paintAiSettings();
      } catch (error) {
        toast(error.message, { kind: 'err' });
      }
    }

    return el(
      'div',
      callout(
        isChange ? 'Replace the Gemini API key' : 'AI documentation needs a free API key',
        '',
        'info',
        el(
          'div',
          el(
            'ol',
            { style: { margin: '6px 0', paddingLeft: '18px', lineHeight: '1.75' } },
            el(
              'li.small',
              'Open ',
              el(
                'a',
                { href: aiStatus.signup_url || 'https://aistudio.google.com/apikey', target: '_blank', rel: 'noopener' },
                'aistudio.google.com/apikey',
              ),
              ' and sign in with your Google account.',
            ),
            el('li.small', 'Click Create API key. No credit card and no GCP project permissions are needed.'),
            el('li.small', 'Copy the key and paste it below.'),
          ),
        ),
      ),
      el('div.field.mt', el('label', 'Gemini API key'), input,
        el('span.hint', 'Stored in dbt_ui/.runtime/ai.json, which is gitignored. Never sent anywhere except Google.')),
      el(
        'div.row.mt',
        { style: { gap: '7px' } },
        el('button.btn.btn-primary', { onclick: save }, 'Save key'),
        aiStatus.configured ? el('button.btn.btn-danger', { onclick: clearKey }, 'Remove') : null,
        isChange
          ? el('button.btn', { onclick: () => paintAiSettings() }, 'Cancel')
          : null,
      ),
      el('div.mt', status),
      aiStatus.vertex_note
        ? el(
            'details.mt',
            el('summary.tiny.faint', { style: { cursor: 'pointer' } }, 'Why not use our GCP project instead?'),
            el('p.tiny.faint', { style: { lineHeight: '1.6' } }, aiStatus.vertex_note),
          )
        : null,
    );
  }

  /* ------------------------------------------------------------ picker --- */

  const modelList = el('div.scroll-list');

  function paintList() {
    clear(modelList);
    const models = state.models || [];
    const inScope = models.filter((m) => m.in_scope !== false);
    const blocked = models.filter((m) => m.in_scope === false);

    for (const model of [...inScope, ...blocked]) {
      const outOfScope = model.in_scope === false;
      modelList.append(
        el(
          'button',
          {
            class: `list-btn${model.name === selected && mode === 'model' ? ' sel' : ''}${outOfScope ? ' out-of-scope' : ''}`,
            dataset: { name: model.name.toLowerCase() },
            title: outOfScope
              ? `Outside the permitted scope (dataset ${model.dataset})`
              : model.name,
            onclick: () => {
              if (outOfScope) {
                toast(`${model.name} is outside the permitted dataset scope.`, {
                  kind: 'warn',
                  detail:
                    `It lives in '${model.dataset}'. This instance may only ` +
                    `document: ${(state.scope?.allowed_datasets || []).join(', ')}.`,
                });
                return;
              }
              selected = model.name;
              state.scratch.schemaModel = model.name;
              mode = 'model';
              paintList();
              paintSource();
              generate();
            },
          },
          layerChip(model.layer),
          el('span.lb-name', model.name),
          el(
            'span.lb-meta',
            outOfScope
              ? el('span.chip.err', 'out of scope')
              : model.has_description
              ? el('span.chip.ok', `${model.documented_columns}/${model.column_count}`)
              : el('span.chip.warn', 'undoc'),
          ),
        ),
      );
    }
  }

  const sourceHost = el('div');

  function paintSource() {
    clear(sourceHost).append(
      mode === 'model'
        ? el(
            'div',
            el('div.field', el('label', 'Source'),
              el('p.small.mono', { style: { margin: 0 } }, selected || 'none selected')),
            el(
              'p.tiny.faint',
              { style: { lineHeight: '1.5' } },
              'Types are read from the live table definition, so the model must have been built at least once.',
            ),
          )
        : el(
            'div',
            el('div.field', el('label', 'Model name for the YAML'), nameInput),
            el('div.field.mt', el('label', 'Query'), sqlEditorInstance.node),
            el(
              'p.tiny.faint',
              { style: { lineHeight: '1.5' } },
              'The statement is planned with a dry run to read its output schema. Nothing is executed and nothing is billed.',
            ),
          ),
    );
  }

  const modeTabs = el('div.row', { style: { gap: '6px' } });

  function paintModeTabs() {
    clear(modeTabs).append(
      modeButton('From a model', 'model'),
      modeButton('From a query', 'sql'),
    );
  }

  function modeButton(label, key) {
    return el(
      'button',
      {
        class: `btn btn-tiny${mode === key ? ' btn-primary' : ''}`,
        onclick: () => {
          mode = key;
          paintModeTabs();
          paintList();
          paintSource();
        },
      },
      label,
    );
  }

  /* ---------------------------------------------------------- generate --- */

  async function generate() {
    if (isAi && !aiStatus.configured) {
      clear(output).append(
        el('div.panel', el('div.panel-body',
          callout('Add the API key first', 'AI documentation needs a Gemini key. The panel on the left walks through it.', 'warn'))),
      );
      return;
    }
    if (mode === 'model' && !selected) {
      toast('Pick a model first.', { kind: 'warn' });
      return;
    }

    generateBtn.disabled = true;
    clear(output).append(
      el(
        'div.panel',
        loading(
          isAi
            ? `Profiling, then asking ${aiModel} to write the descriptions…`
            : profileToggle.checked
            ? 'Reading types and profiling the data…'
            : 'Reading column types…',
        ),
      ),
    );

    const body = {
      engine,
      include_tests: testsToggle.checked,
      include_descriptions: docsToggle.checked,
    };
    if (isAi) {
      body.ai_model = aiModel;
      body.send_sample_values = samplesToggle.checked;
    }

    if (mode === 'model') {
      body.model = selected;
      body.profile = profileToggle.checked;
    } else {
      body.sql = sqlEditorInstance.value;
      body.name = nameInput.value.trim() || 'my_new_model';
    }

    try {
      paintOutput(await api.generateSchema(body));
    } catch (error) {
      paintFailure(error);
    } finally {
      generateBtn.disabled = false;
    }
  }

  function paintFailure(error) {
    const kind = error.payload?.kind;
    const fixable = error.payload?.fixable;

    clear(output).append(
      el(
        'div.panel',
        el(
          'div.panel-body',
          callout(
            kind === 'quota'
              ? 'Free-tier quota reached'
              : kind === 'not_configured'
              ? 'No API key configured'
              : kind === 'bad_key'
              ? 'The API key was rejected'
              : 'Could not generate the documentation',
            error.message,
            'err',
            el(
              'div',
              error.detail ? el('pre.code-block', error.detail) : null,
              fixable && isAi
                ? el(
                    'div.row.wrap.mt',
                    { style: { gap: '7px' } },
                    el(
                      'button.btn.btn-tiny',
                      {
                        onclick: () => {
                          chosenEngine = 'pattern';
                          navigate('schema', { engine: 'pattern', model: selected });
                        },
                      },
                      'Use pattern documentation instead',
                    ),
                    kind === 'quota'
                      ? el(
                          'button.btn.btn-tiny',
                          {
                            onclick: () => {
                              aiModel = 'gemini-2.5-flash-lite';
                              paintAiSettings();
                              generate();
                            },
                          },
                          'Retry with Flash-Lite (1,000/day)',
                        )
                      : null,
                  )
                : null,
              mode === 'model' && !kind
                ? el(
                    'div.mt',
                    el('span.small.faint', 'If the model has never been built there is no table to read types from. '),
                    el(
                      'button.btn.btn-tiny',
                      { onclick: () => navigate('runs', { select: selected, autorun: 'run' }) },
                      `⚡ Build ${selected}`,
                    ),
                  )
                : null,
            ),
          ),
        ),
      ),
    );
  }

  /* ------------------------------------------------------------ output --- */

  function paintOutput(payload) {
    const columns = payload.columns || [];
    const contract = columnContract(columns);
    const review = payload.stats?.needs_review || [];
    const profiled = columns.some((column) => column.profile);
    const ai = payload.ai;

    const view = tabs([
      {
        label: 'Contract',
        count: columns.length,
        render: () =>
          el(
            'div',
            el(
              'div.panel-body',
              el(
                'div.row.wrap.mb',
                { style: { gap: '6px' } },
                el('span.chip.info', payload.name),
                el('span.chip', `${columns.length} columns`),
                el('span.chip.ok', `${payload.stats.documented} documented`),
                review.length ? el('span.chip.warn', `${review.length} need review`) : null,
                profiled ? el('span.chip', 'profiled') : null,
                ai ? el('span.chip.info', ai.model_label) : el('span.chip', 'pattern rules'),
              ),
              ai ? aiUsageNote(ai) : null,
              review.length
                ? callout(
                    `${review.length} description${review.length === 1 ? '' : 's'} need a human`,
                    isAi
                      ? `The model was not confident about: ${review.join(', ')}. Those start with "Unclear:" in the YAML.`
                      : `The rules could not infer meaning for: ${review.join(', ')}. They are marked TODO in the YAML.`,
                    'warn',
                  )
                : callout('Every column has a description', 'Review them, then commit.', 'ok'),
            ),
            schemaTable(columns, { showProfile: profiled, showDescription: true }),
          ),
      },
      {
        label: 'name + data_type',
        render: () =>
          el(
            'div.panel-body',
            callout(
              'The bare contract',
              'Just the column names and their data types. This is the format to hand to whoever builds the next layer.',
              'info',
            ),
            el(
              'div.row.wrap.mt.mb',
              { style: { gap: '7px' } },
              el('button.btn.btn-tiny', { onclick: () => copy(contract, 'Contract copied') }, '⧉ Copy'),
              el(
                'button.btn.btn-tiny',
                { onclick: () => download(`${payload.name}_columns.yml`, contract, 'text/yaml') },
                '↓ Download',
              ),
            ),
            codeBlock(contract, { language: 'yaml', title: `${columns.length} columns` }),
          ),
      },
      { label: 'Full schema YAML', render: () => yamlPanel(payload, navigate) },
      {
        label: 'Markdown',
        render: () =>
          el(
            'div.panel-body',
            el('p.small.faint', { style: { marginTop: 0 } }, 'For a PR description or a Confluence page.'),
            el(
              'div.row.wrap.mb',
              { style: { gap: '7px' } },
              el('button.btn.btn-tiny', { onclick: () => copy(payload.markdown, 'Markdown copied') }, '⧉ Copy'),
              el(
                'button.btn.btn-tiny',
                { onclick: () => download(`${payload.name}.md`, payload.markdown, 'text/markdown') },
                '↓ Download',
              ),
            ),
            el('pre.code-block.tall', payload.markdown),
          ),
      },
    ]);

    clear(output).append(el('div.panel', view.node));
  }

  function aiUsageNote(ai) {
    const usage = ai.usage || {};
    return el(
      'p.tiny.faint',
      { style: { margin: '0 0 10px', lineHeight: '1.5' } },
      `${ai.model_label} · ${usage.requests || 1} request${(usage.requests || 1) === 1 ? '' : 's'} · ` +
        `${num(usage.prompt_tokens)} tokens in, ${num(usage.output_tokens)} out` +
        (ai.missing?.length ? ` · ${ai.missing.length} column(s) got no description and fell back to the pattern rules` : ''),
    );
  }

  /* ---------------------------------------------------------- assemble --- */

  paintList();
  paintSource();
  paintModeTabs();
  paintAiSettings();

  host.append(
    engineBar,
    el(
      'div.split',
      el(
        'div.panel',
        el('div.panel-head', el('h3', 'Source'), modeTabs),
        el(
          'div.panel-body',
          sourceHost,
          isAi ? el('div.mt', aiSettingsHost) : null,
          el('div.mt'),
          el('div.stat-label.mb', 'Options'),
          el(
            'div.grid',
            { style: { gap: '7px' } },
            profileToggle.node,
            testsToggle.node,
            docsToggle.node,
            isAi ? samplesToggle.node : null,
          ),
          el('div.mt', generateBtn),
        ),
      ),
      el(
        'div',
        el('div.panel', el('div.panel-head', el('h3', 'Models')), el('div.panel-body', modelList)),
        output,
      ),
    ),
  );

  clear(output).append(
    el(
      'div.panel',
      emptyState(
        isAi ? 'Ready to generate with Gemini' : 'Pick a model, then generate',
        isAi
          ? 'Choose a model on the right. Its columns and profile go to Gemini in one request, and you get back a description for every column plus the schema YAML.'
          : 'You get the real BigQuery data type for every column, a rule-drafted description, and a schema YAML block ready to commit.',
      ),
    ),
  );

  if ((selected || params.sql) && (!isAi || aiStatus.configured)) generate();

  return host;
}

/* ======================================================================
   editor - revise the descriptions already committed to the project
   ====================================================================== */

/* app.js tears a view down with clear(main), which drops the nodes but cannot
   stop a pending autosave timer. Holding the live instance here lets the next
   render flush and cancel the previous one. */
let liveEditor = null;

function editor(navigate, params = {}) {
  const host = el('div');
  const editorHost = el('div');
  const statusHost = el('div');
  const exportHost = el('div');

  /* Flush anything the previous instance still had pending, then stop its
     timers. saveNow() runs its synchronous guard before the first await, so
     destroying immediately afterwards does not cancel the request. */
  if (liveEditor) {
    if (liveEditor.dirty) liveEditor.saveNow();
    liveEditor.destroy();
    liveEditor = null;
  }

  let selected = params.model || state.scratch.schemaModel || firstModel();

  const modelList = el('div.scroll-list');
  const search = el('input.input', {
    type: 'search',
    placeholder: 'Filter models…',
    'aria-label': 'Filter the model list',
  });

  search.addEventListener('input', () => {
    const needle = search.value.trim().toLowerCase();
    for (const button of modelList.querySelectorAll('.list-btn')) {
      button.hidden = Boolean(needle) && !button.dataset.name.includes(needle);
    }
  });

  /* ------------------------------------------------------------ picker --- */

  function paintList() {
    clear(modelList);

    /* Out-of-scope models are omitted entirely rather than shown disabled.
       In the generator they are listed so you can see why something is
       missing, but here there is nothing to offer: the file cannot be opened
       and there is no partial action to take. */
    const usable = (state.models || []).filter((model) => model.in_scope !== false);

    if (!usable.length) {
      modelList.append(
        el('p.small.faint', { style: { padding: '10px' } },
          'No models are inside the permitted dataset scope.'),
      );
      return;
    }

    for (const model of usable) {
      const complete = model.column_count > 0
        && model.documented_columns === model.column_count;
      modelList.append(
        el(
          'button',
          {
            class: `list-btn${model.name === selected ? ' sel' : ''}`,
            dataset: { name: model.name.toLowerCase() },
            title: model.name,
            onclick: () => switchTo(model.name),
          },
          layerChip(model.layer),
          el('span.lb-name', model.name),
          el(
            'span.lb-meta',
            model.column_count
              ? el(`span.chip.${complete ? 'ok' : 'warn'}`,
                   `${model.documented_columns}/${model.column_count}`)
              : el('span.chip', 'no columns'),
          ),
        ),
      );
    }
  }

  function switchTo(name) {
    if (name === selected) return;

    /* Never lose an edit to a stray click in the list. Offer to save it
       rather than just refusing to move. */
    if (liveEditor?.dirty) {
      const ok = window.confirm(
        `You have unsaved changes to ${selected}.\n\n` +
        'OK to save them and open ' + name + '.\n' +
        'Cancel to stay here.',
      );
      if (!ok) return;
      liveEditor.saveNow();
    }

    selected = name;
    state.scratch.schemaModel = name;
    paintList();
    open();
  }

  /* ------------------------------------------------------------ export --- */

  const FORMATS = [
    { key: 'yml', label: 'YAML', ext: 'yml', mime: 'text/yaml' },
    { key: 'json', label: 'JSON', ext: 'json', mime: 'application/json' },
    { key: 'markdown', label: 'Markdown', ext: 'md', mime: 'text/markdown' },
  ];

  async function doExport(format) {
    /* Flush first, so a download can never hand someone a file that is missing
       the sentence they just typed. */
    if (liveEditor?.dirty) await liveEditor.saveNow();

    try {
      const payload = await api.exportDocs(selected);
      const raw = payload[format.key];
      const text = typeof raw === 'string' ? raw : JSON.stringify(raw, null, 2);
      download(`${selected}.${format.ext}`, text, format.mime);
      toast(`Downloaded ${selected}.${format.ext}`, { kind: 'ok' });
    } catch (error) {
      toast('Could not export', { kind: 'err', detail: error.message });
    }
  }

  function paintExport() {
    clear(exportHost).append(
      el(
        'div.row',
        { style: { gap: '6px' } },
        el('span.tiny.faint', { style: { alignSelf: 'center' } }, 'Download'),
        ...FORMATS.map((format) =>
          el('button.btn.btn-tiny', { onclick: () => doExport(format) }, format.label)),
      ),
    );
  }

  /* -------------------------------------------------------------- open --- */

  function open() {
    if (liveEditor) {
      liveEditor.destroy();
      liveEditor = null;
    }
    clear(statusHost);
    clear(exportHost);

    if (!selected) {
      clear(editorHost).append(
        el('div.panel', emptyState(
          'Pick a model to edit',
          'Choose one from the list. Its committed descriptions open ready to change.',
        )),
      );
      return;
    }

    const instance = documentationEditor({
      model: selected,
      onSaved: (result) => {
        /* The manifest was invalidated server-side, so the cached counts in the
           list are now stale. Refresh them quietly rather than forcing a reload. */
        refreshCounts();
        if (result?.applied?.length) {
          toast(
            `Saved ${result.applied.length} description` +
              `${result.applied.length === 1 ? '' : 's'}`,
            { kind: 'ok', detail: result.path },
          );
        }
      },
    });

    liveEditor = instance;
    statusHost.append(instance.statusNode);
    paintExport();

    clear(editorHost).append(
      el(
        'div.panel',
        el(
          'div.panel-head',
          el('h3', selected),
          el('div.row', { style: { gap: '10px', marginLeft: 'auto' } }, statusHost, exportHost),
        ),
        instance.node,
      ),
    );
  }

  /* The counts on the left come from the models list, which is built from the
     manifest. After a save the manifest is stale until dbt parses again, so
     patch the numbers locally from what we know rather than lying. */
  async function refreshCounts() {
    try {
      const payload = await api.editableDocs(selected);
      const model = (state.models || []).find((m) => m.name === selected);
      if (model) {
        model.documented_columns = payload.documented;
        model.column_count = payload.column_count;
        model.has_description = payload.model_has_description;
        paintList();
      }
    } catch {
      /* A failed count refresh is cosmetic; the editor itself already
         reported anything that actually went wrong. */
    }
  }

  /* ---------------------------------------------------------- assemble --- */

  paintList();

  host.append(
    el(
      'div.row.between.mb',
      el(
        'div.row',
        { style: { gap: '9px' } },
        el('span.chip.info', 'Edit'),
        el('span.small.faint',
          'Editing the descriptions committed in this project\'s schema YAML files'),
      ),
      el(
        'button.btn.btn-tiny',
        {
          onclick: () => {
            if (liveEditor?.dirty) {
              const ok = window.confirm(
                'You have unsaved changes.\n\nOK to save them and leave.\nCancel to stay.',
              );
              if (!ok) return;
              liveEditor.saveNow();
            }
            chosenEngine = null;
            navigate('schema');
          },
        },
        '← Switch mode',
      ),
    ),
    el(
      'div.split',
      el(
        'div.panel',
        el('div.panel-head', el('h3', 'Models')),
        el('div.panel-body', el('div.mb', search), modelList),
      ),
      editorHost,
    ),
  );

  open();

  return host;
}

/* ----------------------------------------------------------- yaml panel --- */

function yamlPanel(payload, navigate) {
  const pathInput = el('input.input', {
    value: payload.suggested_path || `models/_${payload.name}.yml`,
  });
  const status = el('div');

  async function write(mode) {
    const path = pathInput.value.trim();
    if (!path) {
      toast('Give the file a path.', { kind: 'warn' });
      return;
    }

    clear(status).append(loading(`Writing ${path}…`));
    try {
      const result = await api.writeFile(path, payload.yaml, mode);
      clear(status).append(
        callout(
          `Wrote ${result.written}`,
          [
            `${num(result.bytes)} bytes`,
            result.backup ? `previous version saved as ${result.backup}` : null,
            result.note,
          ]
            .filter(Boolean)
            .join(' · '),
          'ok',
          el(
            'button.btn.btn-tiny.mt',
            { onclick: () => navigate('runs', { autorun: 'parse' }) },
            '⟳ Refresh manifest now',
          ),
        ),
      );
      toast(`Wrote ${result.written}`, { kind: 'ok' });
    } catch (error) {
      clear(status).append(
        callout('Write failed', error.message, 'err',
          error.detail ? el('pre.code-block', error.detail) : null),
      );
    }
  }

  return el(
    'div.panel-body',
    callout(
      'Review before committing',
      payload.engine === 'ai'
        ? 'A language model wrote these descriptions from your schema and profile. It is usually right and occasionally confidently wrong, so read them. Overwriting an existing file keeps a .bak copy.'
        : 'Descriptions are drafted from rules, not authoritative. Overwriting an existing file keeps a .bak copy next to it.',
      'warn',
    ),
    el(
      'div.row.wrap.mt.mb',
      { style: { gap: '7px' } },
      el('button.btn.btn-tiny', { onclick: () => copy(payload.yaml, 'YAML copied') }, '⧉ Copy'),
      el(
        'button.btn.btn-tiny',
        { onclick: () => download(`${payload.name}.yml`, payload.yaml, 'text/yaml') },
        '↓ Download',
      ),
    ),
    codeBlock(payload.yaml, { language: 'yaml', tall: true }),
    el(
      'div.mt',
      el('div.stat-label.mb', 'Write into the project'),
      el(
        'div.row.wrap',
        { style: { gap: '7px' } },
        pathInput,
        el('button.btn', { onclick: () => write('overwrite') }, '⤓ Overwrite'),
        el('button.btn', { onclick: () => write('append') }, '+ Append'),
      ),
      el('div.mt', status),
    ),
  );
}

/* ---------------------------------------------------------------- utils --- */

function checkbox(label, checked, hint) {
  const input = el('input', { type: 'checkbox' });
  input.checked = checked;
  const node = el(
    'div',
    el('label.switch', input, el('span', label)),
    hint ? el('p.tiny.faint', { style: { margin: '2px 0 0 22px', lineHeight: '1.45' } }, hint) : null,
  );
  return { node, get checked() { return input.checked; } };
}

function firstModel() {
  // Only ever preselect something the UI is permitted to read, otherwise the
  // page opens on an immediate scope refusal.
  const usable = (state.models || []).filter((model) => model.in_scope !== false);
  const bronze = usable.find((model) => model.layer === 'bronze');
  return bronze?.name || usable[0]?.name || '';
}
