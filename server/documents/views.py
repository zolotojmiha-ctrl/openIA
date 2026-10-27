import csv
import io
import zipfile
from uuid import UUID
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied,ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse,HttpResponse,JsonResponse,Http404
from django.shortcuts import get_object_or_404,redirect,render
from django.views.decorators.http import require_POST
from accounts.models import User
from accounts.views import audit
from projects.models import COMPANY_TAX_ID,LegalEntity
from projects.access import visible_objects,visible_contracts,contract_card
from .models import Document,DocumentVersion,DocumentAccess,DocumentRead,Bundle,BundleAccess,Person,PersonAssignment,Comment,Notice
from .forms import UploadForm,VersionForm,ShareForm,LinkForm,PersonForm,AssignmentForm
from .access import visible_documents,has_permission,can_modify,can_manage,can_manage_bundle,visible_bundles,visible_people,unread_documents,unsorted_documents,can_share
from .storage import store_file,private_path,verified_file
from .services import upload_one,make_version,Conflict
from .presentation import file_groups


def listing(user,query):
    qs=visible_documents(user).select_related('current_version','bundle','created_by').order_by('-created_at')
    if query:
        allowed_contracts=visible_contracts(user)
        if not user.is_leader:allowed_contracts=allowed_contracts.filter(access_rules__user=user,access_rules__show_number=True)
        allowed_contracts=allowed_contracts.filter(number__icontains=query)
        qs=qs.filter(Q(title__icontains=query)|Q(number__icontains=query)|Q(current_version__original_name__icontains=query)|Q(bundle__in=visible_bundles(user).filter(title__icontains=query))|Q(objects_linked__in=visible_objects(user).filter(name__icontains=query))|Q(contracts_linked__in=allowed_contracts)|Q(personnel_linked__in=visible_people(user).filter(full_name__icontains=query))).distinct()
    return qs

@login_required
def documents(request):
    q=request.GET.get('q','').strip()[:200];mode=request.GET.get('mode','all' if request.user.is_leader else 'mine')
    qs=listing(request.user,q).filter(archived=mode=='archive')
    # Search defaults to every accessible document regardless of current folder.
    if not q:
        if mode=='mine':qs=qs.filter(created_by=request.user)
        elif mode=='shared':qs=qs.exclude(created_by=request.user)
        elif mode=='inbox':qs=qs.filter(pk__in=unread_documents(request.user))
        elif mode=='unsorted':qs=qs.filter(pk__in=unsorted_documents(request.user))
    for param,field in [('section','section'),('bundle','bundle_id'),('object','objects_linked'),('contract','contracts_linked'),('person','personnel_linked')]:
        value=request.GET.get(param)
        if value:
            if param!='section':
                try:value=UUID(value)
                except ValueError:continue
            qs=qs.filter(**{field:value})
    from .models import SECTIONS
    section_cards=[{'code':code,'label':label,'count':qs.filter(section=code).distinct().count()} for code,label in SECTIONS]
    params=request.GET.copy();params.pop('page',None)
    page=Paginator(qs.distinct(),25).get_page(request.GET.get('page'))
    unread=set(unread_documents(request.user).filter(pk__in=[d.pk for d in page]).values_list('pk',flat=True))
    from .models import SECTIONS
    return render(request,'documents/list.html',{'page':page,'q':q,'mode':mode,'section_cards':section_cards,'file_groups':file_groups(request.user,qs.filter(pk__in=[d.pk for d in page]).distinct()),'rows':[{'document':d,'unread':d.pk in unread,'manage':can_manage(request.user,d)} for d in page],'sections':SECTIONS,'objects_filter':visible_objects(request.user),'contracts_filter':[contract_card(c,request.user) for c in visible_contracts(request.user)],'people_filter':visible_people(request.user),'query_without_page':params.urlencode(),'upload_allowed':has_permission(request.user,'document.upload'),'export_allowed':request.user.is_leader or has_permission(request.user,'document.export')})

