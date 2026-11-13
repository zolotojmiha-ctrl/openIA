const {chromium}=require('playwright'),assert=require('node:assert/strict');
let browser;
(async()=>{
 browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:390,height:900}}),base=process.env.ARMADA_TEST_URL;
 const errors=[];page.on('pageerror',e=>errors.push(e.name));page.on('response',r=>{if(r.status()>=500)errors.push('HTTP '+r.status());});
 await page.goto(base+'/cabinet/login/');await page.locator('[name=phone]').fill(process.env.ARMADA_TEST_PHONE);
 await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);
 await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');
 await page.goto(base+'/cabinet/repairs/');await page.getByRole('link',{name:'Ведомость выполнения',exact:true}).click();
 await page.waitForURL('**/cabinet/repairs/completed/**');await page.locator('.completion-table').waitFor();
 assert.equal(await page.locator('.completion-table tbody tr').count(),2);
 assert.equal(await page.locator('.completion-table td[rowspan="2"]').count()>1,true);
 for(const width of [320,390,1440]){
  await page.setViewportSize({width,height:900});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.equal(await page.locator('.completion-scroll').evaluate(e=>e.scrollWidth>e.clientWidth),true);
 }
 await page.setViewportSize({width:1440,height:1000});
 await page.locator('[name=start]').fill(process.env.ARMADA_TEST_DATE);
 await page.getByRole('button',{name:'Показать',exact:true}).click();await page.waitForLoadState('networkidle');
 assert.equal(await page.getByText('Нет устранённых дефектов по выбранным фильтрам.',{exact:true}).count(),1);
 await page.getByRole('link',{name:'Сбросить фильтры',exact:true}).click();await page.waitForLoadState('networkidle');
 await page.locator('.completion-scope summary').first().click();await page.locator('[name=contract]').check();
 await page.locator('[name=master]').selectOption(process.env.ARMADA_TEST_MASTER);
 await page.getByRole('button',{name:'Показать',exact:true}).click();await page.waitForLoadState('networkidle');
 const before=page.url();
 const download=page.waitForEvent('download');await page.getByRole('link',{name:'↓ Скачать Excel',exact:true}).click();
 await (await download).saveAs(process.env.ARMADA_TEST_XLSX);
 await page.locator('.completion-table').getByRole('link',{name:'Открыть',exact:true}).click();await page.waitForLoadState('networkidle');
 await page.getByRole('link',{name:'← К списку дефектов',exact:true}).click();await page.waitForLoadState('networkidle');
 assert.equal(page.url(),before);assert.deepEqual(errors,[]);
 await page.screenshot({path:process.env.ARMADA_TEST_SCREENSHOT,fullPage:true});
 await browser.close();console.log('PASS PTO actual register: table, common construction, mobile scroll, filters, download, return');
})().catch(async e=>{console.error('PTO register browser:',e.name,e.message);if(browser)await browser.close();process.exitCode=1;});
