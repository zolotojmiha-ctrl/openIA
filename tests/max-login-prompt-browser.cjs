const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 try{
  const page=await browser.newPage({viewport:{width:390,height:1000}}),base=process.env.ARMADA_TEST_URL,errors=[];
  page.on('pageerror',e=>errors.push(e.name));page.on('response',r=>{if(r.status()>=500)errors.push('HTTP '+r.status())});
  async function login(){await page.goto(base+'/cabinet/login/');await page.locator('[name=phone]').fill(process.env.ARMADA_TEST_PHONE);await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/profile/max/welcome/');}
  await login();
  for(const width of [320,390,768,1440]){await page.setViewportSize({width,height:1000});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);}
  await page.getByRole('button',{name:'Позже',exact:true}).click();await page.waitForURL(base+'/cabinet/');
  await page.reload();assert.equal(new URL(page.url()).pathname,'/cabinet/');
  await page.getByRole('button',{name:'Выйти',exact:true}).click();await page.waitForURL(base+'/cabinet/login/');
  await login();await page.getByRole('button',{name:'Подключить MAX',exact:true}).click();await page.waitForURL(base+'/cabinet/profile/max/');
  assert.deepEqual(errors,[]);console.log('PASS MAX recurring login prompt, defer for session, next-login reminder, connect, four widths');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
