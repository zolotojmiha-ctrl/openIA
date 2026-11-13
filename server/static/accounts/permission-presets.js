(()=>{
 const select=document.querySelector('[name=profile]'),node=document.getElementById('permission-presets');
 if(!select||!node)return;
 const presets=JSON.parse(node.textContent),role=document.querySelector('[name=role]'),help=document.querySelector('[data-role-help]');
 const applyButton=document.querySelector('[data-apply-role-profile]');
 const originalHelp=help?.textContent,inputs=[...document.querySelectorAll('[name=permissions]')];
 let prior=[];
 const available=key=>[...select.options].some(option=>option.value===key);
 const roleAvailable=key=>role&&[...role.options].some(option=>option.value===key);
 function renderRole(){
  const customer=role?.value==='customer',scopes=document.querySelector('[data-account-field=customer_scopes]');
  if(scopes)scopes.hidden=!customer;
  for(const name of ['permissions','request_mail','mail_address']){
   const box=document.querySelector('[data-account-field='+name+']');if(box)box.hidden=customer;
  }
  for(const input of document.querySelectorAll('[name=request_mail]')){
   input.disabled=customer;if(customer)input.checked=false;
  }
  if(applyButton)applyButton.disabled=!available(role?.value);
  if(help)help.textContent=presets[select.value]?.help||originalHelp;
 }
 function applyProfile(initial=false){
  const chosen=presets[select.value],custom=!chosen;
  for(const input of inputs){
   // Custom mode unlocks the currently displayed grants for editing.
   if(!initial&&!custom&&prior.includes(input.value))input.checked=false;
   input.disabled=false;
  }
  prior=chosen?.permissions||[];
  for(const input of inputs)if(prior.includes(input.value)){input.checked=true;input.disabled=true;}
  if(role?.value==='customer')for(const input of inputs){input.checked=false;input.disabled=true;}
  renderRole();
 }
 function chooseRole(){select.value=available(role?.value)?role.value:'custom';applyProfile();}
 role?.addEventListener('change',chooseRole);
 applyButton?.addEventListener('click',chooseRole);
 select.addEventListener('change',()=>{
  if(roleAvailable(select.value))role.value=select.value;
  else if(role?.value==='customer')role.value='other';
  applyProfile();
 });
 // The server chooses a safe initial profile. Do not apply the role again:
 // saved custom permissions must not change just because the form was opened.
 applyProfile(true);
})();
