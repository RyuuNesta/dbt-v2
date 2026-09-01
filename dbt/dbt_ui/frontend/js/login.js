/* ==========================================================================
   login.js - the sign-in gate.

   Rendered in place of the whole app when there is no valid session. Deliberately
   replaces the shell rather than overlaying it: if you are not signed in there is
   nothing behind the dialog worth showing, and an overlay invites the assumption
   that the data underneath is real.

   The session token never passes through JavaScript. It arrives as an HttpOnly
   cookie on the login response and the browser attaches it from then on, which
   is why there is nothing here that stores or reads a token.
   ========================================================================== */

import { api, clear, el, state } from './core.js';
import { callout } from './components.js';

/**
 * Show the login screen.
 *
 * @param {function} onSignedIn  called with the session payload once accepted
 */
export function renderLogin(onSignedIn) {
  const host = document.getElementById('app') || document.body;

  const email = el('input.input', {
    type: 'email',
    autocomplete: 'username',
    placeholder: 'you@company.com',
    required: true,
    'aria-label': 'Email address',
  });
  const password = el('input.input', {
    type: 'password',
    autocomplete: 'current-password',
    placeholder: 'Password',
    required: true,
    'aria-label': 'Password',
  });

  const errorHost = el('div');
  const submit = el('button.btn.btn-primary.btn-block', { type: 'submit' }, 'Sign in');

  const rolesHost = el('div');

  async function attempt(event) {
    event?.preventDefault();
    clear(errorHost);

    const address = email.value.trim();
    if (!address || !password.value) {
      clear(errorHost).append(
        callout('Both fields are required', 'Enter your email and password.', 'warn'));
      return;
    }

    submit.disabled = true;
    submit.textContent = 'Signing in…';

    try {
      const payload = await api.login(address, password.value);
      /* Never keep the plaintext around any longer than the request needs. */
      password.value = '';
      onSignedIn(payload);
    } catch (error) {
      clear(errorHost).append(
        callout(
          error.status === 401 ? 'Sign-in failed' : 'Could not sign in',
          error.message,
          'err',
          error.detail ? el('p.tiny.faint', error.detail) : null,
        ));
      password.select?.();
    } finally {
      submit.disabled = false;
      submit.textContent = 'Sign in';
    }
  }

  const form = el('form.login-form', { onsubmit: attempt },
    el('div.field.mb',
      el('label', { for: 'login-email' }, 'Email'),
      email),
    el('div.field.mb',
      el('label', { for: 'login-password' }, 'Password'),
      password),
    errorHost,
    el('div.mt', submit));

  /* A short reminder of what each role can do. Shown pre-login because it is
     public reference material, not a hint about who exists - it lists roles,
     never accounts. */
  (async () => {
    try {
      const catalogue = await api.roleCatalogue();
      state.roles = catalogue;
      rolesHost.append(
        el('div.login-roles',
          el('div.stat-label.mb', 'Roles'),
          ...catalogue.roles.map((role) =>
            el('div.login-role',
              el('strong', role.label),
              el('span.tiny.faint', role.blurb)))),
      );
    } catch {
      /* Reference material only. If it cannot load, the form still works. */
    }
  })();

  clear(host).append(
    el('div.login-shell',
      el('div.login-card',
        el('div.login-head',
          el('h1', 'dbt Studio'),
          el('p.small.faint', 'Sign in to continue')),
        form),
      rolesHost),
  );

  email.focus();
}
