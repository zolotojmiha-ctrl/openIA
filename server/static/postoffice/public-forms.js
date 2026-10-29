/* A success message means a durable server receipt, not just a button click. */
for(const form of document.querySelectorAll('form[data-site-form]')){
 const status=form.querySelector('.form-status');let busy=false,receipt=null;
 function message(text,error=false){status.textContent=text;status.classList.toggle('form-error',error);status.tabIndex=-1;}
 form.addEventListener('input',e=>e.target.removeAttribute('aria-invalid'));
 form.addEventListener('invalid',()=>message('Проверьте обязательные поля и согласие на обработку данных.',true),true);
 form.addEventListener('submit',async event=>{
  event.preventDefault();if(busy)return;
  if(!form.reportValidity())return;
  const resume=form.querySelector('[name=resume]')?.files?.[0];
  if(resume&&(resume.size===0||resume.size>5*1024*1024)){message('Прикрепите непустое резюме в PDF или DOCX размером до 5 МБ.',true);return;}
  busy=true;form.setAttribute('aria-busy','true');
  const buttons=[...form.querySelectorAll('button[type=submit]')],labels=buttons.map(b=>b.textContent);
  buttons.forEach(b=>{b.disabled=true;b.textContent='Отправляем…';});message('Отправляем обращение…');
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),90000);
  try{
   if(!receipt){
    const response=await fetch('/forms/token/?kind='+encodeURIComponent(form.dataset.siteForm),{credentials:'same-origin',cache:'no-store',signal:controller.signal});
    if(!response.ok)throw Error('token');receipt=await response.json();
   }
   const data=new FormData(form);data.set('token',receipt.token);
   const response=await fetch(form.action,{method:'POST',body:data,credentials:'same-origin',headers:{'X-CSRFToken':receipt.csrf,'Accept':'application/json'},signal:controller.signal});
   if(!response.headers.get('content-type')?.includes('application/json'))throw Error('response');
   const result=await response.json();
   if(!response.ok||!result.ok){
    for(const name of Object.keys(result.errors||{})){const field=form.elements.namedItem(name);if(field?.setAttribute)field.setAttribute('aria-invalid','true');}
    message(Object.values(result.errors||{}).flat().join(' ')||'Не удалось отправить обращение. Проверьте данные и повторите.',true);status.focus({preventScroll:true});return;
   }
   form.reset();receipt=null;message(result.message+' Номер обращения: '+result.reference+'.');status.focus({preventScroll:true});
  }catch(error){message('Не удалось получить подтверждение отправки. Данные остались в форме — проверьте соединение и повторите. Также можно написать на info@armada-sibir.ru.',true);}
  finally{clearTimeout(timer);busy=false;form.removeAttribute('aria-busy');buttons.forEach((b,i)=>{b.disabled=false;b.textContent=labels[i];});}
 });
}
