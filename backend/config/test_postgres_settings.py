"""Explicit opt-in, disposable PostgreSQL cluster only. Never uses the application DB."""
import os

from .settings import *  # noqa: F403

host = os.environ.get('SYNC_TEST_DB_HOST', '')
port = os.environ.get('SYNC_TEST_DB_PORT', '')
name = os.environ.get('SYNC_TEST_DB_NAME', '')
user = os.environ.get('SYNC_TEST_DB_USER', '')
if host != '127.0.0.1' or not port.isdecimal() or int(port) < 1024 or port == os.environ.get('DB_PORT', '5432') or not name.startswith('suivi_sync_test_') or not user.startswith('sync_test_'):
    raise RuntimeError('A separate local disposable test cluster must be configured explicitly.')
DATABASES = {'default': {'ENGINE':'django.db.backends.postgresql', 'NAME':name, 'USER':user,
    'PASSWORD':os.environ.get('SYNC_TEST_DB_PASSWORD', ''), 'HOST':host, 'PORT':port, 'OPTIONS':{'connect_timeout':5}, 'TEST':{'NAME':'test_' + name}}}
