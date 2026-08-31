/* ==========================================================================
   prefs.js - appearance and help-mode preferences.

   Both persist to localStorage so a choice survives a reload, and both are
   applied to <html> as data attributes so CSS does all the work with no
   re-render.
   ========================================================================== */

import { $, el, toast } from './core.js';

const THEME_KEY = 'dbtstudio.theme';
const HELP_KEY = 'dbtstudio.help';

/** auto follows the operating system; the other two are explicit. */
const THEMES = [
  { id: 'auto', label: 'Match system', icon: '◐' },
  { id: 'dark', label: 'Dark', icon: '●' },
  { id: 'light', label: 'Light', icon: '○' },
];

/* ------------------------------------------------------------------ theme --- */

export function currentTheme() {
  const stored = localStorage.getItem(THEME_KEY);
  return THEMES.some((t) => t.id === stored) ? stored : 'auto';
}

export function applyTheme(id) {
  const theme = THEMES.some((t) => t.id === id) ? id : 'auto';
  document.documentElement.dataset.theme = theme;
  localStorage.setItem(THEME_KEY, theme);

  const icon = $('#theme-icon');
  if (icon) icon.textContent = THEMES.find((t) => t.id === theme).icon;

  const button = $('#btn-theme');
  if (button) {
    const label = THEMES.find((t) => t.id === theme).label;
    button.title = `Appearance: ${label}. Click to change.`;
    button.setAttribute('aria-label', `Appearance: ${label}. Click to change.`);
  }
  return theme;
}

/** Cycle auto -> dark -> light -> auto. One control, no menu to open. */
export function cycleTheme() {
  const index = THEMES.findIndex((t) => t.id === currentTheme());
  const next = THEMES[(index + 1) % THEMES.length];
  applyTheme(next.id);
  toast(`Appearance: ${next.label}`, { kind: 'ok', timeout: 1800 });
  return next.id;
}

/* ------------------------------------------------------------- help mode --- */

export function helpEnabled() {
  return localStorage.getItem(HELP_KEY) === 'on';
}

export function applyHelp(on) {
  document.documentElement.dataset.help = on ? 'on' : 'off';
  localStorage.setItem(HELP_KEY, on ? 'on' : 'off');

  const button = $('#btn-help');
  if (button) {
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
    button.title = on
      ? 'Plain-language explanations are on. Click to hide them.'
      : 'Show plain-language explanations.';
  }

  paintNavHelp();
  return on;
}

export function toggleHelp() {
  const next = !helpEnabled();
  applyHelp(next);
  toast(
    next
      ? 'Plain-language explanations are on.'
      : 'Plain-language explanations are off.',
    { kind: 'ok', timeout: 2200 },
  );
  return next;
}

/** Render the sidebar hints once; CSS decides whether they are visible. */
function paintNavHelp() {
  for (const item of document.querySelectorAll('.nav-item[data-help]')) {
    if (item.querySelector('.nav-help')) continue;
    item.append(el('span.nav-help', item.dataset.help));
  }
}

/* ---------------------------------------------------------------- helper --- */

/**
 * A short explanation in ordinary words, shown only in help mode.
 *
 * Use this rather than a `title` attribute for anything a newcomer needs, since
 * a tooltip is invisible on touch and to a screen reader that is not hovering.
 */
export function help(text) {
  return el('p.help-note', text);
}

export function init() {
  applyTheme(currentTheme());
  applyHelp(helpEnabled());

  $('#btn-theme')?.addEventListener('click', cycleTheme);
  $('#btn-help')?.addEventListener('click', toggleHelp);

  /* Re-evaluate `auto` when the OS preference flips mid-session. */
  window.matchMedia?.('(prefers-color-scheme: light)')
    ?.addEventListener?.('change', () => {
      if (currentTheme() === 'auto') applyTheme('auto');
    });
}
