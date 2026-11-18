const {chromium}=require('playwright');
const assert=require('node:assert/strict');
let browser;
(async()=>{
 browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:390,height:1000}});
 const base=process.env.ARMADA_TEST_URL, errors=[];
 page.on('pageerror',e=>errors.push(e.name));
 page.on('response',r=>{if(r.status()>=500)errors.push('HTTP '+r.status())});
 await page.goto(base+'/cabinet/login/');
 await page.locator('[name=phone]').fill(process.env.ARMADA_TEST_PHONE);
 await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);
 await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');
 await page.goto(base+process.env.ARMADA_EQUIPMENT_PATH);
 async function formFor(action,summary){
  await page.locator('summary').filter({hasText:summary}).first().click();
  return page.locator('form').filter({has:page.locator(`[name=action][value=${action}]`)});
 }
 let form=await formFor('payment','Аванс / оплата');
 await form.locator('[name=date]').fill(process.env.ARMADA_PAYMENT_DATE);
 await form.locator('[name=amount]').fill('150');
 await form.locator('[name=deposit_amount]').fill('50');
 await form.locator('[name=note]').fill('ТЕСТ один перевод');
 await form.locator('button').click();await page.waitForLoadState('networkidle');
 assert.equal(await page.locator('.errorlist').count(),0);
 form=await formFor('invoice','Сверить новый счёт за период');
 await form.locator('[name=start]').fill(process.env.ARMADA_RATE_START);
 await form.locator('[name=end]').fill(process.env.ARMADA_PERIOD_END);
 await form.locator('[name=issued_on]').fill(process.env.ARMADA_PAYMENT_DATE);
 await form.locator('[name=reference]').fill('ТЕСТ СЧ-1');
 await form.locator('[name=supplier_amount]').fill('3');
 await form.locator('button').click();await page.waitForLoadState('networkidle');
 assert.equal(await page.locator('.errorlist').count(),0);
 const paymentId=await page.locator('form [name=payment_id][type=hidden]').first().inputValue();
 const invoiceId=await page.locator('[name=invoice_id][type=hidden]').first().inputValue();
 form=await formFor('allocate','Привязать оплату или аванс к счёту');
 await form.locator('[name=payment_id]').fill(paymentId);
 await form.locator('[name=invoice_id]').fill(invoiceId);
 await form.locator('[name=date]').fill(process.env.ARMADA_PAYMENT_DATE);
 await form.locator('[name=amount]').fill('3');
 await form.locator('[name=note]').fill('ТЕСТ оплата периода');
 await form.locator('button').click();await page.waitForLoadState('networkidle');
 assert.equal(await page.locator('.errorlist').count(),0);
 assert.match(await page.locator('article').filter({has:page.locator('h3')}).innerText(),/Оплачен/);
 assert.match(await page.getByText('Возвратный залог у арендодателя').innerText(),/50/);
 for(const width of [320,1440]){
  await page.setViewportSize({width,height:1000});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 }
 const printLink=await page.getByRole('link',{name:'Печать / сохранить PDF'}).getAttribute('href');
 await page.goto(base+printLink);
 assert.match(await page.locator('body').innerText(),/ТЕСТ СЧ-1/);
 const pdf=await page.pdf({format:'A4'});assert.equal(pdf.subarray(0,5).toString(),'%PDF-');
 assert.deepEqual(errors,[]);
 console.log('PASS mobile/desktop split payment, invoice, allocation, printable snapshot and PDF');
 await browser.close();
})().catch(async e=>{console.error(e);if(browser)await browser.close();process.exitCode=1;});