@login_required
def upload(request):
    if not has_permission(request.user,'document.upload'):raise PermissionDenied
    initial={'section':'general','defer_link':True}
    destination=None;back_url=None;bundle_shared=False
    for param,field,qs in [('contract','linked_contracts',visible_contracts(request.user)),('object','linked_objects',visible_objects(request.user)),('person','linked_people',visible_people(request.user)),('bundle','bundle',visible_bundles(request.user))]:
        try:pk=UUID(request.GET.get(param,''))
        except ValueError:continue
        obj=qs.filter(pk=pk).first()
        if obj:
            if field=='bundle' and not can_manage_bundle(request.user,obj):continue
            initial[field]=pk if field=='bundle' else [pk]
            from django.urls import reverse
            if param=='contract':
                initial['section']='contracts';destination=contract_card(obj,request.user)['title'];back_url=reverse('contract_detail',args=[pk])
            elif param=='person':initial['section']='personnel';destination=obj.full_name;back_url=reverse('person_detail',args=[pk])
            elif param=='object':destination=obj.name;back_url=reverse('object_detail',args=[pk])
            else:
                destination=obj.title;back_url=reverse('bundle_detail',args=[pk]);bundle_shared=obj.access_rules.filter(enabled=True).exists()

    equipment_target=None
    if request.GET.get('equipment'):
        from projects.equipment import visible_equipment
        from accounts.permissions import permitted
        if not permitted(request.user,'equipment.manage'):raise PermissionDenied
        try:equipment_id=UUID(request.GET['equipment'])
        except ValueError:raise Http404
        equipment_target=get_object_or_404(visible_equipment(request.user),pk=equipment_id)
        from django.urls import reverse
        destination=str(equipment_target);back_url=reverse('equipment_detail',args=[equipment_target.pk])
    subcontract_target=None
    if request.GET.get('subcontractor'):
        from projects.subcontracts import companies
        from accounts.permissions import permitted
        if not permitted(request.user,'subcontract.manage'):raise PermissionDenied
        try:company_id=UUID(request.GET['subcontractor'])
        except ValueError:raise Http404
        subcontract_target=get_object_or_404(companies(request.user),pk=company_id)
        from django.urls import reverse
        destination=subcontract_target.name;back_url=reverse('subcontract_company',args=[subcontract_target.pk])
    form=UploadForm(request.POST or None,request.FILES or None,user=request.user,initial=initial)
    if request.method=='POST':
        if form.is_valid():
            try:
                with transaction.atomic():
                    user=User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
                    checked=UploadForm(request.POST,request.FILES,user=user)
                    if not checked.is_valid():raise PermissionDenied
                    if equipment_target:
                        from projects.equipment import manager,visible_equipment
                        from projects.models import EquipmentDocument
                        manager(request.user)
                        equipment_target=visible_equipment(user).select_for_update().get(pk=equipment_target.pk)
                    if subcontract_target:
                        from projects.subcontracts import manager,companies
                        from projects.models import SubcontractDocument
                        manager(request.user)
                        subcontract_target=companies(user).select_for_update().get(pk=subcontract_target.pk)
                    doc=upload_one(user,checked)
                    if subcontract_target:
                        link,created=SubcontractDocument.objects.get_or_create(company=subcontract_target,order=None,document=doc,defaults={'label':doc.title[:160],'created_by':user})
                        if created:audit(user,'subcontract_document_linked',company=str(subcontract_target.pk),link=str(link.pk),document=str(doc.pk),kind='upload_link')
                    if equipment_target:
                        link,created=EquipmentDocument.objects.get_or_create(equipment=equipment_target,document=doc,defaults={'label':doc.title[:120],'created_by':user})
                        if created:audit(user,'equipment_document_updated',equipment=str(equipment_target.pk),link=str(link.pk),document=str(doc.pk),kind='upload_link',remind=False)
            except Conflict as e:
                form.add_error(None,str(e))
            else:
                if request.headers.get('X-Requested-With')=='XMLHttpRequest':return JsonResponse({'id':str(doc.pk),'url':doc_url(doc),'name':doc.current_version.original_name,'bundle':str(doc.bundle_id or '')})
                messages.success(request,'Документ загружен. Утверждение не требуется.')
                if subcontract_target:return redirect('subcontract_company',pk=subcontract_target.pk)
                if equipment_target:return redirect('equipment_detail',pk=equipment_target.pk)
                return redirect('document_detail',pk=doc.pk)
        if request.headers.get('X-Requested-With')=='XMLHttpRequest':return JsonResponse({'errors':form.errors.get_json_data()},status=409 if form.non_field_errors() else 400)
    return render(request,'documents/upload.html',{'form':form,'destination':destination,'upload_back':back_url,'bundle_shared':bundle_shared})


