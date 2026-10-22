const{chromium}=require('playwright');const assert=require('node:assert/strict');
let phase='login';
(async()=>{
 const base=process.env.ARMADA_TEST_URL;const b=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',args:['--no-sandbox']});
 const leader=await b.newPage();const errors=[];leader.on('pageerror',e=>errors.push(e.name));
 async function signIn(p,phone){await p.goto(base+'/cabinet/login/');await p.locator('[name=phone]').fill(phone);await p.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);await p.locator('button[type=submit]').click();}
 await signIn(leader,'+70000000001');
 console.log('PASS leader login');phase='create objects';
 const objectUrls=[];
 for(const name of ['Учебный объект А','Закрытый объект Б']){await leader.goto(base+'/cabinet/objects/new/');await leader.locator('[name=name]').fill(name);await leader.locator('[name=kind]').fill('Учебный участок');await leader.locator('main button[type=submit]').click();objectUrls.push(leader.url());}
 console.log('PASS create objects');phase='create contract';
 await leader.goto(base+'/cabinet/contracts/new/');await leader.locator('[name=number]').fill('ДЕМО-ОБЩИЙ-001');await leader.locator('[name=subject]').fill('Закрытый предмет договора');await leader.locator('[name=work_kinds][value=repairs]').check();await leader.locator('[name=counterparty]').fill('Закрытый контрагент');await leader.locator('[name=amount]').fill('125000');await leader.locator('[name=private_notes]').fill('Закрытое внутреннее примечание');
 for(const el of await leader.locator('[name=linked_objects]').all())await el.check();await leader.locator('main button[type=submit]').click();const contractUrl=leader.url();assert.equal(await leader.locator('main a[href*="/objects/"]').count(),2);
 console.log('PASS create many-to-many contract');phase='assign employee';
 await leader.goto(objectUrls[0]+'access/');await leader.locator('[name=employees]').first().check();await leader.locator('main button[type=submit]').click();
 console.log('PASS assign employee');phase='share fields';
 await leader.goto(contractUrl+'access/');await leader.locator('[name=employee]').selectOption({label:'ТЕСТ Мастер'});await leader.waitForURL(/\?employee=/);await leader.locator('[name=enabled]').check();await leader.locator('[name=fields][value=show_number]').check();await leader.locator('main button[type=submit]').click();
 console.log('PASS share fields');phase='employee access';
 const employee=await b.newPage();employee.on('pageerror',e=>errors.push(e.name));await signIn(employee,'+70000000003');await employee.goto(base+'/cabinet/objects/');assert.ok((await employee.locator('main').textContent()).includes('Учебный объект А'));assert.ok(!(await employee.locator('main').textContent()).includes('Закрытый объект Б'));
 await employee.goto(contractUrl);const text=await employee.locator('main').textContent();assert.ok(text.includes('ДЕМО-ОБЩИЙ-001'));for(const hidden of ['Закрытый предмет','Закрытый контрагент','125000','Закрытое внутреннее','Закрытый объект Б'])assert.ok(!text.includes(hidden),hidden);
 await employee.goto(base+'/cabinet/contracts/?q='+encodeURIComponent('Закрытый предмет'));assert.equal(await employee.locator('main a[href="'+new URL(contractUrl).pathname+'"]').count(),0);
 const denied=await employee.goto(objectUrls[1]);assert.equal(denied.status(),404);
 phase='responsive screens';for(const width of [1440,768,390,320]){await leader.setViewportSize({width,height:1000});for(const route of ['/cabinet/objects/','/cabinet/objects/new/','/cabinet/contracts/','/cabinet/contracts/new/',new URL(objectUrls[0]).pathname,new URL(objectUrls[0]).pathname+'access/',new URL(contractUrl).pathname,new URL(contractUrl).pathname+'access/']){await leader.goto(base+route);assert.equal(await leader.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${width} ${route}`);}console.log('PASS project screens width',width);}
 await leader.setViewportSize({width:1440,height:1000});await leader.goto(objectUrls[0]);await leader.screenshot({path:'/tmp/armada-working-object.png',fullPage:true});assert.deepEqual(errors,[]);await b.close();console.log('PASS objects, many-to-many contract, individual field access, restricted search and direct URLs');
})().catch(e=>{console.error('Project browser acceptance failed at '+phase+': '+(phase==='login'?e.name:String(e.message).slice(0,200)));process.exit(1)});
