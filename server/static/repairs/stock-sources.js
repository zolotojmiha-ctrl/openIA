/* Read-only refresh of compatible sources after actual dimensions change. */
(() => {
  const box=document.querySelector('[data-stock-sources]');
  if(!box)return;
  const form=box.closest('form'),status=form.querySelector('[data-stock-sources-status]');
  const names=['length_m','composition','wall_mm','manual_serials','installation_override'];
  let timer,request,sequence=0;
  async function refresh(){
    const current=++sequence;
    if(request)request.abort();
    request=new AbortController();
    const timeout=setTimeout(()=>request.abort(),5000);
    const data=new FormData();
    for(const name of names){const input=form.elements.namedItem(name);if(input)data.set(name,input.value);}
    data.set('csrfmiddlewaretoken',form.querySelector('[name=csrfmiddlewaretoken]').value);
    const selected=new Map([...box.querySelectorAll('select')].map(input=>[input.name,input.value]));
    status.textContent='Проверяем совместимые складские источники…';
    try{
      const response=await fetch(box.dataset.url,{method:'POST',body:data,credentials:'same-origin',signal:request.signal});
      if(!response.ok)throw new Error('Source lookup failed');
      const result=await response.json();
      if(current!==sequence)return;
      box.innerHTML=result.html;
      for(const input of box.querySelectorAll('select')){
        const previous=selected.get(input.name);
        if([...input.options].some(option=>option.value===previous))input.value=previous;
      }
      status.textContent='Источники обновлены по фактическим характеристикам.';
    }catch(error){
      if(current===sequence)status.textContent='Не удалось обновить список. При отправке склад будет проверен повторно; при необходимости форма предложит уточнить источник.';
    }finally{clearTimeout(timeout);}
  }
  for(const name of names){
    const input=form.elements.namedItem(name);
    if(input)input.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(refresh,250);});
  }
})();
