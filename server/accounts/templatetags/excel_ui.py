from django import template
from accounts.xlsx import system,label,choices,References,REFS

register=template.Library()


@register.inclusion_tag('accounts/excel_preview.html')
def excel_preview(user,topic,headers,rows):
    hidden=system(topic,headers);visible=[n for n in range(len(headers)) if n not in hidden]
    references=References(user);result=[]
    for row in rows or []:
        number,raw=(row['row'],row['values']) if isinstance(row,dict) else row
        values=[]
        for n in visible:
            value=raw[n] if n<len(raw) else ''
            if headers[n] in REFS:
                value='; '.join(references.name(REFS[headers[n]],v.strip()) for v in str(value).split(';') if v.strip()) if 'через ;' in headers[n] else references.name(REFS[headers[n]],value)
            elif choices(headers[n]):value=choices(headers[n]).get(str(value),value)
            values.append(value)
        existing=bool(raw and raw[0])
        if topic=='daily':existing=existing and str(raw[1]) not in ('','0')
        result.append({'number':number,'action':'Связать' if topic=='subcontracts.repairs' else 'Изменить' if existing else 'Добавить','values':values})
    return {'headers':[label(headers[n]) for n in visible],'rows':result}
