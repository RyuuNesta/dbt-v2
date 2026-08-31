/* ==========================================================================
   schedule.js - unattended dbt runs through the Windows Task Scheduler.

   The honesty problem this page has to solve: local scheduling looks like an
   orchestrator and is not one. If the machine is asleep at 06:00 nothing runs,
   nothing retries, and nothing tells you. So the caveats are on the page rather
   than in a tooltip, and every run that did not happen shows up as a gap in the
   history rather than as a reassuring green tick.

   Saving and registering are deliberately two steps. Saving writes a JSON
   record. Registering hands the job to Windows, which is the moment dbt gains
   the ability to write to a warehouse with nobody watching.
   ========================================================================== */

import { ago, api, clear, copy, el, num, state, toast } from '../core.js';
import { callout, codeBlock, emptyState, loading, modal } from '../components.js';

export const meta = {
  title: 'Schedules',
  subtitle: 'Run dbt unattended through the Windows Task Scheduler',
};

const STATUS_CHIP = {
  success: { kind: 'ok', label: 'success' },
  failed: { kind: 'err', label: 'failed' },
  error: { kind: 'err', label: 'error' },
  skipped: { kind: 'warn', label: 'skipped' },
};

export function render(navigate) {
  const host = el('div');
  const listHost = el('div');
  const runsHost = el('div');
  const noteHost = el('div');

  let payload = null;

  /* --------------------------------------------------------------- load --- */

  async function load() {
    clear(listHost).append(el('div.panel', loading('Reading schedules and asking Windows about each task…')));

    try {
      payload = await api.schedules();
    } catch (error) {
      clear(listHost).append(
        el('div.panel', el('div.panel-body',
          callout('Could not read the schedules', error.message, 'err',
            error.detail ? el('pre.code-block', error.detail) : null))),
      );
      return;
    }

    paintNotes();
    paintList();
    paintRuns();
  }

  /* -------------------------------------------------------------- notes --- */

  function paintNotes() {
    const blocks = [];

    if (!payload.windows) {
      blocks.push(
        callout(
          'This machine is not running Windows',
          'Schedules can still be defined, and each one writes a .cmd wrapper you '
          + 'can translate into a cron entry. Registering them from here needs '
          + 'schtasks, which is Windows-only.',
          'warn',
        ),
      );
    }

    if (payload.lock?.held) {
      blocks.push(
        callout(
          'A dbt process is running right now',
          `${payload.lock.owner} (pid ${payload.lock.pid}), started `
          + `${Math.round(payload.lock.age || 0)}s ago. A schedule firing now will `
          + 'record itself as skipped rather than run alongside it.',
          'info',
        ),
      );
    }

    clear(noteHost).append(
      ...blocks,
      el(
        'div.panel.mt',
        el('div.panel-head', el('h3', 'Before you rely on this')),
        el(
          'div.panel-body',
          el(
            'div.grid.grid-2',
            ...(payload.notes || []).map((note) =>
              el(
                'div',
                el(
                  'div.row.mb',
                  { style: { gap: '7px' } },
                  el(`span.chip.${note.kind}`, note.kind === 'warn' ? '!' : 'i'),
                  el('strong.small', note.title),
                ),
                el('p.small.muted', { style: { margin: 0, lineHeight: '1.6' } }, note.body),
              )),
          ),
        ),
      ),
    );
  }

  /* --------------------------------------------------------------- list --- */

  function paintList() {
    const schedules = payload.schedules || [];

    if (!schedules.length) {
      clear(listHost).append(
        el(
          'div.panel',
          emptyState(
            'No schedules yet',
            'A schedule runs one dbt command on a timer. Start with a nightly '
            + 'build of the bronze layer, which is the one that usually needs to '
            + 'be current before anyone looks at a dashboard.',
          ),
        ),
      );
      return;
    }

    clear(listHost).append(
      el(
        'div.grid',
        { style: { gap: '12px' } },
        ...schedules.map(scheduleCard),
      ),
    );
  }

  function scheduleCard(schedule) {
    const task = schedule.task || {};
    const registered = Boolean(task.registered);

    /* The distinction that matters most: saved is not the same as scheduled.
       A saved-but-unregistered schedule will never fire, and that has to be
       impossible to miss. */
    const stateChip = registered
      ? el('span.chip.ok', `registered · ${task.state || 'ready'}`)
      : el('span.chip.warn', 'not registered — will not run');

    const lastRun = (payload.runs || [])
      .find((run) => run.schedule_id === schedule.id);

    return el(
      'div.panel',
      el(
        'div.panel-head',
        el('div', el('h3', schedule.name),
           el('p.muted.small', { style: { margin: 0 } }, schedule.schedule_text)),
        el('div.row', { style: { gap: '6px', marginLeft: 'auto' } },
           stateChip,
           schedule.enabled ? null : el('span.chip', 'disabled')),
      ),
      el(
        'div.panel-body',
        el(
          'div.row.wrap.mb',
          { style: { gap: '6px' } },
          el('span.chip.info', `dbt ${schedule.command}`),
          el('span.chip', `target ${schedule.target}`),
          schedule.select ? el('code.chip', `--select ${schedule.select}`) : null,
          schedule.exclude ? el('code.chip', `--exclude ${schedule.exclude}`) : null,
          schedule.full_refresh ? el('span.chip.warn', 'full refresh') : null,
        ),

        registered
          ? el(
              'dl.kv.mb',
              el('dt', 'Next run'), el('dd', task.next_run || 'unknown'),
              el('dt', 'Last run'), el('dd', task.last_run || 'never'),
              ...(task.last_result !== undefined && task.last_result !== null
                ? [el('dt', 'Last result'), el('dd', String(task.last_result))]
                : []),
            )
          : null,

        lastRun
          ? el(
              'div.mb',
              el('div.stat-label.mb', 'Most recent run'),
              runRow(lastRun),
            )
          : null,

        el(
          'div.row.wrap',
          { style: { gap: '7px' } },
          registered
            ? el(
                'button.btn.btn-tiny',
                { onclick: () => setRegistration(schedule, 'unregister') },
                'Unregister',
              )
            : el(
                'button.btn.btn-tiny.btn-primary',
                { onclick: () => confirmRegister(schedule) },
                'Register with Task Scheduler',
              ),
          el('button.btn.btn-tiny', { onclick: () => openEditor(schedule) }, 'Edit'),
          el(
            'button.btn.btn-tiny',
            { onclick: () => showCommands(schedule) },
            'Commands',
          ),
          el(
            'button.btn.btn-tiny.btn-danger',
            { onclick: () => remove(schedule) },
            'Delete',
          ),
        ),
      ),
    );
  }

  /* --------------------------------------------------------------- runs --- */

  function runRow(run) {
    const chip = STATUS_CHIP[run.status] || { kind: '', label: run.status };
    const counts = run.counts || {};

    return el(
      'div.row.wrap',
      { style: { gap: '7px', alignItems: 'center' } },
      el(`span.chip.${chip.kind}`, chip.label),
      el('span.small.faint', ago(run.started_at)),
      run.duration ? el('span.chip', `${run.duration}s`) : null,
      Object.keys(counts).length
        ? el(
            'span.row',
            { style: { gap: '4px' } },
            counts.pass ? el('span.chip.ok', `${counts.pass} pass`) : null,
            counts.warn ? el('span.chip.warn', `${counts.warn} warn`) : null,
            counts.error ? el('span.chip.err', `${counts.error} error`) : null,
            counts.skip ? el('span.chip', `${counts.skip} skip`) : null,
          )
        : null,
      run.log
        ? el('button.btn.btn-tiny.btn-ghost',
             { onclick: () => showLog(run) }, 'Log')
        : null,
      run.error
        ? el('span.small.faint', { title: run.error },
             run.error.length > 70 ? `${run.error.slice(0, 70)}…` : run.error)
        : null,
    );
  }

  function paintRuns() {
    const runs = payload.runs || [];

    clear(runsHost).append(
      el(
        'div.panel',
        el(
          'div.panel-head',
          el('h3', 'Run history'),
          el('div.row', { style: { gap: '6px', marginLeft: 'auto' } },
             el('span.chip', `${runs.length} recorded`),
             el('button.btn.btn-tiny.btn-ghost',
                { title: 'Refresh', onclick: () => load() }, '⟳')),
        ),
        runs.length
          ? el(
              'div.table-wrap',
              { style: { maxHeight: '46vh' } },
              el(
                'table.data.compact',
                el('thead', el('tr',
                  el('th', 'Schedule'), el('th', 'When'), el('th', 'Status'),
                  el('th.num', 'Took'), el('th', 'Result'), el('th', ''))),
                el(
                  'tbody',
                  ...runs.map((run) => {
                    const chip = STATUS_CHIP[run.status] || { kind: '', label: run.status };
                    const counts = run.counts || {};
                    return el(
                      'tr',
                      el('td.small', run.name || '—'),
                      el('td.small.faint',
                         { title: run.started_at
                             ? new Date(run.started_at * 1000).toLocaleString()
                             : '' },
                         ago(run.started_at)),
                      el('td', el(`span.chip.${chip.kind}`, chip.label)),
                      el('td.num.small', run.duration ? `${run.duration}s` : '—'),
                      el(
                        'td.small',
                        Object.keys(counts).length
                          ? `${counts.pass || 0} pass, ${counts.error || 0} error`
                          : el('span.faint', run.error
                              ? (run.error.length > 60
                                  ? `${run.error.slice(0, 60)}…` : run.error)
                              : '—'),
                      ),
                      el('td', run.log
                        ? el('button.btn.btn-tiny.btn-ghost',
                             { onclick: () => showLog(run) }, 'Log')
                        : null),
                    );
                  }),
                ),
              ),
            )
          : el(
              'div.panel-body',
              el('p.small.faint', { style: { margin: 0, lineHeight: '1.6' } },
                'Nothing has run yet. A gap here is how a missed schedule looks: '
                + 'Task Scheduler does not retry, so if the machine was asleep at '
                + 'the trigger time there will simply be no row.'),
            ),
      ),
    );
  }

  async function showLog(run) {
    const bodyHost = el('div', loading('Reading the log…'));
    const dialog = modal({
      title: run.name || 'Scheduled run',
      subtitle: `${run.status} · ${ago(run.started_at)} · exit ${run.exit_code}`,
      body: bodyHost,
      width: '940px',
    });

    try {
      const result = await api.scheduleLog(run.log);
      clear(bodyHost).append(
        el(
          'div',
          el(
            'div.row.wrap.mb',
            { style: { gap: '6px' } },
            el('button.btn.btn-tiny',
               { onclick: () => copy(result.text, 'Log copied') }, '⧉ Copy'),
            el('span.small.faint', `${num(run.line_count)} lines`),
          ),
          el('pre.code-block.tall', result.text),
        ),
      );
    } catch (error) {
      clear(bodyHost).append(callout('Could not read the log', error.message, 'err'));
    }

    return dialog;
  }

  /* ------------------------------------------------------------ actions --- */

  function confirmRegister(schedule) {
    const commands = schedule.commands || {};

    const dialog = modal({
      title: 'Register this schedule with Windows?',
      subtitle: schedule.name,
      width: '820px',
      body: el(
        'div',
        callout(
          'This lets dbt run without anyone watching',
          `${schedule.schedule_text}, Windows will run `
          + `dbt ${schedule.command} against target ${schedule.target}. `
          + 'It writes to BigQuery. Nobody will be looking at the output, so read '
          + 'the command below and make sure it is the one you want.',
          'warn',
        ),
        schedule.target !== 'dev'
          ? callout(
              `Target is '${schedule.target}', not dev`,
              'profiles.yml describes prod as orchestrator-only. A scheduled task '
              + 'on one person\'s machine writing to production datasets is not '
              + 'what that comment intends, and it will stop the day this machine '
              + 'is rebuilt. Consider pointing it at dev.',
              'err',
            )
          : null,
        el('div.mt', el('div.stat-label.mb', 'The dbt command that will run'),
           codeBlock(commands.dbt || '', { language: 'bash' })),
        el('div.mt', el('div.stat-label.mb', 'The schtasks command being issued'),
           codeBlock(commands.register || '', { language: 'bash' })),
        el(
          'div.row.wrap.mt',
          { style: { gap: '7px' } },
          el(
            'button.btn.btn-primary',
            {
              onclick: () => {
                dialog.close();
                setRegistration(schedule, 'register');
              },
            },
            'Register it',
          ),
          el('button.btn', { onclick: () => dialog.close() }, 'Cancel'),
        ),
      ),
    });
  }

  async function setRegistration(schedule, action) {
    try {
      const result = await api.registerSchedule(schedule.id, action);
      toast(
        action === 'register'
          ? `Registered "${result.task_name || schedule.name}"`
          : 'Task removed from Task Scheduler',
        { kind: 'ok', detail: result.output || '' },
      );
      await load();
    } catch (error) {
      toast('Task Scheduler refused', { kind: 'err', detail: error.message });
    }
  }

  function showCommands(schedule) {
    const commands = schedule.commands || {};
    const rows = [
      ['The dbt command', commands.dbt,
       'Assembled by the same code as a manual run, so the gold exclusion is already in it.'],
      ['Run it now, by hand', commands.run_now,
       'The fastest way to find out whether a schedule works without waiting for its trigger.'],
      ['Register', commands.register,
       'Run this yourself if the button is blocked by group policy.'],
      ['Unregister', commands.unregister, ''],
    ];

    modal({
      title: 'Commands for this schedule',
      subtitle: schedule.name,
      width: '900px',
      body: el(
        'div',
        el('p.small.muted', { style: { marginTop: 0, lineHeight: '1.6' } },
          'Everything this schedule involves, in a form you can paste into a '
          + 'terminal. The wrapper file is what Task Scheduler actually calls.'),
        el('div.field.mt', el('label', 'Wrapper file'),
           el('code.small', { style: { wordBreak: 'break-all' } }, commands.wrapper || '')),
        ...rows.filter(([, value]) => value).map(([label, value, hint]) =>
          el(
            'div.mt',
            el(
              'div.row.between.mb',
              el('div.stat-label', label),
              el('button.btn.btn-tiny',
                 { onclick: () => copy(value, `${label} copied`) }, '⧉ Copy'),
            ),
            hint ? el('p.tiny.faint', { style: { margin: '0 0 5px' } }, hint) : null,
            codeBlock(value, { language: 'bash' }),
          )),
      ),
    });
  }

  async function remove(schedule) {
    const ok = window.confirm(
      `Delete the schedule "${schedule.name}"?\n\n`
      + 'This also removes its Windows task and its wrapper file. '
      + 'The run history is kept.',
    );
    if (!ok) return;

    try {
      await api.deleteSchedule(schedule.id);
      toast('Schedule deleted', { kind: 'ok' });
      await load();
    } catch (error) {
      toast('Could not delete it', { kind: 'err', detail: error.message });
    }
  }

  /* ------------------------------------------------------------- editor --- */

  function openEditor(existing = null) {
    const isNew = !existing;

    const nameInput = el('input.input', {
      value: existing?.name || '',
      placeholder: 'Nightly bronze build',
    });

    const commandSelect = el('select.select');
    for (const command of payload.commands || []) {
      commandSelect.append(el('option', {
        value: command,
        selected: command === (existing?.command || 'build'),
      }, `dbt ${command}`));
    }

    const targetSelect = el('select.select');
    for (const target of payload.targets || []) {
      targetSelect.append(el('option', {
        value: target,
        selected: target === (existing?.target || payload.default_target),
      }, target));
    }

    const frequencySelect = el('select.select');
    for (const frequency of payload.frequencies || []) {
      frequencySelect.append(el('option', {
        value: frequency.id,
        selected: frequency.id === (existing?.frequency || 'DAILY'),
      }, frequency.label));
    }

    const atInput = el('input.input', {
      type: 'time',
      value: existing?.at || '06:00',
    });
    const daySelect = el('select.select');
    for (const day of payload.weekdays || []) {
      daySelect.append(el('option', {
        value: day,
        selected: day === (existing?.day || 'MON'),
      }, day));
    }

    const selectInput = el('input.input', {
      value: existing?.select || '',
      placeholder: 'tag:bronze',
    });
    const excludeInput = el('input.input', {
      value: existing?.exclude || '',
      placeholder: 'optional',
    });

    const fullRefresh = el('input', { type: 'checkbox' });
    fullRefresh.checked = Boolean(existing?.full_refresh);
    const enabled = el('input', { type: 'checkbox' });
    enabled.checked = existing ? Boolean(existing.enabled) : true;

    const timeField = el('div.field', el('label', 'Start time'), atInput);
    const dayField = el('div.field', el('label', 'Day'), daySelect);
    const statusHost = el('div');

    function syncFrequencyFields() {
      const spec = (payload.frequencies || [])
        .find((f) => f.id === frequencySelect.value) || {};
      timeField.hidden = !spec.needs_time;
      dayField.hidden = !spec.needs_day;
    }
    frequencySelect.addEventListener('change', syncFrequencyFields);
    syncFrequencyFields();

    async function submit() {
      clear(statusHost).append(loading('Validating…'));
      try {
        const result = await api.saveSchedule({
          id: existing?.id,
          created_at: existing?.created_at,
          name: nameInput.value,
          command: commandSelect.value,
          target: targetSelect.value,
          frequency: frequencySelect.value,
          at: atInput.value,
          day: daySelect.value,
          select: selectInput.value,
          exclude: excludeInput.value,
          full_refresh: fullRefresh.checked,
          enabled: enabled.checked,
        });
        dialog.close();
        toast(isNew ? 'Schedule created' : 'Schedule updated', {
          kind: 'ok',
          detail: result.note,
        });
        await load();
      } catch (error) {
        clear(statusHost).append(
          callout('Cannot save this schedule', error.message, 'err'),
        );
      }
    }

    const dialog = modal({
      title: isNew ? 'New schedule' : 'Edit schedule',
      subtitle: isNew
        ? 'Saved first, registered with Windows separately'
        : existing.name,
      width: '760px',
      body: el(
        'div',
        el(
          'div.grid.grid-2',
          el('div.field', el('label', 'Name'), nameInput,
             el('span.hint', 'Also becomes the Task Scheduler task name.')),
          el('div.field', el('label', 'Command'), commandSelect),
        ),
        el(
          'div.grid.grid-2.mt',
          el('div.field', el('label', 'Frequency'), frequencySelect),
          el('div.field', el('label', 'Target'), targetSelect,
             el('span.hint', 'Decides which datasets it writes to.')),
        ),
        el('div.grid.grid-2.mt', timeField, dayField),
        el(
          'div.grid.grid-2.mt',
          el('div.field', el('label', 'Select'), selectInput,
             el('span.hint', 'Leave empty to build everything in scope.')),
          el('div.field', el('label', 'Exclude'), excludeInput),
        ),
        el(
          'div.mt',
          el('label.switch', fullRefresh, el('span', 'Full refresh')),
          el('p.tiny.faint', { style: { margin: '2px 0 0 22px', lineHeight: '1.45' } },
            'Rebuilds incremental models from scratch. Slower and costs more to '
            + 'scan, so it is rarely what you want on a nightly timer.'),
        ),
        el('div.mt', el('label.switch', enabled, el('span', 'Enabled'))),
        el(
          'p.tiny.faint',
          { style: { marginTop: '9px', lineHeight: '1.5' } },
          'The command is assembled and checked when you save, so a selector '
          + 'naming the gold layer is refused now rather than at the trigger time.',
        ),
        el('div.mt', statusHost),
        el(
          'div.row.wrap.mt',
          { style: { gap: '7px' } },
          el('button.btn.btn-primary', { onclick: submit },
             isNew ? 'Save schedule' : 'Save changes'),
          el('button.btn', { onclick: () => dialog.close() }, 'Cancel'),
        ),
      ),
    });

    nameInput.focus();
  }

  /* ---------------------------------------------------------- assemble --- */

  host.append(
    el(
      'div.row.wrap.between.mb',
      { style: { gap: '10px' } },
      el('p.muted.small', { style: { margin: 0, maxWidth: '640px', lineHeight: '1.6' } },
        'A schedule runs one dbt command on a timer using the Windows Task '
        + 'Scheduler. Nothing here needs the dbt Studio server to be open.'),
      el(
        'div.row',
        { style: { gap: '7px' } },
        el('button.btn.btn-tiny', { onclick: () => load() }, '⟳ Refresh'),
        el('button.btn.btn-primary', { onclick: () => openEditor() }, '+ New schedule'),
      ),
    ),
    noteHost,
    el('div.mt', listHost),
    el('div.mt', runsHost),
  );

  load();
  return host;
}
