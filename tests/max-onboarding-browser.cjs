const {chromium}=require('playwright'), assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 try {
  const page=await browser.newPage({viewport:{width:390,height:1000}}), base=process.env.ARMADA_TEST_URL;
  const errors=[]; page.on('pageerror',e=>errors.push(String(e)));
  await page.goto(base+'/cabinet/login/');
  await page.locator('[name=phone]').fill(process.env.ARMADA_TEST_PHONE);
  await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);
  await page.locator('button[type=submit]').click(); await page.waitForURL(base+'/cabinet/password/');
  await page.locator('[name=old_password]').fill(process.env.ARMADA_TEST_PASSWORD);
  await page.locator('[name=new_password1]').fill(process.env.ARMADA_TEST_NEW_PASSWORD);
  await page.locator('[name=new_password2]').fill(process.env.ARMADA_TEST_NEW_PASSWORD);
  await page.locator('button[type=submit]').click(); await page.waitForURL(base+'/cabinet/profile/max/welcome/');
  assert.equal(await page.getByRole('button',{name:'Подключить MAX',exact:true}).count(),1);
  for(const width of [320,390,768,1440]){
   await page.setViewportSize({width,height:1000});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'overflow '+width);
  }
  await page.getByRole('button',{name:'Позже',exact:true}).click(); await page.waitForURL(base+'/cabinet/');
  await page.goto(base+'/cabinet/profile/');
  assert.equal(await page.getByRole('link',{name:'Подключение MAX',exact:true}).count(),1);
  assert.deepEqual(errors,[]);
  console.log('PASS MAX first password change, optional offer, retained profile entry and four viewport widths');
 } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
