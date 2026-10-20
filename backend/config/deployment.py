"""Pure validation for production configuration. Errors never contain values."""
import re
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured


def required(env, key):
    value = env.get(key, '')
    if not value or value != value.strip() or any(ord(c) < 32 for c in value):
        raise ImproperlyConfigured(f'{key}: missing or invalid configuration.')
    return value


def https_origin(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment or '\\' in value
                or any(c.isspace() for c in value) or parsed.port == 0):
            raise ValueError
        return value.rstrip('/')
    except (ValueError, TypeError):
        raise ImproperlyConfigured('Invalid HTTPS origin.') from None


def oauth_return_url(value, *, production=False, origin=''):
    try:
        if value == '/':
            return value
        parsed = urlsplit(value)
        if (not isinstance(value, str) or '\\' in value or any(c.isspace() for c in value)
                or parsed.username or parsed.password or not parsed.hostname or parsed.query or parsed.fragment
                or parsed.path not in ('', '/') or parsed.port == 0):
            raise ValueError
        if production:
            if https_origin(value) != origin:
                raise ValueError
        elif parsed.scheme != 'https' and not (
                parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1')):
            raise ValueError
        return value
    except (ValueError, TypeError, ImproperlyConfigured):
        raise ImproperlyConfigured('Invalid OAuth return destination.') from None


def production_values(env):
    secret = required(env, 'DJANGO_SECRET_KEY')
    if len(secret) < 50 or len(set(secret)) < 5 or secret.startswith('django-insecure-'):
        raise ImproperlyConfigured('DJANGO_SECRET_KEY: a strong external key is required.')
    origin = https_origin(required(env, 'APP_ORIGIN'))
    hosts = required(env, 'DJANGO_ALLOWED_HOSTS').split(',')
    if any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host)
           or host.startswith('.') or '..' in host for host in hosts):
        raise ImproperlyConfigured('DJANGO_ALLOWED_HOSTS: exact hosts required.')
    if urlsplit(origin).hostname not in hosts:
        raise ImproperlyConfigured('APP_ORIGIN must belong to DJANGO_ALLOWED_HOSTS.')
    csrf = env.get('DJANGO_CSRF_TRUSTED_ORIGINS', origin).split(',')
    if any(https_origin(item) != item or urlsplit(item).hostname not in hosts for item in csrf) or origin not in csrf:
        raise ImproperlyConfigured('Invalid trusted CSRF origins.')
    callback = required(env, 'GOOGLE_REDIRECT_URI')
    if callback != origin + '/api/gmail/callback/':
        raise ImproperlyConfigured('GOOGLE_REDIRECT_URI must match APP_ORIGIN and the callback route.')
    destination = oauth_return_url(env.get('OAUTH_SUCCESS_REDIRECT_URL', origin + '/'), production=True, origin=origin)
    keys = [required(env, key) for key in ('GOOGLE_TOKEN_ENCRYPTION_KEY', 'GMAIL_MESSAGE_ENCRYPTION_KEY')]
    try:
        for key in keys:
            Fernet(key.encode('ascii'))
    except (ValueError, UnicodeError):
        raise ImproperlyConfigured('Invalid Fernet key configuration.') from None
    if keys[0] == keys[1] or secret in keys:
        raise ImproperlyConfigured('Encryption and Django keys must be independent.')
    static = required(env, 'DJANGO_STATIC_ROOT')
    if not Path(static).is_absolute():
        raise ImproperlyConfigured('DJANGO_STATIC_ROOT must be absolute.')
    return dict(
        PRODUCTION=True, DEBUG=False, SECRET_KEY=secret, APP_ORIGIN=origin,
        ALLOWED_HOSTS=hosts, CSRF_TRUSTED_ORIGINS=csrf, GOOGLE_REDIRECT_URI=callback,
        OAUTH_SUCCESS_REDIRECT_URL=destination, STATIC_URL='/static/', STATIC_ROOT=Path(static),
        SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
        CSRF_COOKIE_HTTPONLY=False, SESSION_COOKIE_SAMESITE='Lax', CSRF_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_DOMAIN=None, CSRF_COOKIE_DOMAIN=None, SESSION_COOKIE_PATH='/', CSRF_COOKIE_PATH='/',
        SECURE_SSL_REDIRECT=True, SECURE_SSL_HOST=urlsplit(origin).netloc,
        SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO', 'https'), USE_X_FORWARDED_HOST=False,
        SECURE_HSTS_SECONDS=3600, SECURE_HSTS_INCLUDE_SUBDOMAINS=False, SECURE_HSTS_PRELOAD=False,
        SECURE_CONTENT_TYPE_NOSNIFF=True, SECURE_REFERRER_POLICY='no-referrer',
        X_FRAME_OPTIONS='DENY',
    )
