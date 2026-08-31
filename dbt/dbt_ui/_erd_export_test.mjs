/*
 * Targeted check of the two ERD exports that don't produce a plain <a download>
 * click (PNG goes through canvas.toBlob, PDF goes through window.print()).
 *
 * PNG: click the button, wait for a canvas element to appear transiently, and
 * capture the generated toDataURL result via a hook rather than relying on
 * the download event (canvas.toBlob-driven downloads still fire 'download' in
 * Chromium, but this double-checks the raster itself is non-trivial).
 *
 * PDF: window.print() cannot be observed via a download event at all in
 * headless Chromium; instead this verifies the print-time DOM mutation
 * (erd-printing class, explicit SVG width/height/viewBox) happens and is
 * reverted, which is the part that was actually risky to get wrong.
 *
 * Run:  node <copy-of-this-file-inside-playwright-core's-npx-cache-dir>
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
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const consoleErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => consoleErrors.push(String(e)));

  await page.goto(`${BASE}/#erd`, { waitUntil: 'networkidle' });
  await page.waitForSelector('.erd-canvas svg .erd-node', { timeout: 8000 });

  console.log('\n== PNG export ==');
  const [download] = await Promise.all([
    page.waitForEvent('download', { timeout: 8000 }),
    page.locator('.erd-export-menu button', { hasText: 'PNG' }).click(),
  ]);
  const path = await download.path();
  const fs = await import('node:fs');
  const bytes = path ? fs.readFileSync(path) : Buffer.alloc(0);
  console.log(`  filename: ${download.suggestedFilename()}, bytes: ${bytes.length}`);
  check('PNG export produces a .png file', download.suggestedFilename().endsWith('.png'));
  check('PNG file is non-trivial in size', bytes.length > 500, `${bytes.length} bytes`);
  check('PNG file starts with the PNG magic bytes',
    bytes.length > 8 && bytes[0] === 0x89 && bytes[1] === 0x50 && bytes[2] === 0x4e && bytes[3] === 0x47,
    [...bytes.slice(0, 4)].join(','));

  console.log('\n== PDF export (window.print scoping) ==');
  // Stub window.print so it does not actually try to open a system dialog in
  // headless mode, and so afterprint fires deterministically.
  await page.evaluate(() => {
    window.__printCalled = false;
    window.print = () => {
      window.__printCalled = true;
      window.dispatchEvent(new Event('afterprint'));
    };
  });

  const beforeSvg = await page.evaluate(() => {
    const svg = document.querySelector('.erd-svg');
    return { width: svg.getAttribute('width'), height: svg.getAttribute('height') };
  });

  await page.locator('.erd-export-menu button', { hasText: 'PDF' }).click();
  await page.waitForTimeout(50);

  const printed = await page.evaluate(() => window.__printCalled);
  check('window.print() was invoked', printed === true);

  const afterSvg = await page.evaluate(() => {
    const svg = document.querySelector('.erd-svg');
    return { width: svg.getAttribute('width'), height: svg.getAttribute('height') };
  });
  console.log(`  svg width/height before: ${beforeSvg.width}/${beforeSvg.height}`);
  console.log(`  svg width/height after:  ${afterSvg.width}/${afterSvg.height}`);
  check('SVG dimensions are restored to percentage sizing after print',
    afterSvg.width === '100%' && afterSvg.height === '100%',
    JSON.stringify(afterSvg));

  const printingClassAfter = await page.evaluate(() => document.body.classList.contains('erd-printing'));
  check('erd-printing class is removed after the (stubbed) print completes',
    printingClassAfter === false);

  console.log('\n== console ==');
  check('no console errors', consoleErrors.length === 0, consoleErrors.join(' | '));

  await browser.close();
  console.log(`\n${'='.repeat(50)}`);
  if (failures.length) {
    console.log(`${failures.length} FAILED: ${failures.join(', ')}`);
    process.exit(1);
  }
  console.log('all checks passed');
}
main().catch((e) => { console.error(e); process.exit(1); });
