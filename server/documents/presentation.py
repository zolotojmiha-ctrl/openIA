"""Present only the documents and parent titles the caller is allowed to see."""
from collections import OrderedDict
from .access import visible_bundles,can_manage


def file_groups(user,documents):
    documents=list(documents.select_related('current_version','bundle','created_by'))
    bundles={b.pk:b for b in visible_bundles(user).filter(pk__in={d.bundle_id for d in documents if d.bundle_id})}
    grouped=OrderedDict()
    for doc in documents:
        bundle=bundles.get(doc.bundle_id)
        key=('bundle',bundle.pk) if bundle else ('document',doc.pk)
        if key not in grouped:grouped[key]={'bundle':bundle,'files':[],'unread':False}
        grouped[key]['files'].append({'document':doc,'manage':can_manage(user,doc)})
        grouped[key]['unread']|=bool(getattr(doc,'is_unread',False) and not doc.archived)
    return list(grouped.values())
