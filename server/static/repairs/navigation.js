(()=>{if(window.armadaRepairNavigation)return;window.armadaRepairNavigation=true;
const key='armada-repair-scroll:'+location.pathname+location.search;
try{const y=sessionStorage.getItem(key);if(y!==null)requestAnimationFrame(()=>scrollTo(0,Number(y)));}catch(e){}
addEventListener('pagehide',()=>{try{sessionStorage.setItem(key,String(scrollY));}catch(e){}});
const params=new URLSearchParams(location.search);let back=params.get('return_to');
if(!back){const a=document.querySelector('a.back-link');if(/\/cabinet\/repairs\/[a-f0-9-]+\/$/.test(location.pathname)&&a)back=a.getAttribute('href');}
if(!back||!back.startsWith('/cabinet/')||back.startsWith('//')||back.includes('\\'))return;
for(const a of document.querySelectorAll('a[href]')){if(a.classList.contains('back-link'))continue;const u=new URL(a.href,location.href);if(u.origin===location.origin&&(/\/cabinet\/repairs\/[a-f0-9-]+\//.test(u.pathname)||u.pathname.includes('/defects/'))&&!u.searchParams.has('return_to')){u.searchParams.set('return_to',back);a.href=u.pathname+u.search;}}
for(const form of document.querySelectorAll('form[method="post"]')){if(!form.querySelector('[name="return_to"]')){const i=document.createElement('input');i.type='hidden';i.name='return_to';i.value=back;form.append(i);}}
})();
