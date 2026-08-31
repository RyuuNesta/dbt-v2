/* ==========================================================================
   docedit.js - inline description editing with autosave.

   Editing model on purpose:

   - Each description is a contenteditable cell, not an <input>. Descriptions
     are prose that wraps over several lines, and a single-line input forces
     horizontal scrolling through text you are trying to read.
   - `plaintext-only` so a paste from a wiki cannot inject markup into a YAML
     file. Chrome supports it; the input handler strips any markup that slips
     through on other engines.
   - Autosave is debounced *and* on a ceiling timer: a long editing session
     should not go unsaved just because the user keeps typing. Blur saves
     immediately, because that is the moment the user has finished a thought.
   - The file mtime is carried as a conflict token. Any save that would clobber
     an external change is refused by the backend and surfaced here.
   ========================================================================== */

import { api, clear, el, num, toast } from './core.js';
import { callout } from './components.js';

/** Debounce: quiet period after the last keystroke before saving. */
const DEBOUNCE_MS = 2500;
/** Ceiling: save at least this often while editing continues. */
const AUTOSAVE_MS = 30000;

const SAVE_STATE = {
  clean:    { label: 'All changes saved',  kind: 'ok',    glyph: '✓' },
  dirty:    { label: 'Unsaved changes',    kind: 'warn',  glyph: '●' },
  saving:   { label: 'Saving…',            kind: 'info',  glyph: '⟳' },
  saved:    { label: 'Saved',              kind: 'ok',    glyph: '✓' },
  error:    { label: 'Save failed',        kind: 'err',   glyph: '✕' },
  conflict: { label: 'File changed on disk', kind: 'err', glyph: '⚠' },
};

/**
 * Build the editable documentation panel for one model.
 * Returns { node, destroy } so the caller can cancel timers on teardown.
 */
