import json

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.views.decorators.debug import sensitive_variables

from .import_errors import ImportProblem


@sensitive_variables()
def message_cipher():
    try:
        key = settings.GMAIL_MESSAGE_ENCRYPTION_KEY
        if not key or key == settings.GOOGLE_TOKEN_ENCRYPTION_KEY:
            raise ValueError
        return Fernet(key.encode('ascii'))
    except (ValueError, TypeError, UnicodeError):
        raise ImportProblem('configuration_missing') from None


@sensitive_variables()
def encrypt_message_data(data):
    return message_cipher().encrypt(json.dumps(data, ensure_ascii=False).encode('utf-8'))


@sensitive_variables()
def decrypt_message_data(encrypted):
    try:
        return json.loads(message_cipher().decrypt(bytes(encrypted)))
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        raise ImportProblem('content_unreadable') from None
