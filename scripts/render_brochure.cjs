#!/usr/bin/env node
'use strict';
// Static local HTML -> print PDF, with explicit layout/resource gates.
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');
const { pathToFileURL } = require('url');
const { execFileSync } = require('child_process');

function args(argv) {
  const result = { input: argv.shift() };
  for (let i = 0; i < argv.length; i++) {
    const key = argv[i];
    if (['--overwrite', '--allow-incomplete'].includes(key)) result[key.slice(2)] = true;
    else if (['--out', '--report', '--preview-dir', '--browser', '--python'].includes(key) && argv[i + 1] && !argv[i + 1].startsWith('--')) result[key.slice(2)] = argv[++i];
    else throw new Error(`Unknown or incomplete option: ${key}`);
  }
  if (!result.input || !result.out || !result.report) throw new Error('Usage: node render_brochure.cjs INPUT.html --out OUTPUT.pdf --report REPORT.json [--preview-dir DIR] [--browser PATH] [--python PATH] [--allow-incomplete] [--overwrite]');
  return result;
}
function same(a, b) {
  if (path.resolve(a) === path.resolve(b)) return true;
  if (fs.existsSync(a) && fs.existsSync(b)) {
    // NTFS file IDs can exceed Number.MAX_SAFE_INTEGER; Number would falsely
    // identify different files as aliases after rounding.
    const x = fs.statSync(a, { bigint: true }), y = fs.statSync(b, { bigint: true });
    return fs.realpathSync(a) === fs.realpathSync(b) || (x.ino !== 0n && x.ino === y.ino && x.dev === y.dev);
  }
  return false;
}
function target(file, inputs, overwrite) {
  if (inputs.some(input => same(file, input))) throw new Error(`Output must not overwrite an input or another output: ${file}`);
  if (fs.existsSync(file) && !overwrite) throw new Error(`Output already exists (use --overwrite): ${file}`);
  fs.mkdirSync(path.dirname(file), { recursive: true });
}
function tempFor(file) { return path.join(path.dirname(file), `.${path.basename(file)}.${crypto.randomUUID()}.tmp`); }
function atomicJSON(file, data, overwrite) {
  const temporary = tempFor(file);
  try {
    fs.writeFileSync(temporary, JSON.stringify(data, null, 2) + '\n', { flag: 'wx' });
    if (fs.existsSync(file) && !overwrite) throw new Error(`Output appeared during render: ${file}`);
    fs.renameSync(temporary, file);
  } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
}
function playwright() {
  const candidates = [process.env.BROCHURE_PLAYWRIGHT, 'playwright', path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright')].filter(Boolean);
  for (const name of candidates) {
    try { return require(name); } catch (error) { if (error.code !== 'MODULE_NOT_FOUND') throw error; }
  }
  throw new Error('Playwright is unavailable. Install playwright locally, or set BROCHURE_PLAYWRIGHT to its module directory.');
}
function executable(specified) {
  if (specified || process.env.BROCHURE_BROWSER) {
    const candidate = specified || process.env.BROCHURE_BROWSER;
    if (!fs.existsSync(candidate)) throw new Error(`Browser not found: ${candidate}`);
    return candidate;
  }
  return [
    process.env.ProgramFiles && path.join(process.env.ProgramFiles, 'Google/Chrome/Application/chrome.exe'),
    process.env['ProgramFiles(x86)'] && path.join(process.env['ProgramFiles(x86)'], 'Microsoft/Edge/Application/msedge.exe'),
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
  ].find(candidate => candidate && fs.existsSync(candidate));
}

async function run(options) {
  const input = path.resolve(options.input), output = path.resolve(options.out), reportPath = path.resolve(options.report);
  if (!fs.statSync(input).isFile()) throw new Error(`Not a file: ${input}`);
  target(output, [input, reportPath], options.overwrite);
  target(reportPath, [input, output], options.overwrite);
  let previewDir;
  if (options['preview-dir']) {
    previewDir = path.resolve(options['preview-dir']);
    if ([input, output, reportPath].some(file => same(previewDir, file))) throw new Error('Preview directory collides with an input/output');
    if (fs.existsSync(previewDir) && fs.readdirSync(previewDir).length && !options.overwrite) throw new Error('Preview directory is not empty (use a new directory or --overwrite)');
    fs.mkdirSync(previewDir, { recursive: true });
  }
  const report = {
    source: input, source_sha256: crypto.createHash('sha256').update(fs.readFileSync(input)).digest('hex'),
    output, static_html: true, scripts_executed: false, remote_requests: [], failed_requests: [], pages: [],
    missing_image_count: 0, placeholder_count: 0, overflow_count: 0, spacing_warning_count: 0, exported: false,
    visual_qa_passed: null, preview_type: 'HTML print-media screenshots; final PDF needs separate rendering and visual review',
    limitations: ['Geometry checks do not prove wording, image semantics, font fidelity, print color, or final visual acceptance.'],
  };
  let browser, temporary;
  try {
    const { chromium } = playwright();
    browser = await chromium.launch({ headless: true, executablePath: executable(options.browser) });
    report.browser_version = browser.version();
    const context = await browser.newContext({ viewport: { width: 1000, height: 1200 }, deviceScaleFactor: 1, javaScriptEnabled: false });
    await context.route('**/*', async route => {
      const url = route.request().url();
      if (/^https?:/i.test(url)) { report.remote_requests.push(url); await route.abort(); }
      else await route.continue();
    });
    const page = await context.newPage();
    page.on('requestfailed', request => report.failed_requests.push({ url: request.url(), reason: request.failure()?.errorText }));
    await page.emulateMedia({ media: 'print' });
    await page.goto(pathToFileURL(input).href, { waitUntil: 'load', timeout: 45000 });
    await page.evaluate(async () => {
      await document.fonts.ready;
      await Promise.all(Array.from(document.images, image => image.decode().catch(() => {})));
    });
    report.pages = await page.evaluate(() => Array.from(document.querySelectorAll('section.page')).map((element, index) => {
      const bounds = element.getBoundingClientRect(), main = element.querySelector('main'), paper = element.querySelector('.paper'), footer = element.querySelector('footer');
      const footerTop = footer ? footer.getBoundingClientRect().top : bounds.bottom;
      const paperBounds = paper?.getBoundingClientRect();
      const paperStyle = paper && getComputedStyle(paper);
      const bottomLimit = Math.min(bounds.bottom, footerTop - (footer ? 4 : 0), paperBounds ? paperBounds.bottom : bounds.bottom);
      const preferredBottom = paperBounds ? Math.min(bottomLimit, paperBounds.bottom - parseFloat(paperStyle.paddingBottom || '0')) : bottomLimit;
      const content = main || element;
      const items = Array.from(new Set([element, paper, content, ...content.querySelectorAll('*')])).filter(item => item && !item.closest('svg, .kv-hero') && getComputedStyle(item).display !== 'none');
      const violations = [], spacingWarnings = [];
      for (const item of items) {
        const rect = item.getBoundingClientRect(), style = getComputedStyle(item);
        if (!rect.width && !rect.height) continue;
        const name = `${item.tagName.toLowerCase()}${item.className && typeof item.className === 'string' ? '.' + item.className.trim().replace(/\s+/g, '.') : ''}`;
        const problems = [];
        if (main && (item === main || main.contains(item)) && rect.bottom > bottomLimit + 2) problems.push('content exceeds paper/footer boundary');
        else if (main && (item === main || main.contains(item)) && rect.bottom > preferredBottom + 2) spacingWarnings.push({ element: name, text: (item.innerText || '').slice(0, 100), reason: 'content enters paper bottom padding without crossing the paper/footer boundary', intrusion_px: rect.bottom - preferredBottom });
        if (rect.left < bounds.left - 2 || rect.right > bounds.right + 2) problems.push('outside page horizontally');
        if (rect.bottom > bounds.bottom + 2 || rect.top < bounds.top - 2) problems.push('outside page vertically');
        if (item.clientWidth > 0 && item.scrollWidth > item.clientWidth + 2 && !['IMG', 'SVG'].includes(item.tagName)) problems.push('horizontal content overflow');
        if (item.clientHeight > 0 && item.scrollHeight > item.clientHeight + 2 && ['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowY)) problems.push('clipped or scrolling content');
        if (problems.length) violations.push({ element: name, text: (item.innerText || '').slice(0, 100), problems, bounds: { left: rect.left - bounds.left, top: rect.top - bounds.top, width: rect.width, height: rect.height } });
      }
      const images = Array.from(element.querySelectorAll('img')).map(image => ({ alt: image.alt, width_px: image.naturalWidth, height_px: image.naturalHeight, loaded: image.complete && image.naturalWidth > 0, fit: getComputedStyle(image).objectFit, position: getComputedStyle(image).objectPosition }));
      // Inventory the two supported layouts. Unknown day labels stay explicit;
      // finding a flight does not certify its business date or placement.
      const flights = Array.from(element.querySelectorAll('.flight-strip,.flight-callout')).map(flight => {
        const day = flight.closest('.day'), label = day?.querySelector('.dayhead')?.innerText || '';
        const match = label.match(/^\s*(?:D(?:ay)?\s*(\d+)\b|第\s*(\d+)\s*天|(\d+)(?:\s|$))/i);
        const explicit = day?.dataset.day;
        const inferred = match && (match[1] || match[2] || match[3]);
        return { day: explicit || inferred || null, day_source: explicit ? 'data-day' : inferred ? 'heading-label' : 'unresolved', placement: day ? 'inside-day' : 'unassigned', flight: flight.innerText };
      });
      return { page: index + 1, id: element.dataset.pageId || null, width_mm: bounds.width * 25.4 / 96, height_mm: bounds.height * 25.4 / 96,
        broken_images: images.filter(image => !image.loaded), images, placeholders: element.querySelectorAll('.image-placeholder,.photo-placeholder,[data-placeholder="true"]').length,
        violations, spacing_warnings: spacingWarnings, flight_days: flights };
    }));
    if (!report.pages.length) throw new Error('No section.page elements found; this renderer requires an explicit page layout.');
    report.page_count = report.pages.length;
    report.missing_image_count = report.pages.reduce((n, p) => n + p.broken_images.length, 0);
    report.placeholder_count = report.pages.reduce((n, p) => n + p.placeholders, 0);
    report.overflow_count = report.pages.reduce((n, p) => n + p.violations.length, 0);
    report.spacing_warning_count = report.pages.reduce((n, p) => n + p.spacing_warnings.length, 0);
    if (previewDir) {
      for (let i = 0; i < report.pages.length; i++) await page.locator('section.page').nth(i).screenshot({ path: path.join(previewDir, `page_${String(i + 1).padStart(2, '0')}.png`), timeout: 30000 });
    }
    const failures = [];
    if (report.remote_requests.length || report.failed_requests.length) failures.push('external or failed resources');
    if (report.missing_image_count) failures.push(`${report.missing_image_count} broken images`);
    if (report.overflow_count) failures.push(`${report.overflow_count} layout violations`);
    if (report.placeholder_count && !options['allow-incomplete']) failures.push(`${report.placeholder_count} unfilled image placeholders (only review drafts may use --allow-incomplete)`);
    if (failures.length) throw new Error(`Export stopped: ${failures.join('; ')}. See QA report.`);
    temporary = tempFor(output);
    await page.pdf({ path: temporary, format: 'A4', preferCSSPageSize: true, printBackground: true, displayHeaderFooter: false });
    const python = options.python || process.env.BROCHURE_PYTHON || 'python';
    const inspection = JSON.parse(execFileSync(python, ['-X', 'utf8', '-c', 'import fitz,json,sys; d=fitz.open(sys.argv[1]); print(json.dumps({"page_count":len(d),"pages":[{"width_mm":p.rect.width*25.4/72,"height_mm":p.rect.height*25.4/72} for p in d]}))', temporary], { encoding: 'utf8', windowsHide: true }));
    report.pdf = inspection;
    if (inspection.page_count !== report.page_count) throw new Error(`Print created ${inspection.page_count} PDF pages for ${report.page_count} HTML pages. Check CSS page breaks.`);
    for (let i = 0; i < inspection.page_count; i++) {
      for (const key of ['width_mm', 'height_mm']) if (Math.abs(inspection.pages[i][key] - report.pages[i][key]) > 0.8) throw new Error(`PDF page ${i + 1} ${key} differs from HTML by over 0.8 mm. Check named @page size.`);
    }
    report.output_sha256 = crypto.createHash('sha256').update(fs.readFileSync(temporary)).digest('hex');
    report.draft_with_placeholders = report.placeholder_count > 0;
    target(output, [input, reportPath], options.overwrite);
    fs.renameSync(temporary, output); temporary = null;
    report.exported = true;
  } catch (error) {
    report.error = error.message;
    throw error;
  } finally {
    if (temporary && fs.existsSync(temporary)) fs.unlinkSync(temporary);
    try { if (browser) await browser.close(); }
    finally {
      try { atomicJSON(reportPath, report, options.overwrite); }
      catch (error) { throw new Error(`${report.exported ? 'PDF was exported, but QA report publication failed' : 'QA report publication failed'}: ${error.message}`); }
    }
  }
  console.log(JSON.stringify({ output, page_count: report.page_count, draft_with_placeholders: report.draft_with_placeholders, spacing_warning_count: report.spacing_warning_count, report: reportPath }));
}
if (process.argv.includes('--help')) {
  console.log('node render_brochure.cjs INPUT.html --out OUTPUT.pdf --report REPORT.json [--preview-dir DIR] [--browser PATH] [--python PATH] [--allow-incomplete] [--overwrite]\nRequires Playwright, a Chromium browser, Python and PyMuPDF. Environment: BROCHURE_PLAYWRIGHT, BROCHURE_BROWSER, BROCHURE_PYTHON.');
} else {
  Promise.resolve().then(() => run(args(process.argv.slice(2)))).catch(error => { console.error(error.message); process.exitCode = 1; });
}
