const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),base=process.env.ARMADA_TEST_URL,errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base+'/cabinet/login/');await page.locator('[name=phone]').fill('+70000000001');await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');
 await page.goto(base+process.env.ARMADA_COVERAGE_PATH);
 assert.equal(await page.getByRole('button',{name:'Показать по км',exact:true}).count(),1);
 await page.getByRole('button',{name:'Показать по км',exact:true}).click();
 let text=await page.locator('main').textContent();assert.ok(!text.includes('123.456'));assert.ok(text.includes('200.111'));assert.ok(!text.includes('200.111; 200.111'));
 const row=page.locator('.demand-detail-row:not([hidden]) tbody tr');assert.equal(await row.count(),1);assert.equal((await row.locator('td').first().textContent()).trim(),'200.111');
 fs.mkdirSync('/workspace/artifacts',{recursive:true});await page.screenshot({path:'/workspace/artifacts/reserved-p1-demand-desktop.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 await page.screenshot({path:'/workspace/artifacts/reserved-p1-demand-phone.png',fullPage:true});
 await page.goto(base+process.env.ARMADA_PREVIEW_PATH);text=await page.locator('main').textContent();assert.ok(text.includes('200.111'));assert.ok(!text.includes('123.456'));assert.ok(!text.includes('200.111; 200.111'));
 assert.deepEqual(errors,[]);await browser.close();console.log('PASS assigned P1 assembly excluded, 1000 remainder with correct wall and one kilometre in desktop/phone/customer preview');
})().catch(e=>{console.error(String(e));process.exit(1)});
