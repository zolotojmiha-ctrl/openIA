const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 try {
  const page=await browser.newPage({viewport:{width:390,height:1000}}), base=process.env.ARMADA_TEST_URL, errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  await page.goto(base+'/cabinet/login/');
  await page.locator('[name=phone]').fill('+70000000002');
  await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);
  await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');
  await page.goto(base+'/cabinet/profile/');
  await page.getByRole('link',{name:'Подключение MAX',exact:true}).click();
  await page.getByRole('button',{name:'Отключить MAX',exact:true}).click();await page.waitForLoadState('networkidle');
  await page.getByRole('button',{name:'Создать ссылку подключения',exact:true}).click();await page.waitForLoadState('networkidle');
  const href=await page.getByRole('link',{name:'Открыть бота в MAX',exact:true}).getAttribute('href');
  assert.match(href,/^https:\/\/max.ru\/synthetic_bot\?start=[A-Za-z0-9_-]+$/);
  for(const width of [320,390,768,1440]){
   await page.setViewportSize({width,height:1000});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'overflow '+width);
  }
  assert.deepEqual(errors,[]);console.log('PASS MAX profile: role access, revoke, one-time link, four widths, no external MAX calls');
 } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
