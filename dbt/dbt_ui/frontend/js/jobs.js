/* ==========================================================================
   jobs.js - poll a running dbt job for new log lines.

   Polling rather than a socket: the stdlib server has no websocket support, and
   a cursor-based poll is enough for a log that produces tens of lines a second.
   The cursor means several viewers can follow the same run and a page refresh
   resumes without losing or duplicating output.
   ========================================================================== */

import { api, state } from './core.js';

const FAST_MS = 550;
const IDLE_MS = 1600;

/**
 * Follow a job until it finishes.
 * Returns a stop function; call it when the view is torn down.
 */
export function watchJob(jobId, { onLines, onUpdate, onDone } = {}) {
  let cursor = 0;
  let stopped = false;
  let timer = null;

  async function tick() {
    if (stopped) return;

    try {
      const payload = await api.job(jobId, cursor);
      if (stopped) return;

      if (payload.lines?.length) {
        cursor = payload.cursor;
        onLines?.(payload.lines);
      }

      onUpdate?.(payload.job);
      state.activeJob = payload.job.is_active ? payload.job : null;

      if (!payload.job.is_active) {
        stopped = true;
        onDone?.(payload.job, payload);
        return;
      }

      /* Poll fast while output is flowing, slow down when it is quiet. */
      timer = setTimeout(tick, payload.lines?.length ? FAST_MS : IDLE_MS);
    } catch (error) {
      if (stopped) return;
      /* Transient failure: back off rather than giving up on the run. */
      timer = setTimeout(tick, 2500);
    }
  }

  tick();

  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
  };
}

/**
 * Watch whatever run is currently in flight, for the header dock.
 * Used by app.js so a run started in one view stays visible everywhere.
 */
export function watchActive({ onUpdate, onDone } = {}) {
  let stopped = false;
  let inner = null;

  async function poll() {
    if (stopped) return;
    try {
      const { active } = await api.jobs();
      if (stopped) return;

      state.activeJob = active;
      onUpdate?.(active);

      if (active?.is_active && !inner) {
        inner = watchJob(active.id, {
          onUpdate: (job) => {
            state.activeJob = job.is_active ? job : null;
            onUpdate?.(job.is_active ? job : null);
          },
          onDone: (job, payload) => {
            inner = null;
            state.activeJob = null;
            onUpdate?.(null);
            onDone?.(job, payload);
          },
        });
      }
    } catch {
      /* ignore; the next poll will retry */
    }
    setTimeout(poll, 3000);
  }

  poll();
  return () => {
    stopped = true;
    inner?.();
  };
}
