"""Assemble scans/PDFs into immutable versions, retaining private originals."""
import hashlib,io,json,re,uuid,warnings,threading
from django import forms
from django.core import signing
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404,redirect,render
from django.contrib.auth.decorators import login_required
from django.http import FileResponse,HttpResponse
from accounts.models import User
from accounts.views import audit
from projects.models import LegalEntity,COMPANY_TAX_ID
from .models import Document,DocumentPart,DocumentAssembly
from .access import visible_documents,visible_people,can_modify,has_permission
from .storage import validate_upload,store_file,verified_file,private_path
from .services import make_version

MAX_INPUTS=10
MAX_TOTAL=25*1024*1024
MAX_PAGES=40
MAX_OUTPUT=30*1024*1024
SALT='document-assembly-6.7'
_render_lock=threading.Lock()  # One render at a time in the NAS worker's two request threads.

class MultipleInput(forms.ClearableFileInput):allow_multiple_selected=True
class MultipleFileField(forms.FileField):
    widget=MultipleInput(attrs={'accept':'.jpg,.jpeg,.png,.pdf'})
    def clean(self,data,initial=None):
        files=data if isinstance(data,(list,tuple)) else [data] if data else []
        if len(files)>MAX_INPUTS:raise ValidationError('Выберите до 10 файлов.')
        result=[];size=0
        for file in files:
            value=validate_upload(super().clean(file,initial));size+=value['size']
            if value['content_type'] not in ['image/jpeg','image/png','application/pdf']:raise ValidationError('Для сборки нужны JPG, PNG или PDF.')
            if size>MAX_TOTAL:raise ValidationError('Суммарный размер исходников — до 25 МБ.')
            result.append(value)
        return result

class AssemblyForm(forms.Form):
    token=forms.UUIDField(widget=forms.HiddenInput)
    snapshot=forms.CharField(widget=forms.HiddenInput)
    expected_revision=forms.IntegerField(widget=forms.HiddenInput)
    title=forms.CharField(label='Название документа',max_length=200)
    retain=forms.MultipleChoiceField(label='Сохранить исходники текущей версии',required=False,widget=forms.CheckboxSelectMultiple)
    sources=forms.ModelMultipleChoiceField(label='Добавить сохранённые сканы / PDF',queryset=Document.objects.none(),required=False,widget=forms.CheckboxSelectMultiple)
    files=MultipleFileField(label='Добавить JPG, PNG или PDF (до 10 МБ каждый)',required=False)
    order=forms.CharField(required=False,widget=forms.HiddenInput)
    comment=forms.CharField(label='Что изменено',max_length=2000,required=False,widget=forms.Textarea(attrs={'rows':3}))
    def __init__(self,*args,user,target=None,person=None,**kwargs):
        super().__init__(*args,**kwargs);self.user=user;self.target=target
        qs=visible_documents(user).filter(archived=False,current_version__content_type__in=['image/jpeg','image/png','application/pdf']).select_related('current_version').order_by('title','pk')
        if person:qs=qs.filter(personnel_linked=person)
        ids=list(qs.values_list('pk',flat=True)[:200])
        if target and target.pk not in ids:ids.append(target.pk)
        qs=qs.filter(pk__in=ids)
        self.fields['sources'].queryset=qs
        self.fields['sources'].label_from_instance=lambda doc:doc.title+' — '+doc.current_version.original_name
        parts=list(target.current_version.parts.all()) if target and target.current_version_id else []
        if not parts:self.fields['retain'].widget=forms.MultipleHiddenInput()
        self.fields['retain'].choices=[(str(p.pk),f'{p.position}. {p.original_name} ({p.page_count} стр.)') for p in parts]
        self.initial.update(token=uuid.uuid4(),expected_revision=target.revision if target else 0,title=target.title if target else '',retain=[str(p.pk) for p in parts])
        if target:
            self.fields['title'].disabled=True
            if not parts and target.current_version_id:self.initial['sources']=[target.pk]
        self.initial['snapshot']=signing.dumps({'actor':str(user.pk),'target':str(target.pk) if target else '', 'revision':target.revision if target else 0,'sources':{str(d.pk):str(d.current_version_id) for d in qs}},salt=SALT,compress=True)
    def clean(self):
        data=super().clean()
        if self.errors:return data
        try:snapshot=signing.loads(data['snapshot'],salt=SALT,max_age=3600)
        except signing.BadSignature:raise ValidationError('Форма устарела. Обновите страницу и выберите исходники заново.')
        if snapshot['actor']!=str(self.user.pk) or snapshot['target']!=(str(self.target.pk) if self.target else '') or snapshot['revision']!=data['expected_revision']:raise ValidationError('Форма относится к другому документу или пользователю.')
        tokens=['p:'+p for p in data['retain']]+['d:'+str(d.pk) for d in data['sources']]+['u:'+str(i) for i in range(len(data['files']))]
        if not tokens or len(tokens)>MAX_INPUTS:raise ValidationError('Выберите от 1 до 10 исходных файлов.')
        try:order=json.loads(data['order']) if data.get('order') else tokens
        except (ValueError,TypeError):raise ValidationError('Некорректный порядок исходников.')
        if not isinstance(order,list) or any(not isinstance(v,str) for v in order) or len(order)!=len(tokens) or set(order)!=set(tokens):raise ValidationError('Порядок должен содержать каждый выбранный исходник ровно один раз.')
        data['ordered_tokens']=order;data['source_versions']=snapshot['sources']
        return data

