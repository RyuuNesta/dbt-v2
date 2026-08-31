/*
 * Headless-browser check of the ERD view against the live dev server.
 *
 * A syntax check (node --check) proves the file parses; it says nothing about
 * whether the module actually renders, whether the SVG has the nodes/edges it
 * claims to, or whether pan/zoom/drag/select/export wire up correctly in a
 * real DOM. This drives a real Chromium instance against the running server
 * and asserts on the resulting page.
 *
 * Not part of the app or its dependencies - a throwaway verification script,
 * deleted after use. Requires the dev server already running on :8777 and the
 * Playwright package available via npx (already cached locally).
 *
 * Run:  node dbt_ui\_erd_browser_test.mjs
 */
import { chromium } from 'playwright-core';

const BASE = 'http://localhost:8777';
const failures = [];

function check(label, condition, detail = '') {
  if (condition) {
    console.log(`  ok    ${label}`);
  } else {
    console.log(`  FAIL  ${label}${detail ? `  (${detail})` : ''}`);
    failures.push(label);
  }
}

async function main() {
  const browser = await chromium.launch({
    executablePath:
      'C:\\Users\\richa\\AppData\\Local\\ms-playwright\\chromium-1223\\chrome-win64\\chrome.exe',
  });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });

  const consoleErrors = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });
  page.on('pageerror', (err) => consoleErrors.push(String(err)));

  await page.goto(`${BASE}/#erd`, { waitUntil: 'networkidle' });
  await page.waitForSelector('.erd-page', { timeout: 8000 });
  // Initial load + relationship detection.
  await page.waitForSelector('.erd-canvas svg .erd-node', { timeout: 8000 });

  console.log('\n== initial render ==');
  const navLabel = await page.textContent('#view-title');
  check('view title is ERD', navLabel.trim() === 'ERD', navLabel);

  const nodeCount = await page.locator('.erd-node').count();
  console.log(`  node count: ${nodeCount}`);
  check('at least one table node rendered', nodeCount > 0);

  const edgeCount = await page.locator('.erd-edge').count();
  console.log(`  edge count: ${edgeCount}`);
  check('at least one relationship edge rendered', edgeCount > 0);

  const dimmedCount = await page.locator('.erd-node.is-dimmed').count();
  console.log(`  dimmed (out-of-scope) nodes: ${dimmedCount}`);
  check('gold is present but dimmed, not hidden', dimmedCount >= 1);

  const legendSwatches = await page.locator('.erd-legend-swatch').count();
  check('legend renders at least one layer swatch', legendSwatches > 0);

  const statusChips = await page.locator('.erd-status .chip').count();
  check('status bar shows table/relationship counts', statusChips >= 2);

  // ---- side panel on click ----
  console.log('\n== click a node opens the side panel ==');
  const firstNode = page.locator('.erd-node').first();
  const tableName = await firstNode.locator('.erd-node-title').textContent();
  await firstNode.click({ position: { x: 20, y: 12 } });
  await page.waitForSelector('.erd-side-head h3', { timeout: 4000 });
  const sideTitle = await page.textContent('.erd-side-head h3');
  console.log(`  clicked "${tableName.trim()}", side panel shows "${sideTitle.trim()}"`);
  check('side panel opens with a matching table name',
    sideTitle.trim() === tableName.trim(), `${sideTitle} vs ${tableName}`);

  const columnRows = await page.locator('.erd-side table.data tbody tr').count();
  check('side panel lists at least one column', columnRows > 0);

  // ---- lineage highlight toggle ----
  console.log('\n== lineage highlight ==');
  const highlightBtn = page.locator('.erd-side button', { hasText: 'Highlight upstream/downstream' });
  if (await highlightBtn.count()) {
    await highlightBtn.click();
    await page.waitForTimeout(150);
    const hotEdges = await page.locator('.erd-edge.is-hot').count();
    console.log(`  hot edges after highlight: ${hotEdges}`);
    check('highlighting marks at least one edge as hot', hotEdges > 0);

    const clearBtn = page.locator('.erd-side button', { hasText: 'Clear lineage highlight' });
    check('button relabels to "Clear" once active', await clearBtn.count() === 1);
    await clearBtn.click();
    await page.waitForTimeout(150);
    const hotAfterClear = await page.locator('.erd-edge.is-hot').count();
    check('clearing removes the highlight', hotAfterClear === 0);
  } else {
    check('highlight button present', false, 'not found - table may have no relationships');
  }

  // ---- search filters the canvas ----
  console.log('\n== search ==');
  const beforeSearch = await page.locator('.erd-node').count();
  await page.fill('.erd-toolbar input[type="search"]', 'zzz_definitely_not_a_table');
  await page.waitForTimeout(150);
  const afterSearch = await page.locator('.erd-node').count();
  console.log(`  nodes before: ${beforeSearch}, after nonsense search: ${afterSearch}`);
  check('an unmatched search empties the canvas', afterSearch === 0);

  await page.fill('.erd-toolbar input[type="search"]', '');
  await page.waitForTimeout(150);
  const afterClear = await page.locator('.erd-node').count();
  check('clearing the search restores the nodes', afterClear === beforeSearch);

  // ---- keys-only toggle ----
  console.log('\n== keys only toggle ==');
  const colsBefore = await page.locator('.erd-node').first().locator('.erd-col-row').count();
  const keysToggle = page.locator('.erd-toolbar label.switch', { hasText: 'Keys only' }).locator('input');
  await keysToggle.check();
  await page.waitForSelector('.erd-canvas svg .erd-node', { timeout: 8000 });
  const colsAfter = await page.locator('.erd-node').first().locator('.erd-col-row').count();
  console.log(`  visible column rows before/after: ${colsBefore} / ${colsAfter}`);
  check('keys-only reduces or maintains visible column rows on the first node',
    colsAfter <= colsBefore, `${colsAfter} vs ${colsBefore}`);
  await keysToggle.uncheck();
  await page.waitForSelector('.erd-canvas svg .erd-node', { timeout: 8000 });

  // ---- pan (drag on empty canvas background) ----
  console.log('\n== pan ==');
  const initialTransform = await page.locator('.erd-world').getAttribute('transform');
  const canvasBox = await page.locator('.erd-canvas').boundingBox();
  // Find a point inside the canvas that is NOT on top of any node, so the
  // drag exercises panning rather than accidentally starting a node drag.
  const nodeBoxes = await page.locator('.erd-node').evaluateAll(
    (nodes) => nodes.map((n) => n.getBoundingClientRect())
      .map((r) => ({ left: r.left, top: r.top, right: r.right, bottom: r.bottom })),
  );
  function isEmpty(px, py) {
    return !nodeBoxes.some((b) => px >= b.left && px <= b.right && py >= b.top && py <= b.bottom);
  }
  /* Any toast raised by an earlier step (lineage highlight, search, etc.) is a
     fixed-position overlay pinned to the bottom-right of the viewport, which
     can sit on top of the canvas's own bottom-right corner. elementFromPoint
     is the ground truth for "will this click actually reach the canvas" -
     scan for a pixel it agrees is really the SVG background. */
  async function isReallyCanvas(x, y) {
    return page.evaluate(([px, py]) => {
      const el = document.elementFromPoint(px, py);
      return Boolean(el && (el.closest('.erd-canvas')) && !el.closest('.erd-node'));
    }, [x, y]);
  }

  let panX = canvasBox.x + 40;
  let panY = canvasBox.y + 40;
  outer:
  for (let y = canvasBox.y + 10; y < canvasBox.y + canvasBox.height; y += 15) {
    for (let x = canvasBox.x + 10; x < canvasBox.x + canvasBox.width; x += 15) {
      if (isEmpty(x, y) && (await isReallyCanvas(x, y))) { panX = x; panY = y; break outer; }
    }
  }
  console.log(`  drag start at (${panX}, ${panY})`);

  await page.mouse.move(panX, panY);
  await page.mouse.down();
  await page.waitForTimeout(30);
  const midDragPanning = await page.evaluate(
    () => document.querySelector('.erd-canvas').classList.contains('is-panning'),
  );
  await page.mouse.move(panX + 100, panY + 60, { steps: 6 });
  await page.mouse.up();
  const afterPanTransform = await page.locator('.erd-world').getAttribute('transform');
  console.log(`  is-panning class present mid-drag: ${midDragPanning}`);
  console.log(`  transform before: ${initialTransform}`);
  console.log(`  transform after:  ${afterPanTransform}`);
  check('mousedown on empty canvas enters pan mode', midDragPanning === true);
  check('panning changes the world transform', initialTransform !== afterPanTransform);

  // ---- zoom via toolbar button ----
  console.log('\n== zoom ==');
  const beforeZoomTransform = await page.locator('.erd-world').getAttribute('transform');
  await page.locator('.erd-toolbar button[title="Zoom in"]').click();
  await page.waitForTimeout(80);
  const afterZoomTransform = await page.locator('.erd-world').getAttribute('transform');
  console.log(`  before: ${beforeZoomTransform}`);
  console.log(`  after:  ${afterZoomTransform}`);
  check('zoom in changes the scale factor', beforeZoomTransform !== afterZoomTransform);

  // Reset changes the transform back toward the fitted view.
  await page.locator('.erd-toolbar button', { hasText: 'Fit' }).click();
  await page.waitForSelector('.erd-canvas svg .erd-node', { timeout: 8000 });
  check('Fit re-renders the canvas without throwing', await page.locator('.erd-node').count() > 0);

  // ---- drag a node ----
  console.log('\n== drag a node ==');
  const node = page.locator('.erd-node').nth(1);
  const box = await node.boundingBox();
  const transformBefore = await node.getAttribute('transform');
  await page.mouse.move(box.x + 30, box.y + 12);
  await page.mouse.down();
  await page.mouse.move(box.x + 130, box.y + 90, { steps: 8 });
  await page.mouse.up();
  const transformAfter = await node.getAttribute('transform');
  console.log(`  node transform before: ${transformBefore}`);
  console.log(`  node transform after:  ${transformAfter}`);
  check('dragging a node changes its position', transformBefore !== transformAfter);

  // Drag should not have opened the side panel for a different table nor thrown.
  await page.waitForTimeout(100);

  // ---- in-scope-only toggle hides gold entirely ----
  console.log('\n== in-scope-only toggle ==');
  const scopeToggle = page.locator('.erd-toolbar label.switch', { hasText: 'In-scope only' }).locator('input');
  await scopeToggle.check();
  await page.waitForSelector('.erd-canvas svg .erd-node', { timeout: 8000 });
  const dimmedAfterScope = await page.locator('.erd-node.is-dimmed').count();
  console.log(`  dimmed nodes with in-scope-only checked: ${dimmedAfterScope}`);
  check('in-scope-only leaves no dimmed (out-of-scope) nodes', dimmedAfterScope === 0);
  await scopeToggle.uncheck();
  await page.waitForSelector('.erd-canvas svg .erd-node', { timeout: 8000 });

  // ---- exports: SVG / Mermaid / DBML actually download something ----
  console.log('\n== exports ==');
  async function captureDownload(buttonText) {
    const [download] = await Promise.all([
      page.waitForEvent('download', { timeout: 5000 }),
      page.locator('.erd-export-menu button', { hasText: buttonText }).click(),
    ]);
    const path = await download.path();
    const fs = await import('node:fs');
    const content = path ? fs.readFileSync(path, 'utf-8') : '';
    return { filename: download.suggestedFilename(), content };
  }

  try {
    const svgDl = await captureDownload('SVG');
    check('SVG export produces a .svg file', svgDl.filename.endsWith('.svg'), svgDl.filename);
    check('SVG export content looks like SVG',
      svgDl.content.includes('<svg') && svgDl.content.includes('erd-node-box'),
      svgDl.content.slice(0, 80));
  } catch (err) {
    check('SVG export triggers a download', false, String(err));
  }

  try {
    const mmdDl = await captureDownload('Mermaid');
    check('Mermaid export produces a .mmd file', mmdDl.filename.endsWith('.mmd'), mmdDl.filename);
    check('Mermaid export starts with erDiagram', mmdDl.content.includes('erDiagram'));
  } catch (err) {
    check('Mermaid export triggers a download', false, String(err));
  }

  try {
    const dbmlDl = await captureDownload('DBML');
    check('DBML export produces a .dbml file', dbmlDl.filename.endsWith('.dbml'), dbmlDl.filename);
    check('DBML export contains a Table block', dbmlDl.content.includes('Table '));
  } catch (err) {
    check('DBML export triggers a download', false, String(err));
  }

  // ---- console must be clean throughout ----
  console.log('\n== console errors ==');
  if (consoleErrors.length) {
    console.log(consoleErrors.join('\n'));
  }
  check('no console errors during the entire session', consoleErrors.length === 0,
    consoleErrors.join(' | '));

  // ---- keyboard shortcut navigation (number key 8) still reaches ERD ----
  console.log('\n== keyboard nav ==');
  await page.goto(`${BASE}/#overview`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(200);
  await page.keyboard.press('8');
  await page.waitForTimeout(200);
  const hashAfterKey = await page.evaluate(() => location.hash);
  check('pressing 8 navigates to #erd', hashAfterKey === '#erd', hashAfterKey);

  await browser.close();

  console.log(`\n${'='.repeat(62)}`);
  if (failures.length) {
    console.log(`${failures.length} CHECK(S) FAILED`);
    for (const f of failures) console.log(`  - ${f}`);
    process.exit(1);
  }
  console.log('all checks passed');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