def doc_url(doc):
    from django.urls import reverse
    return reverse('document_detail',args=[doc.pk])

@login_required
def detail(request,pk):
    doc=get_object_or_404(visible_documents(request.user).select_related('current_version','bundle'),pk=pk)
    if doc.current_version_id:
        DocumentRead.objects.update_or_create(document=doc,user=request.user,defaults={'version_id':doc.current_version_id})
        Notice.objects.filter(user=request.user,document=doc,read=False).update(read=True)
    from .actions import origin
    back_url,back_label,back_context=origin(request,doc)
    contracts=[contract_card(c,request.user) for c in visible_contracts(request.user).filter(documents=doc)]
    from projects.equipment import visible_equipment
    equipment_cards=visible_equipment(request.user).filter(file_links__document=doc,file_links__active=True).distinct()
    from projects.subcontracts import files as subcontract_files
    subcontract_back=subcontract_files(request.user).filter(document=doc,active=True)
    text=None
    if doc.current_version_id and doc.current_version.content_type=='text/plain':
        try:
            with verified_file(doc.current_version) as stream:text=stream.read(256*1024).decode('utf-8',errors='replace')
        except OSError:return HttpResponse('Файл не прошёл проверку сохранности.',status=503)
    versions=[{'version':v,'download':True,'parts':list(v.parts.all()),'pages':sum(p.page_count for p in v.parts.all())} for v in doc.versions.select_related('uploaded_by').prefetch_related('parts')]
    parts=list(doc.current_version.parts.all()) if doc.current_version_id else []
    readonly=doc.contracts_linked.filter(status='archived').exists()
    direct=doc.access_rules.select_related('user') if can_manage(request.user,doc) else []
    inherited=doc.bundle.access_rules.select_related('user').filter(enabled=True) if doc.bundle_id and can_manage(request.user,doc) else []
    return render(request,'documents/detail.html',{'back_url':back_url,'back_label':back_label,'back_context':back_context,'document_readonly':readonly,'document_parts':parts,'document_parts_images':bool(parts) and all(p.content_type in ['image/png','image/jpeg'] for p in parts),'subcontract_back':subcontract_back,'equipment_cards':equipment_cards,'document':doc,'text':text,'versions':versions,'contracts':contracts,'objects':visible_objects(request.user).filter(documents=doc),'people':visible_people(request.user).filter(documents=doc),'replace_allowed':not readonly and not doc.archived and can_modify(request.user,doc,'document.replace'),'manage_allowed':can_manage(request.user,doc),'share_allowed':can_share(request.user,doc),'direct_access':direct,'bundle_access':inherited,'bundle_visible':doc.bundle_id and visible_bundles(request.user).filter(pk=doc.bundle_id).exists(),'comments':doc.comments.select_related('author').order_by('created_at'),'comment_form':CommentForm(document=doc)})

@login_required
def replace(request,pk):
    doc=get_object_or_404(visible_documents(request.user),pk=pk)
    if doc.archived or not can_modify(request.user,doc,'document.replace'):raise PermissionDenied
    form=VersionForm(request.POST or None,request.FILES or None,initial={'expected_revision':doc.revision})
    if request.method=='POST' and form.is_valid():
        key=None
        try:
            with transaction.atomic():
                user=User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
                locked=get_object_or_404(Document.objects.select_for_update(),pk=pk)
                if not visible_documents(user).filter(pk=pk).exists() or locked.archived or not can_modify(user,locked,'document.replace'):raise PermissionDenied
                if locked.revision!=form.cleaned_data['expected_revision']:form.add_error(None,'Документ уже изменён. Обновите страницу.')
                else:
                    key=store_file(form.file_data);make_version(locked,user,form.file_data,form.cleaned_data['comment'],key=key)
        except Exception:
            if key:private_path(key).unlink(missing_ok=True)
            raise
        if not form.errors:return redirect('document_detail',pk=pk)
    return render(request,'documents/form.html',{'form':form,'title':'Новая версия · '+doc.title})

@login_required
@require_POST
def archive(request,pk):
    with transaction.atomic():
        user=User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
        doc=get_object_or_404(Document.objects.select_for_update(),pk=pk)
        if not visible_documents(user).filter(pk=pk).exists() or not can_manage(user,doc):raise PermissionDenied
        if str(doc.revision)!=request.POST.get('expected_revision'):return HttpResponse('Документ уже изменён.',status=409)
        doc.archived=not doc.archived;doc.revision+=1;doc.save(update_fields=['archived','revision'])
        audit(user,'document_archived' if doc.archived else 'document_restored',entity_id=str(pk))
    return redirect('document_detail',pk=pk)

