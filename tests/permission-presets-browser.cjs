const {chromium}=require('playwright'),assert=require('node:assert/strict'),fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',args:['--no-sandbox']}),page=await browser.newPage({viewport:{width:1440,height:1000}}),base=process.env.ARMADA_TEST_URL,profiles=JSON.parse(process.env.ARMADA_TEST_PROFILES),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base+'/cabinet/login/');await page.locator('[name=phone]').fill('+70000000001');await page.locator('[name=password]').fill(process.env.ARMADA_TEST_PASSWORD);await page.locator('button[type=submit]').click();await page.waitForURL(base+'/cabinet/');
 await page.goto(base+'/cabinet/users/new/');
 const role=page.locator('[name=role]'),profile=page.locator('[name=profile]');
 const checked=()=>page.locator('[name=permissions]:checked').evaluateAll(inputs=>inputs.map(input=>input.value).sort());
 for(const [key,permissions] of Object.entries(profiles)){
  await role.selectOption(key);assert.equal(await profile.inputValue(),key,key+' profile');
  assert.deepEqual(await checked(),[...permissions].sort(),key+' checkbox preview');
  for(const value of permissions)assert.equal(await page.locator('[name=permissions][value="'+value+'"]').isDisabled(),true);
  assert.equal(await page.locator('[data-account-field=customer_scopes]').isVisible(),key==='customer');
 }
 await profile.selectOption('pto');assert.equal(await role.inputValue(),'pto');assert.deepEqual(await checked(),[...profiles.pto].sort());
 await role.selectOption('foreman');await page.locator('[name=permissions][value="document.export"]').check();
 await role.selectOption('mechanic');assert.deepEqual(await checked(),[...new Set([...profiles.mechanic,'document.export'])].sort());
 await profile.selectOption('custom');assert.equal(await page.locator('[name=permissions]:disabled').count(),0);
 await role.selectOption('customer');await role.selectOption('foreman');assert.deepEqual(await checked(),[...profiles.foreman].sort());
 await page.locator('[name=full_name]').fill('ТЕСТ Новый мастер');await page.locator('[name=phone]').fill('+70000000999');
 fs.mkdirSync('/workspace/artifacts',{recursive:true});
 for(const width of [320,390,768,1440]){await page.setViewportSize({width,height:950});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'width '+width);if(width===390)await page.screenshot({path:'/workspace/artifacts/permission-presets-foreman-phone.png',fullPage:true});}
 await page.getByRole('button',{name:'Создать и выдать временный пароль',exact:true}).click();await page.waitForLoadState('networkidle');assert.equal(await page.locator('.errorlist').count(),0);
 await page.goto(base+'/cabinet/users/'+process.env.ARMADA_TEST_OLD_USER+'/');assert.equal(await profile.inputValue(),'custom');assert.deepEqual(await checked(),[]);
 await page.getByRole('button',{name:'Подставить набор выбранной профессии',exact:true}).click();assert.equal(await profile.inputValue(),'foreman');assert.deepEqual(await checked(),[...profiles.foreman].sort());
 assert.deepEqual(errors,[]);await browser.close();console.log('PASS all 12 role/profile previews, dropdown synchronisation, custom extras, customer isolation, four viewport widths, actual new-master grants and unchanged old employee');
})().catch(e=>{console.error(String(e));process.exit(1)});
