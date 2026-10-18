from .models import COMPANY_TAX_ID, Contract, ProjectObject

def visible_objects(user):
    qs = ProjectObject.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID)
    if not user.is_leader:
        qs = qs.filter(assignments__user=user)
    return qs.distinct()

def visible_contracts(user):
    qs = Contract.objects.filter(legal_entity__tax_id=COMPANY_TAX_ID)
    if not user.is_leader:
        qs = qs.filter(access_rules__user=user, objects_linked__in=visible_objects(user))
    return qs.distinct()

def contract_card(contract, user):
    """Allowlisted presentation data: never pass the full contract to employee templates."""
    if user.is_leader:
        return {
            'id':contract.pk, 'title':contract.number, 'subject':contract.subject,
            'fields':[('Номер договора',contract.number),('Дата',contract.date),('Предмет',contract.subject),
                      ('Контрагент',contract.counterparty),('Срок окончания',contract.deadline),
                      ('Состояние',contract.get_status_display()),('Сумма по карточке, ₽',contract.amount),
                      ('Внутренние примечания',contract.private_notes)],
        }
    rule = contract.access_rules.get(user=user)
    fields = []
    for flag,label,attribute in [('show_number','Номер договора','number'),('show_date','Дата','date'),
                                  ('show_subject','Предмет','subject'),('show_deadline','Срок окончания','deadline'),
                                  ('show_counterparty','Контрагент','counterparty')]:
        if getattr(rule,flag):
            fields.append((label,getattr(contract,attribute)))
    return {'id':contract.pk,'title':contract.number if rule.show_number else 'Договор — ограниченный доступ',
            'subject':contract.subject if rule.show_subject else '', 'fields':fields}
