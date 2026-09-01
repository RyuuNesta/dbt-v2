/* ==========================================================================
   core.js - API client, DOM helpers, shared state, formatting, toasts.
   No dependencies. Loaded as an ES module.
   ========================================================================== */

/* ---------------------------------------------------------------- state --- */

export const state = {
  boot: null,
  target: null,
  stats: null,
  models: [],
  refs: [],
  sources: [],
  lastRun: null,
  activeJob: null,
  docsAvailable: false,
  /* The signed-in user and their permission set, both from the server. Held
     here only as a cache for shaping the UI - the backend re-checks every
     request against the database, so editing these in a console gains nothing
     beyond a briefly misleading screen. */
  user: null,
  permissions: {},
  roles: null,
  /* per-view scratch space so switching tabs does not lose work */
  scratch: {
    workbenchSql: null,
    schemaModel: null,
    advisorModel: null,
    openJobId: null,
  },
};

/* ------------------------------------------------------------------ api --- */

export class ApiError extends Error {
  constructor(payload, status) {
    super(payload?.error || `Request failed (${status})`);
    this.status = status;
    this.payload = payload || {};
    this.detail = payload?.detail || '';
    this.needsParse = Boolean(payload?.needs_parse);
    this.busy = Boolean(payload?.busy);
  }
}

async function request(method, path, { body, query } = {}) {
  let url = path;
  if (query) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== '') {
        params.set(key, String(value));
      }
    }
    const qs = params.toString();
    if (qs) url += `?${qs}`;
  }

  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(url, init);
  } catch (cause) {
    throw new ApiError(
      {
        error: 'Cannot reach the dbt Studio server.',
        detail: `${cause}\n\nThe server process may have stopped. Restart it with:\n  python dbt_ui/serve.py`,
      },
      0,
    );
  }

  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { error: 'Server returned a non-JSON response.', detail: text.slice(0, 800) };
    }
  }

  if (!response.ok) throw new ApiError(payload, response.status);
  return payload;
}

/** Attach the currently selected target to every request that accepts one. */
function withTarget(body = {}) {
  return state.target ? { target: state.target, ...body } : body;
}

