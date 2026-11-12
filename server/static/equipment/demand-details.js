document.addEventListener('click',event=>{
 const button=event.target.closest('[data-demand-target]');if(!button)return;
 const row=document.getElementById(button.dataset.demandTarget);if(!row)return;
 const open=button.getAttribute('aria-expanded')!=='true';row.hidden=!open;
 button.setAttribute('aria-expanded',String(open));button.textContent=open?'Скрыть':'Показать по км';
});