@login_required
def download(request,pk,version,preview=False):
    doc=get_object_or_404(visible_documents(request.user),pk=pk)
    v=get_object_or_404(doc.versions,pk=version)
    if preview and v.content_type not in ['application/pdf','image/png','image/jpeg']:raise PermissionDenied
    try:stream=verified_file(v)
    except OSError:
        audit(request.user,'document_integrity_error',entity_id=str(pk),version_id=str(v.pk));return HttpResponse('Файл не прошёл проверку сохранности.',status=503)
    audit(request.user,'document_downloaded',entity_id=str(pk),version_id=str(v.pk),preview=preview)
    response=FileResponse(stream,as_attachment=not preview,filename=v.original_name,content_type=v.content_type if preview else 'application/octet-stream')
    response['X-Content-Type-Options']='nosniff';response['Content-Security-Policy']="sandbox; default-src 'none'";response['Cache-Control']='no-store'
    if preview:response['X-Frame-Options']='SAMEORIGIN'
    return response

@login_required
def preview(request,pk):
    doc=get_object_or_404(visible_documents(request.user).select_related('current_version'),pk=pk);v=doc.current_version
    text=None
    if v and v.content_type=='text/plain':
        try:
            with verified_file(v) as f:text=f.read(256*1024).decode('utf-8',errors='replace')
        except (OSError,UnicodeDecodeError):
            audit(request.user,'document_integrity_error',entity_id=str(pk),version_id=str(v.pk))
            return HttpResponse('Файл не прошёл проверку.',status=503)
    return render(request,'documents/preview.html',{'document':doc,'text':text})

@login_required
def share(request,pk,bundle=False):
    obj=get_object_or_404(visible_bundles(request.user) if bundle else visible_documents(request.user),pk=pk)
    if request.user.role=='subcontractor' or not (can_manage_bundle(request.user,obj) if bundle else can_share(request.user,obj)):raise PermissionDenied
    initial={'expected_revision':obj.revision}
    try:selected=UUID(request.GET.get('employee',''))
    except ValueError:selected=None
    if selected:
        initial['employee']=selected;rule=obj.access_rules.filter(user_id=selected).first()
        if rule:initial.update(enabled=rule.enabled,can_replace=rule.can_replace)
    form=ShareForm(request.POST or None,document=obj,actor=request.user,initial=initial)
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            actor=User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
            if not actor.is_active or actor.session_version!=request.user.session_version:raise PermissionDenied
            user=User.objects.get(pk=form.cleaned_data['employee'].pk)
            locked=(Bundle if bundle else Document).objects.select_for_update().get(pk=pk)
            if actor.role=='subcontractor' or not (can_manage_bundle(actor,locked) if bundle else can_share(actor,locked)):raise PermissionDenied
            checked=ShareForm(request.POST,document=locked,actor=actor)
            if not checked.is_valid():raise PermissionDenied
            form=checked
            if locked.revision!=form.cleaned_data['expected_revision']:form.add_error(None,'Доступ уже изменён. Обновите страницу.')
            else:
                flags={k:form.cleaned_data[k] for k in ['enabled','can_replace']}
                model=BundleAccess if bundle else DocumentAccess;relation={'bundle':locked} if bundle else {'document':locked}
                prior=model.objects.filter(user=user,**relation).first()
                if flags['enabled'] and (prior is None or not prior.enabled):
                    reads=DocumentRead.objects.filter(user=user)
                    (reads.filter(document__bundle=locked) if bundle else reads.filter(document=locked)).delete()
                model.objects.update_or_create(user=user,**relation,defaults={'granted_by':request.user,**flags})
                locked.revision+=1;locked.save(update_fields=['revision'])
                audit(request.user,'document_access_changed',user,entity_id=str(pk),bundle=bundle,**flags)
                return redirect('bundle_detail' if bundle else 'document_detail',pk=pk)
    return render(request,'documents/share.html',{'form':form,'document':obj,'bundle':bundle,'rules':obj.access_rules.select_related('user')})

