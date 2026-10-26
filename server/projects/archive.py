"""Archive write barrier shared by ordinary saves and bulk ORM operations.

Lock only parent contracts, in UUID order. Closure reads children without locking
 them, so the parent lock serializes closure with writes without reversing child
 lock order. Object, person, file ACLs and shared resource cards remain independent.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from django.core.exceptions import PermissionDenied
from django.db import models,transaction,OperationalError

MESSAGE='Контракт находится в архиве. Изменения доступны после восстановления руководителем.'
_lifecycle=ContextVar('contract_lifecycle',default=False)
PATHS={
 'projects.dailywork':('contract_id',),
 'projects.dailyworkrevision':('work__contract_id',),
 'projects.dailyworkdecision':('revision__work__contract_id',),
 'projects.contractchange':('contract_id',),
 'projects.contractchangerevision':('change__contract_id',),
 'projects.contractchangedecision':('revision__change__contract_id',),
 'projects.contractchangecommercial':('revision__change__contract_id',),
 'projects.contractchangesubmission':('change__contract_id',),
 'projects.contract':('pk',), 'projects.contractobject':('contract_id',),
 'projects.brigadeassignment':('contract_id',), 'projects.equipmentassignment':('contract_id',),
 'projects.subcontractorder':('contract_id',), 'projects.subcontractresult':('order__contract_id',),
 'projects.subcontractdecision':('result__order__contract_id',),
 'projects.subcontractattachment':('result__order__contract_id',),
 'projects.subcontractdocument':('order__contract_id',),
 'projects.stockmovement':('job__contract_id',), 'projects.stockentry':('movement__job__contract_id',),
 'repairs.job':('contract_id',), 'repairs.defect':('contract_id',),
 'repairs.jobdefect':('job__contract_id','defect__contract_id'),
 'repairs.report':('job__contract_id',), 'repairs.attachment':('report__job__contract_id',),
 'repairs.couplinguse':('job__contract_id',), 'repairs.importbatch':('contract_id',),
 'finance.financialscope':('contract_id',), 'finance.openingsnapshot':('scope__contract_id',),
 'finance.pendingwork':('scope__contract_id',), 'finance.paymentallocation':('scope__contract_id',),
 'finance.workexecution':('scope__contract_id',), 'finance.advanceoffset':('work__scope__contract_id',),
 'finance.costentry':('scope__contract_id',), 'finance.subcontractobligation':('scope__contract_id',),
 'finance.pricechange':('contract_id',),
 'documents.documentpart':('version__document__contracts_linked__pk',),
 'documents.documentcontract':('contract_id',),
 'documents.document':('contracts_linked__pk',),
 'documents.documentversion':('document__contracts_linked__pk',),
 'documents.approvalevent':('version__document__contracts_linked__pk',),
}

@contextmanager
def lifecycle_write():
    token=_lifecycle.set(True)
    try:yield
    finally:_lifecycle.reset(token)

def writable(ids,using='default'):
    from .models import Contract
    ids={pk for pk in ids if pk is not None}
    if not ids:return
    try:
        with transaction.atomic(using=using):
            rows=list(Contract.objects.using(using).filter(pk__in=ids).order_by('pk').select_for_update(no_key=True,nowait=True).values_list('status',flat=True))
    except OperationalError as error:
        if getattr(error.__cause__,'sqlstate',None)=='55P03':
            raise PermissionDenied('Контракт сейчас изменяется другим пользователем. Повторите действие после обновления страницы.') from error
        raise
    if 'archived' in rows:raise PermissionDenied(MESSAGE)

def instance_ids(instance,using):
    def resolve(current,parts):
        part,*rest=parts
        if not rest:return [getattr(current,part,None)]
        field=current._meta.get_field(part)
        if field.many_to_many:
            return [pk for row in getattr(current,part).using(using).all() for pk in resolve(row,rest)]
        pk=getattr(current,field.attname)
        if pk is None:return []
        return resolve(field.remote_field.model._base_manager.using(using).get(pk=pk),rest)
    return [pk for path in PATHS[instance._meta.label_lower] for pk in resolve(instance,path.split('__'))]

class ArchiveQuerySet(models.QuerySet):
    def guard(self):
        if self.model._meta.label_lower=='projects.contract' and _lifecycle.get():return
        paths=PATHS[self.model._meta.label_lower]
        ids=[]
        for row in self.values_list(*paths):ids.extend(row)
        writable(ids,self.db)
    def update(self,**kwargs):
        with transaction.atomic(using=self.db):
            self.guard()
            # Changing a parent link must check both the original and target scope.
            relations=[f for f in self.model._meta.fields if f.is_relation and (f.name in kwargs or f.attname in kwargs)]
            if relations:
                for row in self:
                    for field in relations:
                        value=kwargs.get(field.attname,kwargs.get(field.name))
                        if isinstance(value,models.Model):value=value.pk
                        if hasattr(value,'resolve_expression'):raise PermissionDenied('Изменяйте связь с контрактом явным значением.')
                        setattr(row,field.attname,value)
                    writable(instance_ids(row,self.db),self.db)
            return super().update(**kwargs)
    def delete(self):
        with transaction.atomic(using=self.db):
            self.guard()
            return super().delete()
    def bulk_create(self,objs,**kwargs):
        objs=list(objs)
        if kwargs.get('update_conflicts'):raise PermissionDenied('Для изменения существующих записей укажите их ID и версии.')
        with transaction.atomic(using=self.db):
            for obj in objs:
                self.model.objects.using(self.db).filter(pk=obj.pk).guard()
                writable(instance_ids(obj,self.db),self.db)
            return super().bulk_create(objs,**kwargs)
    def bulk_update(self,objs,fields,**kwargs):
        objs=list(objs)
        with transaction.atomic(using=self.db):
            for obj in objs:writable(instance_ids(obj,self.db),self.db)
            count=0
            for obj in objs:
                values={self.model._meta.get_field(name).attname:getattr(obj,self.model._meta.get_field(name).attname) for name in fields}
                count+=self.filter(pk=obj.pk).update(**values)
            return count

class ArchiveProtectedModel(models.Model):
    objects=ArchiveQuerySet.as_manager()
    class Meta:abstract=True
    def save(self,*args,**kwargs):
        from django.db import router
        using=kwargs.get('using') or router.db_for_write(type(self),instance=self)
        with transaction.atomic(using=using):
            fields=set(kwargs.get('update_fields') or [])
            revision_fields={'projects.contract':{'version','updated_at'},'documents.document':{'revision'}}
            # Access administration increments only its conflict counter. Content
            # and lifecycle writes are still guarded, including unrestricted saves.
            access_revision=bool(fields) and fields<=revision_fields.get(self._meta.label_lower,set())
            if not access_revision and not (self._meta.label_lower=='projects.contract' and _lifecycle.get()):
                type(self).objects.using(using).filter(pk=self.pk).guard()
                writable(instance_ids(self,using),using)
            return super().save(*args,**kwargs)
    def delete(self,*args,**kwargs):
        from django.db import router
        using=kwargs.get('using') or router.db_for_write(type(self),instance=self)
        with transaction.atomic(using=using):
            type(self).objects.using(using).filter(pk=self.pk).guard()
            writable(instance_ids(self,using),using)
            return super().delete(*args,**kwargs)
