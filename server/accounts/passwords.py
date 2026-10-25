"""Six-character temporary passwords, using the same validation as password changes."""
import secrets
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

MIN_PASSWORD_LENGTH=6

def temporary_password():
    alphabet='abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    while True:
        value=''.join(secrets.choice(alphabet) for _ in range(MIN_PASSWORD_LENGTH))
        try:validate_password(value)
        except ValidationError:continue
        return value
