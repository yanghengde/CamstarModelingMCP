// Read-only browser acceptance against an independently started preview server.
// Requires Playwright; optional WORKBENCH_URL defaults to port 8041.
const {chromium} = require('playwright');
const fs = require('fs');
const assert = require('assert');
(async () => {
  const browser = await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL||'msedge'});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  const report={checks:[],business_writes_executed:false};
  const base=process.env.WORKBENCH_URL||'http://127.0.0.1:8041';
  async function query(action,trigger){
    const response=page.waitForResponse(r=>r.url().endsWith('/api/workbench')&&r.request().postDataJSON()?.action===action,{timeout:180000});
    await trigger(); const r=await response;assert(r.ok(),await r.text());const data=await r.json();
    await page.waitForFunction(()=>!document.getElementById('feedback').textContent.includes('正在读取'),{timeout:180000});
    return data;
  }
  try {
    await page.goto(base+'/modeling');
    await page.fill('#routeName','WF-WM-900A');await page.fill('#routeRevision','A');
    const route=await query('inspect',()=>page.locator('#routeForm button').click());
    assert(!route.error,JSON.stringify(route));assert(await page.locator('svg g[role=button]').count()>0);
    await page.locator('svg g[role=button]').first().click();assert(await page.locator('#detail').isVisible());await page.click('#closeDetail');
    fs.mkdirSync('data/workbench-preview',{recursive:true});await page.screenshot({path:'data/workbench-preview/workbench-route.png',fullPage:true});
    report.checks.push({feature:'route_and_integrity',read_complete:route.read_complete,issues:route.issues.length});
    await page.click('[data-tab=compare]');
    for(const [id,value]of Object.entries({leftName:'WF-WM-900A',leftRevision:'A',rightName:'WF-WM-900A',rightRevision:'A'}))await page.fill('#'+id,value);
    const comparison=await query('compare',()=>page.click('button[value=compare]'));assert(!comparison.error);assert.equal(comparison.changes.length,0);
    report.checks.push({feature:'version_comparison',same_revision_changes:0});
    await page.fill('#rightRevision','UI-PREVIEW-ONLY');
    const copy=await query('copy',()=>page.click('button[value=copy]'));
    report.checks.push({feature:'copy_preview',executable:copy.executable,error:copy.error||null});
    if(copy.executable)assert(await page.locator('#result a[href*="draft="]').count()===1);
    if(copy.executable){
      await page.locator('#result summary').click();
      await page.getByRole('textbox',{name:'step_name 1',exact:true}).fill('010-EDITED-PREVIEW');
      assert.equal(await page.locator('#result a[href*="draft="]').count(),0);
      const edited=await query('preview',()=>page.getByRole('button',{name:'重新校验并生成计划',exact:true}).click());
      assert(edited.executable,JSON.stringify(edited));
      assert.equal(edited.plan.workflows[0].after_rows[0].step_name,'010-EDITED-PREVIEW');
      assert.notEqual(edited.plan_id,copy.plan_id);
      report.checks.push({feature:'edited_route_revalidation',new_plan:true,old_link_disabled:true});
    }
    await page.click('[data-tab=import]');
    const first=Object.values(route.structure.workflows)[0].steps.find(s=>s.spec?.Name);
    const csv=`workflow_name,workflow_revision,step_name,spec_name,spec_revision\nUI-PREVIEW-ONLY,A,010,${first.spec.Name},${first.spec.Revision}\n`;
    await query('sheets',()=>page.setInputFiles('#importFile',{name:'preview.csv',mimeType:'text/csv',buffer:Buffer.from(csv)}));
    const plan=await query('preview',()=>page.click('#previewImport'));assert(plan.executable,JSON.stringify(plan));
    assert(await page.locator('#result a[href*="draft="]').count()===1);report.checks.push({feature:'csv_mapping_and_plan',executable:true});
    await page.click('[data-tab=order]');await page.fill('#orderName','MO-100076435');
    const order=await query('order',()=>page.locator('#orderForm button').click());assert(!order.error);assert.equal(order.completion_percentage,null);report.checks.push({feature:'order',containers:order.containers.length,has_more:order.page.has_more});
    if(order.page.has_more){const next=await query('order',()=>page.getByRole('button',{name:'下一页',exact:true}).click());assert.equal(next.page.offset,10);report.checks.push({feature:'order_pagination',offset:next.page.offset});}
    await page.screenshot({path:'data/workbench-preview/workbench-order.png',fullPage:true});
    await page.click('[data-tab=trace]');await page.fill('#traceName','BOX-0001');
    const traceResponse=page.waitForResponse(r=>r.url().endsWith('/api/workbench')&&r.request().postDataJSON()?.action==='genealogy',{timeout:180000});
    const hist=await query('history',()=>page.locator('#traceForm button').click());const trace=await(await traceResponse).json();assert(!trace.error);assert(!hist.error);report.checks.push({feature:'trace_and_history',nodes:Object.keys(trace.nodes).length,histories:hist.data.Histories.length});
    await page.screenshot({path:'data/workbench-preview/workbench-trace.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});await page.screenshot({path:'data/workbench-preview/workbench-mobile.png',fullPage:true});
    assert.equal(errors.length,0,errors.join('\n'));
    report.browser_errors=errors;
    fs.writeFileSync('docs/workbench-ui-validation.json',JSON.stringify(report,null,2));
    console.log(JSON.stringify(report,null,2));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
