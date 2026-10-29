const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const base=process.env.ARMADA_TEST_URL, f=JSON.parse(process.env.WAREHOUSE_ACTIONS_FIXTURE);
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[];
 page.on('pageerror',e=>errors.push(String(e)));
 async function login(phone){
  await page.goto(base+'/cabinet/login/');
  await page.locator('[name=phone]').fill(phone);await page.locator('[name=password]').fill('123456');
  await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');
 }
 async function fit(label){
  for(const width of [1440,768,390,320]){
   await page.setViewportSize({width,height:1050});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,label+' '+width);
   for(const link of await page.locator('.stock-actions a.button,.stock-navigation a').all()){
    if(await link.isVisible())assert.ok((await link.boundingBox()).height>=43,label+' touch size');
   }
   if(width===1440||width===390)await page.screenshot({path:'/tmp/warehouse-actions-'+label+'-'+width+'.png',fullPage:true});
  }
 }
 await login('+70000000001');
 await page.locator('.sidebar').getByRole('link',{name:'Склады',exact:false}).click();await page.waitForURL('**/stock/inventory/');
 assert.ok(await page.getByRole('link',{name:'Редактировать список',exact:true}).isVisible());
 assert.notEqual(await page.getByRole('link',{name:'Редактировать список',exact:true}).evaluate(e=>getComputedStyle(e).backgroundColor),await page.getByRole('link',{name:'Удалить',exact:true}).evaluate(e=>getComputedStyle(e).backgroundColor));
 await fit('materials');
 await page.getByRole('link',{name:'Настроить склады',exact:true}).click();
 const card=page.locator('.warehouse-card').filter({hasText:'ТЕСТ Центральный склад'});
 assert.ok(await card.getByRole('link',{name:'Изменить склад',exact:true}).isVisible());
 assert.ok(await card.getByRole('link',{name:'＋ Добавить участок',exact:true}).isVisible());
 await fit('warehouses');
 await card.getByRole('link',{name:'＋ Добавить участок',exact:true}).click();
 await page.waitForURL('**/edit/#warehouse-areas');
 assert.equal(await page.locator('[name=served_objects][value="'+f.first+'"]').isChecked(),true);
 await page.locator('[name=served_objects][value="'+f.second+'"]').check();
 await fit('edit');
 await page.getByRole('button',{name:'Сохранить',exact:true}).click();await page.waitForURL(u=>u.pathname==='/cabinet/stock/'+f.warehouse+'/');
 assert.ok((await page.locator('.warehouse-links-panel').innerText()).includes(f.private_name));
 await fit('detail');
 await page.getByRole('button',{name:'Выйти',exact:true}).click();await page.waitForURL('**/cabinet/login/');
 await login('+70000000002');
 if(await page.locator('.nav-toggle').isVisible())await page.locator('.nav-toggle').click();
 await page.locator('.sidebar').getByRole('link',{name:'Склады',exact:false}).click();
 assert.equal(await page.getByRole('link',{name:'Изменить склад',exact:true}).count(),0);
 assert.equal(await page.getByRole('link',{name:'＋ Добавить участок',exact:true}).count(),0);
 assert.equal((await page.locator('main').innerText()).includes(f.private_name),false);
 await page.getByRole('link',{name:'Остатки и история →',exact:true}).click();
 assert.equal((await page.locator('main').innerText()).includes(f.private_name),false);
 assert.equal(await page.getByRole('link',{name:'Изменить склад',exact:true}).count(),0);
 const denied=await page.goto(base+'/cabinet/stock/'+f.warehouse+'/edit/');assert.equal(denied.status(),403);
 assert.deepEqual(errors,[]);await browser.close();
 console.log('PASS: direct actions, second area preserving first and stock, phone/tablet/desktop, restricted viewer.');
})().catch(e=>{console.error(e);process.exit(1)});