export function documentationEditor({ model, onSaved } = {}) {
  const host = el('div');
  const statusSlot = el('span.save-state');
  const bodyHost = el('div');

  /* Committed values from the file, used to decide what is actually dirty and
     to restore a rejected edit. */
  let baseline = { model_description: '', columns: {} };
  let pending = new Map();     // name (or __model__) -> new text
  let mtime = null;
  let state = 'clean';
  let debounceTimer = null;
  let ceilingTimer = null;
  let destroyed = false;
  let autosaveEnabled = true;

  const MODEL_KEY = '__model__';

  /* ------------------------------------------------------------ status --- */

  function setState(next, detail = '') {
    state = next;
    const meta = SAVE_STATE[next] || SAVE_STATE.clean;
    clear(statusSlot).append(
      el(`span.chip.${meta.kind}`,
        el('span', { 'aria-hidden': 'true' }, meta.glyph),
        el('span', { style: { marginLeft: '5px' } },
          next === 'dirty' && pending.size
            ? `${pending.size} unsaved change${pending.size === 1 ? '' : 's'}`
            : meta.label)),
      detail ? el('span.tiny.faint', { style: { marginLeft: '8px' } }, detail) : null,
    );
    /* Announced politely so a screen reader hears save transitions. */
    statusSlot.setAttribute('role', 'status');
    statusSlot.setAttribute('aria-live', 'polite');
  }

  /* ------------------------------------------------------------ timers --- */

  function scheduleSave() {
    if (!autosaveEnabled) return;

    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => save('debounce'), DEBOUNCE_MS);

    /* The ceiling timer is only armed once per dirty period, so continuous
       typing still gets flushed every AUTOSAVE_MS. */
    if (!ceilingTimer) {
      ceilingTimer = setTimeout(() => save('ceiling'), AUTOSAVE_MS);
    }
  }

  function clearTimers() {
    clearTimeout(debounceTimer);
    clearTimeout(ceilingTimer);
    debounceTimer = null;
    ceilingTimer = null;
  }

  /* -------------------------------------------------------------- edits --- */

  function normalise(text) {
    return String(text ?? '').replace(/\s+/g, ' ').trim();
  }

  function committedFor(key) {
    return key === MODEL_KEY
      ? baseline.model_description
      : (baseline.columns[key] ?? '');
  }

  /**
   * Pending edits that blank out a description which previously had content.
   * These need an explicit confirmation, so they are excluded from autosave and
   * must not keep the retry timer alive.
   */
  function needsConfirming() {
    return new Set(
      [...pending.entries()]
        .filter(([key, text]) => !text && normalise(committedFor(key)))
        .map(([key]) => key),
    );
  }

  /** Pending edits an autosave is actually allowed to send. */
  function autosavableCount() {
    return pending.size - needsConfirming().size;
  }

  function recordEdit(key, text, cell) {
    const cleaned = normalise(text);
    const committed = normalise(committedFor(key));

    if (cleaned === committed) {
      pending.delete(key);
      cell?.classList.remove('is-dirty');
    } else {
      pending.set(key, cleaned);
      cell?.classList.add('is-dirty');
    }

    if (!pending.size) {
      clearTimers();
      setState('clean');
      return;
    }

    /* A lone blanked cell leaves nothing for a timer to do; it waits for the
       user to confirm instead of waking up every few seconds to be refused.
       Say so, or the row just sits there marked unsaved with no explanation. */
    if (autosavableCount() > 0) {
      setState('dirty');
      scheduleSave();
    } else {
      clearTimers();
      setState('dirty', 'blank description needs Save now');
    }
  }

  /* --------------------------------------------------------------- save --- */

  async function save(reason = 'manual', { allowClearing = false } = {}) {
    if (destroyed || !pending.size || state === 'saving') return;

    clearTimers();

    /* Deleting prose someone wrote is not something a timer gets to decide, so
       a blanked description needs confirming. But only a deliberate gesture is
       allowed to raise the prompt: a confirm() dialog appearing mid-sentence
       because a debounce elapsed would be indefensible. Blur counts as
       deliberate - the user chose to leave the field. */
    const deliberate = reason === 'manual' || reason === 'blur';

    const clearing = needsConfirming();

    if (clearing.size && !allowClearing && deliberate) {
      const names = [...clearing].map((k) => (k === MODEL_KEY ? 'the model' : k));
      const ok = window.confirm(
        `You have blanked the description for ${names.join(', ')}.\n\n` +
        'These had content before. Clear them anyway?',
      );
      if (ok) return save(reason, { allowClearing: true });
      /* Declined. Fall through and save everything else, leaving the blanks
         pending so the cells keep showing unsaved. */
    }

    /* One blanked cell must not hold back the paragraph the user just fixed in
       a different row, so the blanks are dropped from this batch rather than
       the whole save being abandoned. */
    const holdBack = allowClearing ? new Set() : clearing;

    const snapshot = new Map(
      [...pending.entries()].filter(([key]) => !holdBack.has(key)),
    );
    if (!snapshot.size) {
      /* Nothing sendable. Say why, and do not arm a timer that can only fail. */
      setState('dirty', holdBack.size ? 'blank description needs Save now' : '');
      return;
    }

    const columns = {};
    let modelDescription;
    for (const [key, text] of snapshot) {
      if (key === MODEL_KEY) modelDescription = text;
      else columns[key] = text;
    }

    setState('saving');

    try {
      const result = await api.patchDocs({
        model,
        mtime,
        columns,
        model_description: modelDescription,
        allow_clearing: allowClearing,
      });

      mtime = result.mtime ?? mtime;

      /* The server names the model-level target 'model:<name>'; columns are
         named plainly. Map back so a rejection can be matched to its cell. */
      const rejectedKeys = new Set(
        (result.rejected || []).map((entry) =>
          (entry.target === `model:${model}` ? MODEL_KEY : entry.target)),
      );

      /* Only settle what the server did not reject.
         A rejected edit has to stay pending and stay marked dirty, otherwise
         the cell goes quiet while the file still holds the old text and nothing
         ever reconciles the two. A key that is absent from `rejected` is
         settled even if it is absent from `applied` too: that is the no-op
         case, where the file already contained exactly this text.

         Anything the user typed while the request was in flight has a
         different value in `pending` than in `snapshot`, so it survives. */
      for (const [key, text] of snapshot) {
        if (rejectedKeys.has(key)) continue;
        if (pending.get(key) === text) pending.delete(key);
        if (key === MODEL_KEY) baseline.model_description = text;
        else baseline.columns[key] = text;
      }

      for (const cell of host.querySelectorAll('.doc-cell.is-dirty')) {
        const key = cell.dataset.key;
        if (!pending.has(key)) {
          cell.classList.remove('is-dirty');
          cell.classList.add('is-saved');
          setTimeout(() => cell.classList.remove('is-saved'), 1400);
        }
      }

      if (result.rejected?.length) {
        setState('error', `${result.rejected.length} not applied`);
        toast('Some edits were not applied', {
          kind: 'warn',
          detail: result.rejected.map((r) => `${r.target}: ${r.reason}`).join('\n'),
        });
        /* Left dirty on purpose, but do not spin: retrying on a timer would
           just re-reject. The user has to change something or confirm. */
      } else if (pending.size) {
        /* More arrived while we were saving, or blanks are still waiting on a
           confirmation. Only re-arm the timer for the former. */
        if (autosavableCount() > 0) {
          setState('dirty');
          scheduleSave();
        } else {
          setState('dirty', 'blank description needs Save now');
        }
      } else {
        setState('saved', result.written ? result.path : 'no change needed');
        setTimeout(() => { if (state === 'saved' && !pending.size) setState('clean'); }, 2600);
      }

      if (result.written) onSaved?.(result);
    } catch (error) {
      if (error.payload?.conflict) {
        handleConflict(error);
        return;
      }
      setState('error', error.message);
      toast('Could not save the documentation', {
        kind: 'err',
        detail: `${error.message}\n\n${error.detail || ''}`.trim(),
      });
    }
  }

  /* ---------------------------------------------------------- conflict --- */

  function handleConflict(error) {
    autosaveEnabled = false;
    clearTimers();
    setState('conflict');

    const current = error.payload?.current || {};

    clear(conflictHost).append(
      callout(
        'This file changed outside the UI',
        error.message,
        'err',
        el(
          'div',
          el('p.small.muted', { style: { lineHeight: '1.6' } },
            error.detail ||
            'A git pull, an editor, or another tab has written to this file. ' +
            'Autosave is paused so your edits are not lost.'),
          el(
            'div.row.wrap.mt',
            { style: { gap: '7px' } },
            el(
              'button.btn.btn-tiny.btn-primary',
              {
                onclick: () => {
                  autosaveEnabled = true;
                  load();
                },
              },
              'Discard mine and reload',
            ),
            el(
              'button.btn.btn-tiny',
              {
                onclick: async () => {
                  /* Adopt the file's mtime, then push our edits over the top. */
                  mtime = error.payload?.disk_mtime ?? mtime;
                  autosaveEnabled = true;
                  clear(conflictHost);
                  await save('manual', { allowClearing: true });
                },
              },
              'Keep mine and overwrite',
            ),
          ),
          Object.keys(current.columns || {}).length
            ? el(
                'details.mt',
                el('summary.small.faint', { style: { cursor: 'pointer' } },
                  'What the file says now'),
                el(
                  'div.mt',
                  ...[...pending.keys()].map((key) => {
                    const theirs = key === MODEL_KEY
                      ? current.model_description
                      : current.columns?.[key];
                    return el(
                      'div.conflict-row',
                      el('code.small', key === MODEL_KEY ? '(model)' : key),
                      el('div.small',
                        el('div.faint', 'on disk: ', el('span', theirs || '(empty)')),
                        el('div.faint', 'yours: ', el('span', pending.get(key) || '(empty)'))),
                    );
                  }),
                ),
              )
            : null,
        ),
      ),
    );
  }

  const conflictHost = el('div');

  /* --------------------------------------------------------------- cell --- */

  function editableCell(key, text, { placeholder = 'Add a description…' } = {}) {
    const cell = el('div', {
      class: 'doc-cell',
      contenteditable: 'plaintext-only',
      role: 'textbox',
      'aria-multiline': 'true',
      'aria-label': key === MODEL_KEY
        ? 'Model description'
        : `Description for column ${key}`,
      spellcheck: 'true',
      dataset: { key, placeholder },
      tabindex: '0',
    });
    cell.textContent = text || '';

    cell.addEventListener('input', () => {
      /* Belt and braces: plaintext-only is not universal, so strip anything
         that arrived as markup rather than trusting the attribute. */
      if (cell.querySelector('*')) cell.textContent = cell.textContent;
      recordEdit(key, cell.textContent, cell);
    });

    /* Blur is the natural "I've finished this thought" moment. */
    cell.addEventListener('blur', () => {
      if (pending.has(key)) save('blur');
    });

    cell.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        cell.textContent = committedFor(key);
        recordEdit(key, cell.textContent, cell);
        cell.blur();
        return;
      }
      /* Enter commits rather than inserting a newline: YAML descriptions are
         folded to a single logical string anyway. Shift+Enter still breaks. */
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        cell.blur();
      }
    });

    return cell;
  }

  /* --------------------------------------------------------------- load --- */

  async function load() {
    clear(conflictHost);
    clear(bodyHost).append(el('div.loading', el('span.spinner'), el('span', 'Reading the schema file…')));
    setState('clean');
    pending = new Map();

    let payload;
    try {
      payload = await api.editableDocs(model);
    } catch (error) {
      clear(bodyHost).append(
        callout(
          error.payload?.needs_generate
            ? 'No schema file for this model yet'
            : 'Could not open the documentation',
          error.message,
          error.payload?.needs_generate ? 'warn' : 'err',
          error.payload?.needs_generate
            ? el('p.small.muted',
                'Generate a contract first on this page, write it to '
                + (error.payload.suggested_path || 'the models folder')
                + ', then come back to edit it.')
            : null,
        ),
      );
      return;
    }

    mtime = payload.mtime;
    baseline = {
      model_description: payload.model_description || '',
      columns: Object.fromEntries(
        (payload.columns || []).map((c) => [c.name, c.description || '']),
      ),
    };

    const rows = (payload.columns || []).map((column) =>
      el(
        'tr',
        el('td.mono.small', column.name),
        el('td', column.data_type
          ? el('span.type-badge', column.data_type)
          : el('span.faint.tiny', '—')),
        el('td', editableCell(column.name, column.description)),
      ),
    );

    clear(bodyHost).append(
      el(
        'div.panel-body',
        el(
          'div.row.wrap.between.mb',
          el(
            'div.row.wrap',
            { style: { gap: '6px' } },
            el('code.chip', payload.path),
            el('span.chip', `${payload.column_count} columns`),
            el(`span.chip.${payload.documented === payload.column_count ? 'ok' : 'warn'}`,
               `${payload.documented} described`),
          ),
          el(
            'div.row',
            { style: { gap: '6px' } },
            el('button.btn.btn-tiny', { onclick: () => save('manual') }, 'Save now'),
            el('button.btn.btn-tiny.btn-ghost', { onclick: () => load() }, 'Reload'),
          ),
        ),
        conflictHost,
        el(
          'div.field.mb',
          el('label', 'Model description'),
          editableCell(MODEL_KEY, payload.model_description,
                       { placeholder: 'What is this table for?' }),
        ),
      ),
      el(
        'div.table-wrap',
        { style: { maxHeight: '52vh' } },
        el(
          'table.data.compact.doc-table',
          el('thead', el('tr', el('th', 'Column'), el('th', 'Type'), el('th', 'Description'))),
          el('tbody', ...rows),
        ),
      ),
      el(
        'div.panel-body',
        el('p.tiny.faint', { style: { margin: 0, lineHeight: '1.6' } },
          'Click any description to edit it. Enter or clicking away saves; ' +
          'Escape reverts. Autosave runs a few seconds after you stop typing, ' +
          'and at least every 30 seconds while you keep going. Only ' +
          'descriptions are written - comments, tests and config are left ' +
          'untouched.'),
      ),
    );
  }

  host.append(el('div', bodyHost));
  load();

  return {
    node: host,
    statusNode: statusSlot,
    saveNow: () => save('manual'),
    reload: load,
    get dirty() { return pending.size > 0; },
    destroy() {
      destroyed = true;
      clearTimers();
    },
  };
}

export { SAVE_STATE };
