/* Selection is scoped to one visible form group; it never submits or changes access. */
(()=>{'use strict';const groups=new Set([...document.querySelectorAll('[data-bulk-check]')].map(node=>node.matches('input')?document.getElementById('id_'+node.name):node).filter(Boolean));for(const group of groups){
 const inputs=[...group.querySelectorAll('input[type=checkbox]')].filter(input=>!input.disabled);if(!inputs.length)continue;
 const controls=document.createElement('div');controls.style.cssText='display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:12px 0';
 const all=document.createElement('button'),none=document.createElement('button'),count=document.createElement('span');
 for(const button of [all,none]){button.type='button';button.className='button secondary';}
 all.textContent='Выделить всё';none.textContent='Снять выделение';count.setAttribute('role','status');
 function update(){count.textContent='Выбрано: '+inputs.filter(i=>i.checked).length+' из '+inputs.length;}
 function select(value){for(const input of inputs){input.checked=value;input.dispatchEvent(new Event('change',{bubbles:true}));}update();}
 all.addEventListener('click',()=>select(true));none.addEventListener('click',()=>select(false));group.addEventListener('change',update);
 controls.append(all,none,count);group.before(controls);update();
}})();