export const api = {
  bootstrap: () => request('GET', '/api/bootstrap'),
  connection: () => request('GET', '/api/connection', { query: { target: state.target } }),
  stats: () => request('GET', '/api/stats'),

  models: (query) => request('GET', '/api/models', { query }),
  model: (name) => request('GET', `/api/models/${encodeURIComponent(name)}`),
  sources: () => request('GET', '/api/sources'),
  graph: () => request('GET', '/api/graph'),
  refs: () => request('GET', '/api/refs'),

  autocompleteCatalog: () =>
    request('GET', '/api/autocomplete/catalog', { query: { target: state.target } }),
  autocompleteColumns: (model) =>
    request('GET', '/api/autocomplete/columns', { query: { model } }),
  autocompleteSchema: (dataset, refresh) =>
    request('GET', '/api/autocomplete/schema', {
      query: { dataset, target: state.target, refresh: refresh ? '1' : '' },
    }),

  /* Entity relationship model, derived from the manifest. Manifest-only by
     default - counts and BigQuery-declared constraints are both opt-in because
     each costs a query. */
  erd: (options = {}) =>
    request('GET', '/api/erd', {
      query: {
        target: state.target,
        in_scope_only: options.inScopeOnly ? '1' : '',
        include_staging: options.includeStaging === false ? '0' : '',
        include_sources: options.includeSources === false ? '0' : '',
        tables: (options.tables || []).join(','),
        datasets: (options.datasets || []).join(','),
        counts: options.counts ? '1' : '',
        constraints: options.constraints ? '1' : '',
      },
    }),
  erdExport: (format, options = {}) =>
    request('GET', '/api/erd/export', {
      query: {
        format,
        target: state.target,
        in_scope_only: options.inScopeOnly ? '1' : '',
        include_staging: options.includeStaging === false ? '0' : '',
        include_sources: options.includeSources === false ? '0' : '',
        tables: (options.tables || []).join(','),
        datasets: (options.datasets || []).join(','),
        keys_only: options.keysOnly ? '1' : '',
      },
    }),

  compile: (sql) => request('POST', '/api/query/compile', { body: withTarget({ sql }) }),
  validate: (sql) => request('POST', '/api/query/validate', { body: withTarget({ sql }) }),
  run: (sql, limit) => request('POST', '/api/query/run', { body: withTarget({ sql, limit }) }),

  generateSchema: (body) => request('POST', '/api/schema/generate', { body: withTarget(body) }),

  /* Re-render the proposal YAML from columns already generated, using the
     descriptions the user edited in place. No warehouse call. No target: the
     column shapes and profiles are already in hand from generateSchema. */
  rebuildSchema: (body) => request('POST', '/api/schema/rebuild', { body }),

  /* Preview the model file a workbench query would become. Writes nothing;
     committing it goes through writeFile so the .bak behaviour is shared. */
  scaffoldModel: (body) => request('POST', '/api/models/scaffold', { body }),

  /* dbt's own documentation: status of the generated static site, and the
     committed catalog for one model straight from the manifest. */
  docsSite: () => request('GET', '/api/docs/site'),
  modelDetail: (name) => request('GET', `/api/models/${encodeURIComponent(name)}`),

  /* Declare a foreign table (one dbt does not build) as a dbt source, so dbt
     documents it. Reads the table's schema from BigQuery and drafts a sources:
     block; rebuild re-renders it from edited descriptions with no warehouse call. */
  generateSource: (body) => request('POST', '/api/schema/source', { body: withTarget(body) }),
  rebuildSource: (body) => request('POST', '/api/schema/source/rebuild', { body }),

  /* Committed documentation: read, patch in place, export. These deliberately
     do not carry the target - a schema YAML is one file shared by all targets. */
  editableDocs: (model) => request('GET', '/api/docs/editable', { query: { model } }),
  patchDocs: (body) => request('POST', '/api/docs/patch', { body }),
  exportDocs: (model) => request('GET', '/api/docs/export', { query: { model } }),

  aiStatus: () => request('GET', '/api/ai/status'),
  saveAiKey: (apiKey) => request('POST', '/api/ai/key', { body: { action: 'save', api_key: apiKey } }),
  clearAiKey: () => request('POST', '/api/ai/key', { body: { action: 'clear' } }),
  profile: (body) => request('POST', '/api/profile', { body: withTarget(body) }),
  analyse: (body) => request('POST', '/api/advisor/analyse', { body: withTarget(body) }),
  /* Explains the transformation before it is generated. Writes nothing and
     compiles nothing - the estimate comes from the profile already taken. */
  previewSilver: (body) => request('POST', '/api/advisor/preview', { body: withTarget(body) }),
  generateSilver: (body) => request('POST', '/api/advisor/generate', { body: withTarget(body) }),

  /* Authentication. The session token lives in an HttpOnly cookie, so there is
     nothing to attach here - the browser sends it automatically. */
  login: (email, password) =>
    request('POST', '/api/auth/login', { body: { email, password } }),
  logout: () => request('POST', '/api/auth/logout', { body: {} }),
  session: () => request('GET', '/api/auth/session'),
  roleCatalogue: () => request('GET', '/api/auth/roles'),
  /* Flip one cell of the permission matrix. Manager only; the backend enforces
     that and rejects pinned/lockout-causing changes. */
  setRolePermission: (role, permission, value) =>
    request('POST', '/api/roles/permission', { body: { role, permission, value } }),
  changeOwnPassword: (currentPassword, newPassword) =>
    request('POST', '/api/auth/password', {
      body: { current_password: currentPassword, new_password: newPassword },
    }),

  /* User management. Manager only - the backend enforces that independently of
     whether the UI chose to show the screen. */
  users: () => request('GET', '/api/users'),
  createUser: (email, password, role) =>
    request('POST', '/api/users/create', { body: { email, password, role } }),
  setUserRole: (userId, role) =>
    request('POST', '/api/users/role', { body: { user_id: userId, role } }),
  setUserActive: (userId, isActive) =>
    request('POST', '/api/users/active', { body: { user_id: userId, is_active: isActive } }),
  setUserDatasets: (userId, datasets) =>
    request('POST', '/api/users/datasets', { body: { user_id: userId, datasets } }),
  setUserPassword: (userId, password) =>
    request('POST', '/api/users/password', { body: { user_id: userId, password } }),
  deleteUser: (userId) =>
    request('POST', '/api/users/delete', { body: { user_id: userId } }),

  /* Access settings: every dataset the credentials can see, plus which ones the
     UI is allowed to use, and the active role. */
  accessSettings: () =>
    request('GET', '/api/access/settings', { query: { target: state.target } }),
  saveAccessSettings: (body) =>
    request('POST', '/api/access/settings', { body, query: { target: state.target } }),

  datasets: () => request('GET', '/api/warehouse/datasets', { query: { target: state.target } }),
  /* Row counts and sizes for every in-scope table. Free metadata, cached
     server-side; pass refresh to force a re-read. */
  inventory: (refresh) =>
    request('GET', '/api/warehouse/inventory', {
      query: { target: state.target, refresh: refresh ? '1' : '' },
    }),
  tables: (dataset) =>
    request('GET', '/api/warehouse/tables', { query: { dataset, target: state.target } }),
  describe: (body) => request('POST', '/api/warehouse/describe', { body: withTarget(body) }),
  preview: (body) => request('POST', '/api/warehouse/preview', { body: withTarget(body) }),

  /* Scheduled runs. Saving and registering are separate calls on purpose:
     registering is what lets dbt run unattended. */
  schedules: () => request('GET', '/api/schedules', { query: { target: state.target } }),
  saveSchedule: (body) => request('POST', '/api/schedules', { body }),
  deleteSchedule: (id) => request('POST', '/api/schedules/delete', { body: { id } }),
  registerSchedule: (id, action) =>
    request('POST', '/api/schedules/register', { body: { id, action } }),
  scheduleRuns: (id) => request('GET', '/api/schedules/runs', { query: { id } }),
  scheduleLog: (log) => request('GET', '/api/schedules/log', { query: { log } }),

  dbtRun: (body) => request('POST', '/api/dbt/run', { body: withTarget(body) }),
  jobs: () => request('GET', '/api/dbt/jobs'),
  job: (id, cursor) => request('GET', `/api/dbt/jobs/${id}`, { query: { cursor } }),
  cancelJob: (id) => request('POST', `/api/dbt/jobs/${id}/cancel`, { body: {} }),
  refreshManifest: () => request('POST', '/api/manifest/refresh', { body: withTarget() }),

  writeFile: (path, content, mode = 'overwrite') =>
    request('POST', '/api/files/write', { body: { path, content, mode } }),
  readFile: (path) => request('POST', '/api/files/read', { body: { path } }),
};

