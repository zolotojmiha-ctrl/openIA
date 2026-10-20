document.documentElement.classList.add('js');
for(const form of document.querySelectorAll('[data-selection-form],[data-people-selection]')){
 const start=form.querySelector('[data-start-selection]');
 start?.addEventListener('click',()=>{form.classList.add('selecting');form.querySelectorAll('.file-group').forEach(x=>x.open=true);form.querySelector('.selection-control')?.focus()});
 form.querySelector('[data-end-selection]')?.addEventListener('click',()=>{form.classList.remove('selecting');form.querySelectorAll('input[type=checkbox]').forEach(x=>x.checked=false);form.dispatchEvent(new Event('change'));start?.focus()});
 form.addEventListener('submit',e=>{const boxes=[...form.querySelectorAll('input[name=documents],input[name=people]')];if(boxes.length&&!boxes.some(x=>x.checked)){e.preventDefault();start?.click();announce('Сначала выберите нужные файлы или людей.')}});
}
function announce(text){let status=document.getElementById('interface-status');if(!status){status=document.createElement('div');status.id='interface-status';status.setAttribute('role','status');document.body.append(status)}status.textContent=text;setTimeout(()=>status.remove(),6000)}
for(const set of document.querySelectorAll('[data-tab-set]')){
 const buttons=[...set.querySelectorAll('[data-tab]')],panels=[...set.querySelectorAll('[data-tab-panel]')];
 function activate(button){for(const b of buttons){b.classList.toggle('active',b===button);b.setAttribute('aria-selected',String(b===button));b.setAttribute('tabindex',b===button?'0':'-1')}for(const p of panels)p.hidden=p.id!==button.dataset.tab;}
 for(const [index,b]of buttons.entries()){b.setAttribute('role','tab');b.setAttribute('aria-controls',b.dataset.tab);b.addEventListener('click',()=>activate(b));b.addEventListener('keydown',e=>{let target;if(e.key==='ArrowRight')target=buttons[(index+1)%buttons.length];if(e.key==='ArrowLeft')target=buttons[(index+buttons.length-1)%buttons.length];if(e.key==='Home')target=buttons[0];if(e.key==='End')target=buttons.at(-1);if(target){e.preventDefault();activate(target);target.focus()}})}
 if(buttons.length){buttons[0].parentElement.setAttribute('role','tablist');activate(buttons.find(b=>b.getAttribute('aria-selected')==='true')||buttons[0]);}
}
for(const form of document.querySelectorAll('form[data-confirm]'))form.addEventListener('submit',e=>{if(!confirm(form.dataset.confirm))e.preventDefault()});
const toggle=document.querySelector('.nav-toggle'),nav=document.getElementById('cabinet-navigation');toggle?.addEventListener('click',()=>{const open=toggle.getAttribute('aria-expanded')!=='true';toggle.setAttribute('aria-expanded',String(open));nav.classList.toggle('mobile-open',open)});
for(const link of document.querySelectorAll('.sidebar nav a')){const path=new URL(link.href).pathname;link.classList.toggle('active',path==='/cabinet/'?location.pathname===path:location.pathname.startsWith(path));}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){document.querySelectorAll('.action-menu[open],.inline-menu[open]').forEach(x=>x.open=false);nav?.classList.remove('mobile-open');toggle?.setAttribute('aria-expanded','false')}});
document.addEventListener('click',e=>{for(const menu of document.querySelectorAll('.action-menu[open],.inline-menu[open]'))if(!menu.contains(e.target))menu.open=false});
// Reveal invalid fields inside optional details after server-side validation.
for(const list of document.querySelectorAll('.errorlist')){let details=list.closest('details');while(details){details.open=true;details=details.parentElement.closest('details')}}

// Clear private field drafts on an explicit logout, from every cabinet page.
for(const form of document.querySelectorAll('form[action$="/logout/"]'))form.addEventListener('submit',async event=>{
 if(!window.indexedDB)return;event.preventDefault();window.dispatchEvent(new Event('armada:logout'));
 try{await new Promise((resolve,reject)=>{const req=indexedDB.open('armada-field-drafts',1);req.onupgradeneeded=()=>req.result.createObjectStore('drafts',{keyPath:'key'});req.onerror=()=>reject(req.error);req.onsuccess=()=>{const db=req.result;if(!db.objectStoreNames.contains('drafts')){db.close();resolve();return;}const tx=db.transaction('drafts','readwrite');tx.objectStore('drafts').clear();tx.oncomplete=()=>{db.close();resolve();};tx.onerror=()=>{db.close();reject(tx.error);};};});}catch{}
 form.submit();
});
