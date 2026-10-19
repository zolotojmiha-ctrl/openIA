const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const base=process.env.ARMADA_TEST_URL;
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',args:['--no-sandbox']});
 const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.name));
 await page.goto(base+'/cabinet/login/');
 await page.locator('[name=phone]').fill('+70000000001');await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);
 await page.locator('button[type=submit]').click();assert.ok(page.url().includes('/cabinet/password/'));
 await page.locator('[name=old_password]').fill(process.env.ARMADA_TEST_PASSWORD);await page.locator('[name=new_password1]').fill(process.env.ARMADA_TEST_NEW_PASSWORD);await page.locator('[name=new_password2]').fill(process.env.ARMADA_TEST_NEW_PASSWORD);await page.locator('main button[type=submit]').click();assert.ok(page.url().endsWith('/cabinet/'));
 await page.goto(base+'/cabinet/users/new/');await page.locator('[name=full_name]').fill('ТЕСТ Сотрудник');await page.locator('[name=phone]').fill('+70000000003');await page.locator('[name=role]').selectOption('foreman');await page.locator('[name=permissions][value="document.upload"]').check();await page.locator('main button[type=submit]').click();assert.ok((await page.locator('h1').textContent()).includes('Временный доступ создан'));
 const temporary=(await page.locator('.temporary').textContent()).trim();assert.ok(temporary.length>=20);
 await page.goto(base+'/cabinet/users/');assert.equal(await page.locator('tbody tr').count(),1);
 await page.locator('td a').click();await page.locator('[name=is_active]').uncheck();await page.locator('main form').first().locator('button[type=submit]').click();assert.ok((await page.locator('tbody').textContent()).includes('Заблокирован'));
 for(const width of [1440,768,390,320]){await page.setViewportSize({width,height:1000});for(const route of ['/cabinet/','/cabinet/users/','/cabinet/users/new/','/cabinet/password/','/cabinet/audit/']){await page.goto(base+route);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${width} ${route}`);}console.log('PASS account screens width',width);}
 await page.setViewportSize({width:1440,height:1000});await page.goto(base+'/cabinet/users/');await page.screenshot({path:'/tmp/armada-working-users.png',fullPage:true});
 await page.locator('form[action="/cabinet/logout/"] button').click();await page.locator('[name=phone]').fill('+70000000003');await page.locator('[name=password]').fill(temporary);await page.locator('button[type=submit]').click();assert.ok((await page.locator('main').textContent()).includes('Не удалось войти.'));
 assert.deepEqual(errors,[]);await browser.close();console.log('PASS real login, mandatory password change, employee creation, individual permissions, blocking and logout');
})().catch(()=>{console.error('Browser acceptance failed; credential values omitted.');process.exitCode=1});
