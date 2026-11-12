"""Additional bare-pipe area beyond an approved axial coating length."""
from decimal import Decimal


def extra_pipe_area(diameter_mm, actual_length_m, norm_length_m):
    if actual_length_m is None:return Decimal(0)
    extra=max(Decimal(0),Decimal(actual_length_m)-Decimal(norm_length_m))
    return extra*Decimal(diameter_mm)/1000*Decimal('3.14')
