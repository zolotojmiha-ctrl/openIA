from decimal import Decimal,InvalidOperation
from django import forms


class WholeWallField(forms.DecimalField):
    """Accept 10, 10.0 and stored Decimal('10.00'), reject fractional millimetres."""
    def clean(self,value):
        if value not in self.empty_values:
            try:value=Decimal(str(value).replace(',','.')).normalize()
            except (InvalidOperation,ValueError,TypeError):raise forms.ValidationError('Укажите толщину стенки целым числом миллиметров.')
        return super().clean(value)