/* ------------------------------------------------------------------ dom --- */

/**
 * Terse element factory.
 *   el('div.card', { onclick: fn }, 'text', childEl)
 * Tag supports `tag.class1.class2#id` shorthand.
 */
export function el(spec, props = null, ...children) {
  const [head, ...classes] = String(spec).split('.');
  const [tag, id] = head.split('#');
  const node = document.createElement(tag || 'div');
  if (id) node.id = id;
  if (classes.length) node.className = classes.join(' ');

  if (props && typeof props === 'object' && !isRenderable(props)) {
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = [node.className, value].filter(Boolean).join(' ');
      else if (key === 'html') node.innerHTML = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, String(value));
    }
  } else if (props !== null && props !== undefined) {
    children.unshift(props);
  }

  append(node, children);
  return node;
}

function isRenderable(value) {
  return value instanceof Node || Array.isArray(value) || typeof value !== 'object';
}

function append(parent, children) {
  for (const child of children.flat(4)) {
    if (child === null || child === undefined || child === false || child === '') continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/* ------------------------------------------------------------ permissions --- */

/**
 * Does the signed-in user hold this permission?
 *
 * Used to shape the UI: hide what cannot be done, and disable-with-a-reason
 * where hiding would be confusing. This is a courtesy, not a control - every
 * one of these is independently enforced server-side, so a user who flips a
 * flag in the console just gets a 403 from the API instead of a hidden button.
 */
export function can(permission) {
  return Boolean(state.permissions?.[permission]);
}

/** The active role's display label, e.g. 'Manager'. */
export function roleLabel() {
  return state.permissions?.label || state.user?.role || 'unknown';
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* ----------------------------------------------------------- formatting --- */

export function num(value) {
  if (value === null || value === undefined || value === '') return '-';
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toLocaleString('en-US') : String(value);
}

export function bytes(value) {
  const size = Number(value || 0);
  if (!size) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  const index = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1);
  const scaled = size / 1024 ** index;
  return `${scaled >= 100 || index === 0 ? Math.round(scaled) : scaled.toFixed(1)} ${units[index]}`;
}

export function ms(value) {
  const total = Number(value || 0);
  if (total < 1000) return `${Math.round(total)} ms`;
  if (total < 60000) return `${(total / 1000).toFixed(2)} s`;
  return `${Math.floor(total / 60000)}m ${Math.round((total % 60000) / 1000)}s`;
}

export function secs(value) {
  const total = Number(value || 0);
  if (total < 60) return `${total.toFixed(total < 10 ? 2 : 1)}s`;
  return `${Math.floor(total / 60)}m ${Math.round(total % 60)}s`;
}

export function ago(input) {
  if (!input) return 'never';
  const then = typeof input === 'number' ? input * 1000 : Date.parse(input);
  if (!Number.isFinite(then)) return String(input);
  const diff = Math.max(0, Date.now() - then) / 1000;
  if (diff < 45) return 'just now';
  if (diff < 5400) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400 * 2) return `${Math.round(diff / 3600)} h ago`;
  return new Date(then).toLocaleString();
}

export function pct(value) {
  if (value === null || value === undefined) return '-';
  return `${Number(value).toFixed(Number(value) % 1 === 0 ? 0 : 1)}%`;
}

/** Strip BigQuery backticks for display: `p`.`d`.`t` -> p.d.t */
export function plainRelation(relation) {
  return String(relation || '').replace(/`/g, '');
}

/** Just the dataset.table part, which is what people actually scan for. */
export function shortRelation(relation) {
  const parts = plainRelation(relation).split('.');
  return parts.length >= 2 ? parts.slice(-2).join('.') : parts.join('.');
}

/* --------------------------------------------------------------- toasts --- */

const ICONS = { ok: '✓', err: '✕', warn: '!', info: 'i' };

export function toast(message, { kind = 'info', detail = '', timeout } = {}) {
  const host = $('#toasts');
  if (!host) return () => {};

  /* An error interrupts; anything else is announced politely when the reader
     next pauses. Using role=alert for everything would make the interface
     shout over the user constantly. */
  const node = el(
    `div.toast.${kind}`,
    {
      role: kind === 'err' ? 'alert' : 'status',
      'aria-live': kind === 'err' ? 'assertive' : 'polite',
    },
    el('span.toast-ico', { 'aria-hidden': 'true' }, ICONS[kind] || 'i'),
    el(
      'div.toast-body',
      el('strong', message),
      detail ? el('div.detail', String(detail).slice(0, 1200)) : null,
    ),
    el('button.toast-close', { onclick: () => node.remove(), 'aria-label': 'Dismiss notification' }, '✕'),
  );

  host.append(node);

  const life = timeout ?? (kind === 'err' ? 14000 : 5000);
  const timer = setTimeout(() => node.remove(), life);
  return () => {
    clearTimeout(timer);
    node.remove();
  };
}

/** Standard error presentation, so every view reports failures the same way. */
export function reportError(error, context = '') {
  const message = error instanceof ApiError ? error.message : String(error?.message || error);
  const detail = error instanceof ApiError ? error.detail : error?.stack || '';
  toast(context ? `${context}: ${message}` : message, { kind: 'err', detail });
  return { message, detail };
}

/* ------------------------------------------------------------- clipboard --- */

export async function copy(text, label = 'Copied') {
  try {
    await navigator.clipboard.writeText(text);
    toast(label, { kind: 'ok', timeout: 2200 });
    return true;
  } catch {
    /* clipboard API needs a secure context; fall back to a hidden textarea */
    const area = el('textarea', {
      style: { position: 'fixed', top: '-1000px', opacity: '0' },
    });
    area.value = text;
    document.body.append(area);
    area.select();
    const ok = document.execCommand?.('copy');
    area.remove();
    toast(ok ? label : 'Could not copy. Select the text manually.', {
      kind: ok ? 'ok' : 'warn',
      timeout: 2600,
    });
    return Boolean(ok);
  }
}

export function download(filename, text, mime = 'text/plain') {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const link = el('a', { href: url, download: filename });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

/* ---------------------------------------------------------------- misc --- */

export function debounce(fn, wait = 220) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

export function layerRank(layer) {
  return { source: -1, seed: 0, bronze: 1, silver: 2, gold: 3 }[layer] ?? 9;
}

export function layerLabel(layer) {
  return { seed: 'Seed', bronze: 'Bronze', silver: 'Silver', gold: 'Gold', source: 'Source' }[
    layer
  ] || 'Other';
}

/** True when a job is finished, whatever the outcome. */
export function jobDone(job) {
  return Boolean(job) && !job.is_active;
}

export const CSV_ESCAPE = /[",\n\r]/;

export function toCsv(columns, rows) {
  const head = columns.map((c) => c.name);
  const escape = (value) => {
    if (value === null || value === undefined) return '';
    const text = String(value);
    return CSV_ESCAPE.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  return [head.map(escape).join(','), ...rows.map((row) => row.map(escape).join(','))].join('\r\n');
}
