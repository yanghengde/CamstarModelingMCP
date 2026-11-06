// Read-only navigation timing against the running service; never sends a chat.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');
const path = require('path');

(async () => {
  const browser = await chromium.launch({headless:true, channel:'msedge'});
  const results = [], errors = [], external = [];
  try {
    const page = await browser.newPage();
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (!['localhost','127.0.0.1'].includes(new URL(request.url()).hostname)) external.push(request.url());
    });
    for (const host of ['localhost', '127.0.0.1']) {
      for (const route of ['/logs', '/', '/records', '/logs']) {
        const started = Date.now();
        await page.goto(`http://${host}:8030${route}`, {waitUntil:'domcontentloaded'});
        if (route === '/logs') {
          await page.waitForFunction(() => !logsLoading && document.querySelector('.log-load-status'));
          if (!(await page.locator('.log-item').count())) throw new Error('No log records rendered');
        } else {
          await page.waitForFunction(() => initialized && !loading);
          if (!await page.evaluate(() => !!window.marked && !!window.DOMPurify)) throw new Error('Missing local Markdown dependencies');
        }
        results.push({host, route, usable_ms:Date.now() - started,
          timing:await page.evaluate(() => {
            const nav = performance.getEntriesByType('navigation')[0];
            return {before_request_ms:Math.round(nav.requestStart),
              server_response_ms:Math.round(nav.responseStart - nav.requestStart),
              dom_ready_ms:Math.round(nav.domContentLoadedEventEnd)};
          })});
      }
    }
    if (errors.length || external.length) throw new Error(JSON.stringify({errors, external}));
    const report = {results, browser_errors:errors, external_requests:external, chat_submitted:false};
    fs.writeFileSync(path.join(__dirname, '../docs/navigation-performance-validation.json'), JSON.stringify(report,null,2));
    console.log(JSON.stringify(report,null,2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