@login_required
def bundle_detail(request,pk):
    obj=get_object_or_404(visible_bundles(request.user),pk=pk)
    return render(request,'documents/bundle.html',{'bundle':obj,'documents':visible_documents(request.user).filter(bundle=obj).order_by('-is_primary','title'),'manage_allowed':can_manage_bundle(request.user,obj)})

@login_required
def bundles(request):
    return render(request,'documents/bundles.html',{'bundles':visible_bundles(request.user).order_by('title')})

@login_required
def links(request,pk):
    doc=get_object_or_404(visible_documents(request.user),pk=pk)
    if not can_manage(request.user,doc):raise PermissionDenied
    initial={'expected_revision':doc.revision,'linked_objects':visible_objects(request.user).filter(documents=doc),'linked_contracts':visible_contracts(request.user).filter(documents=doc),'linked_people':visible_people(request.user).filter(documents=doc)}
    form=LinkForm(request.POST or None,instance=doc,user=request.user,initial=initial)
    original_bundle=doc.bundle_id
    recipients=[];access_changes=None
    if request.method=='POST' and form.is_valid():
        with transaction.atomic():
            user=User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
            bundle_ids={value for value in [original_bundle,getattr(form.cleaned_data.get('bundle'),'pk',None)] if value}
            list(Bundle.objects.select_for_update().filter(pk__in=bundle_ids).order_by('pk'))
            locked=Document.objects.select_for_update().get(pk=pk)
            if not can_manage(user,locked):raise PermissionDenied
            if locked.revision!=form.cleaned_data['expected_revision']:form.add_error(None,'Связи уже изменились. Обновите страницу.')
            else:
                changed_bundle=locked.bundle_id!=(form.cleaned_data.get('bundle').pk if form.cleaned_data.get('bundle') else None)
                if changed_bundle:
                    old=set(BundleAccess.objects.filter(bundle_id=locked.bundle_id,enabled=True).values_list('user_id',flat=True))
                    new=set(BundleAccess.objects.filter(bundle=form.cleaned_data.get('bundle'),enabled=True).values_list('user_id',flat=True))
                    direct=set(doc.access_rules.filter(enabled=True).values_list('user_id',flat=True));denied=set(doc.access_rules.filter(enabled=False).values_list('user_id',flat=True))
                    always=set(User.objects.filter(leader_slot__isnull=False).values_list('pk',flat=True))|{doc.created_by_id}
                    if doc.responsible_id:always.add(doc.responsible_id)
                    old=((old|direct)-denied)|always;new=((new|direct)-denied)|always
                    access_changes={'gained':User.objects.filter(pk__in=new-old),'lost':User.objects.filter(pk__in=old-new),'kept':User.objects.filter(pk__in=old&new)}
                    recipients=list(User.objects.filter(pk__in=old|new).order_by('full_name'))
                    if not form.cleaned_data['confirm_access']:form.add_error('confirm_access','Смена комплекта меняет унаследованный доступ. Проверьте показанных получателей и подтвердите.')
                if not form.errors:
                    saved=form.save(commit=False);saved.revision=locked.revision+1
                    if saved.is_primary and saved.bundle_id:
                        Bundle.objects.select_for_update().get(pk=saved.bundle_id)
                        Document.objects.filter(bundle_id=saved.bundle_id,is_primary=True).exclude(pk=saved.pk).update(is_primary=False)
                    saved.save()
                    # Preserve hidden links; a file grant does not disclose parent cards.
                    for relation,field,visible in [('objects_linked','linked_objects',visible_objects(user)),('contracts_linked','linked_contracts',visible_contracts(user)),('personnel_linked','linked_people',visible_people(user))]:
                        manager=getattr(saved,relation);hidden=list(manager.exclude(pk__in=visible.values('pk')));manager.set([*hidden,*form.cleaned_data[field]])
                    audit(user,'document_links_changed',entity_id=str(pk),old_bundle=str(locked.bundle_id or ''),new_bundle=str(saved.bundle_id or ''))
                    return redirect('document_detail',pk=pk)
    return render(request,'documents/form.html',{'form':form,'title':'Изменить сведения и связи · '+doc.title,'access_warning':recipients,'access_changes':access_changes})

