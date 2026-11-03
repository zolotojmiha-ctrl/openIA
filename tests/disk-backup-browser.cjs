const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
try{
const page=await browser.newPage({viewport:{width:390,height:1100}}),base=process.env.ARMADA_TEST_URL,errors=[];
page.on('pageerror',e=>errors.push(String(e)));
await page.goto(base+'/cabinet/login/');
await page.locator('[name=phone]').fill('+70000000001');await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);
await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');
await page.goto(base+'/cabinet/settings/backups/');
await page.locator('[name=token]').fill('y0_SYNTHETIC_NOT_A_REAL_TOKEN_123456789');
await page.getByRole('button',{name:'Сохранить настройки',exact:true}).click();await page.waitForLoadState('networkidle');
assert.equal(await page.locator('[name=token]').inputValue(),'');assert.equal(await page.locator('[name=enabled]').isChecked(),false);
const downloaded=page.waitForEvent('download');await page.getByRole('button',{name:'Создать и скачать ключ восстановления',exact:true}).click();const file=await downloaded;assert.match(file.suggestedFilename(),/^armada-recovery-.*\.pem$/);
await page.reload();await page.locator('[name=saved]').check();await page.getByRole('button',{name:'Подтвердить сохранение ключа',exact:true}).click();await page.waitForLoadState('networkidle');
assert.ok((await page.locator('main').textContent()).includes('Ключ настроен'));
for(const width of [320,390,768,1440]){await page.setViewportSize({width,height:1100});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'overflow '+width);}
assert.deepEqual(errors,[]);console.log('PASS backup setup: token masked, key download/confirmation, schedule off, four widths');
}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
