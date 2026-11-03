const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
try{
const page=await browser.newPage({viewport:{width:390,height:1100}}),base=process.env.ARMADA_TEST_URL,id=process.env.ARMADA_DEMAND_CONFIG,errors=[];
page.on('pageerror',e=>errors.push(String(e)));
await page.goto(base+'/cabinet/login/');
await page.locator('[name=phone]').fill('+70000000001');await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);
await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');
await page.goto(base+'/cabinet/stock/coverage/');
await page.getByRole('link',{name:'Рассылка заказчику',exact:true}).click();await page.waitForURL(/coverage\/mail\/$/);
assert.ok((await page.locator('main').textContent()).includes('Автоматическая отправка выключена'));
await page.getByRole('link',{name:'Предпросмотр и отправка',exact:true}).click();await page.waitForURL(/preview\/$/);
const text=await page.locator('main').textContent();assert.ok(text.includes('DN 700'));assert.ok(!text.includes('Не хватает'));assert.ok(!text.includes('На складе'));
for(const width of [320,390,768,1440]){await page.setViewportSize({width,height:1100});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'overflow '+width);}
const download=page.waitForEvent('download');await page.getByRole('link',{name:'Скачать прикладываемый Excel',exact:true}).click();await download;
await page.getByRole('button',{name:'Отправить потребность сейчас',exact:true}).click();await page.waitForURL(/history\/$/);
assert.ok((await page.locator('main').textContent()).includes('В очереди'));
await page.goto(base+'/cabinet/stock/coverage/mail/'+id+'/');assert.equal(await page.locator('[name=enabled]').isChecked(),false);
assert.deepEqual(errors,[]);
console.log('PASS customer mail: scoped DN preview, pure demand XLSX, manual queue with schedule off, history, four widths');
}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