class CommentForm(forms.Form):
    text=forms.CharField(label='Комментарий',max_length=2000,widget=forms.Textarea(attrs={'rows':3}))
    recipients=forms.ModelMultipleChoiceField(label='Обратить внимание (только имеющие доступ)',queryset=User.objects.none(),required=False,widget=forms.CheckboxSelectMultiple)
    def __init__(self,*args,document,**kwargs):
        super().__init__(*args,**kwargs)
        ids=[u.pk for u in User.objects.filter(is_active=True) if visible_documents(u).filter(pk=document.pk).exists()]
        self.fields['recipients'].queryset=User.objects.filter(pk__in=ids);self.fields['recipients'].label_from_instance=lambda u:u.full_name

@login_required
@require_POST
def comment(request,pk):
    doc=get_object_or_404(visible_documents(request.user),pk=pk)
    form=CommentForm(request.POST,document=doc)
    if form.is_valid():
        with transaction.atomic():
            c=Comment.objects.create(document=doc,author=request.user,text=form.cleaned_data['text'])
            for u in form.cleaned_data['recipients']:
                if u.pk!=request.user.pk and visible_documents(u).filter(pk=pk).exists():Notice.objects.create(user=u,document=doc,comment=c)
            audit(request.user,'document_commented',entity_id=str(pk),comment_id=c.pk)
    else:messages.error(request,'Комментарий не отправлен. Проверьте текст и получателей.')
    return redirect('document_detail',pk=pk)


def zip_response(user,documents,filename,person_folders=False,people=None):
    import tempfile
    from pathlib import Path
    from django.conf import settings
    temp_root=Path(settings.PRIVATE_DOCUMENT_ROOT)/'.exports'
    temp_root.mkdir(parents=True,exist_ok=True,mode=0o700)
    output=tempfile.TemporaryFile(dir=temp_root)
    try:
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as z:
            total=0
            for doc in documents.select_related('current_version'):
                v=doc.current_version
                if not v:continue
                folders=['']
                if person_folders:
                    persons=visible_people(user).filter(documents=doc)
                    if people is not None:persons=persons.filter(pk__in=[p.pk for p in people])
                    folders=[''.join(c for c in p.full_name if c.isalnum() or c in ' -_')[:80]+'-'+str(p.pk)[:8]+'/' for p in persons] or ['']
                name=v.original_name.replace('/','_').replace('\\','_')
                for folder in folders:
                    total+=v.size
                    if total>200*1024*1024:raise ValueError('Один ZIP — до 200 МБ.')
                    with verified_file(v) as source,z.open(folder+str(doc.pk)[:8]+'-'+name,'w') as target:
                        while chunk:=source.read(65536):target.write(chunk)
        output.seek(0)
    except (OSError,ValueError) as e:
        output.close();return HttpResponse('Не удалось сформировать ZIP: проверьте сохранность и объём выбранных файлов.',status=409)
    audit(user,'document_exported',format='zip',count=documents.count())
    return FileResponse(output,as_attachment=True,filename=filename,content_type='application/zip')

@login_required
def bundle_zip(request,pk):
    obj=get_object_or_404(visible_bundles(request.user),pk=pk)
    return zip_response(request.user,visible_documents(request.user).filter(bundle=obj,archived=False),'documents.zip')

