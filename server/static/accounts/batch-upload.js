(()=>{
const form=document.querySelector('#batch-upload'),input=document.querySelector('#batch-files'),list=document.querySelector('#batch-list');if(!form)return;
const uuid=()=>{const b=new Uint8Array(16);crypto.getRandomValues(b);b[6]=(b[6]&15)|64;b[8]=(b[8]&63)|128;const h=[...b].map(x=>x.toString(16).padStart(2,'0')).join('');return h.slice(0,8)+'-'+h.slice(8,12)+'-'+h.slice(12,16)+'-'+h.slice(16,20)+'-'+h.slice(20)};
let entries=[],busy=false,bundle='',attempt=0;
function show(){list.replaceChildren();for(const entry of entries){const row=document.createElement('div');row.className='row';const text=document.createElement('span');const name=document.createElement('input');name.type='text';name.value=entry.name;name.maxLength=200;name.disabled=entry.done||busy;name.setAttribute('aria-label','Имя файла');name.addEventListener('input',()=>entry.name=name.value);text.textContent=entry.status;row.append(name,text);if(entry.error)row.classList.add('notice');if(!entry.done){const remove=document.createElement('button');remove.type='button';remove.textContent='Убрать';remove.disabled=busy;remove.addEventListener('click',()=>{entries=entries.filter(x=>x!==entry);show()});row.append(remove)}if(entry.url){const link=document.createElement('a');link.href=entry.url;link.textContent='Открыть';row.append(link)}list.append(row);}}
function add(files){if(busy)return;for(const file of files)entries.push({file,name:file.name,token:uuid(),status:'Готов к загрузке',done:false,error:false,collision:null});show();}
input.addEventListener('change',()=>add(input.files));const dropzone=document.querySelector('#file-dropzone');dropzone.addEventListener('dragover',e=>e.preventDefault());dropzone.addEventListener('drop',e=>{e.preventDefault();add(e.dataTransfer.files)});
form.addEventListener('submit',async e=>{e.preventDefault();if(busy||!entries.length)return;const common=new FormData(form);busy=true;const frozen=[...form.elements].filter(el=>el.id!=='apply-collision'&&!el.disabled);for(const el of frozen)el.disabled=true;const button=form.querySelector('button[type=submit]');button.disabled=true;let apply=form.elements.collision.value;
for(const entry of entries.filter(x=>!x.done)){
 entry.status='Загружается…';entry.error=false;show();let result;
 try{
  for(let retry=0;retry<3;retry++){
   const data=new FormData();for(const[k,v]of common.entries())data.append(k,v);data.set('file',entry.file,entry.name||entry.file.name);data.set('upload_token',entry.token);data.set('collision',entry.collision||apply);if(entries.length>1)data.set('title','');
   if(bundle){data.set('is_primary','');data.set('bundle',bundle);data.set('bundle_title','');}
   const response=await fetch(form.action||location.href,{method:'POST',body:data,headers:{'X-Requested-With':'XMLHttpRequest'}});result=await response.json();
   if(response.ok){entry.done=true;entry.url=result.url;entry.name=result.name;entry.status='Сохранён';if(result.bundle)bundle=result.bundle;break;}
   const errors=Object.values(result.errors||{}).flat().map(x=>x.message).join(' ');
   if(response.status===409&&errors.includes('названием')&&(entry.collision||apply)==='ask'){
    const dialog=document.createElement('dialog'),p=document.createElement('p');p.textContent=entry.name+': файл с таким названием уже есть.';dialog.append(p);
    const choice=await new Promise(resolve=>{for(const [value,label]of [['replace','Заменить'],['both','Оставить оба'],['skip','Пропустить']]){const b=document.createElement('button');b.type='button';b.textContent=label;b.addEventListener('click',()=>{dialog.close();dialog.remove();resolve(value)});dialog.append(b);}dialog.addEventListener('cancel',ev=>{ev.preventDefault();dialog.close();dialog.remove();resolve('skip')});document.body.append(dialog);dialog.showModal();});
    if(choice==='skip'){entry.status='Пропущен';entry.error=true;break;}
    entry.collision=choice;if(document.querySelector('#apply-collision').checked)apply=choice;continue;
   }
   entry.status=errors||'Не удалось сохранить';entry.error=true;break;
  }
 }catch(err){entry.status='Нет подтверждения сервера. Проверьте список документов перед повтором.';entry.error=true;}
 show();}
for(const el of frozen)el.disabled=false;button.disabled=false;button.textContent=entries.some(x=>!x.done)?'Повторить только несохранённые':'Все файлы сохранены';busy=false;show();
});
})();
