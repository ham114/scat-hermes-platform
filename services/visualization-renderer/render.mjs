// Trusted host: generated JavaScript runs only as a browser module, never in Node.
import fs from 'node:fs/promises';
import { chromium } from 'playwright';
import { PNG } from 'pngjs';

const input = JSON.parse(await fs.readFile('/input/request.json', 'utf8'));
const sizes = { desktop: { width: 1200, height: 720 }, mobile: { width: 390, height: 640 } };
const results = [];
let browser;
try {
  if (!Array.isArray(input.viewports) || input.viewports.length < 1 || input.viewports.length > 2)
    throw new Error('invalid_viewports');
  browser = await chromium.launch({ chromiumSandbox: true, headless: true,
    args: ['--disable-dev-shm-usage', '--disable-background-networking'] });
  for (const viewport of input.viewports) {
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
    await page.evaluate(async ({ source, data }) => {
      Math.random = (() => { let seed = 123456; return () => ((seed = (seed * 16807) % 2147483647) / 2147483647); })();
      const originalInit = echarts.init.bind(echarts);
      echarts.init = (...args) => {
        const chart = originalInit(...args);
        const originalSet = chart.setOption.bind(chart);
        chart.setOption = (option, ...rest) => originalSet({ ...option, animation: false }, ...rest);
        return chart;
      };
      const moduleURL = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));
      try {
        const module = await import(moduleURL);
        if (typeof module.render !== 'function') throw new Error('render_export_required');
        await module.render({ echarts, container: document.getElementById('chart'), data,
          metadata: { viewport: { width: innerWidth, height: innerHeight }, locale: 'zh-CN', timezone: 'UTC' } });
        await document.fonts.ready;
      } finally { URL.revokeObjectURL(moduleURL); }
    }, { source: input.source, data: input.snapshot });
    await page.waitForTimeout(100);
    if (pageErrors) throw new Error('browser_execution_error');
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
  await fs.writeFile('/output/manifest.json', JSON.stringify({ schema_version: 1, state: 'failed', error: 'render_execution_failed' }));
  process.exitCode = 1;
} finally { if (browser) await browser.close(); }
