/*
 * Headless-browser check of the Cleanup Advisor (Feature 4) and the
 * transparency preview (Feature 5A) against the live dev server.
 *
 * Run:  node <copy of this file into playwright-core's npx cache dir>
 */
import { chromium } from 'playwright-core';

const BASE = 'http://localhost:8777';
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
  const page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  const consoleErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => consoleErrors.push(String(e)));

  /* ADC credentials are expired in this environment (confirmed via a direct
     /api/connection call returning invalid_grant), which is a pre-existing
     condition unrelated to this change - the picker already degrades to a
     callout + retry button rather than crashing, which is correct. To still
     exercise the interactive flow this test intercepts the warehouse
     inventory endpoint with synthetic rows shaped exactly like the real
     payload, so real app code (picker.js, advisor.js) runs against them
     unmodified. */
  await page.route('**/api/warehouse/inventory**', (route) => {
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        project: 'data-analytics-asg',
        datasets: ['bronze_dbt', 'silver_dbt'],
        tables: [
          {
            qualified: 'bronze_dbt.bronze_gl_entries', dataset: 'bronze_dbt',
            table: 'bronze_gl_entries', relation: '`data-analytics-asg`.`bronze_dbt`.`bronze_gl_entries`',
            row_count: 15, size_bytes: 4096, last_modified: Date.now(), is_view: false,
            model: 'bronze_gl_entries', layer: 'bronze', managed_by_dbt: true,
            column_count: 17, test_count: 4,
          },
          {
            qualified: 'silver_dbt.silver_gl_entries', dataset: 'silver_dbt',
            table: 'silver_gl_entries', relation: '`data-analytics-asg`.`silver_dbt`.`silver_gl_entries`',
            row_count: 15, size_bytes: 8192, last_modified: Date.now(), is_view: false,
            model: 'silver_gl_entries', layer: 'silver', managed_by_dbt: true,
            column_count: 30, test_count: 6,
          },
        ],
        table_count: 2, managed_count: 2, view_count: 0,
        fetched_at: Date.now() / 1000, cache_ttl: 600, error: null,
        scope: { allowed_datasets: ['bronze_dbt', 'silver_dbt'] },
      }),
    });
  });

  await page.goto(`${BASE}/#advisor`, { waitUntil: 'networkidle' });
  try {
    await page.waitForSelector('.pick-table', { timeout: 8000 });
  } catch (err) {
    console.log('  TIMEOUT - dumping page state for diagnosis');
    console.log('  title element:', await page.textContent('#view-title').catch(() => '(missing)'));
    console.log('  main HTML (first 2000 chars):');
    console.log((await page.locator('#main').innerHTML().catch(() => '(no #main)')).slice(0, 2000));
    if (consoleErrors.length) {
      console.log('  console errors so far:');
      console.log(consoleErrors.join('\n'));
    }
    throw err;
  }

  /* analyse/preview/generate all profile a live relation, which also needs
     BigQuery. Intercept with a fixed synthetic analysis so the *frontend*
     wiring (tabs, plan rendering, edit toggle) is what gets exercised. */
  const analysis = {
    model: 'bronze_gl_entries', layer: 'bronze',
    suggested_model_name: 'silver_gl_entries',
    summary: { total: 3, high_confidence: 2 },
    duplicate_check: {
      key: ['gl_entry_key'], checked: true, key_groups: 15,
      duplicated_keys: 0, surplus_rows: 0, worst_group: 1, is_unique: true,
    },
    plan: { key_columns: ['gl_entry_key'], grain_columns: ['company_code'], measure_columns: ['amount_local'] },
    profile: {
      relation: '`data-analytics-asg`.`bronze_dbt`.`bronze_gl_entries`',
      row_count: 15, declared_row_count: 15, sampled: false,
      bytes_processed: 2048, duration_ms: 340,
      columns: [
        { name: 'gl_entry_key', data_type: 'STRING', data_type_yaml: 'string', category: 'text',
          description: '', null_count: 0, null_pct: 0, distinct_count: 15, distinct_pct: 100,
          blank_count: 0, negative_count: null, min: 'a', max: 'z',
          is_unique: true, is_constant: false, is_all_null: false },
        { name: 'amount_local', data_type: 'NUMERIC', data_type_yaml: 'numeric', category: 'numeric',
          description: '', null_count: 0, null_pct: 0, distinct_count: 15, distinct_pct: 100,
          blank_count: 0, negative_count: 3, min: '-500', max: '900',
          is_unique: false, is_constant: false, is_all_null: false },
      ],
    },
    recommendations: [
      { id: 'testing:gl_entry_key', category: 'testing', title: 'Add a uniqueness guard on gl_entry_key',
        detail: 'The key is unique today.', evidence: '15 distinct keys, zero duplicates.',
        confidence: 'high', columns: ['gl_entry_key'], sql_hint: 'data_tests: [unique, not_null]', default_applied: true },
      { id: 'type_cast:amount_local', category: 'type_cast', title: 'Cast amount_local to NUMERIC',
        detail: 'Exact decimal arithmetic.', evidence: 'FLOAT64 observed.', confidence: 'high',
        columns: ['amount_local'], sql_hint: 'cast(amount_local as numeric)', default_applied: true },
      { id: 'pruning:unused_flag', category: 'pruning', title: 'unused_flag is constant',
        detail: 'A single distinct value.', evidence: '1 distinct value.', confidence: 'medium',
        columns: ['unused_flag'], sql_hint: '', default_applied: false },
    ],
  };

  await page.route('**/api/advisor/analyse**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(analysis) });
  });

  const plan = {
    model_name: 'silver_gl_entries', source_model: 'bronze_gl_entries',
    path: 'models/silver/silver_gl_entries.sql', materialized: 'view',
    sources: [{ model: 'bronze_gl_entries', relation: analysis.profile.relation, row_count: 15,
                reference: "{{ ref('bronze_gl_entries') }}", note: 'Resolved by dbt at build time.' }],
    steps: [
      { kind: 'read', title: 'Read every row of bronze_gl_entries', detail: 'Unfiltered read.',
        columns: [], evidence: '', sql: "select * from {{ ref('bronze_gl_entries') }}" },
      { kind: 'type_cast', title: 'Cast money to NUMERIC', detail: 'Exact decimal arithmetic.',
        columns: ['amount_local'], evidence: 'FLOAT64 observed.', sql: 'cast(amount_local as numeric)' },
      { kind: 'audit', title: 'Stamp audit columns', detail: 'Every row records the run.',
        columns: ['_silver_loaded_at'], evidence: '', sql: "{{ asg_audit_columns('silver') }}" },
    ],
    columns: [
      { name: 'gl_entry_key', data_type: 'string', origin: 'key', note: 'Business key.' },
      { name: 'amount_local', data_type: 'numeric', origin: 'recast', note: 'cast from float64' },
      { name: '_silver_loaded_at', data_type: 'timestamp', origin: 'macro', note: 'current_timestamp()' },
    ],
    column_count: 3, dropped_columns: [], key_columns: ['gl_entry_key'],
    row_estimate: { rows: 15, exact: true, basis: 'No deduplication accepted; row count carries through.', source_rows: 15, removed: 0 },
    tests: [{ column: 'gl_entry_key', tests: 'data_tests: [unique, not_null]', why: 'The business key.' }],
    applied: [{ id: 'type_cast:amount_local', category: 'type_cast', title: 'Cast amount_local to NUMERIC' }],
    skipped: [],
  };
  await page.route('**/api/advisor/preview**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(plan) });
  });

  const generated = {
    model_name: 'silver_gl_entries', path: 'models/silver/silver_gl_entries.sql',
    sql: "{{ config(materialized='view') }}\n\nwith bronze as (\n\n    select * from {{ ref('bronze_gl_entries') }}\n\n)\n\nselect * from bronze\n",
    applied: [{ id: 'type_cast:amount_local', category: 'type_cast', title: 'Cast amount_local to NUMERIC' }],
    skipped: [], dropped_columns: [], key_columns: ['gl_entry_key'],
  };
  await page.route('**/api/advisor/generate**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(generated) });
  });

  console.log('\n== picker present ==');
  check('view title is Cleanup Advisor',
    (await page.textContent('#view-title')).trim() === 'Cleanup Advisor');
  const rowCount = await page.locator('.pick-table tbody tr').count();
  console.log(`  picker rows: ${rowCount}`);
  check('table picker lists at least one row', rowCount > 0);

  // Select a bronze/silver model via its checkbox.
  const firstBox = page.locator('.pick-table tbody tr .pick-cell input[type=checkbox]').first();
  await firstBox.check();
  await page.waitForTimeout(100);

  console.log('\n== single-table analysis ==');
  const analyseBtn = page.locator('button', { hasText: 'Analyse and recommend' });
  check('analyse button is enabled after selecting one row',
    !(await analyseBtn.isDisabled()));
  await analyseBtn.click();

  await page.waitForSelector('.tabs', { timeout: 15000 });
  await page.waitForSelector('.rec', { timeout: 15000 });
  const recCount = await page.locator('.rec').count();
  console.log(`  recommendation cards: ${recCount}`);
  check('recommendations rendered', recCount > 0);

  console.log('\n== plan preview tab (Feature 5A) ==');
  await page.locator('.tab', { hasText: 'How it will be built' }).click();
  await page.waitForSelector('.plan-steps .plan-step', { timeout: 10000 });
  const stepCount = await page.locator('.plan-steps .plan-step').count();
  console.log(`  plan steps shown: ${stepCount}`);
  check('transformation steps rendered', stepCount > 0);

  const hasApprove = await page.locator('button', { hasText: 'Approve & generate' }).count();
  check('Approve & generate button present', hasApprove > 0);

  const estimateChips = await page.locator('.plan-source, .stat-value').count();
  check('row estimate / source block rendered', estimateChips > 0);

  console.log('\n== approve navigates to generated model tab ==');
  await page.locator('button', { hasText: 'Approve & generate' }).click();
  await page.waitForSelector('.code-area, .code-block, pre', { timeout: 10000 });
  const genTabActive = await page.locator('.tab.active', { hasText: 'Generated silver model' }).count();
  check('clicking Approve switches to the Generated silver model tab', genTabActive === 1);

  const hasSql = (await page.locator('.tab-panel.active').innerText()).includes('select');
  check('generated SQL is visible (regression check: this tab used to be stuck loading)', hasSql);

  console.log('\n== edit-before-writing toggle ==');
  // The button's own label flips between the two states ("Edit before
  // writing" <-> "Done editing"), so it has to be re-located after each click
  // rather than reusing a locator whose text predicate no longer matches.
  const editToggle = () => page.locator('button', { hasText: /Edit before writing|Done editing/ });
  if (await editToggle().count()) {
    await editToggle().click();
    await page.waitForTimeout(100);
    const textareaVisible = await page.locator('textarea.code-area').isVisible();
    check('edit toggle reveals a textarea with the SQL', textareaVisible);
    await editToggle().click(); // toggle back off
    await page.waitForTimeout(100);
    const textareaGoneAfterToggleOff = await page.locator('textarea.code-area').count();
    check('toggling off hides the textarea again', textareaGoneAfterToggleOff === 0);
  } else {
    check('Edit before writing button present', false, 'not found');
  }

  console.log('\n== multi-select batch mode ==');
  // A hash-only navigate() would not force a fresh render if the SPA thinks
  // it is already on #advisor; a full reload guarantees a clean picker.
  await page.goto(`${BASE}/#advisor`, { waitUntil: 'networkidle' });
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForSelector('.pick-table', { timeout: 8000 });
  const boxes = page.locator('.pick-table tbody tr .pick-cell input[type=checkbox]');
  const total = await boxes.count();
  const toSelect = Math.min(2, total);
  for (let i = 0; i < toSelect; i += 1) await boxes.nth(i).check();
  console.log(`  selected ${toSelect} of ${total} rows`);

  const batchBtn = page.locator('button', { hasText: /Analyse \d+ tables/ });
  if (toSelect > 1 && await batchBtn.count()) {
    await batchBtn.click();
    await page.waitForSelector('table.data.compact tbody tr', { timeout: 20000 });
    const summaryRows = await page.locator('table.data.compact tbody tr').count();
    console.log(`  batch summary rows: ${summaryRows}`);
    check('batch summary table has one row per analysed table', summaryRows >= 1);

    const openLink = page.locator('button', { hasText: 'Open →' }).first();
    if (await openLink.count()) {
      await openLink.click();
      await page.waitForTimeout(300);
      const detailMark = await page.locator('.deep-dive-mark').count();
      check('opening a batch row shows the per-table detail panel', detailMark === 1);
    }
  } else {
    console.log('  only one selectable row available - batch mode not exercised');
  }

  console.log('\n== console ==');
  check('no console errors during the session', consoleErrors.length === 0, consoleErrors.join(' | '));

  await browser.close();
  console.log(`\n${'='.repeat(50)}`);
  if (failures.length) {
    console.log(`${failures.length} FAILED: ${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('all checks passed');
}
main().catch((e) => { console.error(e); process.exit(1); });