def pdf_bytes(inputs):
    with _render_lock:return _pdf_bytes(inputs)

def _pdf_bytes(inputs):
    from PIL import Image,ImageOps,UnidentifiedImageError
    from pypdf import PdfReader,PdfWriter
    from pypdf.errors import PdfReadError
    writer=PdfWriter();counts=[]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            for data in inputs:
                content=data['content'];mime=data['content_type']
                if mime in ('image/jpeg','image/png'):
                    with Image.open(io.BytesIO(content)) as original:
                        if original.width*original.height>25_000_000 or getattr(original,'n_frames',1)!=1:raise ValidationError('Один скан — одно изображение до 25 мегапикселей.')
                        ImageOps.exif_transpose(original,in_place=True)
                        layer=original.convert('RGBA') if 'transparency' in original.info and 'A' not in original.getbands() else original
                        image=Image.new('RGB',layer.size,'white') if 'A' in layer.getbands() else layer.convert('RGB')
                        try:
                            if 'A' in layer.getbands():image.paste(layer,mask=layer.getchannel('A'))
                            output=io.BytesIO();image.save(output,format='PDF',resolution=150)
                            content=output.getvalue()
                        finally:
                            image.close()
                            if layer is not original:layer.close()
                elif mime!='application/pdf':raise ValidationError('Поддерживаются только JPG, PNG и PDF.')
                reader=PdfReader(io.BytesIO(content),strict=False)
                if reader.is_encrypted:raise ValidationError('Снимите пароль с PDF перед сборкой.')
                count=len(reader.pages)
                if not count or len(writer.pages)+count>MAX_PAGES:raise ValidationError('Собранный документ должен содержать от 1 до 40 страниц.')
                for page in reader.pages:writer.add_page(page,excluded_keys=['/AA','/A','/Metadata'])
                counts.append(count)
        output=io.BytesIO();writer.write(output)
        if output.tell()>MAX_OUTPUT:raise ValidationError('Собранный PDF больше 30 МБ. Уменьшите размер сканов.')
        return output.getvalue(),counts
    except (UnidentifiedImageError,OSError,ValueError,RecursionError,PdfReadError,Image.DecompressionBombError,Image.DecompressionBombWarning) as error:
        raise ValidationError('Не удалось прочитать один из сканов / PDF. Проверьте исходные файлы.') from error
    finally:writer.close()

def read_input(row):
    with verified_file(row) as stream:content=stream.read(MAX_TOTAL+1)
    return {'content':content,'name':row.original_name,'content_type':row.content_type,'size':row.size,'sha256':row.sha256,'key':row.storage_key}

