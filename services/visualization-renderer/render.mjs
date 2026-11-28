// Trusted host: generated JavaScript runs only as a browser module, never in Node.
import fs from 'node:fs/promises';
import { chromium } from 'playwright';
import { PNG } from 'pngjs';

const input = JSON.parse(await fs.readFile('/input/request.json', 'utf8'));
const sizes = { desktop: { width: 1200, height: 720 }, mobile: { width: 390, height: 640 } };
const results = [];
let browser;
let diagnostic;
let stage = 'browser_launch';
let activeViewport;
try {
  if (!Array.isArray(input.viewports) || input.viewports.length < 1 || input.viewports.length > 2)
    throw new Error('invalid_viewports');
  browser = await chromium.launch({ chromiumSandbox: true, headless: true,
    args: ['--disable-dev-shm-usage', '--disable-background-networking'] });
  for (const viewport of input.viewports) {
    activeViewport = viewport;
    stage = 'browser_setup';
    if (!sizes[viewport]) throw new Error('invalid_viewport');
    const context = await browser.newContext({ viewport: sizes[viewport], deviceScaleFactor: 1,
      locale: 'zh-CN', timezoneId: 'UTC', offline: true, serviceWorkers: 'block', acceptDownloads: false });
    await context.route('**/*', route => route.abort());
    const page = await context.newPage();
    let pageErrors = 0;
    page.on('pageerror', () => { pageErrors += 1; });
    page.on('dialog', dialog => dialog.dismiss());
    await page.setContent(`<html><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' blob:; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; font-src data:"><style>html,body{margin:0;background:#fff;width:100%;height:100%;font-family:'WenQuanYi Zen Hei',sans-serif}#chart{width:100%;height:100%;overflow:hidden}</style></head><body><div id="chart"></div></body></html>`);
    await page.addScriptTag({ path: '/opt/renderer/node_modules/echarts/dist/echarts.min.js' });
    stage = 'generated_code';
    const execution = await page.evaluate(async ({ source, data }) => {
      Math.random = (() => { let seed = 123456; return () => ((seed = (seed * 16807) % 2147483647) / 2147483647); })();
      const originalInit = echarts.init.bind(echarts);
      echarts.init = (...args) => {
        const chart = originalInit(...args);
        const originalSet = chart.setOption.bind(chart);
        chart.setOption = (option, ...rest) => originalSet({ ...option, animation: false }, ...rest);
        return chart;
      };
      const moduleURL = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));
      let phase = 'module_load';
      try {
        const module = await import(moduleURL);
        if (typeof module.render !== 'function') throw new Error('render_export_required');
        phase = 'render';
        await module.render({ echarts, container: document.getElementById('chart'), data,
          metadata: { viewport: { width: innerWidth, height: innerHeight }, locale: 'zh-CN', timezone: 'UTC' } });
        await document.fonts.ready;
        return { ok: true };
      } catch (error) {
        // Generated errors are untrusted: never return raw messages, stacks or data values.
        const types = ['Error', 'TypeError', 'ReferenceError', 'SyntaxError', 'RangeError', 'URIError', 'EvalError'];
        const detail = { stage: phase, error_type: 'Error', message_code: 'execution_error' };
        try {
          if (types.includes(error?.name)) detail.error_type = error.name;
          const message = typeof error?.message === 'string' ? error.message.slice(0, 512) : '';
          const missing = /^Cannot read properties of (undefined|null) \(reading '([A-Za-z_$][\w$]{0,63})'\)$/.exec(message);
          const identifier = /^([A-Za-z_$][\w$]{0,63}) is not defined$/.exec(message);
          if (detail.error_type === 'TypeError' && missing) {
            Object.assign(detail, { message_code: 'missing_property', receiver: missing[1], property: missing[2] });
          } else if (detail.error_type === 'ReferenceError' && identifier) {
            Object.assign(detail, { message_code: 'undefined_identifier', property: identifier[1] });
          }
          const stack = typeof error?.stack === 'string' ? error.stack.slice(0, 8192) : '';
          const offset = stack.indexOf(moduleURL + ':');
          const location = offset < 0 ? null : /^(\d+):(\d+)/.exec(stack.slice(offset + moduleURL.length + 1));
          if (location && +location[1] <= source.split('\n').length) {
            detail.source_line = +location[1]; detail.source_column = +location[2];
          }
        } catch { /* Hostile thrown objects must not suppress the bounded receipt. */ }
        return { ok: false, diagnostic: detail };
      } finally { URL.revokeObjectURL(moduleURL); }
    }, { source: input.source, data: input.snapshot });
    if (!execution.ok) {
      diagnostic = { ...execution.diagnostic, viewport };
      throw new Error('generated_execution_failed');
    }
    stage = 'browser_execution';
    await page.waitForTimeout(100);
    if (pageErrors) throw new Error('browser_execution_error');
    stage = 'screenshot';
    const png = await page.screenshot({ path: `/output/${viewport}.png`, animations: 'disabled' });
    const decoded = PNG.sync.read(png);
    let ink = 0, minimum = 255, maximum = 0;
    for (let i = 0; i < decoded.data.length; i += 4) {
      const level = Math.min(...decoded.data.subarray(i, i + 3));
      if (level < 245) ink += 1;
      minimum = Math.min(minimum, level); maximum = Math.max(maximum, level);
    }
    results.push({ viewport, ...sizes[viewport], file: `${viewport}.png`,
      ink_ratio: ink / (decoded.width * decoded.height), contrast: maximum - minimum });
    await context.close();
  }
  await fs.writeFile('/output/manifest.json', JSON.stringify({ schema_version: 1, state: 'rendered', images: results }));
} catch {
  diagnostic ??= { stage, error_type: 'Error', message_code: 'execution_error', ...(activeViewport ? { viewport: activeViewport } : {}) };
  await fs.writeFile('/output/manifest.json', JSON.stringify({ schema_version: 1, state: 'failed', error: 'render_execution_failed', diagnostic }));
  process.exitCode = 1;
} finally { if (browser) await browser.close(); }