@login_required
@require_POST
def bulk(request):
    ids=[]
    for value in request.POST.getlist('documents'):
        try:ids.append(UUID(value))
        except ValueError:return HttpResponse('Некорректный выбор.',status=400)
    if not ids or len(ids)>200:return HttpResponse('Выберите от 1 до 200 документов.',status=400)
    qs=visible_documents(request.user).filter(pk__in=ids)
    if qs.count()!=len(set(ids)):raise PermissionDenied
    if request.POST.get('action')=='zip':return zip_response(request.user,qs,'selected-documents.zip',request.POST.get('person_folders')=='1')
    # One shared form applies to every selected document, with atomic rights checking.
    if request.POST.get('action')=='share' and request.user.role=='subcontractor':raise PermissionDenied
    if any(not can_manage(request.user,d) for d in qs):raise PermissionDenied
    from django.core import signing
    bound=request.POST if request.POST.get('apply')=='1' else None
    form=ShareForm(bound,actor=request.user) if request.POST.get('action')=='share' else LinkForm(bound,user=request.user)
    for field in ['expected_revision','title','section','number','date','bundle','is_primary','confirm_access']:form.fields.pop(field,None)
    version_snapshot=signing.dumps({str(d.pk):d.revision for d in qs},salt='document-bulk')
    if request.POST.get('apply')=='1' and form.is_valid():
        with transaction.atomic():
            User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
            docs=list(Document.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
            if any(not can_manage(request.user,d) for d in docs):raise PermissionDenied
            try:expected=signing.loads(request.POST.get('version_snapshot',''),salt='document-bulk',max_age=3600)
            except signing.BadSignature:return HttpResponse('Список изменился. Выберите документы заново.',status=409)
            if any(expected.get(str(d.pk))!=d.revision for d in docs):return HttpResponse('Один из документов уже изменён. Обновите список.',status=409)
            for doc in docs:
                if request.POST.get('action')=='share':
                    d=form.cleaned_data;DocumentAccess.objects.update_or_create(document=doc,user=d['employee'],defaults={'enabled':d['enabled'],'can_replace':d['can_replace'],'granted_by':request.user})
                    if d['enabled']:DocumentRead.objects.filter(document=doc,user=d['employee']).delete()
                else:
                    for relation,field in [('objects_linked','linked_objects'),('contracts_linked','linked_contracts'),('personnel_linked','linked_people')]:getattr(doc,relation).add(*form.cleaned_data[field])
                    if form.cleaned_data['link_not_required']:doc.link_not_required=True
                doc.revision+=1;doc.save(update_fields=['revision','link_not_required'])
                audit(request.user,'document_bulk_changed',entity_id=str(doc.pk),operation=request.POST.get('action'))
        return redirect('documents')
    return render(request,'documents/bulk.html',{'form':form,'ids':ids,'action':request.POST.get('action'),'documents':qs,'version_snapshot':version_snapshot})

@login_required
def export(request):
    if not (request.user.is_leader or has_permission(request.user,'document.export')):raise PermissionDenied
    response=HttpResponse(content_type='text/csv; charset=utf-8');response['Content-Disposition']='attachment; filename="documents.csv"';response.write('\ufeff')
    w=csv.writer(response);w.writerow(['Название','Номер','Дата','Версия','Архив'])
    def safe(v):
        v=str(v or '');return "'"+v if v.lstrip().startswith(('=','+','-','@')) else v
    for doc in listing(request.user,request.GET.get('q','').strip()[:200]):w.writerow([safe(doc.title),safe(doc.number),doc.date,doc.current_version.sequence if doc.current_version else '',doc.archived])
    audit(request.user,'document_exported',format='csv');return response

@login_required
def approvals(request):return redirect('documents')
@login_required
def approve(request,pk,version):return HttpResponse('Обязательное утверждение отменено. Исторические решения сохранены.',status=410)

@login_required
def people(request):
    qs=visible_people(request.user).filter(archived=request.GET.get('archive')=='1').order_by('full_name')
    q=request.GET.get('q','').strip()[:150]
    if q:qs=qs.filter(Q(full_name__icontains=q)|Q(position__icontains=q))
    return render(request,'documents/people.html',{'people':qs,'can_manage':has_permission(request.user,'personnel.manage'),'q':q})

@login_required
def person_detail(request,pk):
    person=get_object_or_404(visible_people(request.user),pk=pk)
    return render(request,'documents/person.html',{'person':person,'assignments':person.assignments.filter(project_object__in=visible_objects(request.user)).select_related('project_object'),'documents':visible_documents(request.user).filter(personnel_linked=person),'file_groups':file_groups(request.user,visible_documents(request.user).filter(personnel_linked=person)),'can_manage':has_permission(request.user,'personnel.manage')})

@login_required
def person_edit(request,pk=None):
    if not has_permission(request.user,'personnel.manage'):raise PermissionDenied
    obj=get_object_or_404(visible_people(request.user),pk=pk) if pk else None
    form=PersonForm(request.POST or None,actor=request.user,instance=obj,initial={'expected_revision':obj.revision} if obj else None)
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                if obj:
                    locked=Person.objects.select_for_update().get(pk=pk)
                    if locked.revision!=form.cleaned_data['expected_revision']:form.add_error(None,'Карточка изменена. Обновите страницу.')
                if not form.errors:
                    saved=form.save(commit=False)
                    if not obj:saved.created_by=request.user;saved.legal_entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID)
                    saved.revision+=1;saved.save();audit(request.user,'person_updated',entity_id=str(saved.pk))
                    if form.cleaned_data.get('request_mail'):
                        from postoffice.services import request_address
                        request_address(request.user,form.cleaned_data['mail_address'],person=saved)
        except ValidationError as exc:form.add_error(None,exc)
        else:
            if not form.errors:return redirect('person_detail',pk=saved.pk)
    return render(request,'documents/form.html',{'form':form,'title':'Карточка работника'})

