const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
const base=process.env.ARMADA_TEST_URL,job=process.env.ARMADA_RECONCILE_JOB;
const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
const page=await browser.newPage({viewport:{width:390,height:844}}),errors=[];
page.on('pageerror',e=>errors.push(String(e)));
await page.goto(base+'/cabinet/login/');await page.locator('[name=phone]').fill('+70000000001');await page.locator('[name=password]').fill('123456');await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');
const url=base+'/cabinet/repairs/'+job+'/stock-reconcile/';await page.goto(url);
await page.getByRole('button',{name:'Проверить списание',exact:true}).click();
await page.getByRole('button',{name:'Подтвердить списание',exact:true}).waitFor();
assert.ok((await page.locator('main').innerText()).includes('ТЕСТ ФАКТ'));
assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
const token=await page.locator('[name=preview]').inputValue(),csrf=await page.locator('main [name=csrfmiddlewaretoken]').inputValue();
await page.screenshot({path:'/workspace/artifacts/6332-reconciliation-preview-390.png',fullPage:true});
await page.getByRole('button',{name:'Подтвердить списание',exact:true}).click();await page.waitForURL(base+'/cabinet/repairs/'+job+'/');
const again=await page.request.post(url,{form:{stage:'confirm',preview:token,csrfmiddlewaretoken:csrf}});assert.equal(again.status(),200);
assert.deepEqual(errors,[]);await browser.close();console.log('PASS mobile owner preview, explicit confirmation, repeated submission');
})().catch(e=>{console.error(e);process.exit(1)});
