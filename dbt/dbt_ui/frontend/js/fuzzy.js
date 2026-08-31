/* ==========================================================================
   fuzzy.js - subsequence scoring for the autocomplete dropdown.

   Substring matching was the previous behaviour and it is too strict: typing
   "sgl" should find "silver_gl_entries", and "dtm" should find "date_trunc".
   This scores a candidate on how well the typed characters appear *in order*
   anywhere in the name, then ranks by quality of match rather than position of
   a literal substring.

   Designed to stay under a millisecond for a few thousand candidates, since it
   runs on every keystroke:
     - one pass per candidate, no regex construction, no allocation in the
       hot loop beyond the match-index array
     - an early reject as soon as a query character cannot be found
     - candidates are lower-cased once by the caller, not per comparison
   ========================================================================== */

/* Scoring weights. Tuned so that, for the query "gl":
     gl_entries          wins on prefix
     silver_gl_entries   next, matches at a word boundary
     single_line         last, scattered subsequence                        */
const SCORE_PREFIX = 40;      // candidate starts with the whole query
const SCORE_WORD_START = 22;  // char matches at a word boundary
const SCORE_CONSECUTIVE = 14; // char immediately follows the previous match
const SCORE_CAMEL = 8;        // char starts a camelCase hump
const PENALTY_LEADING = 2;    // per char skipped before the first match
const PENALTY_GAP = 1;        // per char skipped between matches
const SCORE_EXACT = 120;      // candidate is exactly the query
const SCORE_SHORTER = 6;      // mild preference for shorter names

const isBoundary = (ch) => ch === '_' || ch === '.' || ch === '-' || ch === ' ';

/**
 * Score `query` against `candidate`.
 * Returns { score, positions } or null when the query is not a subsequence.
 */
export function score(query, candidate) {
  if (!query) return { score: 0, positions: [] };
  if (!candidate) return null;

  const q = query.toLowerCase();
  const c = candidate.toLowerCase();

  if (q.length > c.length) return null;
  if (q === c) return { score: SCORE_EXACT + q.length, positions: [...q].map((_, i) => i) };

  let total = 0;
  let ci = 0;
  let previousMatch = -2;
  const positions = [];

  for (let qi = 0; qi < q.length; qi += 1) {
    const wanted = q[qi];
    let found = -1;

    while (ci < c.length) {
      if (c[ci] === wanted) {
        found = ci;
        break;
      }
      ci += 1;
    }

    /* Not a subsequence: bail immediately rather than scoring a bad match. */
    if (found === -1) return null;

    let charScore = 1;

    if (found === previousMatch + 1) {
      charScore += SCORE_CONSECUTIVE;
    }
    if (found === 0 || isBoundary(c[found - 1])) {
      charScore += SCORE_WORD_START;
    } else if (
      candidate[found] === candidate[found].toUpperCase() &&
      candidate[found] !== candidate[found].toLowerCase()
    ) {
      charScore += SCORE_CAMEL;
    }

    const gap = qi === 0 ? found : found - previousMatch - 1;
    charScore -= gap * (qi === 0 ? PENALTY_LEADING : PENALTY_GAP);

    total += charScore;
    positions.push(found);
    previousMatch = found;
    ci += 1;
  }

  if (c.startsWith(q)) total += SCORE_PREFIX;

  /* Prefer the tighter name when two candidates score alike, so "id" ranks
     "id" above "invoice_document_reference". */
  total += Math.max(0, SCORE_SHORTER - Math.floor(c.length / 6));

  return { score: total, positions };
}

/**
 * Filter and rank `items` against `query`.
 *
 * `key` extracts the string to match. Items keep their identity and gain
 * `_score` and `_positions` so the renderer can bold the matched characters.
 * A falsy query returns the input order untouched, capped at `limit`.
 */
export function rank(query, items, { key = (item) => item.label, limit = 60 } = {}) {
  if (!query) return items.slice(0, limit);

  const scored = [];
  for (const item of items) {
    const result = score(query, key(item));
    if (result) {
      scored.push({ ...item, _score: result.score, _positions: result.positions });
    }
  }

  /* Stable within equal scores: sort by score desc, then name length, then
     alphabetically, so the list does not jitter as you type. */
  scored.sort((a, b) => {
    if (b._score !== a._score) return b._score - a._score;
    const ka = key(a);
    const kb = key(b);
    if (ka.length !== kb.length) return ka.length - kb.length;
    return ka < kb ? -1 : ka > kb ? 1 : 0;
  });

  return scored.slice(0, limit);
}

/**
 * Build a DocumentFragment with the matched characters wrapped in <b>.
 * Returns a plain text node when there are no positions to highlight.
 */
export function highlight(text, positions) {
  const fragment = document.createDocumentFragment();
  if (!positions || !positions.length) {
    fragment.append(document.createTextNode(text));
    return fragment;
  }

  const marked = new Set(positions);
  let run = '';
  let runMarked = false;

  const flush = () => {
    if (!run) return;
    if (runMarked) {
      const strong = document.createElement('b');
      strong.className = 'ac-hit';
      strong.textContent = run;
      fragment.append(strong);
    } else {
      fragment.append(document.createTextNode(run));
    }
    run = '';
  };

  for (let i = 0; i < text.length; i += 1) {
    const isMarked = marked.has(i);
    if (isMarked !== runMarked) {
      flush();
      runMarked = isMarked;
    }
    run += text[i];
  }
  flush();

  return fragment;
}
