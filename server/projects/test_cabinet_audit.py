"""Navigation audit: discover screens from visible links, never from private URLs."""
import tempfile,hashlib,json
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit,parse_qs,urljoin,urlencode
from unittest.mock import patch
from django.test import TestCase,override_settings
from django.urls import reverse,resolve
from django.utils import timezone
from accounts.models import User,AccessGrant
from projects import test_complete616 as fixtures
from projects.models import ObjectAccess,ContractAccess,CustomerAccess,Subcontractor,SubcontractOrder,Warehouse,StockItem,MaterialRequest,CounterpartyAgreement
from documents.models import Document,DocumentVersion,Bundle
from documents.storage import private_path
from finance.models import FinancialScope

class Links(HTMLParser):
    def __init__(self):super().__init__();self.links=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='a' and attrs.get('href'):self.links.append(attrs['href'])

class CabinetAudit(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.CompleteWorkflowTests.setUpTestData.__func__(cls)
        cls.pto=User.objects.create_user('+79999989901','123456',role='pto',full_name='ПТО',must_change_password=False)
        for p in ['repairs.prepare','works.prepare','document.upload','document.export','contract.changes']:AccessGrant.objects.create(user=cls.pto,permission=p,granted_by=cls.leader)
        for u in [cls.master,cls.pto]:
            ObjectAccess.objects.create(user=u,project_object=cls.area,granted_by=cls.leader)
            ContractAccess.objects.create(user=u,contract=cls.contract,granted_by=cls.leader,show_number=True,show_subject=True)
        cls.job.assigned_to=cls.master;cls.job.save(update_fields=['assigned_to'])
        cls.company=Subcontractor.objects.create(legal_entity=cls.entity,name='Подрядчик',tax_id='1234567890',created_by=cls.leader)
        cls.order=SubcontractOrder.objects.create(company=cls.company,contract=cls.contract,project_object=cls.area,kind='repairs',title='Работы',created_by=cls.leader)
        cls.wh=Warehouse.objects.create(legal_entity=cls.entity,name='Склад',created_by=cls.leader);cls.wh.served_objects.add(cls.area)
        StockItem.objects.create(legal_entity=cls.entity,sku='ТЕСТ',name='Материал',created_by=cls.leader)
        FinancialScope.objects.create(legal_entity=cls.entity,title='Финансы контракта',contract=cls.contract,created_by=cls.leader)
        CounterpartyAgreement.objects.create(number='Аренда',kind='housing',subject='Жильё',counterparty='Компания',created_by=cls.leader)
        cls.bundle=Bundle.objects.create(legal_entity=cls.entity,title='Комплект документов',created_by=cls.leader)
        MaterialRequest.objects.create(contract=cls.contract,project_object=cls.area,name='Муфта',quantity=2,unit='шт',created_by=cls.leader)
        CustomerAccess.objects.create(user=cls.customer,contract=cls.contract,project_object=cls.area,created_by=cls.leader)
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        settings=override_settings(PRIVATE_DOCUMENT_ROOT=self.tmp.name);settings.enable();self.addCleanup(settings.disable)
        content=b'Synthetic audit document'
        key='ab/'+'a'*32;path=private_path(key);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
        doc=Document.objects.create(legal_entity=self.entity,title='Документ проверки',section='general',created_by=self.leader,bundle=self.bundle)
        v=DocumentVersion.objects.create(document=doc,sequence=1,storage_key=key,original_name='audit.txt',content_type='text/plain',size=len(content),sha256=hashlib.sha256(content).hexdigest(),uploaded_by=self.leader)
        doc.current_version=v;doc.save();doc.objects_linked.add(self.area);doc.contracts_linked.add(self.contract);doc.personnel_linked.add(self.worker)
    def crawl(self,user):
        self.client.force_login(user);queue=deque([(reverse('dashboard'),'menu')]);seen=set();errors=[];routes=set();edges=[]
        while queue and len(seen)<350:
            url,origin=queue.popleft();parsed=urlsplit(url)
            if parsed.netloc or not parsed.path.startswith('/cabinet/') or '/logout/' in parsed.path:continue
            # Deduplicate redirects and recurring combinations of filter links.
            match=resolve(parsed.path);key=(parsed.path,tuple((k,tuple(v)) for k,v in sorted(parse_qs(parsed.query).items()) if k in ('contract','object','section','kind','archive','mode')))
            if key in seen:continue
            seen.add(key);routes.add(match.url_name);edges.append({'url':url,'from':origin,'route':match.url_name})
            with patch('socket.create_connection',side_effect=OSError('External connections disabled during audit')):
                response=self.client.get(url,follow=False)
            if response.status_code in (301,302,303,307,308):queue.append((response['Location'],url));continue
            if response.status_code>=400:errors.append((response.status_code,url,origin));continue
            if response.streaming:
                for chunk in response.streaming_content:pass
                continue
            if 'text/html' not in response.get('Content-Type',''):continue
            parser=Links();parser.feed(response.content.decode())
            for link in parser.links:
                target=urljoin(url,link);parts=urlsplit(target)
                if parts.path!=parsed.path or parts.query!=parsed.query:queue.append((target.split('#')[0],url))
        Path('/tmp/cabinet-audit-'+('leader' if user.is_leader else user.role)+'.json').write_text(json.dumps({'pages':len(seen),'routes':sorted(routes),'errors':errors,'edges':edges},ensure_ascii=False,indent=2))
        self.assertFalse(queue,'Navigation audit reached its page limit before completing the graph')
        self.assertFalse(errors,'Broken visible links: '+repr(errors));return routes
    def test_leader_routes_from_menu(self):
        routes=self.crawl(self.leader)
        for required in ('repairs','work_home','work_contract','work_place','repair_excel_export','documents','document_detail','people','person_detail','equipment','stock','finance','subcontracts','agreements','users','beget_setup','customer_access','contract_changes_review'):
            self.assertIn(required,routes,required+' is unreachable from visible links')
    def test_pto_routes_from_menu(self):self.crawl(self.pto)
    def test_foreman_routes_from_menu(self):self.crawl(self.master)
    def test_customer_routes_from_menu(self):
        routes=self.crawl(self.customer)
        self.assertNotIn('people',routes);self.assertNotIn('finance',routes);self.assertNotIn('users',routes)

    def test_export_applies_revision_filter_and_preserves_area_links(self):
        from repairs.models import Job,Defect,JobDefect
        from repairs.importer import read_book
        from django.core.files.uploadedfile import SimpleUploadedFile
        job=Job.objects.create(section=self.section,contract=self.contract,method='grind',review_note='Нужно уточнение',created_by=self.leader)
        defect=Defect.objects.create(section=self.section,contract=self.contract,kilometre=125,number='999')
        JobDefect.objects.create(job=job,defect=defect)
        self.client.force_login(self.leader)
        response=self.client.get(reverse('repair_excel_export'),{'contract':self.contract.pk,'object':self.area.pk,'state':'revision'})
        book=read_book(SimpleUploadedFile('audit.xlsx',response.content))
        self.assertEqual(book['cells'][(2,4)],'999')
        self.assertFalse(any(row>2 for row,col in book['cells']))
        response=self.client.get(reverse('work_place',args=[self.contract.pk,self.area.pk]))
        parser=Links();parser.feed(response.content.decode())
        export=[u for u in parser.links if u.startswith(reverse('repair_excel_export'))]
        self.assertEqual(len(export),1);self.assertEqual(parse_qs(urlsplit(export[0]).query)['object'],[str(self.area.pk)])
        archived=[u for u in parser.links if parse_qs(urlsplit(u).query).get('archive')==['1']]
        self.assertTrue(archived)
        self.assertTrue(all(parse_qs(urlsplit(u).query).get('object')==[str(self.area.pk)] for u in archived))

    def test_export_more_than_300_repairs_is_not_truncated(self):
        from repairs.models import Job,Defect,JobDefect
        from repairs.importer import read_book
        from django.core.files.uploadedfile import SimpleUploadedFile
        jobs=[Job(section=self.section,contract=self.contract,method='grind',created_by=self.leader) for _ in range(303)]
        defects=[Defect(section=self.section,contract=self.contract,kilometre=200+i,number=str(10000+i)) for i in range(303)]
        Job.objects.bulk_create(jobs);Defect.objects.bulk_create(defects)
        JobDefect.objects.bulk_create([JobDefect(job=j,defect=d) for j,d in zip(jobs,defects)])
        self.client.force_login(self.leader)
        response=self.client.get(reverse('repair_excel_export'),{'contract':self.contract.pk,'object':self.area.pk})
        self.assertEqual(response.status_code,200)
        book=read_book(SimpleUploadedFile('all.xlsx',response.content))
        self.assertEqual(len({row for row,col in book['cells'] if row>1}),304)

    def test_import_context_is_pinned_and_returns_to_area(self):
        from repairs.models import Section,ImportBatch
        from repairs.excel import workbook
        from django.core.files.uploadedfile import SimpleUploadedFile
        from projects.models import ProjectObject,ContractObject
        other=ProjectObject.objects.create(legal_entity=self.entity,name='Другой участок')
        ContractObject.objects.create(contract=self.contract,project_object=other)
        alternative=Section.objects.create(project_object=other,rnu='РНУ',pipeline='УБО',name='Другая ведомость')
        self.client.force_login(self.leader)
        url=reverse('repair_import')+'?'+urlencode({'contract':self.contract.pk,'object':self.area.pk})
        response=self.client.get(url)
        self.assertTrue(response.context['form'].fields['contract'].disabled)
        self.assertTrue(response.context['form'].fields['section'].disabled)
        response=self.client.post(url,{'section':str(alternative.pk),'file':SimpleUploadedFile('defects.xlsx',workbook([['A-01','126.123','123','998','шлифовка','','','']]))})
        self.assertEqual(response.status_code,302,str(response.context['form'].errors) if response.status_code==200 else '')
        batch=ImportBatch.objects.get();self.assertEqual(batch.section_id,self.section.pk)
        response=self.client.post(reverse('repair_import_review',args=[batch.pk]),{'row_0':'new'})
        self.assertRedirects(response,reverse('work_place',args=[self.contract.pk,self.area.pk]))

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.conf import settings
import subprocess,os
class CabinetMenuBrowser(StaticLiveServerTestCase):
    def test_export_reached_only_by_menu_and_keeps_scope(self):
        from projects.models import LegalEntity,COMPANY_TAX_ID,Contract,ProjectObject,ContractObject
        from repairs.models import Section,Job,Defect,JobDefect
        from repairs.importer import read_book
        from django.core.files.uploadedfile import SimpleUploadedFile
        LegalEntity.objects.get_or_create(tax_id=COMPANY_TAX_ID,defaults={'name':'ТЕСТ Компания'})
        fixtures.CompleteWorkflowTests.setUpTestData.__func__(type(self))
        other=Contract.objects.create(legal_entity=self.entity,number='ДРУГОЙ КОНТРАКТ',work_kinds=['repairs'])
        place=ProjectObject.objects.create(legal_entity=self.entity,name='Другой участок');ContractObject.objects.create(contract=other,project_object=place)
        section=Section.objects.create(project_object=place,rnu='РНУ',pipeline='УБО',name='Другой участок')
        job=Job.objects.create(section=section,contract=other,method='grind',created_by=self.leader)
        d=Defect.objects.create(section=section,contract=other,kilometre=300,number='987654');JobDefect.objects.create(job=job,defect=d)
        env=os.environ|{'ARMADA_TEST_URL':self.live_server_url,'ARMADA_AUDIT_FIXTURE':json.dumps({'contract':str(self.contract.pk),'area':str(self.area.pk)})}
        result=subprocess.run(['node',str(settings.BASE_DIR/'tests/cabinet-audit-browser.cjs')],env=env,capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        book=read_book(SimpleUploadedFile('audit.xlsx',Path('/tmp/cabinet-audit-export.xlsx').read_bytes()))
        values=set(book['cells'].values());self.assertIn('001',values);self.assertNotIn('987654',values)
