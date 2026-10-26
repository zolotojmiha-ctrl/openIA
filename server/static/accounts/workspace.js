// Selection and progressive help; never insert document names as HTML.
for(const toggle of document.querySelectorAll('[data-select-all]'))toggle.addEventListener('change',()=>{for(const cb of toggle.closest('form').querySelectorAll('[name=documents]'))cb.checked=toggle.checked;});
if(!document.body.dataset.hintsOff){
 const descriptions={
 'id_hints':'Короткие пояснения к полям и действиям. Эта настройка меняется только у вас.',
 'id_notify_documents':'Всплывающее сообщение о новом доступном файле. Счётчик «Входящие» остаётся независимо от этой настройки.',
 'id_notify_versions':'Сообщать о новых версиях доступных файлов. Предыдущие версии сохраняются.',
 'id_notify_comments':'Сообщать, когда коллега обратил ваше внимание на комментарий к доступному документу.',
 'id_large_text':'Увеличивает текст в вашем кабинете. Настройки других пользователей не меняются.',
 'id_compact':'Уменьшает отступы в списке документов, чтобы видеть больше строк.',
 'id_role':'Рабочая роль помогает организовать кабинет. Доступ к чужим файлам открывается отдельно.',
 'id_is_active':'При отключении вход блокируется и прежние сеансы завершаются. Документы и история сохраняются.',
 'id_confirm_duplicate':'Отмечайте только если это действительно другая выплата, а не повторный ввод одной операции.',
 'id_factual_amount':'Фактически выполненная сумма. Принятое заказчиком выполнение подтверждает отдельно один из руководителей.',
 'id_accepted_amount':'Сумма принятого выполнения. Она учитывается отдельно от полученных денег и зачёта аванса.',
 'id_paid_amount':'Только фактически оплаченная часть затрат. Обязательства и списанные деньги — разные показатели.',
 'id_reference':'Номер платежа или другое основание, по которому его можно сверить с исходным документом.',
 'id_source':'Источник или счёт поступления — для сверки нескольких источников.',
 'id_basis':'Ссылка на документ, условие договора или пояснение, по которому сделана запись.',
 'id_planned_profit':'Плановый показатель вводится отдельно и не подменяет фактический результат.',
 'id_text':'Текст будет доступен участникам этого обсуждения или обращения в пределах их прав.',
 'id_full_name':'Фамилия, имя и отчество. Карточка работника сама по себе не создаёт ему вход в программу.',
 'id_date':'Дата документа или операции по источнику. Не заменяет дату фактической загрузки в систему.',
 'id_number':'Реквизит для поиска. Можно заполнить позднее.',
 'id_title':'Понятное название для поиска. При загрузке файлов можно оставить их исходные имена.',
 'id_bundle_title':'Общее название для договора и приложений. Комплект создаётся один раз на всю пачку.',
 'id_comment':'Краткое пояснение к изменениям. Комментарий не изменяет содержимое предыдущих версий.',

 'id_section':'Раздел помогает найти файл. Сам по себе он не открывает доступ коллегам.',
 'id_bundle':'Получатели комплекта увидят и добавленные позже файлы. Отдельный запрет файла сильнее общего доступа.',
 'id_is_primary':'Основной договор показывается над приложениями в комплекте.',
 'id_link_not_required':'Документу не нужна связь с объектом, договором или работником. Он не попадёт в список «На разбор».',
 'id_defer_link':'Сохраните файлы сейчас, а связь назначьте позже из списка «На разбор».',
 'id_can_replace':'Получатель сможет загружать новые версии. Старые файлы останутся в истории.',
 'id_enabled':'Снимите отметку, чтобы закрыть этому человеку доступ. Скачанные копии останутся на его компьютере.',
 'id_collision':'Совпадение определяется по имени файла в выбранном месте, а не по его содержимому.',
 'id_payment_due':'Дата оплаты из условий договора. Без даты программа не объявляет долг просроченным.',
 'id_scope':'Если для накладных затрат направление не выбрано, они остаются общими без автоматического распределения.',
 'id_vat_description':'Условия НДС вводятся по конкретному договору. Отсутствующая ставка не заменяется предположением.'};
 if(location.pathname.startsWith('/cabinet/users/'))descriptions.id_full_name='Имя пользователя, которому предоставляется вход в кабинет.';
 if(location.pathname.startsWith('/cabinet/reminders/'))descriptions.id_date='Дата вашего личного напоминания. Другим пользователям оно не назначается.';
 if(location.pathname.startsWith('/cabinet/repairs/'))Object.assign(descriptions,{
  id_section:'Участок трубопровода в пределах объекта. Определяет место работ; доступ мастеру назначается отдельно.',
  id_kilometre:'Километр из ведомости. Сохраняем до шести знаков после запятой.',
  id_defect_numbers:'Несколько номеров через точку с запятой относятся к одной общей ремонтной конструкции.',
  id_pipe_section:'Номер трубы между двумя кольцевыми стыками из исходной ведомости.',
  id_length_m:'Общая длина конструкции в метрах. Размеры отдельных муфт указываются отдельно.',
  id_composition:'Например, 2+2+1,5 — отдельные муфты в составе общей конструкции.',
  id_performed_date:'Фактическая дата этого события. Она не заменяет плановую дату или приёмку заказчиком.',
  id_assigned_to:'Мастер увидит эту работу, если ему также назначен доступ к объекту.',
  id_materials:'Наличие необходимых материалов учитывается отдельно от выполнения ремонта.',
  id_problem:'Причина, которая мешает работе. Фотографию и пояснение можно добавить ниже.',
  id_basis:'Номер акта или другое основание приёмки заказчиком. Финансовые суммы автоматически не меняются.',
  id_date:'Дата приёмки заказчиком по подтверждающему документу.',
  id_serial:'Заводской номер с маркировки или паспорта. Ведущие нули и знаки сохраняются.',
  id_length_mm:'Длина конкретной муфты в миллиметрах.',
  id_warehouse:'Место хранения или получения конкретной муфты.'});
 if(!location.pathname.includes('/cost'))descriptions.id_scope='Направление или договор, к которому относится финансовая запись.';
 for(const [id,description] of Object.entries(descriptions)){const field=document.getElementById(id);const label=document.querySelector('label[for="'+id+'"]');if(field&&label)label.dataset.hint=description;}
 const permissions={
  'document.upload':'Можно загружать свои файлы. Это не открывает доступ к чужим документам.',
  'document.export':'Можно выгрузить список доступных документов в CSV.',
  'personnel.manage':'Можно вести карточки работников и назначения на объекты. Учётные записи создают только руководители.',
  'finance.read':'Открывает финансовый учёт компании. Не выдавайте это право пользователям, которым суммы не нужны.',
  'finance.write':'Позволяет вносить финансовые записи и вручную распределять выплаты. Принятие выполнения подтверждают только руководители.'};
 for(const input of document.querySelectorAll('[name=permissions]')){const label=document.querySelector('label[for="'+input.id+'"]');if(label&&permissions[input.value])label.dataset.hint=permissions[input.value];}
 const actions={
  'Загрузить всё':'Сохранить выбранные файлы с общими настройками. Уже сохранённые файлы не загружаются повторно.',
  'Новая версия':'Загрузить обновлённый файл, сохранив предыдущие версии, связи и получателей.',
  'Открыть доступ':'Выбрать конкретных получателей и разрешить просмотр или также новые версии.',
  'В архив':'Скрыть файл от коллег без удаления содержимого и истории. Его можно восстановить.',
  'На разбор':'Документы, которым ещё не назначили ожидаемую связь. Открытие файла не убирает его из этого списка.',
  'Входящие':'Новые доступные файлы и версии. Прочтение учитывается отдельно для каждого человека.',
  'Персонал':'Карточки работников и их документов. Для хранения удостоверений вход работнику не нужен.',
  'Мой профиль':'Личные настройки, пароль, права и активные сеансы.',
  'Финансы':'Выполнения, выплаты, авансы, зачёты и затраты с раздельным учётом.'};
 for(const element of document.querySelectorAll('a,button')){const text=element.textContent.trim();for(const[key,description]of Object.entries(actions))if(text===key||text.startsWith(key+' ·')){element.title=description;break;}}
 for(const element of document.querySelectorAll('[data-hint]')){
  const button=document.createElement('button');button.type='button';button.className='hint';button.textContent='ⓘ';button.setAttribute('aria-label',element.dataset.hint);button.title=element.dataset.hint;
  button.addEventListener('click',()=>{let text=button.nextElementSibling;if(text?.classList.contains('hint-text'))text.remove();else{text=document.createElement('small');text.className='hint-text';text.textContent=element.dataset.hint;button.after(text)}});element.append(button);
 }
}

