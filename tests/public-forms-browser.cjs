const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const base=process.env.ARMADA_TEST_URL,browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(String(e)));
 async function fit(label){
  for(const width of [1440,768,390,320]){
   await page.setViewportSize({width,height:1000});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,label+' overflow '+width);
   if(width===390)await page.screenshot({path:'/tmp/public-forms-'+label+'-390.png',fullPage:true});
  }
 }
 await page.goto(base+'/');
 assert.equal(await page.locator('.review-bar').count(),0);
 assert.equal((await page.locator('body').innerText()).includes('Версия для согласования'),false);
 await page.goto(base+'/#careers');
 const form=page.locator('#candidate');
 for(const [name,value] of Object.entries({name:'ТЕСТ Кандидат',phone:'+7 (900) 000-00-01',email:'candidate@example.invalid',city:'ТЕСТ Город',profession:'Монтажник',experience:'Опыт 5 лет'}))await form.locator('[name='+name+']').fill(value);
 await form.locator('[name=travel]').selectOption('Готов(а)');
 assert.equal(await form.locator('[name=consent]').isChecked(),false);
 let sent=0;page.on('request',r=>{if(r.method()==='POST'&&r.url().endsWith('/forms/candidate/'))sent++;});
 await form.getByRole('button',{name:'Отправить анкету',exact:true}).click();assert.equal(sent,0);
 await form.locator('[name=consent]').check();
 await form.locator('[name=resume]').setInputFiles({name:'oversized.pdf',mimeType:'application/pdf',buffer:Buffer.alloc(5*1024*1024+1)});
 await form.getByRole('button',{name:'Отправить анкету',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('#candidate .form-status').textContent.includes('до 5 МБ'));
 assert.equal(sent,0);
 await form.locator('[name=resume]').setInputFiles({name:'Резюме кандидата.pdf',mimeType:'application/pdf',buffer:Buffer.from('%PDF-1.4\nsynthetic browser resume')});
 await fit('candidate');
 await page.route('**/forms/candidate/',async route=>{await route.fetch();await route.abort('failed');});
 await form.getByRole('button',{name:'Отправить анкету',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('#candidate .form-status').textContent.includes('Данные остались'));
 assert.equal(await form.locator('[name=name]').inputValue(),'ТЕСТ Кандидат');
 assert.equal(await form.locator('[name=resume]').evaluate(e=>e.files.length),1);
 await page.unroute('**/forms/candidate/');
 await form.getByRole('button',{name:'Отправить анкету',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('#candidate .form-status').textContent.includes('Анкета принята'));
 assert.equal(sent,2);
 assert.equal(await form.locator('[name=resume]').evaluate(e=>e.files.length),0);
 for(const [route,id] of [['rental','rental-callback'],['couplings','couplings-request'],['centralizers','centralizers-request']]){
  await page.goto(base+'/#'+route);const target=page.locator('#'+id);
  await target.locator('[name=phone]').fill('+7 (900) 000-00-02');
  if(route==='rental')await target.locator('[name=equipment]').fill('Экскаватор');
  else for(const [name,value] of Object.entries({name:'ТЕСТ Заказчик',email:'customer@example.invalid',dn:'720',quantity:'2'}))await target.locator('[name='+name+']').fill(value);
  assert.equal(await target.locator('[name=consent]').isChecked(),false);await target.locator('[name=consent]').check();
  await fit(route);await target.locator('button[type=submit]').click();
  await page.waitForFunction(id=>document.querySelector('#'+id+' .form-status').textContent.includes('Запрос принят'),id);
 }
 await page.goto(base+'/#privacy');await fit('privacy');
 assert.equal((await page.locator('[data-page="/privacy"]').innerText()).includes('Проект'),false);
 await page.goto(base+'/cabinet/login/');await page.locator('[name=phone]').fill('+70000000401');await page.locator('[name=password]').fill('123456');await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');
 await page.goto(base+'/cabinet/mail/');await page.getByRole('link',{name:'Заявки с сайта · Новых: 4',exact:true}).click();
 await page.getByRole('link',{name:'Анкета кандидата',exact:true}).click();
 assert.ok((await page.locator('main').innerText()).includes('ТЕСТ Кандидат'));
 const [download]=await Promise.all([page.waitForEvent('download'),page.getByRole('link',{name:'↓ Резюме: Резюме кандидата.pdf',exact:true}).click()]);
 assert.equal(await download.failure(),null);await fit('private-request');
 assert.deepEqual(errors,[]);await browser.close();
 console.log('PASS: 4 public forms, consent, size check, lost-response retry without duplicate, resume and private inbox, desktop/tablet/mobile.');
})().catch(e=>{console.error(e);process.exit(1)});
