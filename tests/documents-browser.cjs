const{chromium}=require('playwright');const assert=require('node:assert/strict');
let phase='login';
(async()=>{
 const base=process.env.ARMADA_TEST_URL;const b=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',args:['--no-sandbox']});
 const leader=await b.newPage(),employee=await b.newPage();const errors=[];
 for(const p of [leader,employee])p.on('pageerror',e=>errors.push(e.name));
 async function signIn(p,phone){await p.goto(base+'/cabinet/login/');await p.locator('[name=phone]').fill(phone);await p.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);await p.locator('button[type=submit]').click();}
 await signIn(employee,'+70000000003');await signIn(leader,'+70000000001');
 phase='upload';await employee.goto(base+'/cabinet/documents/upload/');await employee.locator('[name=title]').fill('Учебный документ');await employee.locator('[name=linked_objects]').first().check();await employee.locator('[name=file]').setInputFiles({name:'example.pdf',mimeType:'application/pdf',buffer:Buffer.from('%PDF-1.7\nsynthetic\n%%EOF')});await employee.locator('main button').click();await employee.waitForURL(/\/documents\/[a-f0-9-]+\/$/);const docUrl=employee.url();
 assert.ok((await employee.locator('main').textContent()).includes('Ожидает утверждения'));
 phase='decision';await leader.goto(docUrl);await leader.getByRole('link',{name:'Принять решение по версии 1'}).click();await leader.locator('[name=decision][value=revision]').check();await leader.locator('[name=reason]').fill('Исправить учебный файл');await leader.locator('main button').click();
 await employee.reload();assert.equal(await employee.locator('main a[href$="/download/"]').count(),0);assert.ok((await employee.locator('main').textContent()).includes('Исправить учебный файл'));
 phase='new version';await employee.getByRole('link',{name:'Загрузить новую версию'}).click();await employee.locator('[name=comment]').fill('Учебное исправление');await employee.locator('[name=file]').setInputFiles({name:'fixed.pdf',mimeType:'application/pdf',buffer:Buffer.from('%PDF-1.7\nfixed\n%%EOF')});await employee.locator('main button').click();await employee.waitForURL(docUrl);
 await leader.goto(docUrl);await leader.getByRole('link',{name:'Принять решение по версии 2'}).click();await leader.locator('[name=decision][value=approved]').check();await leader.locator('main button').click();await employee.reload();assert.ok((await employee.locator('main').textContent()).includes('Утверждена'));
 phase='responsive';for(const width of [1440,768,390,320]){await leader.setViewportSize({width,height:1000});for(const route of ['/cabinet/documents/','/cabinet/documents/upload/','/cabinet/documents/approvals/',new URL(docUrl).pathname,new URL(docUrl).pathname+'access/']){await leader.goto(base+route);assert.equal(await leader.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${width} ${route}`);}}
 await leader.goto(docUrl);await leader.screenshot({path:'/tmp/armada-documents-v3.png',fullPage:true});assert.deepEqual(errors,[]);await b.close();console.log('PASS upload, rejection, new version, approval and responsive documents screens');
})().catch(e=>{console.error('Documents browser failed at '+phase+': '+String(e.message).slice(0,300));process.exit(1)});
