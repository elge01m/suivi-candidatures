"""Linux deployment only. Never reads the development .env implicitly."""
import os
from django.core.exceptions import ImproperlyConfigured

if os.environ.get('DJANGO_LOAD_DOTENV') != '0':
    raise ImproperlyConfigured('Production requires DJANGO_LOAD_DOTENV=0 and an external environment.')

from .settings import *  # noqa: F403,E402
from .deployment import production_values  # noqa: E402
from .production_logging import logging_config  # noqa: E402

globals().update(production_values(os.environ))
host = DATABASES['default']['HOST']  # noqa: F405
local = host in ('127.0.0.1', 'localhost', '::1') or host.startswith('/')
sslmode = os.environ.get('DB_SSLMODE', 'disable' if local else 'verify-full')
if not local and (sslmode != 'verify-full' or not os.environ.get('DB_SSLROOTCERT')):
    raise ImproperlyConfigured('Remote PostgreSQL requires verify-full and DB_SSLROOTCERT.')
DATABASES['default']['OPTIONS']['sslmode'] = sslmode  # noqa: F405
if os.environ.get('DB_SSLROOTCERT'):
    DATABASES['default']['OPTIONS']['sslrootcert'] = os.environ['DB_SSLROOTCERT']  # noqa: F405
DATABASES['default']['CONN_MAX_AGE'] = 60  # noqa: F405
DATABASES['default']['CONN_HEALTH_CHECKS'] = True  # noqa: F405
LOGGING = logging_config()