def assemble(user,data,target_id=None,person_id=None):
    stored=[]
    try:
        with transaction.atomic():
            actor=User.objects.select_for_update(no_key=True).get(pk=user.pk)
            if not actor.is_active or actor.session_version!=user.session_version:raise PermissionDenied
            # Lock all source/target documents in one order, including opposing assemblies.
            ids={d.pk for d in data['sources']}
            if target_id:ids.add(target_id)
            list(Document.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
            target=None
            if target_id:
                target=get_object_or_404(Document.objects.select_for_update(),pk=target_id)
                if not visible_documents(actor).filter(pk=target.pk).exists() or target.archived or not can_modify(actor,target,'document.replace'):raise PermissionDenied
            elif not has_permission(actor,'document.upload'):raise PermissionDenied
            digest=hashlib.sha256(json.dumps({'target':str(target_id or ''),'person':str(person_id or ''),'title':data['title'],'revision':data['expected_revision'],'comment':data['comment'],'order':data['ordered_tokens'],'versions':{str(d.pk):data['source_versions'].get(str(d.pk)) for d in data['sources']},'uploads':[(v['name'],v['sha256']) for v in data['files']]},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            prior=DocumentAssembly.objects.filter(token=data['token']).select_related('version__document').first()
            if prior:
                if prior.actor_id!=actor.pk or prior.digest!=digest or not visible_documents(actor).filter(pk=prior.version.document_id).exists():raise ValidationError('Этот запрос уже сохранён с другим составом. Обновите страницу.')
                return prior.version.document
            if target and target.revision!=data['expected_revision']:raise ValidationError('Документ уже изменён. Обновите страницу перед созданием новой версии.')
            person=get_object_or_404(visible_people(actor).filter(archived=False),pk=person_id) if person_id else None
            inputs={}
            if target and target.current_version_id:
                for part in target.current_version.parts.filter(pk__in=data['retain']):inputs['p:'+str(part.pk)]=read_input(part)
            for selected in data['sources']:
                if not visible_documents(actor).filter(pk=selected.pk).exists():raise PermissionDenied
                source=Document.objects.select_for_update().get(pk=selected.pk)
                if source.archived or str(source.current_version_id)!=data['source_versions'].get(str(source.pk)):raise ValidationError('Один из исходников изменён. Обновите выбор.')
                inputs['d:'+str(source.pk)]=read_input(source.current_version)
            for index,value in enumerate(data['files']):inputs['u:'+str(index)]={**value,'key':None}
            if set(inputs)!=set(data['ordered_tokens']):raise ValidationError('Состав исходников изменился.')
            ordered=[inputs[token] for token in data['ordered_tokens']]
            if sum(v['size'] for v in ordered)>MAX_TOTAL:raise ValidationError('Суммарный размер исходников — до 25 МБ.')
            pdf,counts=pdf_bytes(ordered)
            if not target:
                target=Document.objects.create(legal_entity=LegalEntity.objects.get(tax_id=COMPANY_TAX_ID),created_by=actor,title=data['title'],section='personnel' if person else 'general')
                if person:target.personnel_linked.add(person)
            name=re.sub(r'[\\/\x00-\x1f\x7f]','_',target.title)[:190]+'.pdf'
            file={'name':name,'content_type':'application/pdf','size':len(pdf),'sha256':hashlib.sha256(pdf).hexdigest(),'content':pdf}
            key=store_file(file);stored.append(key)
            version=make_version(target,actor,file,data['comment'],key=key)
            for position,(source,count) in enumerate(zip(ordered,counts),1):
                key=source['key']
                if not key:key=store_file(source);stored.append(key)
                DocumentPart.objects.create(version=version,position=position,page_count=count,storage_key=key,original_name=source['name'],content_type=source['content_type'],size=source['size'],sha256=source['sha256'])
            DocumentAssembly.objects.create(token=data['token'],actor=actor,version=version,digest=digest)
            audit(actor,'document_assembled',entity_id=str(target.pk),kind='assembled',version=str(version.pk),pages=sum(counts),sources=len(ordered))
            return target
    except Exception:
        for key in stored:private_path(key).unlink(missing_ok=True)
        raise

@login_required
def compose(request,pk=None):
    target=get_object_or_404(visible_documents(request.user),pk=pk) if pk else None
    if target:
        if target.archived or not can_modify(request.user,target,'document.replace'):raise PermissionDenied
    elif not has_permission(request.user,'document.upload'):raise PermissionDenied
    person=None
    if request.GET.get('person'):
        try:person=get_object_or_404(visible_people(request.user).filter(archived=False),pk=uuid.UUID(request.GET['person']))
        except ValueError:raise PermissionDenied
    form=AssemblyForm(request.POST or None,request.FILES or None,user=request.user,target=target,person=person)
    if request.method=='POST' and form.is_valid():
        try:doc=assemble(request.user,form.cleaned_data,target.pk if target else None,person.pk if person else None)
        except ValidationError as error:form.add_error(None,error)
        except OSError:form.add_error(None,'Исходный файл не прошёл проверку сохранности. Сборка не сохранена.')
        else:return redirect('document_detail',pk=doc.pk)
    return render(request,'documents/compose.html',{'form':form,'target':target,'person':person})

@login_required
def original(request,pk,part_id):
    doc=get_object_or_404(visible_documents(request.user),pk=pk)
    part=get_object_or_404(DocumentPart,pk=part_id,version__document=doc)
    try:stream=verified_file(part)
    except OSError:return HttpResponse('Файл не прошёл проверку сохранности.',status=503)
    preview=request.GET.get('preview')=='1' and part.content_type in ['image/png','image/jpeg']
    response=FileResponse(stream,as_attachment=not preview,filename=part.original_name,content_type=part.content_type if preview else 'application/octet-stream')
    if preview:response['Content-Security-Policy']="sandbox; default-src 'none'";response['X-Frame-Options']='SAMEORIGIN'
    response['Cache-Control']='no-store';response['X-Content-Type-Options']='nosniff'
    audit(request.user,'document_downloaded',entity_id=str(pk),part=str(part.pk))
    return response
