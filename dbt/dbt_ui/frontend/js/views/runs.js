/* ==========================================================================
   runs.js - run dbt commands with live logs.

   Replaces the terminal for day-to-day work. Selectors are the same strings
   the team already types, so knowledge transfers both ways.
   ========================================================================== */

import { ago, api, clear, el, num, secs, state, toast } from '../core.js';
import { callout, emptyState, logConsole, modal, tabs } from '../components.js';
import {
  COMMAND_DETAIL, FLOW_NOTES, FLOW_STAGES, RUN_STATE, SELECTOR_SYNTAX,
} from '../dbtdocs.js';
import { help } from '../prefs.js';
import { watchJob } from '../jobs.js';

export const meta = {
  title: 'Build & Test',
  subtitle: 'Rebuild your tables and watch the progress live',
};

/**
 * Outcome of the most recent run of each command, so the cards can carry state
 * across view switches. Keyed by command name.
 *   command -> { status, duration, counts, progress: {done, total} }
 */
const lastOutcome = new Map();

/** Which command is in flight, for the flow diagram highlight. */
let activeCommand = null;

const COMMAND_GROUPS = [
  {
    label: 'Build',
    commands: [
      ['build', '⚡', 'Seed, run and test in dependency order. The default for most work.'],
      ['run', '▶', 'Models only, no seeds and no tests.'],
      ['test', '✓', 'Tests only, against whatever is already built.'],
      ['seed', '⤓', 'Load the CSV files in seeds/.'],
    ],
  },
  {
    label: 'Inspect',
    commands: [
      ['parse', '⟳', 'Rebuild target/manifest.json. Every screen here reads from it.'],
      ['compile', '⌗', 'Render SQL without touching the warehouse.'],
      ['debug', '⚙', 'Check the profile, credentials and connection.'],
      ['deps', '⇩', 'Install the packages in packages.yml.'],
      ['docs', '📖', 'Generate the browsable catalog and lineage site.'],
      ['source', '⏱', 'Check source freshness.'],
    ],
  },
];