@login_required
def person_assignment(request,pk):
    if not has_permission(request.user,'personnel.manage'):raise PermissionDenied
    person=get_object_or_404(visible_people(request.user),pk=pk)
    obj=get_object_or_404(person.assignments,pk=request.GET['assignment']) if request.GET.get('assignment','').isdigit() else None
    form=AssignmentForm(request.POST or None,instance=obj,user=request.user)
    if request.method=='POST' and form.is_valid():
        saved=form.save(commit=False);saved.person=person;saved.created_by=request.user;saved.save();audit(request.user,'person_assigned',entity_id=str(person.pk),assignment=saved.pk)
        return redirect('person_detail',pk=pk)
    return render(request,'documents/form.html',{'form':form,'title':'Назначение на объект · '+person.full_name})

@login_required
@require_POST
def person_archive(request,pk):
    if not has_permission(request.user,'personnel.manage'):raise PermissionDenied
    with transaction.atomic():
        person=get_object_or_404(visible_people(request.user),pk=pk)
        if person.user_id and not request.user.is_leader:raise PermissionDenied
        person=Person.objects.select_for_update().get(pk=pk)
        if str(person.revision)!=request.POST.get('expected_revision'):return HttpResponse('Карточка уже изменена.',status=409)
        person.archived=not person.archived;person.revision+=1;person.save(update_fields=['archived','revision'])
        if person.user_id and person.archived:
            u=User.objects.select_for_update(no_key=True).get(pk=person.user_id)
            if u.is_leader:raise PermissionDenied
            u.is_active=False;u.session_version+=1;u.save(update_fields=['is_active','session_version'])
        audit(request.user,'person_archived',entity_id=str(pk),archived=person.archived)
    return redirect('person_detail',pk=pk)

@login_required
@require_POST
def bundle_primary(request,pk):
    with transaction.atomic():
        actor=User.objects.select_for_update(no_key=True).get(pk=request.user.pk)
        if not actor.is_active:raise PermissionDenied
        obj=get_object_or_404(Bundle.objects.select_for_update(),pk=pk,legal_entity__tax_id=COMPANY_TAX_ID)
        if not can_manage_bundle(request.user,obj):raise PermissionDenied
        if str(obj.revision)!=request.POST.get('revision'):return HttpResponse('Комплект изменён. Обновите страницу.',status=409)
        try:key=UUID(request.POST.get('document',''))
        except ValueError:return HttpResponse('Выберите документ.',status=400)
        doc=get_object_or_404(visible_documents(request.user),pk=key,bundle=obj)
        Document.objects.filter(bundle=obj,is_primary=True).exclude(pk=doc.pk).update(is_primary=False)
        Document.objects.filter(pk=doc.pk).update(is_primary=True)
        obj.revision+=1;obj.save(update_fields=['revision']);audit(request.user,'document_bundle_primary',entity_id=str(doc.pk),bundle_id=str(pk))
    return redirect('bundle_detail',pk=pk)

@login_required
def personnel_export(request,pk):
    obj=get_object_or_404(visible_objects(request.user),pk=pk)
    available=visible_people(request.user).filter(assignments__project_object=obj).distinct()
    selected=[]
    for value in request.POST.getlist('people'):
        try:selected.append(UUID(value))
        except ValueError:raise PermissionDenied
    people=list(available.filter(pk__in=selected))
    if len(people)!=len(set(selected)) or not people:raise PermissionDenied
    qs=visible_documents(request.user).filter(personnel_linked__in=people,archived=False).distinct()
    if request.method=='POST' and request.POST.get('download')=='1':
        ids=[]
        for value in request.POST.getlist('documents'):
            try:ids.append(UUID(value))
            except ValueError:raise PermissionDenied
        chosen=qs.filter(pk__in=ids)
        if not ids or chosen.count()!=len(set(ids)):raise PermissionDenied
        return zip_response(request.user,chosen,'personnel-documents.zip',True,people=people)
    return render(request,'documents/personnel_export.html',{'object':obj,'people':people,'documents':qs})
