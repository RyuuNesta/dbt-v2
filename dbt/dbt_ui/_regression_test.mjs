/*
 * Full-app regression sweep: every nav page loads without a console error or
 * an uncaught render exception, after touching shared files (core.js, app.js,
 * index.html, catalog.js's new export).
 *
 * Run:  node <copy of this file into playwright-core's npx cache dir>
 */
import { chromium } from 'playwright-core';

const BASE = 'http://localhost:8777';
const PAGES = ['overview', 'pipeline', 'workbench', 'schema', 'advisor', 'runs',
               'catalog', 'erd', 'schedule'];
const failures = [];
function check(label, condition, detail = '') {
  if (condition) console.log(`  ok    ${label}`);
  else { console.log(`  FAIL  ${label}${detail ? `  (${detail})` : ''}`); failures.push(label); }
}

async function main() {
  const browser = await chromium.launch({
    executablePath:
      'C:\\Users\\richa\\AppData\\Local\\ms-playwright\\chromium-1223\\chrome-win64\\chrome.exe',
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  for (const name of PAGES) {
    const errors = [];
    const onConsole = (m) => { if (m.type() === 'error') errors.push(m.text()); };
    const onError = (e) => errors.push(String(e));
    page.on('console', onConsole);
    page.on('pageerror', onError);

    console.log(`\n== #${name} ==`);
    await page.goto(`${BASE}/#${name}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(600); // let async panel loads (graph, warehouse) settle

    const failedPanel = await page.locator('.panel-body', { hasText: 'failed to render' }).count();
    check(`${name}: no "failed to render" panel`, failedPanel === 0);

    const title = await page.textContent('#view-title').catch(() => '');
    check(`${name}: view title rendered`, Boolean(title && title.trim()), title);

    // A 400/404 to a *_inventory/warehouse/analyse endpoint is expected here
    // (ADC credentials are expired in this environment) - only flag genuine
    // JS errors (uncaught exceptions, syntax problems, undefined is not a
    // function, etc.), not the network-level failure text those produce.
    const jsErrors = errors.filter((e) => !/Failed to load resource/.test(e));
    check(`${name}: no JS console errors`, jsErrors.length === 0, jsErrors.join(' | '));

    page.off('console', onConsole);
    page.off('pageerror', onError);
  }

  await browser.close();
  console.log(`\n${'='.repeat(50)}`);
  if (failures.length) {
    console.log(`${failures.length} FAILED: ${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('all checks passed');
}
main().catch((e) => { console.error(e); process.exit(1); });
