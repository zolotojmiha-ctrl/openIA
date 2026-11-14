const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const base=process.env.ARMADA_TEST_URL,id=process.env.ARMADA_COMPLETED_ID;
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']}),page=await browser.newPage();
 await page.goto(base+'/cabinet/login/');await page.locator('[name=phone]').fill('+70000000001');await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');
 await page.goto(base+'/cabinet/work/'+id+'/completed-import/');await page.locator('[name=file]').setInputFiles(process.env.ARMADA_COMPLETED_FILE);await page.getByRole('button',{name:'Проверить план',exact:true}).click();await page.waitForURL(/\/cabinet\/completed-import\//);
 for(const width of [320,390,1440]){await page.setViewportSize({width,height:900});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'preview overflow '+width);}
 assert.ok((await page.locator('main').innerText()).includes('Без известной даты: 1'));
 await page.screenshot({path:'/tmp/completed-2026-preview.png',fullPage:true});
 await page.locator('[name=confirm]').check();await page.getByRole('button',{name:'Добавить устранённые',exact:true}).click();await page.waitForURL(/\/cabinet\/work\/contracts\//);
 await page.goto(base+'/cabinet/repairs/?contract='+id);assert.equal(await page.locator('[data-repair-group]').count(),1);
 await page.locator('.repair-counters').getByRole('link',{name:/Устранённые/}).click();assert.equal(await page.locator('[data-repair-group]').count(),2);
 await page.goto(base+'/cabinet/repairs/completed/?contract='+id);assert.ok((await page.locator('main').innerText()).includes('Не указана'));
 await browser.close();console.log('PASS completed import preview, confirmation, current contract active, default active tab, completed tab and unknown dates.');
})().catch(e=>{console.error(e);process.exit(1)});
