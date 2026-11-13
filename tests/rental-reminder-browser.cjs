const {chromium}=require('playwright'),assert=require('node:assert/strict');
let browser;
(async()=>{
 browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:390,height:900}}),base=process.env.ARMADA_TEST_URL;
 const errors=[];page.on('pageerror',e=>errors.push(e.name));
 page.on('response',r=>{if(r.status()>=500)errors.push('HTTP '+r.status());});
 await page.goto(base+'/cabinet/login/');
 await page.locator('[name=phone]').fill(process.env.ARMADA_TEST_PHONE);
 await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);
 await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');
 await page.goto(base+process.env.ARMADA_REMINDER_PATH);
 const form=page.locator('#rental-day-form');
 assert.equal(await form.locator('[name=date]').inputValue(),process.env.ARMADA_TEST_DATE);
 assert.equal(await page.getByText('Взаиморасчёты',{exact:true}).count(),0);
 for(const width of [320,390,1440]){
  await page.setViewportSize({width,height:900});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 }
 await form.locator('button[name=kind][value=work]').click();
 await page.waitForLoadState('networkidle');
 assert.equal(await page.locator('.errorlist').count(),0);
 assert.deepEqual(errors,[]);await browser.close();
 console.log('PASS reminder opens today, saves actual hours, phone and desktop');
})().catch(async error=>{console.error('Reminder browser:',error.name);if(browser)await browser.close();process.exitCode=1;});
