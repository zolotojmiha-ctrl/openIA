"""Daily photographic evidence, separate from completion and material consumption."""
import io,uuid
from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone
from PIL import Image,UnidentifiedImageError
from .forms import MultipleFiles,MultipleInput

STAGES=[('excavation','Вскрыли котлован'),('stripping','Сняли изоляцию'),('ddk','Провели ДДК'),
        ('surface','Подготовили поверхность'),('installation','Смонтировали муфту'),
        ('sealing','Герметизировали / залили'),('coating','Нанесли изоляцию'),('backfill','Выполнили обратную засыпку')]


def normalize(value):
    if not isinstance(value,dict) or not isinstance(value.get('stages',[]),list):raise ValidationError('Проверьте этапы работ.')
    stages=value.get('stages',[])
    if any(not isinstance(s,str) or s not in dict(STAGES) for s in stages):raise ValidationError('Неизвестный этап работ.')
    return {'stages':[key for key,label in STAGES if key in stages]}


def check_images(files):
    if not files:raise ValidationError('Добавьте хотя бы одну фотографию.')
    for item in files:
        if item['content_type'] not in ('image/jpeg','image/png'):raise ValidationError('Для хода работ загрузите фотографии PNG/JPEG.')
        try:
            with Image.open(io.BytesIO(item['content'])) as image:
                if image.format not in ('PNG','JPEG') or image.width*image.height>36000000:raise ValueError
                image.verify()
        except (OSError,ValueError,SyntaxError,EOFError,UnidentifiedImageError,Image.DecompressionBombError):raise ValidationError('Не удалось проверить фотографию. Загрузите исправный PNG/JPEG.') from None
    return files


class ProgressFiles(MultipleFiles):
    def clean(self,data,initial=None):return check_images(super().clean(data,initial))


class ProgressForm(forms.Form):
    token=forms.UUIDField(widget=forms.HiddenInput)
    expected_version=forms.IntegerField(widget=forms.HiddenInput)
    performed_date=forms.DateField(label='Дата работ',widget=forms.DateInput(format='%Y-%m-%d',attrs={'type':'date'}))
    stages=forms.MultipleChoiceField(label='Что сделано — необязательно',choices=STAGES,required=False,widget=forms.CheckboxSelectMultiple)
    note=forms.CharField(label='Подпись — необязательно',max_length=3000,required=False,widget=forms.Textarea(attrs={'rows':3,'placeholder':'Можно оставить пустым'}))
    files=ProgressFiles(label='Фотографии',required=False,widget=MultipleInput(attrs={'accept':'image/jpeg,image/png'}))
    action='note'
    def __init__(self,*args,job,user,**kwargs):
        super().__init__(*args,**kwargs)
        self.initial.update(token=uuid.uuid4(),expected_version=job.version,performed_date=timezone.localdate())
    def clean_performed_date(self):
        value=self.cleaned_data['performed_date']
        if value>timezone.localdate():raise forms.ValidationError('Дата работ не может быть в будущем.')
        return value
    def clean(self):
        data=super().clean();data['progress']=normalize({'stages':data.get('stages',[])})
        return data
