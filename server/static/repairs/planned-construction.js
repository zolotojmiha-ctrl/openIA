/* Explain the plan while preserving the separate actual warehouse dimensions. */
(()=>{'use strict';
for(const root of document.querySelectorAll('[data-planned-construction]')){
 const length=root.querySelector('[name="'+(root.dataset.planLength||'length_m')+'"]');
 const composition=root.querySelector('[name="'+(root.dataset.planComposition||'composition')+'"]');
 const method=root.querySelector('[name="method"]');
 if(!length||!composition)continue;
 const notice=document.createElement('div');notice.className='notice';notice.setAttribute('role','status');notice.dataset.plannedFeedback='1';
 (length.closest('p')||length).after(notice);
 const number=text=>{text=text.trim().replace(',','.');if(!/^\d+(?:\.\d{1,3})?$/.test(text))return null;const [whole,fraction='']=text.split('.'),mm=Number(whole)*1000+Number(fraction.padEnd(3,'0'));return mm>0&&mm<1000000000?mm:null;};
 const metres=mm=>String(mm/1000).replace('.',',');
 function update(){
  notice.replaceChildren();notice.hidden=true;
  if(!['p1','p2','p3','p4','p5u'].includes(method?method.value:root.dataset.planMethodValue))return;
  const text=composition.value.trim();if(!text)return;
  const parts=text.split('+').map(number);
  notice.hidden=false;
  if(parts.some(p=>p===null)){notice.textContent='В составе укажите длины в метрах через плюс, например 1,5+1,5. Для одной муфты оставьте состав пустым.';return;}
  const sum=parts.reduce((a,b)=>a+b,0);if(sum>=1000000000){notice.textContent='Общая длина должна быть меньше 1000000 м.';return;}
  if(!length.value.trim()){notice.textContent='Общая плановая длина будет рассчитана из состава: '+metres(sum)+' м.';return;}
  const planned=number(length.value);if(planned===sum){notice.textContent='Состав: '+parts.length+' шт. Общая плановая длина: '+metres(sum)+' м.';return;}
  notice.append(document.createTextNode('Общая длина '+length.value+' м, а сумма состава '+metres(sum)+' м. Уточните план перед сохранением. '));
  const use=document.createElement('button');use.type='button';use.className='button secondary small';use.textContent='Взять длину из состава';use.addEventListener('click',()=>{length.value=String(sum/1000);length.dispatchEvent(new Event('input',{bubbles:true}));});
  const clear=document.createElement('button');clear.type='button';clear.className='button secondary small';clear.textContent='Одна муфта — убрать состав';clear.addEventListener('click',()=>{composition.value='';composition.dispatchEvent(new Event('input',{bubbles:true}));});
  notice.append(use,document.createTextNode(' '),clear);
 }
 length.addEventListener('input',update);composition.addEventListener('input',update);if(method)method.addEventListener('change',update);update();
}
})();