for(const form of document.querySelectorAll('form')){
 const buttons=form.querySelectorAll('[data-requires-manage]');if(!buttons.length)continue;
 const update=()=>{const checked=[...form.querySelectorAll('[name=documents]:checked')];for(const button of buttons)button.disabled=!checked.length||checked.some(x=>x.dataset.manage==='0');};
 form.addEventListener('change',update);update();
}

if(document.querySelector('[data-inbox-count]')){
 let polling=false;
 async function notifications(){if(polling||document.hidden)return;polling=true;try{
  const response=await fetch('/cabinet/notifications/',{headers:{'Accept':'application/json'}});if(!response.ok||response.redirected)return;const data=await response.json();
  for(const item of document.querySelectorAll('[data-inbox-count]')){item.textContent=data.inbox;item.classList.toggle('has-new',data.inbox>0);}
  for(const item of document.querySelectorAll('[data-business-count]')){item.textContent=data.business||0;item.classList.toggle('has-new',data.business>0);}
  for(const item of document.querySelectorAll('[data-mail-count]'))item.textContent=data.mail||0;
  for(const item of document.querySelectorAll('[data-box-unread]')){const count=(data.mailboxes||{})[item.dataset.boxUnread]||0;item.textContent='Непрочитанных: '+count;item.closest('.panel').classList.toggle('mailbox-has-new',count>0);}
  for(const item of document.querySelectorAll('[data-unsorted-count]'))item.textContent=data.unsorted;
  const storageKey='armada-notices:'+data.identity;let seen=[];try{seen=JSON.parse(sessionStorage.getItem(storageKey)||'[]')}catch{}const unseen=(data.keys||[]).some(key=>!seen.includes(key));try{sessionStorage.setItem(storageKey,JSON.stringify([...new Set([...seen,...(data.keys||[])])].slice(-200)))}catch{}
  if(data.notification&&unseen){let toast=document.getElementById('workspace-notice');if(!toast){toast=document.createElement('a');toast.id='workspace-notice';toast.href='/cabinet/documents/?mode=inbox';toast.setAttribute('role','status');document.body.append(toast)}toast.href=data.url;toast.textContent=data.notification+' · Открыть входящие';setTimeout(()=>toast.remove(),12000);}
 }catch{}finally{polling=false}}
 notifications();setInterval(notifications,60000);
}

const readMailForm=document.querySelector('form[data-mail-mark-read]');
if(readMailForm){const data=new FormData(readMailForm);data.set('action','seen');fetch(readMailForm.action,{method:'POST',credentials:'same-origin',body:data,headers:{Accept:'application/json'}}).then(r=>{if(!r.ok)throw Error();return r.json()}).catch(()=>{const notice=document.createElement('p');notice.className='notice';notice.textContent='Не удалось обновить статус чтения. Нажмите «Отметить прочитанным», чтобы повторить.';readMailForm.prepend(notice)})}
