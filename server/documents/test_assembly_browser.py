import os,secrets,subprocess,tempfile,io,base64
from datetime import timedelta
from PIL import Image,ImageDraw,ImageFont
from pypdf import PdfReader
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from accounts.models import User
from projects.models import LegalEntity,COMPANY_TAX_ID
from .models import Document,Person
from .services import make_version
from .storage import validate_upload,verified_file

class AssemblyBrowserTests(StaticLiveServerTestCase):
    def test_build_reorder_deadline_and_excel_from_phone(self):
        password=secrets.token_urlsafe(24)
        leader=User.objects.create_user('+79990009401',password,leader_slot=1,full_name='ТЕСТ Руководитель',must_change_password=False)
        entity,_=LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ'})
        person=Person.objects.create(legal_entity=entity,full_name='ТЕСТ Сотрудник',position='Мастер',created_by=leader)
        stream=io.BytesIO()
        with Image.new('RGB',(360,480),'white') as image:
            draw=ImageDraw.Draw(image)
            try:font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',22)
            except OSError:font=ImageFont.load_default(size=22)
            draw.rectangle((12,12,348,468),outline='navy',width=3)
            draw.multiline_text((26,54),'TEST CERTIFICATE\n\nDEMO EMPLOYEE\n\nTraining: completed\n\nSynthetic scan',font=font,fill='navy',spacing=15)
            image.save(stream,format='PNG')
        png=stream.getvalue();sources=[]
        with tempfile.TemporaryDirectory() as root,override_settings(PRIVATE_DOCUMENT_ROOT=root):
            for index in range(2):
                doc=Document.objects.create(legal_entity=entity,title=f'ТЕСТ Скан {index+1}',section='personnel',created_by=leader)
                make_version(doc,leader,validate_upload(SimpleUploadedFile(f'page{index+1}.png',png)));doc.personnel_linked.add(person);sources.append(doc)
            env=os.environ.copy();env.update(ARMADA_TEST_URL=self.live_server_url,ARMADA_TEST_PASSWORD=password,ARMADA_ASSEMBLY_PERSON=str(person.pk),ARMADA_ASSEMBLY_SOURCES=','.join(str(d.pk) for d in sources),ARMADA_ASSEMBLY_PNG=base64.b64encode(png).decode(),ARMADA_ASSEMBLY_DATE=str(timezone.localdate()+timedelta(days=3)))
            result=subprocess.run(['node',str(settings.BASE_DIR/'tests/assembly-browser.cjs')],env=env,capture_output=True,text=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            doc=Document.objects.get(title='ТЕСТ Удостоверение: три страницы')
            self.assertEqual(doc.current_version.parts.count(),3);self.assertTrue(doc.remind)
            with verified_file(doc.current_version) as file:self.assertEqual(len(PdfReader(file).pages),3)
            self.assertEqual(list(doc.current_version.parts.values_list('original_name',flat=True)),['page2.png','page1.png','new.png'])
