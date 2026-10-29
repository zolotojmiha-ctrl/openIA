import io,uuid
from datetime import timedelta
from decimal import Decimal
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from accounts import xlsx
from documents.models import Person
from repairs.excel import workbook,SIMPLE_HEADERS
from repairs.importer import read_book,parse_work
from . import test_stock as base
from . import registry_excel,equipment_excel,crew_excel,stock_excel,subcontract_excel
from . import stock_exchange,stock_receipt_excel,stock_groups
from .models import ProjectObject,StockItem,Equipment


class HumanExcelTests(TestCase):
    setUpTestData=classmethod(base.StockTests.setUpTestData.__func__)
    wh=base.StockTests.wh;data=base.StockTests.data;make_job=base.StockTests.make_job

    def upload(self,headers,rows):
        return SimpleUploadedFile('human.xlsx',workbook(rows,headers=headers))

    def apply(self,module,kind,headers,rows):
        parsed=module.parse_rows(self.upload(headers,rows),kind,self.leader)
        with transaction.atomic():module.apply_rows(self.leader,kind,parsed)
        return parsed

    def test_new_people_file_has_only_name_and_position(self):
        self.apply(registry_excel,'people',['ФИО','Должность'],[['Зарипов Рафил','Мастер']])
        self.assertTrue(Person.objects.filter(full_name='Зарипов Рафил',position='Мастер').exists())

    def test_new_area_file_does_not_need_id_version_or_status(self):
        self.apply(registry_excel,'objects',['Название','Местоположение'],[['Новый участок','Омск']])
        self.assertEqual(ProjectObject.objects.get(name='Новый участок').status,'planned')

    def test_contract_links_use_area_names_and_russian_dates(self):
        area=ProjectObject.objects.create(legal_entity=self.company,name='Свободный участок')
        self.apply(registry_excel,'contracts',
            ['Номер договора','Дата договора','Предмет договора','Виды работ','Контрагент','Участки работ (через ;)'],
            [['Новый контракт','18.08.2026','Дефекты','Устранение дефектов','Заказчик',area.name]])
        from .models import Contract
        contract=Contract.objects.get(number='Новый контракт')
        self.assertEqual(contract.objects_linked.get().pk,area.pk)
        self.assertEqual(str(contract.date),'2026-08-18')

    def test_equipment_can_be_added_without_english_codes(self):
        self.apply(equipment_excel,'register',['Название / модель','Вид техники','Инвентарный номер'],
                   [['Соболь','Автомобиль','992']])
        item=Equipment.objects.get(inventory_number='992')
        self.assertEqual((item.ownership,item.status),('own','ready'))

    def test_crew_master_is_a_person_name(self):
        Person.objects.create(legal_entity=self.company,full_name='Зарипов Рафил',created_by=self.leader)
        self.apply(crew_excel,'brigades',['Название','Мастер','Начало участия нового мастера'],
                   [['Бригада Зарипова','Зарипов Рафил',str(self.today)]])
        from .models import Brigade
        self.assertEqual(Brigade.objects.get(name='Бригада Зарипова').foreman.full_name,'Зарипов Рафил')

    def test_stock_catalogue_generates_its_code_and_unit(self):
        self.apply(stock_excel,'items',['Наименование','Вид','Метод','Диаметр мм','Длина мм','Стенка мм'],
                   [['Муфта П1','Муфта','П1','720','1500','10']])
        item=StockItem.objects.get()
        self.assertTrue(item.sku.startswith('МТР-'))
        self.assertEqual((item.kind,item.unit,item.method),('coupling','pcs','p1'))

    def test_subcontract_company_needs_no_archive_flag_or_version(self):
        self.apply(subcontract_excel,'companies',['Компания','ИНН','Контакт'],
                   [['Подрядчик','1234567890','Телефон']])
        from .models import Subcontractor
        self.assertTrue(Subcontractor.objects.filter(name='Подрядчик').exists())

    def test_related_ambiguous_names_are_rejected_without_guessing(self):
        ProjectObject.objects.create(legal_entity=self.company,name=self.obj.name)
        with self.assertRaisesMessage(ValidationError,'несколько записей'):
            xlsx.References(self.leader).resolve('objects',self.obj.name,'Участок')

    def test_inaccessible_name_is_not_resolved(self):
        hidden=ProjectObject.objects.create(legal_entity=self.company,name='Чужой участок')
        with self.assertRaisesMessage(ValidationError,'не найдена среди доступных'):
            xlsx.References(self.worker).resolve('objects',hidden.name,'Участок')

    def test_export_keeps_versions_hidden_and_supports_edit_roundtrip(self):
        rows=registry_excel.export_rows(self.leader,'objects')
        data=xlsx.workbook(self.leader,'registry.objects',rows,registry_excel.HEADERS['objects'])
        with ZipFile(io.BytesIO(data)) as z:
            root=ET.fromstring(z.read('xl/worksheets/sheet1.xml'))
        ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        self.assertTrue(root.findall('s:cols/s:col[@hidden="1"]',ns))
        raw=read_book(io.BytesIO(data))
        self.assertEqual(raw['cells'][1,1],'Название')
        parsed=registry_excel.parse_rows(io.BytesIO(data),'objects',self.leader)
        self.assertEqual(parsed[0]['values'][0],str(self.obj.pk))
        self.assertEqual(parsed[0]['values'][-1],str(self.obj.version))

    def test_equipment_roundtrip_names_and_choices(self):
        self.test_equipment_can_be_added_without_english_codes()
        data=xlsx.workbook(self.leader,'equipment.register',equipment_excel.export_rows(self.leader,'register'),equipment_excel.HEADERS['register'])
        parsed=equipment_excel.parse_rows(io.BytesIO(data),'register',self.leader)
        self.assertEqual(parsed[0]['values'][6],'own')
        self.assertEqual(parsed[0]['values'][8],'ready')

    def test_stock_receipt_has_no_codes_auto_name_and_unit_and_preserves_serial(self):
        warehouse=self.wh('Володя Барабинск')
        headers=['Склад','Тип муфты','Диаметр, мм','Длина, мм','Стенка, мм','Количество','Дата получения','Заводской №']
        file=self.upload(headers,[[warehouse.name,'П2','720','1500','10',1,'18.08.2026','ЗАВ-1']])
        tables=stock_receipt_excel.parse(read_book(file))
        stock_exchange.apply(self.leader,tables)
        item=StockItem.objects.get()
        self.assertEqual((item.kind,item.method,item.unit,item.serial),('coupling','p2','pcs','ЗАВ-1'))
        self.assertEqual(str(item.receipt_date),'2026-08-18')
        self.assertEqual(item.balances.get().warehouse_id,warehouse.pk)

    def test_stock_simple_real_upload_preview_apply_and_retry(self):
        warehouse=self.wh('Володя Барабинск')
        content=stock_exchange.book([('Поступление',stock_receipt_excel.HEADERS,
            [[warehouse.name,'','П2',720,1500,8,7,'','18.08.2026','','','','','']])])
        self.client.force_login(self.leader)
        response=self.client.post(reverse('stock_inventory_import'),{'file':SimpleUploadedFile('stock.xlsx',content)})
        self.assertEqual(response.status_code,200,response.content)
        preview=response.context['preview'];self.assertTrue(preview,response.context['form'].errors)
        self.assertFalse(StockItem.objects.exists())
        for _ in range(2):
            self.assertEqual(self.client.post(reverse('stock_inventory_import'),{'apply':'1','preview':preview}).status_code,302)
        self.assertEqual(StockItem.objects.count(),7)
        self.assertEqual(stock_groups.groups(self.leader)[0]['quantity'],7)

    def test_insulation_and_electrode_units_are_automatic(self):
        warehouse=self.wh()
        headers=['Склад','Категория','Количество','Дата получения','Марка электродов','Диаметр электрода, мм']
        tables=stock_receipt_excel.parse(read_book(self.upload(headers,
            [[warehouse.name,'Изоляция','12.5',str(self.today),'',''],
             [warehouse.name,'Электроды','5',str(self.today),'УОНИ','3.2']])))
        stock_exchange.apply(self.leader,tables)
        self.assertEqual(StockItem.objects.get(kind='insulation').unit,'m2')
        self.assertEqual(StockItem.objects.get(kind='electrode').unit,'kg')

    def test_defect_simple_template_has_no_system_fields_and_auto_group(self):
        file=self.upload(SIMPLE_HEADERS,[[153.569,'1748','135490','П2',1,8,'','','','']])
        parsed=parse_work(file)
        self.assertEqual(len(parsed['jobs']),1)
        job=parsed['jobs'][0]
        self.assertTrue(job['code']);self.assertEqual(job['defects'][0]['number'],'1748')
        self.assertEqual((job['section_id'],job['contract_id']),('',''))

    def test_repeated_blank_groups_create_independent_constructions(self):
        file=self.upload(['Км','№ дефекта','Метод ремонта','Длина, м'],
            [[1,'001','П1',1],[2,'002','П1',1]])
        jobs=parse_work(file)['jobs']
        self.assertNotEqual(jobs[0]['code'],jobs[1]['code'])

    def test_new_export_templates_and_preview_pages_render(self):
        self.client.force_login(self.leader)
        urls=[reverse('registry_export',args=['people']),reverse('equipment_export',args=['register']),
              reverse('crew_excel_export',args=['brigades']),reverse('stock_inventory_export')+'?template=1']
        for url in urls:
            response=self.client.get(url);self.assertEqual(response.status_code,200,(url,response.content))
        for url in [reverse('registry_import',args=['people']),reverse('equipment_import',args=['register']),
                    reverse('crew_excel_import',args=['brigades']),reverse('stock_inventory_import')]:
            response=self.client.get(url);self.assertEqual(response.status_code,200,(url,response.content))

    def test_unknown_column_is_not_silently_discarded(self):
        with self.assertRaisesMessage(ValidationError,'Неизвестные столбцы'):
            registry_excel.parse_rows(self.upload(['ФИО','Непонятно'],[['Иванов','данные']]),'people',self.leader)

    def test_unchanged_export_reference_survives_identical_person_names(self):
        first=Person.objects.create(legal_entity=self.company,full_name='Иванов Иван',created_by=self.leader)
        Person.objects.create(legal_entity=self.company,full_name=first.full_name,created_by=self.leader)
        rows=[['','Новая бригада',str(first.pk),first.full_name,str(self.today),'0','']]
        content=xlsx.workbook(self.leader,'crews.brigades',rows,crew_excel.HEADERS['brigades'])
        parsed=crew_excel.parse_rows(io.BytesIO(content),'brigades',self.leader)
        self.assertEqual(parsed[0]['values'][2],str(first.pk))

    def test_hidden_reference_metadata_cannot_be_reused_by_another_user(self):
        content=xlsx.workbook(self.leader,'registry.objects',registry_excel.export_rows(self.leader,'objects'),registry_excel.HEADERS['objects'])
        with self.assertRaisesMessage(ValidationError,'под своей учётной записью'):
            xlsx.normalize(self.worker,'registry.objects',read_book(io.BytesIO(content)),registry_excel.HEADERS['objects'])

    def exchange_tables(self):
        self.client.force_login(self.leader)
        return stock_exchange.parse(io.BytesIO(self.client.get(reverse('stock_inventory_export')).content))

    def exchange_file(self,tables,legacy=False):
        return io.BytesIO(stock_exchange.book([(name,stock_exchange.HEADERS[name][:-1] if legacy else stock_exchange.HEADERS[name],
            [entry['values'][:-1] if legacy else entry['values'] for entry in rows]) for name,rows in tables.items()]))

    def test_advanced_allocation_and_transfer_use_material_names_without_internal_codes(self):
        from . import stock_planning as planning
        warehouse=self.wh('Володя');other=self.wh('Омск');job=self.make_job()
        planning.receive(self.leader,self.data(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8),warehouse,3,self.today,'База',uuid.uuid4())
        tables=self.exchange_tables();row=tables['Остатки'][0]['values'];label=row[-1]
        tables['Распределение']=[{'row':2,'values':['',planning.job_label(job),'1',warehouse.name,label]}]
        tables['Перемещения']=[{'row':2,'values':['',other.name,'1',self.today.strftime('%d.%m.%Y'),'нет','На Омск',label]}]
        parsed=stock_exchange.parse(self.exchange_file(tables))
        self.assertEqual(parsed['Распределение'][0]['values'][0],row[0])
        self.assertEqual(parsed['Перемещения'][0]['values'][0],row[0])
        stock_exchange.apply(self.leader,parsed)
        current={g['warehouse'].name:g for g in stock_groups.groups(self.leader)}
        self.assertEqual((current[warehouse.name]['quantity'],current[warehouse.name]['reserved'],current[other.name]['quantity']),(2,1,1))

    def test_advanced_legacy_file_without_material_column_remains_readable(self):
        from . import stock_planning as planning
        warehouse=self.wh()
        planning.receive(self.leader,self.data(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8),warehouse,1,self.today,'База',uuid.uuid4())
        tables=stock_exchange.parse(self.exchange_file(self.exchange_tables(),legacy=True))
        stock_exchange.apply(self.leader,tables)
        self.assertEqual(stock_groups.groups(self.leader)[0]['quantity'],1)

    def test_advanced_new_material_automatically_gets_service_code(self):
        warehouse=self.wh()
        values=['']*len(stock_exchange.HEADERS['Остатки'])
        for index,value in {2:warehouse.name,3:'Изоляция',4:'Изоляция',5:'м²',16:'12.5',18:self.today.strftime('%d.%m.%Y'),19:'База'}.items():values[index]=value
        tables={name:[] for name in ['Остатки','Распределение','Изделия','Перемещения']}
        tables['Остатки']=[{'row':2,'values':values}]
        parsed=stock_exchange.parse(self.exchange_file(tables))
        stock_exchange.apply(self.leader,parsed)
        self.assertEqual(stock_groups.groups(self.leader)[0]['quantity'],Decimal('12.5'))

    def test_renumbered_old_exchange_row_explains_how_to_restore_import(self):
        from . import stock_planning as planning
        warehouse=self.wh()
        planning.receive(self.leader,self.data(kind='coupling',unit='pcs',method='p2',length_mm=1000,wall_mm=8),warehouse,1,self.today,'База',uuid.uuid4())
        tables=self.exchange_tables();tables['Остатки'][0]['values'][0]='1'
        with self.assertRaisesMessage(ValidationError,'Скачайте актуальный список'):
            stock_exchange.apply(self.leader,tables)
        self.assertEqual(stock_groups.groups(self.leader)[0]['quantity'],1)
