const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
try{
const page=await browser.newPage({viewport:{width:390,height:1100}}),base=process.env.ARMADA_TEST_URL,id=process.env.ARMADA_DEMAND_CONFIG,errors=[];
page.on('pageerror',e=>errors.push(String(e)));
await page.goto(base+'/cabinet/login/');
await page.locator('[name=phone]').fill('+70000000001');await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);
await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');
await page.locator('.nav-toggle').click();assert.equal(await page.getByRole('link',{name:'↗ Сайт компании',exact:true}).getAttribute('href'),'https://armada-sibir.ru/');await page.locator('.nav-toggle').click();
await page.goto(base+'/cabinet/stock/coverage/');
async function expandRows(){const button=page.locator('.demand-toggle').first(),target=await button.getAttribute('data-demand-target'),row=page.locator('[id="'+target+'"]');assert.equal(await row.isVisible(),false);await button.click();assert.equal(await row.isVisible(),true);assert.equal(await row.locator('.demand-location-table tbody tr').first().locator('td').count(),4);assert.ok((await row.textContent()).includes('1.000'));return row;}
const expanded=await expandRows();
for(const width of [320,390,768,1440]){await page.setViewportSize({width,height:1100});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'internal shortage overflow '+width);assert.equal(await expanded.locator('thead').isVisible(),true);assert.equal(await expanded.locator('tbody tr').first().evaluate(r=>getComputedStyle(r).display),'table-row');const cells=await expanded.locator('tbody tr').first().locator('td').evaluateAll(cells=>cells.map(c=>c.getBoundingClientRect().x));assert.ok(cells.every((x,i)=>!i||x>cells[i-1]));}
await page.setViewportSize({width:1440,height:1100});
const action=await page.locator('.demand-action').first().boundingBox(),button=await page.locator('.demand-toggle').first().boundingBox();assert.ok(button.x>action.x+action.width/2-button.width/2);
await page.screenshot({path:'/workspace/artifacts/shortage-demand-table.png',fullPage:true});
await page.goto(base+process.env.ARMADA_MATERIALS_PATH);
if(await page.locator('.demand-toggle').count()){await expandRows();}
await page.goto(base+'/cabinet/stock/coverage/');
await page.getByRole('link',{name:'Рассылка заказчику',exact:true}).click();await page.waitForURL(/coverage\/mail\/$/);
assert.ok((await page.locator('main').textContent()).includes('Автоматическая отправка выключена'));
await page.getByRole('link',{name:'Предпросмотр и отправка',exact:true}).click();await page.waitForURL(/preview\/$/);
const text=await page.locator('main').textContent();assert.ok(text.includes('DN 700'));assert.ok(!text.includes('Не хватает'));assert.ok(!text.includes('На складе'));
await expandRows();
for(const width of [320,390,768,1440]){await page.setViewportSize({width,height:1100});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'overflow '+width);}
const download=page.waitForEvent('download');await page.getByRole('link',{name:'Скачать прикладываемый Excel',exact:true}).click();await download;
await page.getByRole('button',{name:'Отправить потребность сейчас',exact:true}).click();await page.waitForURL(/history\/$/);
assert.ok((await page.locator('main').textContent()).includes('В очереди'));
await page.goto(base+'/cabinet/stock/coverage/mail/'+id+'/');assert.equal(await page.locator('[name=enabled]').isChecked(),false);
assert.deepEqual(errors,[]);
console.log('PASS customer mail: scoped DN preview, pure demand XLSX, manual queue with schedule off, history, four widths');
}finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
