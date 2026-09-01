/* ==========================================================================
   settings.js - dataset access and role.

   Two controls, both of which used to be hardcoded in config.py:

   1. Dataset access. Every dataset the signed-in credentials can see, with a
      checkbox for whether this UI may read, document and write to it. The saved
      list replaces the built-in default outright, so unticking a default
      dataset actually revokes it.

   2. Role. Which actions the UI offers at all.

   Said plainly, because it would be dishonest to imply otherwise: neither of
   these is a security boundary. The server binds to loopback with no
   authentication, so anyone who can open this page can change both. They exist
   to prevent accidents - a reviewer cannot fat-finger a production write, and a
   dataset nobody ticked cannot be queried by mistake. What the credentials can
   *actually* reach is decided by BigQuery IAM, and nothing here widens that.
   ========================================================================== */

import { api, can, clear, el, num, roleLabel, state, toast } from '../core.js';
import { callout, emptyState, loading, modal } from '../components.js';

export const meta = {
  title: 'Settings',
  subtitle: 'Dataset access, users and roles',
};

export function render(navigate) {
  const host = el('div');
  const accessHost = el('div.panel');
  const usersHost = el('div.panel.mt');
  const roleHost = el('div.panel.mt');

  /** dataset name -> checkbox, so Save can read the current ticks. */
  let boxes = new Map();
  let payload = null;

  /* ------------------------------------------------------------- load --- */

  async function load() {
    clear(accessHost).append(loading('Reading the datasets your credentials can see…'));
    clear(usersHost);
    clear(roleHost);

    try {
      payload = await api.accessSettings();
    } catch (error) {
      clear(accessHost).append(
        el('div.panel-body',
          callout('Could not read the access settings', error.message, 'err',
            el('div',
              error.detail ? el('pre.code-block', error.detail) : null,
              el('button.btn.btn-tiny.mt', { onclick: () => load() }, 'Retry')))),
      );
      return;
    }

    paintAccess();
    paintRole();
    /* User management is Manager-only. The backend refuses the call for anyone
       else, so this is about not showing a panel that can only ever error. */
    if (can('can_manage_access')) loadUsers();
  }

  /* ----------------------------------------------------------- access --- */

  function paintAccess() {
    const datasets = payload.datasets || [];
    const scope = payload.scope || {};
    const canManage = can('can_modify_datasets');
    const envLocked = Boolean(scope.env_locked);

    boxes = new Map();

    const rows = el('tbody');
    for (const entry of datasets) {
      const box = el('input', {
        type: 'checkbox',
        'aria-label': `Allow access to ${entry.dataset}`,
      });
      box.checked = Boolean(entry.allowed);
      box.disabled = !canManage || envLocked;
      box.addEventListener('change', paintCount);
      boxes.set(entry.dataset, box);

      rows.append(
        el(entry.allowed ? 'tr.is-picked' : 'tr',
          el('td.pick-cell', box),
          el('td', el('span.mono.small', entry.dataset)),
          el('td.small.faint', entry.location || '—'),
          el('td',
            entry.allowed
              ? el('span.chip.ok', 'allowed')
              : el('span.chip.faint', 'not allowed')),
        ),
      );
    }

    const countChip = el('span.chip');
    function paintCount() {
      const on = [...boxes.values()].filter((b) => b.checked).length;
      countChip.textContent = `${on} of ${boxes.size} allowed`;
      /* Keep the row highlight in step with the tick so the table reads as the
         pending state, not the saved one. */
      for (const [name, box] of boxes) {
        const row = box.closest('tr');
        if (row) row.classList.toggle('is-picked', box.checked);
      }
    }

    const saveBtn = el('button.btn.btn-primary', {
      disabled: !canManage || envLocked,
      onclick: () => save(),
    }, 'Save dataset access');

    async function save() {
      const chosen = [...boxes.entries()]
        .filter(([, box]) => box.checked)
        .map(([name]) => name);

      if (!chosen.length) {
        const ok = window.confirm(
          'No datasets are ticked.\n\n'
          + 'With an empty list the UI falls back to its built-in default '
          + '(bronze and silver). To genuinely restrict it, tick just the '
          + 'datasets you want.\n\nSave anyway?',
        );
        if (!ok) return;
      }

      saveBtn.disabled = true;
      try {
        const result = await api.saveAccessSettings({ datasets: chosen });
        toast('Dataset access saved', { kind: 'ok', detail: result.note });
        /* The boundary changed, so every cached list in the app is stale.
           Reload the project rather than leaving screens on the old scope. */
        await load();
      } catch (error) {
        toast('Could not save', { kind: 'err', detail: error.message });
      } finally {
        saveBtn.disabled = false;
      }
    }

    clear(accessHost).append(
      el('div.panel-head',
        el('h3', 'BigQuery dataset access'),
        el('span.small.faint', `source: ${scope.source || 'default'}`)),
      el('div.panel-body',
        el('p.small.faint', { style: { marginTop: 0, lineHeight: '1.6' } },
          'Tick a dataset to let this UI read, profile, document and write to '
          + 'it. Untick to take that away. The list below is what your BigQuery '
          + 'credentials can already see, so ticking a box never grants you '
          + 'access you did not already have in IAM - it only decides what this '
          + 'app is willing to touch.'),

        payload.error
          ? callout('Could not list datasets from BigQuery', payload.error, 'warn',
              el('p.tiny.faint',
                'The saved settings below are still in force. Fix the connection '
                + 'to see the full list.'))
          : null,

        envLocked
          ? callout(
              'Managed by an environment variable',
              'DBT_UI_ALLOWED_DATASETS is set, which takes precedence over this '
              + 'screen. Unset it and restart to manage access here.',
              'info')
          : null,

        !canManage
          ? callout(
              `The ${roleLabel()} role cannot change dataset access`,
              'Only a Manager can change which datasets this UI may use. You can '
              + 'see the current boundary below but not edit it.',
              'info')
          : null,

        (payload.missing || []).length
          ? callout(
              `${payload.missing.length} allowed dataset${payload.missing.length === 1 ? '' : 's'} no longer visible`,
              payload.missing.join(', '),
              'warn',
              el('p.tiny.faint',
                'These are in the allowed list but your credentials cannot see '
                + 'them. They may have been dropped, renamed, or the grant was '
                + 'removed.'))
          : null,

        el('div.row.wrap.between.mb.mt', { style: { gap: '10px' } },
          el('div.row.wrap', { style: { gap: '6px' } }, countChip),
          el('div.row', { style: { gap: '6px' } },
            el('button.btn.btn-tiny', {
              disabled: !canManage || envLocked,
              onclick: () => { for (const b of boxes.values()) b.checked = true; paintCount(); },
            }, 'Select all'),
            el('button.btn.btn-tiny.btn-ghost', {
              disabled: !canManage || envLocked,
              onclick: () => { for (const b of boxes.values()) b.checked = false; paintCount(); },
            }, 'Clear all'),
            el('button.btn.btn-tiny.btn-ghost', { onclick: () => load() }, '⟳ Refresh'))),

        datasets.length
          ? el('div.table-wrap.pick-table', { style: { maxHeight: '46vh' } },
              el('table.data.compact',
                el('thead', el('tr',
                  el('th', { style: { width: '1%' } }, ''),
                  el('th', 'Dataset'),
                  el('th', 'Region'),
                  el('th', 'Status'))),
                rows))
          : emptyState('No datasets visible',
              'Either the connection failed or these credentials cannot see any '
              + 'dataset in the project.'),

        el('div.mt', saveBtn),

        el('p.tiny.faint', { style: { marginBottom: 0, lineHeight: '1.6' } },
          'Saved to dbt_ui/.runtime/access.json. A dbt run is a separate '
          + 'subprocess and issues its own SQL, so this list governs what the UI '
          + 'queries directly; the layers dbt may build are controlled '
          + 'separately by the blocked-layer rule.'),
      ),
    );

    paintCount();
  }

  /* ------------------------------------------------- users and roles --- */

  let usersPayload = null;

  async function loadUsers() {
    clear(usersHost).append(loading('Reading the user list…'));
    try {
      usersPayload = await api.users();
    } catch (error) {
      clear(usersHost).append(
        el('div.panel-head', el('h3', 'Users & roles')),
        el('div.panel-body',
          callout('Could not read the user list', error.message,
                  error.status === 403 ? 'info' : 'err')),
      );
      return;
    }
    paintUsers();
  }

  function paintUsers() {
    const users = usersPayload.users || [];
    const me = state.user || {};
    const roleKeys = (usersPayload.roles?.roles || []).map((r) => r.key);
    const datasetNames = (payload.datasets || []).map((d) => d.dataset);

    const rows = el('tbody');
    for (const user of users) {
      const isMe = user.id === me.id;

      /* Role selector. Changing it writes to the database immediately - there is
         no separate save step, because a half-applied permission change is worse
         than an immediate one. */
      const roleSelect = el('select.select.input-tiny',
        ...roleKeys.map((key) => el('option',
          { value: key, selected: key === user.role },
          (usersPayload.roles.roles.find((r) => r.key === key) || {}).label || key)));

      roleSelect.addEventListener('change', async () => {
        const next = roleSelect.value;
        const previous = user.role;
        if (next === previous) return;

        const ok = window.confirm(
          `Change ${user.email} from ${previous} to ${next}?\n\n`
          + 'This is saved immediately and applies to their next request, '
          + 'including any session they already have open.',
        );
        if (!ok) { roleSelect.value = previous; return; }

        roleSelect.disabled = true;
        try {
          const result = await api.setUserRole(user.id, next);
          user.role = result.user.role;
          toast(`${user.email} is now ${result.user.role}`, { kind: 'ok' });
          /* If it was our own role, the whole UI's permissions changed. */
          if (isMe) document.location.reload();
          else loadUsers();
        } catch (error) {
          roleSelect.value = previous;
          toast('Could not change the role', { kind: 'err', detail: error.message });
        } finally {
          roleSelect.disabled = false;
        }
      });

      /* Per-user dataset grants. Empty means "no per-user restriction", i.e. the
         project allowlist applies. A grant can only narrow it. */
      const grantCount = (user.datasets || []).length;
      const grantBtn = el('button.btn.btn-tiny.btn-ghost', {
        title: grantCount
          ? `Restricted to ${grantCount} dataset(s)`
          : 'No per-user restriction; the project allowlist applies',
        onclick: () => openGrants(user, datasetNames),
      }, grantCount ? `${grantCount} dataset${grantCount === 1 ? '' : 's'}` : 'all allowed');

      rows.append(
        el(user.is_active ? 'tr' : 'tr.is-failed',
          el('td',
            el('span.mono.small', user.email),
            isMe ? el('span.chip.tiny', { style: { marginLeft: '6px' } }, 'you') : null),
          el('td', roleSelect),
          el('td', grantBtn),
          el('td',
            user.is_active
              ? el('span.chip.ok', 'active')
              : el('span.chip.err', 'disabled')),
          el('td',
            el('div.row', { style: { gap: '5px' } },
              el('button.btn.btn-tiny.btn-ghost', {
                disabled: isMe,
                title: isMe ? 'You cannot disable your own account' : '',
                onclick: () => toggleActive(user),
              }, user.is_active ? 'Disable' : 'Enable'),
              el('button.btn.btn-tiny.btn-ghost', {
                onclick: () => resetPassword(user),
              }, 'Reset password'))),
        ),
      );
    }

    async function toggleActive(user) {
      try {
        await api.setUserActive(user.id, !user.is_active);
        toast(`${user.email} ${user.is_active ? 'disabled' : 'enabled'}`, { kind: 'ok' });
        loadUsers();
      } catch (error) {
        toast('Could not update the account', { kind: 'err', detail: error.message });
      }
    }

    async function resetPassword(user) {
      const next = window.prompt(
        `New password for ${user.email}\n\nMinimum 8 characters.`);
      if (next === null) return;
      try {
        await api.setUserPassword(user.id, next);
        toast(`Password reset for ${user.email}`, { kind: 'ok' });
      } catch (error) {
        toast('Could not reset the password', { kind: 'err', detail: error.message });
      }
    }

    /* ---- add a user ---- */
    const newEmail = el('input.input.input-tiny',
      { type: 'email', placeholder: 'email', style: { width: '22ch' } });
    const newPassword = el('input.input.input-tiny',
      { type: 'password', placeholder: 'password (min 8)', style: { width: '18ch' } });
    const newRole = el('select.select.input-tiny',
      ...roleKeys.map((key) => el('option', { value: key },
        (usersPayload.roles.roles.find((r) => r.key === key) || {}).label || key)));
    newRole.value = 'analyst';

    const addBtn = el('button.btn.btn-tiny.btn-primary', {
      onclick: async () => {
        addBtn.disabled = true;
        try {
          await api.createUser(newEmail.value.trim(), newPassword.value, newRole.value);
          toast(`Added ${newEmail.value.trim()}`, { kind: 'ok' });
          newEmail.value = '';
          newPassword.value = '';
          loadUsers();
        } catch (error) {
          toast('Could not add the user', { kind: 'err', detail: error.message });
        } finally {
          addBtn.disabled = false;
        }
      },
    }, '＋ Add user');

    clear(usersHost).append(
      el('div.panel-head',
        el('h3', 'Users & roles'),
        el('span.small.faint',
           `${users.length} user${users.length === 1 ? '' : 's'} · `
           + `${usersPayload.stats?.active_sessions ?? 0} active session(s)`)),
      el('div.panel-body',
        el('p.small.faint', { style: { marginTop: 0, lineHeight: '1.6' } },
          'Only a Manager can see or change this. Roles are stored in the '
          + 'project\'s own user database and are re-read on every request, so a '
          + 'change here takes effect on that person\'s next action even if they '
          + 'are already signed in.'),

        el('div.table-wrap', { style: { maxHeight: '44vh' } },
          el('table.data.compact',
            el('thead', el('tr',
              el('th', 'User'),
              el('th', 'Role'),
              el('th', 'Dataset access'),
              el('th', 'Status'),
              el('th', ''))),
            rows)),

        el('div.mt',
          el('div.stat-label.mb', 'Add a user'),
          el('div.row.wrap', { style: { gap: '6px', alignItems: 'center' } },
            newEmail, newPassword, newRole, addBtn)),
      ),
    );
  }

  /** Per-user dataset grants, in a small inline editor. */
  function openGrants(user, datasetNames) {
    const current = new Set((user.datasets || []).map((d) => d.toLowerCase()));
    const boxes = new Map();

    const list = el('div.ct-fields');
    for (const name of datasetNames) {
      const box = el('input', { type: 'checkbox' });
      box.checked = current.has(name.toLowerCase());
      boxes.set(name, box);
      list.append(el('label.ct-radio', box, el('span.mono.small', name)));
    }

    const dialog = modal({
      title: `Dataset access for ${user.email}`,
      width: '520px',
      body: el('div',
        callout(
          'Leave everything unticked for no restriction',
          'With nothing ticked this user gets the project-wide allowlist. Ticking '
          + 'datasets restricts them to just those. A grant can only ever narrow '
          + 'the project boundary, never widen it.',
          'info'),
        el('div.mt', list),
        el('div.row.mt', { style: { gap: '7px' } },
          el('button.btn.btn-primary', {
            onclick: async () => {
              const chosen = [...boxes.entries()]
                .filter(([, b]) => b.checked)
                .map(([name]) => name);
              try {
                await api.setUserDatasets(user.id, chosen);
                toast(`Dataset access updated for ${user.email}`, { kind: 'ok' });
                dialog.close();
                loadUsers();
              } catch (error) {
                toast('Could not save', { kind: 'err', detail: error.message });
              }
            },
          }, 'Save'),
          el('button.btn.btn-ghost', { onclick: () => dialog.close() }, 'Cancel')),
      ),
    });
  }

  /* -------------------------------------------------- role reference --- */

  function paintRole() {
    const catalogue = state.roles || usersPayload?.roles || { roles: [], permissions: [] };
    const perms = catalogue.permissions || [];
    const roles = catalogue.roles || [];
    const pinned = new Set(catalogue.pinned || []);
    const active = state.user?.role;
    /* Manager (can_modify_roles) may edit the matrix by clicking cells. Everyone
       else sees it read-only. The server enforces this regardless. */
    const editable = can('can_modify_roles');

    const head = el('tr',
      el('th', 'Permission'),
      ...roles.map((r) => el('th.num',
        r.key === active ? el('span.chip.info', r.label) : r.label)));

    /* One cell. When editable and not pinned it is a button that flips the
       value and saves; otherwise a static ✓/— . */
    function cell(role, permission) {
      const on = Boolean(role[permission.key]);
      const isPinned = pinned.has(permission.key);

      if (!editable || isPinned) {
        return el('td.num',
          on ? el('span', { style: { color: 'var(--ok)' } }, '✓')
            : el('span.faint', '—'),
          isPinned && editable
            ? el('span.tiny.faint', { style: { marginLeft: '4px' }, title: 'Always on' }, '🔒')
            : null);
      }

      const btn = el('button.perm-toggle', {
        type: 'button',
        class: `perm-toggle${on ? ' is-on' : ' is-off'}`,
        title: `${on ? 'Allowed' : 'Denied'} - click to ${on ? 'deny' : 'allow'} ${permission.label} for ${role.label}`,
        'aria-pressed': on ? 'true' : 'false',
        onclick: async () => {
          btn.disabled = true;
          try {
            const result = await api.setRolePermission(role.key, permission.key, !on);
            /* The response carries the fresh matrix - adopt it as the source of
               truth and repaint so every cell reflects the saved state. */
            state.roles = result.roles;
            /* If we just changed our own role, our live permissions moved too. */
            if (role.key === state.user?.role) {
              const mine = result.roles.roles.find((x) => x.key === role.key);
              if (mine) state.permissions = mine;
            }
            toast(`${role.label}: ${permission.label} ${!on ? 'allowed' : 'denied'}`, { kind: 'ok' });
            paintRole();
            /* A change to our own rights can hide/show whole sections. */
            if (role.key === state.user?.role) load();
          } catch (error) {
            btn.disabled = false;
            toast('Could not change the permission', { kind: 'err', detail: error.message });
          }
        },
      }, on ? '✓' : '—');
      return el('td.num', btn);
    }

    const body = el('tbody');
    for (const permission of perms) {
      body.append(el('tr',
        el('td.small', permission.label),
        ...roles.map((r) => cell(r, permission))));
    }

    clear(roleHost).append(
      el('div.panel-head',
        el('h3', 'Permission matrix'),
        el('span.small.faint',
          editable ? 'click a cell to allow / deny' : `you are signed in as ${roleLabel()}`)),
      el('div.panel-body',
        el('p.small.faint', { style: { marginTop: 0, lineHeight: '1.6' } },
          editable
            ? 'Click any cell to toggle that permission for the role. Changes '
              + 'save immediately and apply to everyone with that role on their '
              + 'next request. Login is always on and cannot be turned off.'
            : (catalogue.note || '')),
        el('div.table-wrap',
          el('table.data.compact', el('thead', head), body)),
        el('div.mt',
          ...roles.map((r) => el('p.tiny.faint',
            { style: { margin: '0 0 5px', lineHeight: '1.55' } },
            el('strong', `${r.label}: `), r.blurb))),
        el('div.mt',
          callout(
            'Enforced on the server, not just here',
            'Every permission above is checked again in the API before the action '
            + 'runs, so a hidden button is a convenience rather than the control. '
            + 'Calling the endpoint directly returns 403.',
            'ok',
            el('p.tiny.faint', { style: { marginBottom: 0, lineHeight: '1.6' } },
              'One boundary this does not move: BigQuery IAM. These roles decide '
              + 'what dbt Studio will do on your behalf, not what the signed-in '
              + 'Google credentials are allowed to reach.'))),
      ),
    );
  }

  /* ------------------------------------------------------- own password --- */

  function paintOwnPassword() {
    const currentPw = el('input.input.input-tiny',
      { type: 'password', placeholder: 'current password', style: { width: '20ch' } });
    const nextPw = el('input.input.input-tiny',
      { type: 'password', placeholder: 'new password (min 8)', style: { width: '20ch' } });
    const btn = el('button.btn.btn-tiny', {
      onclick: async () => {
        btn.disabled = true;
        try {
          await api.changeOwnPassword(currentPw.value, nextPw.value);
          currentPw.value = '';
          nextPw.value = '';
          toast('Password updated', { kind: 'ok' });
        } catch (error) {
          toast('Could not change the password', { kind: 'err', detail: error.message });
        } finally {
          btn.disabled = false;
        }
      },
    }, 'Change password');

    return el('div.panel.mt',
      el('div.panel-head', el('h3', 'Your account'),
         el('span.small.faint', state.user?.email || '')),
      el('div.panel-body',
        el('div.row.wrap', { style: { gap: '6px', alignItems: 'center' } },
          currentPw, nextPw, btn)));
  }

  /* --------------------------------------------------------- assemble --- */

  host.append(accessHost, usersHost, roleHost, paintOwnPassword());
  load();
  return host;
}
