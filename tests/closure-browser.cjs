const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']}),base=process.env.ARMADA_TEST_URL,path='/cabinet/contracts/'+process.env.ARMADA_CLOSURE_CONTRACT+'/',shots='/tmp/openia-66-showcase';fs.mkdirSync(shots,{recursive:true});
 const errors=[],tech=await browser.newPage({viewport:{width:390,height:844}}),leader=await browser.newPage({viewport:{width:1440,height:1000}});
 for(const page of[tech,leader])page.on('pageerror',e=>errors.push(e.message));
 async function login(page,phone){await page.goto(base+'/cabinet/login/');await page.locator('[name=phone]').fill(phone);await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');}
 async function decision(page,action,note){await page.goto(base+path+'closure/');await page.locator('[name=action]').selectOption(action);await page.locator('[name=note]').fill(note);await page.getByRole('button',{name:'Сохранить решение',exact:true}).click();await page.waitForURL(base+path);}
 await login(tech,'+79990009202');await login(leader,'+79990009201');
 await tech.goto(base+path+'closure/');await tech.locator('[name=note]').fill('ТЕСТ Работы завершены, документы переданы на проверку.');
 for(const width of[1440,768,390,320]){await tech.setViewportSize({width,height:900});assert.equal(await tech.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'closure form '+width);}
 await tech.setViewportSize({width:390,height:844});await tech.screenshot({path:shots+'/01-request-phone.png',fullPage:true});await tech.getByRole('button',{name:'Сохранить решение',exact:true}).click();await tech.waitForURL(base+path);
 await leader.goto(base+'/cabinet/contracts/');assert.ok((await leader.locator('main').textContent()).includes('Рассмотреть: ТЕСТ-1726'));await leader.screenshot({path:shots+'/02-leader-queue.png',fullPage:true});
 await decision(leader,'return','ТЕСТ Дополните комплект исполнительной документации.');
 await decision(tech,'request','ТЕСТ Комплект дополнен, замечания устранены.');
 await decision(leader,'archive','ТЕСТ Работы, документы и расчёты проверены.');assert.ok((await leader.locator('main').textContent()).includes('Контракт в архиве.'));
 assert.equal(await leader.getByRole('link',{name:'Редактировать',exact:true}).count(),0);await leader.screenshot({path:shots+'/03-archive-history.png',fullPage:true});
 const response=await leader.request.get(base+path+'edit/');assert.equal(response.status(),403);
 await decision(leader,'restore','ТЕСТ Обнаружены дополнительные работы, контракт восстановлен.');assert.ok((await leader.locator('main').textContent()).includes('Восстановлен из архива'));assert.deepEqual(errors,[]);await browser.close();console.log('PASS request, global queue, return, resubmission, archive, denied edit, restore and 320–1440px layouts');
})().catch(error=>{console.error(String(error));process.exit(1)});
