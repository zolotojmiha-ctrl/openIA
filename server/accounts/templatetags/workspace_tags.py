from django import template
from accounts.models import Favorite
register=template.Library()
@register.simple_tag(takes_context=True)
def favorite_state(context,kind,pk):
    user=context['request'].user
    return user.is_authenticated and Favorite.objects.filter(user=user,kind=kind,entity_id=pk).exists()
