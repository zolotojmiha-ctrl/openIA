const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 const base=process.env.ARMADA_TEST_URL,out=process.env.ARMADA_SHOWCASE_DIR||'/tmp/openia-excel-showcase';fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',args:['--no-sandbox']});
 const technician=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];technician.on('pageerror',e=>errors.push(e.message));
 async function login(page,phone){await page.goto(base+'/cabinet/login/');await page.locator('[name=phone]').fill(phone);await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');}
 await login(technician,'+70000000004');await technician.goto(base+'/cabinet/repairs/');
 const template=technician.waitForEvent('download');await technician.getByRole('link',{name:'Шаблон Excel',exact:true}).click();assert.equal((await template).suggestedFilename(),'openIA-defects-template.xlsx');
 await technician.goto(base+'/cabinet/repairs/import/');await technician.locator('[name=section]').selectOption(process.env.ARMADA_SECTION_ID);await technician.locator('[name=file]').setInputFiles(process.env.ARMADA_SAMPLE_FILE);
 await technician.getByRole('button',{name:'Посмотреть перед переносом'}).click();await technician.waitForURL(/\/repairs\/import\/[a-f0-9-]+\/$/);
 for(const text of ['Р-02','Р-03','ТЕСТ Мастер','ДЕМО-XL03','ДЕМО-XL04','ДЕМО-XL05'])assert.ok((await technician.locator('main').textContent()).includes(text));
 await technician.screenshot({path:out+'/06-excel-review.png',fullPage:true});
 await technician.getByRole('button',{name:'Сохранить проверенные записи'}).click();await technician.waitForURL(base+'/cabinet/repairs/');
 await technician.locator('[name=master]').selectOption({label:'ТЕСТ Мастер'});await technician.getByRole('button',{name:'Найти',exact:true}).click();
 const exported=technician.waitForEvent('download');await technician.getByRole('link',{name:'Скачать список в Excel',exact:true}).click();assert.equal((await exported).suggestedFilename(),'openIA-defects.xlsx');
 const worker=await browser.newPage({viewport:{width:1280,height:1000}});worker.on('pageerror',e=>errors.push(e.message));await login(worker,'+70000000003');await worker.goto(base+'/cabinet/repairs/?q=ДЕМО-XL');
 assert.equal(await worker.getByRole('link',{name:'Устранён',exact:true}).count(),2);
 await worker.screenshot({path:out+'/07-separate-repairs.png',fullPage:true});
 const repair=worker.locator('article').filter({hasText:'Ремонт Р-02'});await repair.getByRole('link',{name:'Устранён',exact:true}).click();await worker.waitForSelector('[data-draft-ready="1"]');
 await worker.locator('[name=coordinates]').fill('56.900001, 73.900001');await worker.screenshot({path:out+'/08-single-coordinate-field.png',fullPage:true});
 await worker.locator('[data-send-report]').click();await worker.waitForURL(/\/repairs\/[a-f0-9-]+\/$/);
 await worker.goto(base+'/cabinet/repairs/?q=ДЕМО-XL');assert.ok((await worker.locator('article').filter({hasText:'Ремонт Р-03'}).textContent()).includes('Запланировано'));
 assert.deepEqual(errors,[]);await browser.close();console.log('PASS technician permission, XLSX template/import/preview/assignment/export, separate repair groups and one pasted coordinate field');
})().catch(e=>{console.error(e.message);process.exit(1);});
