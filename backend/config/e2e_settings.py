"""Real browser / real PostgreSQL, fictional providers only. Never production."""
import base64

from .test_postgres_settings import *  # noqa: F403

if not name.startswith('suivi_sync_test_e2e_'):  # noqa: F405
    raise RuntimeError('E2E requires its own explicitly named disposable database.')
E2E_ISOLATED = True
SECRET_KEY = 'public-fictional-e2e-key-not-for-production'
DEBUG = False
ALLOWED_HOSTS = ['127.0.0.1', 'localhost']
CSRF_TRUSTED_ORIGINS = ['http://127.0.0.1:15173']
GOOGLE_TOKEN_ENCRYPTION_KEY = base64.urlsafe_b64encode(b't' * 32).decode()
GMAIL_MESSAGE_ENCRYPTION_KEY = base64.urlsafe_b64encode(b'm' * 32).decode()
GEMINI_API_KEY = 'fictional-never-used'
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
