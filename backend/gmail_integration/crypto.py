import json

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.views.decorators.debug import sensitive_variables


class OAuthConfigurationError(Exception):
    pass


class OAuthFailure(Exception):
    pass


@sensitive_variables()
def cipher():
    try:
        key = settings.GOOGLE_TOKEN_ENCRYPTION_KEY
        if not key:
            raise ValueError
        return Fernet(key.encode('ascii'))
    except (ValueError, TypeError, UnicodeError):
        raise OAuthConfigurationError from None


@sensitive_variables()
def encrypt_tokens(access_token, refresh_token):
    if not isinstance(access_token, str) or not access_token or not isinstance(refresh_token, str) or not refresh_token:
        raise OAuthFailure
    payload = json.dumps({'access_token': access_token, 'refresh_token': refresh_token}).encode('utf-8')
    return cipher().encrypt(payload)


@sensitive_variables()
def decrypt_tokens(encrypted):
    try:
        payload = json.loads(cipher().decrypt(bytes(encrypted)))
        if not all(isinstance(payload.get(name), str) and payload[name] for name in ('access_token', 'refresh_token')):
            raise ValueError
        return payload
    except (InvalidToken, ValueError, TypeError, AttributeError):
        raise OAuthFailure from None