export function render(navigate, params = {}) {
  const host = el('div');
  const consoleView = logConsole({ tall: true });
  const jobHost = el('div');
  const historyHost = el('div.scroll-list');

  const selectInput = el('input.input', {
    id: 'sel-input',
    placeholder: 'e.g. silver_gl_entries+ or tag:bronze',
    value: params.select || '',
  });
  const excludeInput = el('input.input', {
    id: 'exc-input',
    placeholder: 'optional',
  });
  const fullRefresh = el('input', { type: 'checkbox' });

  let stopWatching = null;

  /* ------------------------------------------------------------- launch --- */

  async function launch(command) {
    const writes = (state.boot?.commands || []).find((entry) => entry.key === command)?.writes;

    if (writes && state.target === 'prod') {
      const ok = window.confirm(
        `Target is prod.\n\n"dbt ${command}" writes tables the business reads from ` +
          `(bronze_dbt, silver_dbt, gold_dbt).\n\nRun it anyway?`,
      );
      if (!ok) return;
    }

    const body = {
      command,
      select: selectInput.value.trim() || undefined,
      exclude: excludeInput.value.trim() || undefined,
      full_refresh: fullRefresh.checked || undefined,
    };
    if (writes && state.target === 'prod') body.confirm_prod = true;

    consoleView.clear();
    clear(jobHost).append(el('div.row', el('span.spinner'), el('span.small.faint', 'starting dbt…')));

    try {
      const { job } = await api.dbtRun(body);
      state.scratch.openJobId = job.id;
      attach(job);
      await refreshHistory();
    } catch (error) {
      clear(jobHost).append(
        callout(
          error.busy ? 'A run is already in progress' : 'Could not start dbt',
          error.message,
          error.busy ? 'warn' : 'err',
          error.detail ? el('pre.code-block', error.detail) : null,
        ),
      );
    }
  }

  /* -------------------------------------------------------------- watch --- */

  /**
   * dbt prints "12 of 46 START ..." for every node, which is the only
   * progress signal available while a run is in flight. Parsing it lets the
   * card show 12/46 instead of a bare spinner.
   */
  function readProgress(lines) {
    let latest = null;
    for (const line of lines) {
      const match = /(\d+)\s+of\s+(\d+)\s+(START|OK|PASS|FAIL|ERROR|SKIP)/i.exec(line.text || '');
      if (match) latest = { done: Number(match[1]), total: Number(match[2]) };
    }
    return latest;
  }

  function attach(job) {
    stopWatching?.();
    activeCommand = job.is_active ? job.command : null;
    paintJobHeader(job);
    paintCardStates();

    stopWatching = watchJob(job.id, {
      onLines: (lines) => {
        consoleView.append(lines);

        const progress = readProgress(lines);
        if (progress) {
          const entry = lastOutcome.get(job.command) || {};
          entry.progress = progress;
          lastOutcome.set(job.command, entry);
          paintCardStates();
        }
      },
      onUpdate: (updated) => paintJobHeader(updated),
      onDone: (finished, payload) => {
        activeCommand = null;
        paintJobHeader(finished);

        if (payload?.stats) state.stats = payload.stats;
        if (payload?.last_run) state.lastRun = payload.last_run;
        if (payload?.docs_available !== undefined) {
          state.docsAvailable = payload.docs_available;
        }

        lastOutcome.set(finished.command, {
          status: finished.status,
          duration: finished.duration,
          counts: payload?.last_run?.counts || null,
          progress: null,
        });
        paintCardStates();
        refreshHistory();

        toast(
          `${finished.label} ${finished.status} in ${secs(finished.duration)}`,
          { kind: finished.status === 'success' ? 'ok' : 'err' },
        );
      },
    });
  }

  function paintJobHeader(job) {
    const kind =
      job.status === 'success' ? 'ok' : job.status === 'failed' ? 'err' : job.status === 'cancelled' ? 'warn' : 'info';

    clear(jobHost).append(
      el(
        'div.row.wrap.between',
        el(
          'div.row.wrap',
          { style: { gap: '7px' } },
          job.is_active ? el('span.spinner') : null,
          el(`span.chip.${kind}`, job.status),
          el('span.small.mono', job.label),
          el('span.chip', `target ${job.target}`),
          el('span.chip', secs(job.duration)),
          el('span.small.faint', `${num(job.line_count)} lines`),
        ),
        el(
          'div.row',
          { style: { gap: '6px' } },
          job.is_active
            ? el(
                'button.btn.btn-tiny.btn-danger',
                {
                  onclick: async () => {
                    try {
                      await api.cancelJob(job.id);
                      toast('Cancellation requested.', { kind: 'warn' });
                    } catch (error) {
                      toast(error.message, { kind: 'err' });
                    }
                  },
                },
                'Cancel',
              )
            : null,
          !job.is_active && job.command === 'docs' && state.docsAvailable
            ? el('a.btn.btn-tiny', { href: '/dbt-docs', target: '_blank', rel: 'noopener' }, 'Open dbt docs ↗')
            : null,
        ),
      ),
      el('div.mt', el('code.tiny.faint', job.display_command || `dbt ${job.command}`)),
    );
  }

  /* ------------------------------------------------------------ history --- */

  async function refreshHistory() {
    try {
      const { jobs } = await api.jobs();
      clear(historyHost);
      if (!jobs.length) {
        historyHost.append(el('p.small.faint', 'No runs in this session yet.'));
        return;
      }
      for (const job of jobs) {
        const kind =
          job.status === 'success' ? 'ok' : job.status === 'failed' ? 'err' : job.status === 'cancelled' ? 'warn' : 'info';
        historyHost.append(
          el(
            'button',
            {
              class: `job-row${job.id === state.scratch.openJobId ? ' active' : ''}`,
              onclick: () => {
                state.scratch.openJobId = job.id;
                consoleView.clear();
                attach(job);
                refreshHistory();
              },
            },
            el(`span.chip.${kind}`, job.status),
            el('span.small.mono', job.command),
            el('span.tiny.faint', job.target),
            el('div.spacer'),
            el('span.tiny.faint', secs(job.duration)),
            el('span.tiny.faint', ago(job.created_at)),
          ),
        );
      }
    } catch {
      /* history is a convenience; a failure here should not break the view */
    }
  }

  /* -------------------------------------------------------- flow guide --- */

  /**
   * The stage diagram, drawn as SVG.
   *
   * Hand-rolled rather than a library: it is five boxes and four arrows, and a
   * charting dependency for that would be absurd. The current stage is
   * highlighted from activeCommand / lastOutcome, so the diagram doubles as a
   * progress indicator while a run is going.
   */
  function flowDiagram() {
    const NS = 'http://www.w3.org/2000/svg';
    const W = 150;
    const H = 62;
    const GAP = 44;
    const PAD = 8;
    const width = PAD * 2 + FLOW_STAGES.length * W + (FLOW_STAGES.length - 1) * GAP;

    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${width} ${H + PAD * 2 + 20}`);
    svg.setAttribute('width', '100%');
    svg.setAttribute('role', 'img');
    svg.setAttribute(
      'aria-label',
      `dbt execution order: ${FLOW_STAGES.map((s) => s.label).join(', then ')}`,
    );
    svg.classList.add('flow-svg');

    FLOW_STAGES.forEach((stage, index) => {
      const x = PAD + index * (W + GAP);
      const y = PAD;

      const outcome = lastOutcome.get(stage.command);
      const status = activeCommand === stage.command ? 'running' : outcome?.status;

      const group = document.createElementNS(NS, 'g');
      group.setAttribute('class', `flow-node${status ? ` is-${status}` : ''}`);
      group.setAttribute('transform', `translate(${x}, ${y})`);

      const rect = document.createElementNS(NS, 'rect');
      rect.setAttribute('width', String(W));
      rect.setAttribute('height', String(H));
      rect.setAttribute('rx', '8');
      group.append(rect);

      const label = document.createElementNS(NS, 'text');
      label.setAttribute('x', '12');
      label.setAttribute('y', '24');
      label.setAttribute('class', 'flow-cmd');
      label.textContent = `dbt ${stage.label}`;
      group.append(label);

      const title = document.createElementNS(NS, 'text');
      title.setAttribute('x', '12');
      title.setAttribute('y', '42');
      title.setAttribute('class', 'flow-title');
      title.textContent = stage.title;
      group.append(title);

      if (stage.writes) {
        const badge = document.createElementNS(NS, 'text');
        badge.setAttribute('x', String(W - 10));
        badge.setAttribute('y', '24');
        badge.setAttribute('text-anchor', 'end');
        badge.setAttribute('class', 'flow-writes');
        badge.textContent = 'writes';
        group.append(badge);
      }

      if (status) {
        const mark = document.createElementNS(NS, 'text');
        mark.setAttribute('x', String(W - 10));
        mark.setAttribute('y', '42');
        mark.setAttribute('text-anchor', 'end');
        mark.setAttribute('class', 'flow-state');
        mark.textContent = RUN_STATE[status]?.glyph || '';
        group.append(mark);
      }

      const tooltip = document.createElementNS(NS, 'title');
      tooltip.textContent = `${stage.title}. ${stage.body}`;
      group.append(tooltip);

      svg.append(group);

      /* Arrow to the next stage. */
      if (index < FLOW_STAGES.length - 1) {
        const x1 = x + W + 6;
        const x2 = x + W + GAP - 6;
        const midY = y + H / 2;

        const line = document.createElementNS(NS, 'path');
        line.setAttribute('d', `M ${x1} ${midY} L ${x2} ${midY}`);
        line.setAttribute('class', 'flow-arrow');
        svg.append(line);

        const head = document.createElementNS(NS, 'path');
        head.setAttribute(
          'd',
          `M ${x2} ${midY} l -6 -4 l 0 8 z`,
        );
        head.setAttribute('class', 'flow-head');
        svg.append(head);
      }
    });

    return el('div.flow-wrap', svg);
  }

  function openFlowGuide(trigger = null) {
    modal({
      title: 'How dbt works',
      subtitle: 'The order things happen in, and why',
      returnFocusTo: trigger,
      body: el(
        'div',
        el('p.muted', { style: { marginTop: 0, lineHeight: '1.65' } },
          'dbt turns SQL SELECT statements into tables and views. You never write ' +
          'CREATE or DROP; you write a query per table and dbt works out the DDL, ' +
          'the order, and the rebuild.'),

        el('div.stat-label.mt.mb', 'Execution order'),
        flowDiagram(),

        el(
          'div.grid.mt',
          { style: { gap: '10px' } },
          ...FLOW_STAGES.map((stage) =>
            el(
              'div.flow-detail',
              el(
                'div.row',
                { style: { gap: '8px' } },
                el('code.chip', `dbt ${stage.label}`),
                el('strong.small', stage.title),
                stage.writes ? el('span.chip.warn', 'writes') : el('span.chip', 'read-only'),
                stage.optional ? el('span.chip', 'as needed') : null,
              ),
              el('p.small.muted', { style: { margin: '5px 0 0', lineHeight: '1.6' } }, stage.body),
            ),
          ),
        ),

        el('div.stat-label.mt.mb', 'What to know'),
        el(
          'div.grid',
          { style: { gap: '9px' } },
          ...FLOW_NOTES.map((note) =>
            el(
              'details.flow-note',
              el('summary', note.title),
              el('p.small.muted', { style: { lineHeight: '1.65' } }, note.body),
            ),
          ),
        ),

        el(
          'div.mt',
          callout(
            'In this project',
            `dbt build is what you want almost always. The ${(state.scope?.blocked_layers || ['gold']).join(', ')} ` +
              'layer is always excluded from runs started here, and the environment ' +
              'you pick in the header decides which datasets get written.',
            'info',
          ),
        ),
      ),
    });
  }

  /* ----------------------------------------------------------- assemble --- */

  /* ------------------------------------------------- command palette --- */

  /** Card elements by command, so status can be repainted without a rerender. */
  const cardNodes = new Map();

  function commandCard(command, icon, hint) {
    const writes = (state.boot?.commands || []).find((e) => e.key === command)?.writes;
    const detail = COMMAND_DETAIL[command];

    const statusSlot = el('span.cmd-status', { dataset: { role: 'status' } });
    const runButton = el(
      'button.cmd-run',
      {
        onclick: () => launch(command),
        title: `Run dbt ${command}`,
        'aria-label': `Run dbt ${command}`,
      },
      el('span.cmd-ico', { 'aria-hidden': 'true' }, icon),
      el('span.cmd-name', `dbt ${command}`),
      writes ? el('span.chip.warn.cmd-writes', 'writes') : null,
      statusSlot,
    );

    /* <details> gives a native, keyboard-accessible, mobile-friendly
       disclosure with no JS state to manage. Collapsed by default. */
    const expander = detail
      ? el(
          'details.cmd-detail',
          el('summary', el('span', 'What this does'), el('span.cmd-chev', { 'aria-hidden': 'true' }, '▾')),
          el(
            'div.cmd-detail-body',
            section('What it does', el('p', detail.what)),
            section('When to use it', el('p', detail.when)),
            section('What it affects', el('p', detail.affects)),
            detail.pitfalls?.length
              ? section(
                  'Watch out for',
                  el('ul', ...detail.pitfalls.map((p) => el('li', p))),
                )
              : null,
            detail.examples?.length
              ? section(
                  'Examples',
                  el(
                    'div.cmd-examples',
                    ...detail.examples.map(([cmd, note]) =>
                      el(
                        'div.cmd-example',
                        el('code', cmd),
                        el('span.tiny.faint', note),
                        el(
                          'button.btn.btn-tiny.btn-ghost',
                          {
                            title: 'Put this selector in the field below',
                            onclick: () => applyExample(cmd),
                          },
                          'use',
                        ),
                      ),
                    ),
                  ),
                )
              : null,
          ),
        )
      : null;

    const card = el('div.cmd-card', { dataset: { command } }, runButton,
      el('p.cmd-hint', hint), expander);

    cardNodes.set(command, card);
    return card;
  }

  function section(label, content) {
    return el('div.cmd-section', el('div.stat-label', label), content);
  }

  /** Pull --select / --exclude out of an example and load it into the fields. */
  function applyExample(commandLine) {
    const select = commandLine.match(/--select\s+(\S+)/);
    const exclude = commandLine.match(/--exclude\s+(\S+)/);
    selectInput.value = select ? select[1] : '';
    excludeInput.value = exclude ? exclude[1] : '';
    fullRefresh.checked = commandLine.includes('--full-refresh');
    selectInput.focus();
    toast(
      select ? `Selector set to ${select[1]}` : 'Selector cleared',
      { kind: 'ok', timeout: 2200 },
    );
  }

  /** Repaint every card from lastOutcome / activeCommand. */
  function paintCardStates() {
    for (const [command, card] of cardNodes) {
      const outcome = lastOutcome.get(command);
      const running = activeCommand === command;
      const status = running ? 'running' : outcome?.status;
      const meta = status ? RUN_STATE[status] : null;

      card.dataset.state = status || '';

      const slot = card.querySelector('[data-role="status"]');
      clear(slot);
      if (!meta) continue;

      const bits = [];
      if (running && outcome?.progress?.total) {
        bits.push(`${outcome.progress.done}/${outcome.progress.total}`);
      } else if (outcome?.duration) {
        bits.push(secs(outcome.duration));
      }

      slot.append(
        el(`span.chip.${meta.kind}`,
          el('span', { 'aria-hidden': 'true' }, meta.glyph),
          el('span', { style: { marginLeft: '4px' } }, bits.length ? bits.join(' · ') : meta.label)),
      );
      slot.title = `Last run ${meta.label}`;
    }
  }

  /* --------------------------------------------------- cheat sheet --- */

  const cheatSheet = el('div.cheat', { hidden: true },
    el(
      'table.data.compact',
      el('thead', el('tr', el('th', 'Syntax'), el('th', 'Selects'), el('th', 'Example'))),
      el(
        'tbody',
        ...SELECTOR_SYNTAX.map(([syntax, meaning, example]) =>
          el(
            'tr',
            el('td.mono.small', syntax),
            el('td.small', meaning),
            el(
              'td',
              el(
                'button.btn.btn-tiny.btn-ghost.mono',
                {
                  style: { fontSize: '11px' },
                  title: 'Load this into the selector field',
                  onclick: () => {
                    selectInput.value = example;
                    selectInput.focus();
                  },
                },
                example,
              ),
            ),
          ),
        ),
      ),
    ),
    el('p.tiny.faint', { style: { marginBottom: 0, lineHeight: '1.5' } },
      'A space between two selectors means "both". --exclude subtracts from whatever --select matched.'),
  );

  const cheatToggle = el(
    'button.btn.btn-tiny.btn-ghost',
    {
      'aria-expanded': 'false',
      onclick: () => {
        const open = cheatSheet.hidden;
        cheatSheet.hidden = !open;
        cheatToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        cheatToggle.textContent = open ? 'Hide syntax help' : 'Selector syntax help';
      },
    },
    'Selector syntax help',
  );

  const palette = el(
    'div.panel',
    el(
      'div.panel-head',
      el('h3', 'Commands'),
      el(
        'button.btn.btn-tiny',
        { onclick: (event) => openFlowGuide(event.currentTarget) },
        el('span', { 'aria-hidden': 'true' }, 'ℹ'),
        el('span', 'How does this work?'),
      ),
    ),
    el(
      'div.panel-body',
      help(
        'Each button runs a dbt command against your warehouse. Expand "What ' +
        'this does" on any card for the detail, or open the flow guide above ' +
        'to see how the stages fit together.',
      ),
      ...COMMAND_GROUPS.map((group) =>
        el(
          'div.mb',
          el('div.stat-label.mb', group.label),
          el(
            'div.grid',
            { style: { gap: '7px' } },
            ...group.commands.map(([command, icon, hint]) => commandCard(command, icon, hint)),
          ),
        ),
      ),
      el('div.stat-label.mb', 'Selection'),
      el('div.field.mb', el('label', { for: 'sel-input' }, '--select'), selectInput,
        el('span.hint', 'Which models to act on. Leave empty for everything in scope.')),
      el('div.field.mb', el('label', { for: 'exc-input' }, '--exclude'), excludeInput),
      el('label.switch.mb', fullRefresh, el('span', '--full-refresh'),
        el('span.tiny.faint', { style: { marginLeft: '6px' } }, '(rebuilds incrementals from scratch)')),
      el('div.mb', cheatToggle),
      cheatSheet,
      (state.scope?.blocked_layers || []).length
        ? el(
            'div.mt',
            callout(
              `The ${(state.scope.blocked_layers || []).join(', ')} layer is never built from here`,
              state.scope.build_summary ||
                'Every dbt command from this UI adds that exclusion automatically.',
              'info',
            ),
          )
        : null,
    ),
  );

  host.append(
    el(
      'div.split',
      palette,
      el(
        'div',
        el(
          'div.panel',
          el('div.panel-head', el('h3', 'Output'), el('div', jobHost)),
          el('div.panel-body', consoleView.node),
        ),
        el(
          'div.panel.mt',
          el('div.panel-head', el('h3', 'This session')),
          el('div.panel-body', historyHost),
        ),
      ),
    ),
  );

  clear(jobHost).append(el('span.small.faint', 'idle'));
  consoleView.append([
    { text: 'dbt Studio run console', level: 'meta' },
    { text: `project  ${state.boot?.project?.name || ''}`, level: 'plain' },
    { text: `target   ${state.target}`, level: 'plain' },
    { text: '', level: 'plain' },
    { text: 'Pick a command on the left. Output streams here line by line.', level: 'info' },
  ]);

  refreshHistory();

  /* Reattach to a run already in flight, e.g. after switching views. */
  const active = state.activeJob;
  if (active?.is_active) {
    consoleView.clear();
    attach(active);
  } else if (params.autorun) {
    launch(params.autorun);
  }

  return host;
}
