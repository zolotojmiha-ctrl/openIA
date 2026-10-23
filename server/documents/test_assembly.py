import io,uuid,tempfile,hashlib
from unittest.mock import patch
from datetime import timedelta
from pathlib import Path
from PIL import Image
from pypdf import PdfReader
from django.test import TransactionTestCase,override_settings
from django.core.exceptions import ValidationError,PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core import signing
from django.urls import reverse
from django.utils import timezone
from accounts.models import User
from projects.models import LegalEntity,COMPANY_TAX_ID,Contract
from .models import Document,DocumentPart,DocumentAccess,DocumentVersion,DocumentContract
from .assembly import AssemblyForm,assemble,pdf_bytes
from .services import make_version
from .storage import validate_upload,private_path,verified_file
from .deadlines import save_expiry,due_documents,apply_rows,rows_from_file,HEADERS
from repairs.excel import workbook

class AssemblyTests(TransactionTestCase):
    def setUp(self):
        self.root=tempfile.TemporaryDirectory();self.addCleanup(self.root.cleanup)
        setting=override_settings(PRIVATE_DOCUMENT_ROOT=self.root.name);setting.enable();self.addCleanup(setting.disable)
        self.leader=User.objects.create_user('+79990009301','test-secret',full_name='ТЕСТ Руководитель',leader_slot=1,must_change_password=False)
        self.reader=User.objects.create_user('+79990009302','test-secret',full_name='ТЕСТ Инженер по ОТ и ПБ',role='foreman',must_change_password=False)
        self.entity=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})[0]
    def scan(self,color='red',name='scan.png'):
        stream=io.BytesIO()
        with Image.new('RGB',(120,160),color) as image:image.save(stream,format='PNG')
        return SimpleUploadedFile(name,stream.getvalue(),content_type='image/png')
    def form(self,files=None,target=None,sources=(),retain=None,order=None,**changes):
        initial=AssemblyForm(user=self.leader,target=target)
        values={'token':str(initial.initial['token']),'snapshot':initial.initial['snapshot'],'expected_revision':target.revision if target else 0,'title':'ТЕСТ Удостоверение','sources':[str(d.pk) for d in sources],'retain':retain if retain is not None else initial.initial['retain'],'comment':'ТЕСТ',**changes}
        if order is not None:
            import json
            values['order']=json.dumps(order)
        form=AssemblyForm(values,{'files':files or []},user=self.leader,target=target)
        self.assertTrue(form.is_valid(),str(form.errors));return form
    def create(self):
        form=self.form(files=[self.scan(),self.scan('blue','page2.png')])
        return assemble(self.leader,form.cleaned_data)
    def source(self):
        doc=Document.objects.create(legal_entity=self.entity,title='ТЕСТ Скан',section='personnel',created_by=self.leader)
        make_version(doc,self.leader,validate_upload(self.scan()),'ТЕСТ');return doc
    def test_two_scans_one_pdf_and_private_originals(self):
        doc=self.create();self.assertEqual(Document.objects.count(),1)
        with verified_file(doc.current_version) as stream:self.assertEqual(len(PdfReader(stream).pages),2)
        parts=list(doc.current_version.parts.all());self.assertEqual([p.position for p in parts],[1,2])
        with verified_file(parts[0]) as stream:self.assertEqual(stream.read(),self.scan().read())
        self.assertEqual(doc.current_version.content_type,'application/pdf');self.assertFalse(doc.access_rules.exists())
    def test_reorder_and_add_creates_new_version_preserving_old(self):
        doc=self.create();old=doc.current_version;parts=list(old.parts.all())
        order=['p:'+str(parts[1].pk),'u:0','p:'+str(parts[0].pk)]
        form=self.form(target=doc,files=[self.scan('green','page3.png')],order=order)
        assemble(self.leader,form.cleaned_data,doc.pk);doc.refresh_from_db()
        self.assertEqual(doc.versions.count(),2);self.assertEqual(old.parts.count(),2)
        self.assertEqual(list(doc.current_version.parts.values_list('original_name',flat=True)),['page2.png','page3.png','scan.png'])
    def test_select_existing_scans_and_retry(self):
        source=self.source();form=self.form(sources=[source])
        doc=assemble(self.leader,form.cleaned_data);same=assemble(self.leader,form.cleaned_data)
        self.assertEqual(same.pk,doc.pk);self.assertEqual(Document.objects.count(),2);self.assertEqual(doc.versions.count(),1)
    def test_stale_target_changes_nothing(self):
        doc=self.create();form=self.form(target=doc)
        doc.title='Изменён';doc.revision+=1;doc.save()
        with self.assertRaises(ValidationError):assemble(self.leader,form.cleaned_data,doc.pk)
        self.assertEqual(doc.versions.count(),1)
    def test_changed_source_rejected(self):
        source=self.source();form=self.form(sources=[source]);make_version(source,self.leader,validate_upload(self.scan('blue')))
        with self.assertRaises(ValidationError):assemble(self.leader,form.cleaned_data)
        self.assertEqual(Document.objects.count(),1)
    def test_invalid_order_rejected(self):
        first=self.form(files=[self.scan()]);data=dict(first.data);data['order']='["u:0","u:0"]'
        self.assertFalse(AssemblyForm(data,{'files':[self.scan()]},user=self.leader).is_valid())
    def test_forged_snapshot_rejected(self):
        first=self.form(files=[self.scan()]);data=dict(first.data);data['snapshot']='bad'
        self.assertFalse(AssemblyForm(data,{'files':[self.scan()]},user=self.leader).is_valid())
    def test_revoke_source_access_before_apply(self):
        source=self.source();DocumentAccess.objects.create(document=source,user=self.reader,enabled=True,granted_by=self.leader)
        from accounts.models import AccessGrant
        AccessGrant.objects.create(user=self.reader,permission='document.upload',granted_by=self.leader)
        data=self.form(sources=[source]).cleaned_data
        DocumentAccess.objects.filter(user=self.reader).update(enabled=False)
        with self.assertRaises(PermissionDenied):assemble(self.reader,data)
    def test_failure_cleans_generated_files_and_rolls_back(self):
        form=self.form(files=[self.scan()])
        with patch('documents.assembly.DocumentPart.objects.create',side_effect=RuntimeError('ТЕСТ')):
            with self.assertRaises(RuntimeError):assemble(self.leader,form.cleaned_data)
        self.assertFalse(Document.objects.exists());self.assertFalse(list(Path(self.root.name).rglob('*.*')))
        self.assertEqual([p for p in Path(self.root.name).rglob('*') if p.is_file()],[])
    def test_invalid_image_and_encrypted_pdf(self):
        with self.assertRaises(ValidationError):pdf_bytes([{'content':b'bad','content_type':'image/png'}])
        from pypdf import PdfWriter
        output=io.BytesIO();writer=PdfWriter();writer.add_blank_page(width=100,height=100);writer.encrypt('test');writer.write(output)
        with self.assertRaises(ValidationError):pdf_bytes([{'content':output.getvalue(),'content_type':'application/pdf'}])
    def test_page_limit(self):
        from pypdf import PdfWriter
        output=io.BytesIO();writer=PdfWriter()
        for i in range(41):writer.add_blank_page(width=100,height=100)
        writer.write(output)
        with self.assertRaises(ValidationError):pdf_bytes([{'content':output.getvalue(),'content_type':'application/pdf'}])
    def test_original_endpoint_rechecks_file_acl(self):
        doc=self.create();part=doc.current_version.parts.first()
        self.client.force_login(self.reader);url=reverse('document_part',args=[doc.pk,part.pk]);self.assertEqual(self.client.get(url).status_code,404)
        grant=DocumentAccess.objects.create(document=doc,user=self.reader,enabled=True,granted_by=self.leader)
        response=self.client.get(url);self.assertEqual(response.status_code,200);response.close()
        preview=self.client.get(url+'?preview=1');self.assertEqual(preview.status_code,200);self.assertEqual(preview['Content-Type'],'image/png');self.assertTrue(b''.join(preview.streaming_content).startswith(b'\x89PNG'))
        grant.enabled=False;grant.save();self.assertEqual(self.client.get(url).status_code,404)
    def test_archived_contract_blocks_new_composition_version(self):
        doc=self.create();form=self.form(target=doc)
        contract=Contract.objects.create(legal_entity=self.entity,number='ТЕСТ',subject='ТЕСТ')
        DocumentContract.objects.create(document=doc,contract=contract);contract.status='archived';contract.save()
        with self.assertRaises(PermissionDenied):assemble(self.leader,form.cleaned_data,doc.pk)
        self.assertEqual(doc.versions.count(),1)
    def test_originals_are_immutable(self):
        part=self.create().current_version.parts.first()
        with self.assertRaises(ValueError):part.save()
        with self.assertRaises(ValueError):DocumentPart.objects.filter(pk=part.pk).update(original_name='Другое')
    def test_deadlines_opt_in_and_current_acl(self):
        doc=self.source();day=timezone.localdate()+timedelta(days=7)
        save_expiry(self.leader,doc.pk,doc.revision,day,False,30);self.assertFalse(due_documents(self.leader))
        doc.refresh_from_db();save_expiry(self.leader,doc.pk,doc.revision,day,True,7)
        self.assertEqual(len(due_documents(self.leader)),1);self.assertFalse(due_documents(self.reader))
        grant=DocumentAccess.objects.create(document=doc,user=self.reader,enabled=True,granted_by=self.leader)
        self.assertEqual(len(due_documents(self.reader)),1);grant.enabled=False;grant.save();self.assertFalse(due_documents(self.reader))
    def test_deadline_permissions_validation_and_stale_revision(self):
        doc=self.source();day=timezone.localdate()
        with self.assertRaises(PermissionDenied):save_expiry(self.reader,doc.pk,doc.revision,day,True,30)
        with self.assertRaises(ValidationError):save_expiry(self.leader,doc.pk,doc.revision,None,True,30)
        with self.assertRaises(ValidationError):save_expiry(self.leader,doc.pk,doc.revision,day,True,366)
        with self.assertRaises(ValidationError):save_expiry(self.leader,doc.pk,0,day,True,30)
    def test_deadline_excel_preview_is_atomic_and_versioned(self):
        doc=self.source();rows=[{'id':str(doc.pk),'expires':str(timezone.localdate()),'remind':True,'days':30,'version':doc.revision,'row':2}]
        apply_rows(self.leader,rows);doc.refresh_from_db();self.assertTrue(doc.remind)
        with self.assertRaises(ValidationError):apply_rows(self.leader,rows)
        from django.db import transaction
        newer=[dict(rows[0],version=doc.revision,remind=False),dict(rows[0],version=0,row=3)]
        with self.assertRaises(ValidationError):apply_rows(self.leader,newer)
        doc.refresh_from_db();self.assertTrue(doc.remind)
    def test_excel_dates_and_roundtrip(self):
        doc=self.source();content=workbook([[str(doc.pk),doc.title,'2027-01-01',1,30,doc.revision]],headers=HEADERS)
        rows=rows_from_file(SimpleUploadedFile('dates.xlsx',content));self.assertEqual(rows[0]['expires'],'2027-01-01')
    def test_forms_and_deadline_notice(self):
        doc=self.create();save_expiry(self.leader,doc.pk,doc.revision,timezone.localdate(),True,30);self.client.force_login(self.leader)
        for name,args in [('document_compose',[]),('document_detail',[doc.pk]),('document_pages',[doc.pk]),('document_deadlines',[]),('document_expiry',[doc.pk]),('document_deadlines_import',[])]:
            self.assertEqual(self.client.get(reverse(name,args=args)).status_code,200,name)
        self.assertContains(self.client.get(reverse('document_detail',args=[doc.pk])),'Требуют внимания сроки документов: 1')

    def test_palette_transparency_is_white_in_pdf(self):
        output=io.BytesIO()
        with Image.new('P',(30,30),0) as image:
            image.putpalette([0,0,0,255,0,0]+[0]*762)
            image.putpixel((15,15),1);image.save(output,format='PNG',transparency=0)
        data,_=pdf_bytes([{'content':output.getvalue(),'content_type':'image/png'}])
        picture=PdfReader(io.BytesIO(data)).pages[0].images[0].image.convert('RGB')
        try:self.assertTrue(all(channel>200 for channel in picture.getpixel((0,0))))
        finally:picture.close()
