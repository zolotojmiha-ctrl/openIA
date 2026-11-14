const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const base=process.env.ARMADA_TEST_URL,id=process.env.ARMADA_ARCHIVE_ID;
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']}),page=await browser.newPage(),errors=[];
 page.on('pageerror',e=>errors.push(e.name));
 await page.goto(base+'/cabinet/login/');await page.locator('[name=phone]').fill('+70000000001');await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');
 await page.goto(base+'/cabinet/repairs/archive/import/'+id+'/');await page.locator('[name=file]').setInputFiles(process.env.ARMADA_ARCHIVE_FILE);await page.getByRole('button',{name:'Проверить план',exact:true}).click();await page.waitForURL(/\/archive\/review\//);
 for(const width of [320,390,1440]){await page.setViewportSize({width,height:900});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'archive preview '+width);}
 assert.ok((await page.locator('main').textContent()).includes('Не указана'));await page.locator('[name=confirm]').check();await page.getByRole('button',{name:'Подтвердить перенос в архив',exact:true}).click();await page.waitForURL(/\/cabinet\/work\/contracts\//);
 await page.goto(base+'/cabinet/repairs/?contract='+id);assert.equal(await page.locator('.repair-counters .active').innerText(),'Устранённые 2');assert.equal(await page.locator('[data-repair-group]').count(),2);
 await page.locator('.repair-counters').getByRole('link',{name:/Не устранены/}).click();assert.equal(await page.locator('[data-repair-group]').count(),0);
 await page.goto(base+'/cabinet/repairs/completed/?contract='+id);assert.ok((await page.locator('main').textContent()).includes('Не указана'));assert.deepEqual(errors,[]);
 await browser.close();console.log('PASS archive browser upload, confirmation, unknown date, scoped completed/default tabs and mobile widths');
})().catch(e=>{console.error(e);process.exit(1)});
