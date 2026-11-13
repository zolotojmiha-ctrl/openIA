const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']}),page=await browser.newPage({viewport:{width:1440,height:1000}}),base=process.env.ARMADA_TEST_URL,errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 async function login(phone){await page.goto(base+'/cabinet/login/');await page.locator('[name=phone]').fill(phone);await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');}
 await login('+70000000001');
 await page.goto(base+process.env.ARMADA_EMPLOYEE_PATH);await page.locator('[name=permissions][value="repairs.overview"]').check();await page.getByRole('button',{name:'Сохранить настройки',exact:true}).click();await page.waitForLoadState('networkidle');
 assert.equal(await page.locator('.errorlist').count(),0);
 await page.goto(base+process.env.ARMADA_ASSIGNMENTS_PATH);await page.locator('[name=employees][value="'+process.env.ARMADA_WORKER_ID+'"]').check();await page.getByRole('button',{name:'Сохранить назначения',exact:true}).click();await page.waitForLoadState('networkidle');
 await page.getByRole('button',{name:'Выйти',exact:true}).click();await page.waitForURL('**/cabinet/login/');await page.waitForLoadState('networkidle');await login('+70000000003');
 await page.getByRole('link',{name:/Все дефекты доступных участков/}).click();await page.waitForURL('**/repairs/');
 await page.locator('[name=q]').fill('135.651');await page.getByRole('button',{name:'Найти',exact:true}).click();await page.waitForLoadState('networkidle');
 assert.ok((await page.locator('main').textContent()).includes('999999'));
 assert.equal(await page.getByRole('link',{name:'Внести результат',exact:true}).count(),0);
 fs.mkdirSync('/workspace/artifacts',{recursive:true});
 for(const width of [320,390,768,1440]){await page.setViewportSize({width,height:950});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'width '+width);if(width===390)await page.screenshot({path:'/workspace/artifacts/repair-overview-phone.png',fullPage:true});}
 const response=await page.goto(base+process.env.ARMADA_OTHER_JOB_PATH);assert.equal(response.status(),200);
 const denied=await page.goto(base+process.env.ARMADA_OTHER_START_PATH);assert.equal(denied.status(),404);
 assert.deepEqual(errors,[]);await browser.close();console.log('PASS administrator grants overview and missing object; master sees other assignee repair without reporting; four viewport widths');
})().catch(e=>{console.error(String(e));process.exit(1)});
