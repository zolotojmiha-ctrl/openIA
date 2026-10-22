(()=>{'use strict';
const section=document.querySelector('select[name=section]'),master=document.querySelector('select[name=assigned_to]'),contract=document.querySelector('select[name=contract]');let requestId=0;
if(section&&master&&!section.disabled)section.addEventListener('change',async()=>{
 const token=++requestId,current=section.value,cid=contract?.value||'';
 if(!current){master.replaceChildren(new Option('Сначала выберите участок',''));master.disabled=false;return;}
 master.disabled=true;master.replaceChildren(new Option('Загружаем мастеров…',''));
 try{
  const response=await fetch('/cabinet/repairs/sections/'+encodeURIComponent(current)+'/assignees/?contract='+encodeURIComponent(cid),{headers:{Accept:'application/json'}});
  if(!response.ok)throw Error();const data=await response.json();
  if(token!==requestId||section.value!==current||(contract?.value||'')!==cid)return;
  master.replaceChildren(new Option('Пока не назначать',''));for(const user of data.users)master.add(new Option(user.name,user.id));
 }catch{if(token===requestId)master.replaceChildren(new Option('Можно назначить после сохранения работы',''));}
 finally{if(token===requestId)master.disabled=false;}
});
const all=document.querySelector('[data-repair-select-all]');if(all)all.addEventListener('click',()=>{const checkboxes=[...document.querySelectorAll('[name=jobs]')];const checked=checkboxes.some(c=>!c.checked);checkboxes.forEach(c=>c.checked=checked);});
})();
