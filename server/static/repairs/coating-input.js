(()=>{
 const section=document.querySelector('[data-coating-completion]');if(!section)return;
 const form=section.closest('form'),input=section.querySelector('[name=actual_coating_length_m]');
 function sync(){const partial=form.querySelector('[name=outcome]:checked')?.value==='partial';section.hidden=partial;input.disabled=partial;input.required=!partial&&section.dataset.requiredOnComplete==='1';}
 form.addEventListener('change',event=>{if(event.target.name==='outcome')sync();});
 new MutationObserver(sync).observe(form,{attributes:true,attributeFilter:['data-draft-ready']});sync();
})();
