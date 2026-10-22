const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 const base=process.env.ARMADA_TEST_URL,out=process.env.ARMADA_SHOWCASE_DIR||'/tmp/openia-excel-showcase';fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',args:['--no-sandbox']});
 const technician=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];technician.on('pageerror',e=>errors.push(e.message));
 async function login(page,phone){await page.goto(base+'/cabinet/login/');await page.locator('[name=phone]').fill(phone);await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');}
 await login(technician,'+70000000004');await technician.goto(base+'/cabinet/repairs/');
 const resolved=technician.locator('tbody.repair-tone-resolved').first();assert.equal(await resolved.evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(227, 243, 232)');await resolved.hover();assert.equal(await resolved.evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(213, 235, 220)');

 const template=technician.waitForEvent('download');await technician.getByRole('link',{name:'Шаблон Excel',exact:true}).click();assert.equal((await template).suggestedFilename(),'openIA-defects-template.xlsx');
 await technician.goto(base+'/cabinet/repairs/import/');await technician.locator('[name=contract]').selectOption(process.env.ARMADA_CONTRACT_ID);await technician.locator('[name=section]').selectOption(process.env.ARMADA_SECTION_ID);await technician.locator('[name=file]').setInputFiles(process.env.ARMADA_SAMPLE_FILE);
 await technician.getByRole('button',{name:'Посмотреть перед переносом'}).click();await technician.waitForURL(/\/repairs\/import\/[a-f0-9-]+\/$/);
 for(const text of ['Р-02','Р-03','ТЕСТ Мастер','55003','55004','55005'])assert.ok((await technician.locator('main').textContent()).includes(text));
 await technician.screenshot({path:out+'/06-excel-review.png',fullPage:true});
 await technician.getByRole('button',{name:'Сохранить проверенные записи'}).click();await technician.waitForURL(base+'/cabinet/repairs/');
 await technician.locator('[name=master]').selectOption({label:'ТЕСТ Мастер'});await technician.getByRole('button',{name:'Найти',exact:true}).click();
 const exported=technician.waitForEvent('download');await technician.getByRole('link',{name:'↓ Скачать Excel',exact:true}).click();assert.equal((await exported).suggestedFilename(),'openIA-defects.xlsx');
 await technician.goto(base+'/cabinet/repairs/');
 const original=technician.locator('[data-repair-group="'+process.env.ARMADA_REPAIR_ID+'"]');
 assert.equal(await original.locator('.repair-delete').count(),2);
 await original.locator('.repair-delete').first().click();await technician.getByRole('button',{name:'Удалить из списка',exact:true}).click();await technician.waitForURL(base+'/cabinet/repairs/');
 assert.equal(await technician.locator('[data-repair-group="'+process.env.ARMADA_REPAIR_ID+'"]').locator('.repair-delete').count(),1);
 await technician.goto(base+'/cabinet/repairs/'+process.env.ARMADA_REPAIR_ID+'/');assert.equal(await technician.locator('.repair-photos img').count(),1);
 await technician.goto(base+'/cabinet/repairs/?archive=1');await technician.getByRole('link',{name:'Восстановить',exact:true}).click();await technician.getByRole('button',{name:'Восстановить',exact:true}).click();await technician.waitForURL(base+'/cabinet/repairs/');
 assert.equal(await technician.locator('[data-repair-group="'+process.env.ARMADA_REPAIR_ID+'"]').locator('.repair-delete').count(),2);
 for(const width of [1440,768,390,320]){await technician.setViewportSize({width,height:900});assert.equal(await technician.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'register '+width);}
 const firstRow=technician.locator('.repair-register tbody tr').first();assert.ok((await firstRow.boundingBox()).height<100);await technician.setViewportSize({width:1440,height:900});await technician.screenshot({path:out+'/09-compact-register.png',fullPage:true});
 const worker=await browser.newPage({viewport:{width:1280,height:1000}});worker.on('pageerror',e=>errors.push(e.message));await login(worker,'+70000000003');await worker.goto(base+'/cabinet/repairs/?q=550');
 assert.equal(await worker.getByRole('link',{name:'Устранён',exact:true}).count(),2);
 await worker.screenshot({path:out+'/07-separate-repairs.png',fullPage:true});
 const repair=worker.locator('[data-repair-code="Р-02"]');await repair.getByRole('link',{name:'Устранён',exact:true}).click();await worker.waitForSelector('[data-draft-ready="1"]');
 await worker.locator('[name=coordinates]').fill('56.900001, 73.900001');await worker.screenshot({path:out+'/08-single-coordinate-field.png',fullPage:true});
 await worker.locator('[data-send-report]').click();await worker.waitForURL(/\/repairs\/[a-f0-9-]+\/$/);
 await worker.goto(base+'/cabinet/repairs/?q=550');assert.ok((await worker.locator('[data-repair-code="Р-03"]').textContent()).includes('Не устранён'));
 await technician.goto(base+'/cabinet/repairs/new/');await technician.locator('[name=contract]').selectOption(process.env.ARMADA_CONTRACT_ID);await technician.locator('[name=section]').selectOption(process.env.ARMADA_SECTION_ID);await technician.locator('summary').filter({hasText:'Назначение, состав'}).click();
 await technician.waitForFunction(()=>[...document.querySelector('[name=assigned_to]').options].some(o=>o.textContent==='ТЕСТ Мастер'));
 await technician.locator('[name=assigned_to]').selectOption({label:'ТЕСТ Мастер'});await technician.locator('[name=kilometre]').fill('444.4445');await technician.locator('[name=defect_numbers]').fill('55006');await technician.locator('[name=pipe_section]').fill('1004');await technician.locator('[name=method]').selectOption('p1');await technician.locator('[name=length_m]').fill('1');await technician.locator('[name=wall_mm]').fill('8');await technician.getByRole('button',{name:'Сохранить',exact:true}).click();await technician.waitForURL(/\/repairs\/[a-f0-9-]+\/$/);
 const transferJob=technician.url();assert.ok((await technician.locator('main').textContent()).includes('444.445'));
 await worker.goto(transferJob+'report/unrepaired/');await worker.waitForSelector('[data-draft-ready="1"]');await worker.locator('[name=problem]').selectOption('existing_coupling');await worker.locator('[name=note]').fill('В котловане обнаружена ранее установленная П2.');await worker.locator('[data-send-report]').click();await worker.waitForURL(transferJob);
 await technician.goto(transferJob+'reassign/');await technician.locator('[name=master]').selectOption({label:'ТЕСТ Техник'});await technician.getByRole('button',{name:'Передать работу',exact:true}).click();await technician.waitForURL(transferJob);
 assert.equal((await worker.goto(transferJob)).status(),404);
 await technician.goto(transferJob+'report/unrepaired/');await technician.waitForSelector('[data-draft-ready="1"]');await technician.locator('[name=problem]').selectOption('oversize_p1');await technician.locator('[name=note]').fill('Нужна П1 увеличенного диаметра поверх найденной П2. Муфты нет в наличии.');await technician.locator('[data-send-report]').click();await technician.waitForURL(transferJob);
 const finalText=await technician.locator('main').textContent();for(const text of ['Не устранён','П1 увеличенного диаметра','ТЕСТ Мастер','Смена ответственного мастера'])assert.ok(finalText.includes(text));
 await technician.goto(base+'/cabinet/repairs/');assert.equal(await technician.getByRole('columnheader',{name:'Код ремонта / участок',exact:true}).count(),0);assert.equal(await technician.getByRole('columnheader',{name:'Метод ремонта',exact:true}).count(),1);await technician.screenshot({path:out+'/10-register-terms.png',fullPage:true});
 assert.deepEqual(errors,[]);await browser.close();console.log('PASS technician permission, XLSX template/import/preview/assignment/export, separate repair groups and one pasted coordinate field');
})().catch(e=>{console.error(e.message);process.exit(1);});
