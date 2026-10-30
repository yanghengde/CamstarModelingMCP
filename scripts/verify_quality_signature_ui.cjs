// Isolated UI acceptance. Use scripts/preview_quality_signature.py, never a real signature request.
const {chromium}=require('playwright');
const fs=require('fs');
const assert=require('assert');
(async()=>{
  const url=process.env.SIGNATURE_PREVIEW_URL;
  assert(url && /^http:\/\/127\.0\.0\.1:\d+\/quality-signature#/.test(url));
  const browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL||'msedge'});
  const page=await browser.newPage({viewport:{width:1100,height:1000}}),errors=[],external=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('request',r=>{if(!r.url().startsWith(new URL(url).origin+'/'))external.push('unexpected_external_request');});
  try{
    await page.goto(url);await page.waitForFunction(()=>!document.getElementById('save').disabled);
    assert.equal(new URL(page.url()).hash,'');assert((await page.locator('#details').innerText()).includes('测试员工'));
    fs.mkdirSync('docs/quality-fixtures',{recursive:true});
    const multi=(await page.locator('#participants input[type=password]').count())>0,suffix=multi?'-multi':'';
    await page.screenshot({path:`docs/quality-fixtures/signature-input${suffix}-desktop.png`,fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.screenshot({path:`docs/quality-fixtures/signature-input${suffix}-mobile.png`,fullPage:true});
    await page.check('#approved');
    if(multi){
      assert.equal(await page.locator('#participants input[type=password]').count(),3);
      for(let n=0;n<3;n++){await page.fill('#password-'+n,'UI_DUMMY_PASSWORD_ONLY');await page.check('#confirm-'+n);}
    }else await page.fill('#password','UI_DUMMY_PASSWORD_ONLY');
    await page.click('#save');
    await page.waitForFunction(()=>document.getElementById('status').textContent.includes('返回聊天'));
    assert.equal(await page.inputValue('#password'),'');assert(await page.isDisabled('#password'));
    if(multi){for(let n=0;n<3;n++){assert.equal(await page.inputValue('#password-'+n),'');assert(await page.isDisabled('#password-'+n));}}
    await page.click('#cancel');await page.waitForFunction(()=>document.getElementById('status').textContent.includes('已取消'));
    assert.equal(errors.length,0);assert.equal(external.length,0);
    const report={checked:'local_dummy_page_only',checks:['desktop','mobile_no_overflow','fragment_removed','capture_without_chat','password_cleared','cancel'],
      browser_errors:errors,external_requests:external,business_write_performed:false};
    report.mode=multi?'multiple_signers_and_cosigner':'single_signer';
    fs.writeFileSync(`docs/quality-fixtures/approval-c2b${suffix}-ui.json`,JSON.stringify(report,null,2));console.log(JSON.stringify(report));
  }finally{await browser.close();}
})().catch(()=>{console.error('Signature UI validation failed; no credential output.');process.exit(1);});
