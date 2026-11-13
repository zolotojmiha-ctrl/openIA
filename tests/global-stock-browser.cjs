const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),base=process.env.ARMADA_TEST_URL,errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base+'/cabinet/login/');await page.locator('[name=phone]').fill('+70000000001');await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');
 await page.goto(base+process.env.ARMADA_COVERAGE_PATH);
 await page.locator('summary').filter({hasText:'Подбор из наличия'}).click();
 const panel=page.locator('details').filter({has:page.locator('button').filter({hasText:'Закрепить показанный подбор'})});
 const text=await panel.textContent();for(const value of ['100','102','1500','1000','2000'])assert.ok(text.includes(value));
 assert.equal(await panel.locator('tbody tr').count(),2);
 fs.mkdirSync('/workspace/artifacts',{recursive:true});
 for(const width of [320,390,768,1440]){
  await page.setViewportSize({width,height:900});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'viewport '+width);
  if(width===390||width===1440)await page.screenshot({path:'/workspace/artifacts/global-stock-'+width+'.png',fullPage:true});
 }
 await page.getByRole('button',{name:'Закрепить показанный подбор',exact:true}).click();
 await page.waitForLoadState('networkidle');assert.ok((await page.locator('main').textContent()).includes('Комплекты закреплены за работами: 2'));
 assert.equal(await page.getByRole('button',{name:'Закрепить показанный подбор',exact:true}).count(),0);
 assert.deepEqual(errors,[]);await browser.close();console.log('PASS global stock preview and explicit approval at four viewport widths');
})().catch(e=>{console.error(String(e));process.exit(1)});
