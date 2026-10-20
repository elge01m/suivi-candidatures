"""Isolated, in-memory Django test database; no PostgreSQL CREATEDB required.

Run with: python manage.py test candidatures --settings=config.test_settings
These tests cover API behavior, not PostgreSQL-specific database behavior.
"""

from .settings import *  # noqa: F403


DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    },
}
