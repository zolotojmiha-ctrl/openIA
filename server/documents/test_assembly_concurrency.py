import io,secrets,tempfile,threading
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from PIL import Image
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.db import connection,connections,close_old_connections
from django.test import TransactionTestCase,override_settings
from accounts.models import User
from projects.models import LegalEntity,COMPANY_TAX_ID
from .models import Document
from .assembly import AssemblyForm,assemble
from .services import make_version
from .storage import validate_upload

@skipUnless(connection.vendor=='postgresql','Проверка блокировок требует PostgreSQL')
class AssemblyConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.root=tempfile.TemporaryDirectory();self.addCleanup(self.root.cleanup)
        setting=override_settings(PRIVATE_DOCUMENT_ROOT=self.root.name);setting.enable();self.addCleanup(setting.disable)
        self.users=[User.objects.create_user(f'+7999000950{i}',secrets.token_urlsafe(24),leader_slot=i,full_name='ТЕСТ',must_change_password=False) for i in (1,2)]
        self.entity,_=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
    def image(self):
        out=io.BytesIO()
        with Image.new('RGB',(40,60),'white') as image:image.save(out,format='PNG')
        return SimpleUploadedFile('scan.png',out.getvalue())
    def form(self,user,target=None,sources=()):
        initial=AssemblyForm(user=user,target=target)
        form=AssemblyForm(dict(token=initial.initial['token'],snapshot=initial.initial['snapshot'],expected_revision=target.revision if target else 0,title='ТЕСТ',sources=[str(d.pk) for d in sources],comment='ТЕСТ'),{'files':[] if sources else [self.image()]},user=user,target=target)
        self.assertTrue(form.is_valid(),form.errors);return form.cleaned_data
    def race(self,operation):
        barrier=threading.Barrier(2)
        def run(index):
            close_old_connections()
            try:barrier.wait(timeout=10);operation(index);return 'saved'
            except ValidationError:return 'changed'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(run,[0,1]))
    def test_duplicate_send_creates_one_version(self):
        data=self.form(self.users[0])
        self.assertEqual(self.race(lambda i:assemble(self.users[0],data)),['saved','saved'])
        self.assertEqual(Document.objects.count(),1);self.assertEqual(Document.objects.get().versions.count(),1)
    def test_opposing_assemblies_lock_sources_in_same_order(self):
        docs=[]
        for i in range(2):
            doc=Document.objects.create(legal_entity=self.entity,title=f'ТЕСТ {i}',section='general',created_by=self.users[i]);make_version(doc,self.users[i],validate_upload(self.image()));docs.append(doc)
        data=[self.form(self.users[i],docs[i],[docs[1-i]]) for i in range(2)]
        self.assertCountEqual(self.race(lambda i:assemble(self.users[i],data[i],docs[i].pk)),['saved','changed'])
        self.assertEqual(sum(d.versions.count() for d in docs),3)
