const {chromium}=require('playwright'),assert=require('node:assert/strict');
let browser,phase='start';
(async()=>{
 browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:390,height:950},locale:'ru-RU'}),base=process.env.ARMADA_TEST_URL;
 const errors=[];page.on('pageerror',e=>errors.push(e.name));page.on('response',r=>{if(r.status()>=500)errors.push('HTTP '+r.status());});
 async function navigateClick(locator){await Promise.all([page.waitForNavigation({waitUntil:'networkidle'}),locator.click()]);}
 async function login(phone){await page.goto(base+'/cabinet/login/');await page.locator('[name=phone]').fill(phone);await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);await navigateClick(page.locator('button[type=submit]'));}
 async function logout(){await navigateClick(page.getByRole('button',{name:'Выйти',exact:true}));}
 await login(process.env.ARMADA_MASTER_PHONE);phase='DDK';
 await page.goto(base+process.env.ARMADA_REPORT_PATH);
 await page.getByRole('link',{name:'После ДДК изменился метод ремонта',exact:true}).click();await page.waitForURL('**/ddk/');
 await page.locator('[name=method]').selectOption('p1');await page.locator('[name=length_m]').fill('3');await page.locator('[name=wall_mm]').fill('10');
 await page.locator('[name=composition]').fill('1.5+1.5');
 await page.locator('[name=reason]').fill('ТЕСТ ДДК: нужна муфта');
 await page.locator('[name=files]').setInputFiles({name:'ddk.txt',mimeType:'text/plain',buffer:Buffer.from('TEST DDK evidence')});
 await navigateClick(page.getByRole('button',{name:'Сохранить изменение после ДДК',exact:true}));
 assert.equal(await page.getByText('Метод после ДДК сохранён.',{exact:false}).count(),1);
 await logout();await login(process.env.ARMADA_LEADER_PHONE);phase='grant';
 await page.goto(base+process.env.ARMADA_EQUIPMENT_PATH+'?rental='+process.env.ARMADA_RENTAL);
 await navigateClick(page.getByRole('link',{name:'Доступ арендодателя',exact:true}));
 await page.locator('[name=user]').selectOption(process.env.ARMADA_OWNER_ID);assert.equal(await page.locator('[name=financial]').isChecked(),false);
 await navigateClick(page.getByRole('button',{name:'Сохранить доступ',exact:true}));
 assert.equal(await page.getByText('Доступ арендодателя сохранён.',{exact:false}).count(),1);
 await logout();await login(process.env.ARMADA_OWNER_PHONE);phase='owner';
 await navigateClick(page.getByRole('link',{name:'Открыть табель',exact:true}));
 assert.equal(await page.getByRole('link',{name:'Финансы',exact:false}).count(),0);
 assert.equal(await page.getByText('Расчёты по этой аренде',{exact:true}).count(),0);
 for(const width of [320,390,1440]){await page.setViewportSize({width,height:950});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);}
 await page.setViewportSize({width:390,height:950});await page.screenshot({path:'/tmp/rental-owner-phone.png',fullPage:true});
 await page.locator('[name=text]').fill('ТЕСТ разногласие из браузера');await navigateClick(page.getByRole('button',{name:'Отправить на проверку',exact:true}));
 assert.equal(await page.getByText('Разногласие направлено на проверку.',{exact:false}).count(),1);
 const download=page.waitForEvent('download');await page.getByRole('link',{name:'↓ Скачать табель Excel',exact:true}).click();await (await download).saveAs(process.env.ARMADA_OWNER_XLSX);
 await logout();await login(process.env.ARMADA_LEADER_PHONE);phase='review';await page.goto(base+process.env.ARMADA_EQUIPMENT_PATH+'?rental='+process.env.ARMADA_RENTAL);
 await page.locator('[name=resolution]').fill('ТЕСТ сверено по журналу');await navigateClick(page.getByRole('button',{name:'Отметить рассмотрение',exact:true}));
 assert.deepEqual(errors,[]);await browser.close();console.log('PASS DDK revision with evidence, owner grant, isolated read-only ledger, dispute/review, export and mobile');
})().catch(async e=>{console.error('Remaining updates browser:',phase,e.name,e.message);if(browser)await browser.close();process.exitCode=1;});
