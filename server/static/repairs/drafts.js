/* Read only the signed-in user's local drafts. No private page cache or automatic submission. */
(()=>{'use strict';const root=document.querySelector('[data-local-drafts]');if(!root)return;
const state=document.querySelector('[data-local-drafts-state]');let db;
const actions={repaired:'Устранён',unrepaired:'Не устранён',note:'Заметка',problem:'Препятствие',result:'Результат',start:'Начало работ',resolve:'Препятствие устранено',correction:'Исправление'};
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function request(mode,fn){return new Promise((resolve,reject)=>{const tx=db.transaction('drafts',mode);const req=fn(tx.objectStore('drafts'));let value;req.onsuccess=()=>value=req.result;tx.oncomplete=()=>resolve(value);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);});}
function text(tag,value){const node=document.createElement(tag);node.textContent=value;return node;}
async function render(){const all=await request('readonly',s=>s.getAll());root.replaceChildren();const rows=all.filter(d=>{if(typeof d.key!=='string')return false;const [user,job,action,...rest]=d.key.split(':');return user===root.dataset.user&&uuid.test(job)&&Object.hasOwn(actions,action)&&!rest.length;}).sort((a,b)=>(b.time||0)-(a.time||0));
state.textContent=rows.length?'Черновиков на устройстве: '+rows.length:'Неотправленных черновиков в этом браузере нет.';
for(const row of rows){const [,job,action]=row.key.split(':');const panel=document.createElement('section');panel.className='panel';panel.append(text('h2',actions[action]));
if(row.label)panel.append(text('p',String(row.label).slice(0,500)));else panel.append(text('p','Работа № '+job));
const date=new Date(row.time);panel.append(text('p','Сохранён: '+(Number.isNaN(date.getTime())?'дата неизвестна':date.toLocaleString('ru-RU'))));
const files=Array.isArray(row.files)?row.files:[];panel.append(text('p','Фотографий / файлов: '+files.length+' · '+Math.ceil(files.reduce((n,f)=>n+(Number(f.size)||0),0)/1024)+' КБ'));
if(row.delivery==='received')panel.append(text('p','Сервер подтвердил отправку. Осталась локальная копия — её можно удалить.'));
if(row.delivery==='uncertain')panel.append(text('p','Нет подтверждения отправки. Проверьте историю работы; повторная отправка использует тот же номер запроса.'));
const controls=document.createElement('div');controls.style.cssText='display:flex;gap:12px;flex-wrap:wrap';const link=text('a',row.delivery==='received'?'Открыть историю работы':'Открыть и проверить');link.className='button';link.href=root.dataset.repairsUrl+job+(row.delivery==='received'?'/':'/report/'+action+'/');controls.append(link);
const remove=text('button','Удалить черновик с устройства');remove.type='button';remove.className='button secondary';remove.addEventListener('click',async()=>{if(!window.confirm('Удалить этот черновик и его локальные фотографии? Сохранённая на сервере история не изменится.'))return;remove.disabled=true;try{await request('readwrite',s=>s.delete(row.key));await render();}catch{remove.disabled=false;state.textContent='Не удалось удалить черновик. Попробуйте ещё раз.';}});controls.append(remove);panel.append(controls);root.append(panel);}}
(async()=>{try{db=await new Promise((resolve,reject)=>{const req=indexedDB.open('armada-field-drafts',1);req.onupgradeneeded=()=>req.result.createObjectStore('drafts',{keyPath:'key'});req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);});await render();}catch{state.textContent='Не удалось открыть локальное хранилище. Проверьте настройки браузера; черновики с других устройств здесь не отображаются.';}})();
})();
