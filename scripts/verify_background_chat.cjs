// Starts an isolated local server with fake replies. No LLM or MES execution.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {spawn} = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const net = require('net');
const assert = require('assert');

(async () => {
  const root = path.resolve(__dirname, '..');
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'camstar-chat-browser-'));
  const port = await new Promise(resolve => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); }); });
  const base = `http://127.0.0.1:${port}`;
  const python = process.env.CHAT_TEST_PYTHON || path.join(root, 'venv', 'Scripts', 'python.exe');
  const server = spawn(python, [path.join(__dirname, 'preview_background_chat.py'), '--port', String(port)], {
    cwd:root, env:{...process.env, CHAT_PREVIEW_ROOT:temp}, windowsHide:true, stdio:['ignore','pipe','pipe']
  });
  let logs = ''; server.stderr.on('data', data => { logs = (logs + data).slice(-6000); });
  server.stdout.on('data', data => { logs = (logs + data).slice(-6000); });
  const report = {checks:[], business_writes_executed:false, browser_errors:[]};
  let browser;
  try {
    for (let i = 0; i < 150; i++) {
      try { if ((await fetch(base + '/config')).ok) break; } catch (_) {}
      if (server.exitCode !== null) throw new Error(logs);
      await new Promise(r => setTimeout(r, 300));
    }
    browser = await chromium.launch({headless:true, channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror', error => report.browser_errors.push(error.message));
    await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    const api = async url => (await page.request.get(base + url)).json();
    const sid = () => new URL(page.url()).searchParams.get('session_id');
    async function send(text) {
      await page.locator('#inputBox').waitFor({state:'visible'});
      await page.waitForFunction(() => !document.getElementById('inputBox').disabled);
      await page.fill('#inputBox', text);
      const response = page.waitForResponse(r => r.url() === base + '/api/chat/runs' && r.request().method() === 'POST');
      await page.click('#sendBtn');
      const result = await response; assert.equal(result.status(), 202, await result.text());
      return result.json();
    }
    async function finished(id, expected = 'completed') {
      await page.waitForFunction(async ({base,id,expected}) => (await (await fetch(`${base}/api/chat/runs/${id}?username=preview`)).json()).status === expected,
        {base,id,expected}, {timeout:25000});
    }
    async function newChat() {
      await page.click('#newChatTopBtn');
      await page.waitForFunction(() => !document.getElementById('inputBox').disabled);
    }
    await page.goto(base);
    const first = await send('后台连续执行');
    await page.waitForFunction(() => document.getElementById('msgs').textContent.includes('进度 01'));
    assert.equal(await page.locator('#newChatTopBtn').isEnabled(), true);
    await page.click('#recordsNav');
    assert.equal(await page.locator('#recordsView').isVisible(), true);
    await page.locator('a[href="/logs"]').click();
    await page.waitForURL('**/logs');
    assert.equal((await api(`/api/chat/runs/${first.id}?username=preview`)).status, 'running');
    await page.goto(`${base}/?session_id=${first.session_id}`);
    await page.waitForFunction(() => document.getElementById('msgs').textContent.includes('进度 01'));
    await page.reload();
    await page.waitForFunction(() => document.getElementById('msgs').textContent.includes('进度 01'));
    assert.equal((await api('/test/probe')).calls['后台连续执行'], 1);
    report.checks.push('切换记录/完整离开到日志页/返回/刷新均不重新执行');
    fs.mkdirSync(path.join(root, 'data', 'background-chat-ui'), {recursive:true});
    await page.screenshot({path:path.join(root, 'data/background-chat-ui/running.png'), fullPage:true});
    await newChat();
    const second = await send('第二个对话');
    assert.notEqual(first.session_id, second.session_id);
    await page.locator('.session-item').filter({hasText:'后台连续执行'}).click();
    await page.waitForFunction(id => new URL(location.href).searchParams.get('session_id') === id, first.session_id);
    await finished(first.id); await finished(second.id);
    await page.waitForFunction(() => document.getElementById('workflowStatus').textContent.includes('任务已完成'));
    assert.equal(await page.locator('.agent-bubble').filter({hasText:'后台演示已完成'}).count(), 1);
    assert.equal(((await page.locator('#msgs').innerText()).match(/进度 01/g) || []).length, 1);
    report.checks.push('执行中可新建/切换聊天；两个会话独立运行，回放无重复内容');
    await newChat();
    const stopped = await send('需要停止');
    await page.waitForFunction(() => document.getElementById('msgs').textContent.includes('进度 01'));
    await page.getByRole('button', {name:'停止执行', exact:true}).click();
    await finished(stopped.id, 'cancelled');
    const before = (await api('/test/probe')).ticks[stopped.session_id];
    await page.waitForTimeout(500);
    assert.equal((await api('/test/probe')).ticks[stopped.session_id], before);
    report.checks.push('只有明确点击停止执行才取消后台任务');
    await newChat();
    const approval = await send('审批演示');
    await page.locator('#approvalCard').waitFor({state:'visible'});
    await page.goto(base + '/logs');
    await page.goto(`${base}/?session_id=${approval.session_id}`);
    await page.locator('#approvalCard').waitFor({state:'visible'});
    assert.equal((await api('/test/probe')).calls['确认执行'] || 0, 0);
    assert.match(await page.locator('#approvalText').innerText(), /每 5 个抽检 1 个/);
    assert.equal(await page.locator('#approvalDetails').getAttribute('open'), null);
    await page.locator('#approvalDetails summary').click();
    assert.match(await page.locator('#approvalDetailsText').innerText(), /sampleRate=5/);
    assert.equal(await page.locator('#approvalDetailsText b').count(), 0);
    await page.locator('#approvalDetails summary').click();
    report.checks.push('确认卡片默认显示业务摘要，完整参数按需展开且按纯文本展示');
    const accepted = page.waitForResponse(r => r.url() === base + '/api/chat/runs' && r.request().method() === 'POST');
    await page.click('#approveBtn');
    const resumed = await (await accepted).json();
    await finished(resumed.id);
    assert.equal((await api('/test/probe')).calls['确认执行'], 1);
    report.checks.push('离开/返回保留待确认，确认前不自动继续');
    await newChat();
    let dropResponse = true;
    await page.route('**/api/chat/runs', async route => {
      if (dropResponse && route.request().method() === 'POST') {
        dropResponse = false;
        const accepted = await route.fetch();
        assert.equal(accepted.status(), 202);
        await route.abort('failed');
      } else await route.continue();
    });
    await page.fill('#inputBox', '提交断线演示');
    await page.click('#sendBtn');
    await page.waitForFunction(() => document.getElementById('msgs').textContent.includes('进度 01'));
    const recovered = await api(`/history/preview?session_id=${sid()}`);
    await finished(recovered.run.id);
    assert.equal((await api('/test/probe')).calls['提交断线演示'], 1);
    assert.equal(await page.locator('#inputBox').inputValue(), '');
    report.checks.push('提交成功但响应丢失时恢复原任务，不重复执行或保留误导性待发送内容');
    assert.deepEqual(report.browser_errors, []);
    await page.waitForFunction(() => document.getElementById('workflowStatus').textContent.includes('任务已完成') && document.getElementById('stopBtn').hidden);
    await page.setViewportSize({width:390,height:844});
    await page.waitForFunction(() => document.querySelector('.sidebar').getBoundingClientRect().right <= 1);
    assert.equal(await page.locator('#menuToggle').isVisible(), true);
    await page.screenshot({path:path.join(root, 'data/background-chat-ui/mobile.png'), fullPage:true});
    await page.setViewportSize({width:1440,height:1000});
    await newChat();
    await page.evaluate(() => {
      window.markdownPasses = 0;
      const original = renderMarkdown;
      renderMarkdown = (...args) => { window.markdownPasses++; return original(...args); };
    });
    const burst = await send('流式性能演示');
    await finished(burst.id);
    await page.waitForFunction(() => !document.getElementById('inputBox').disabled && document.getElementById('msgs').textContent.includes('后台演示已完成。'));
    assert.match(await page.locator('#msgs').innerText(), /进度 1000/);
    report.stream_markdown_passes = await page.evaluate(() => window.markdownPasses);
    assert.ok(report.stream_markdown_passes < 25, `Excessive renders: ${report.stream_markdown_passes}`);
    const unchangedList = await page.evaluate(async () => {
      await refreshSessions();
      const first = document.getElementById('sessionListUI').firstChild;
      await refreshSessions();
      return first === document.getElementById('sessionListUI').firstChild;
    });
    assert.ok(unchangedList, 'Unchanged session polling must preserve DOM nodes');
    report.checks.push('1000 个流式片段合并渲染且末尾完整；未变化的会话列表保留 DOM');
    assert.deepEqual(report.browser_errors, []);
    fs.writeFileSync(path.join(root, 'docs/background-chat-ui-validation.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report, null, 2));
  } catch (error) { console.error(logs); throw error; }
  finally { if (browser) await browser.close(); server.kill(); }
})().catch(error => { console.error(error); process.exit(1); });
