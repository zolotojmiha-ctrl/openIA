from cryptography.fernet import Fernet,InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError

def cipher():
    try:return Fernet(settings.ARMADA_MAIL_KEY.encode())
    except (ValueError,AttributeError):raise ValidationError('Почтовый ключ шифрования не настроен на сервере.') from None

def seal(value):return cipher().encrypt(value.encode()).decode()
def reveal(value):
    try:return cipher().decrypt(value.encode()).decode()
    except (InvalidToken,UnicodeError):raise ValidationError('Не удалось открыть почтовые реквизиты. Проверьте ключ шифрования.') from None
