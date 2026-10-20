"""Validate production settings using PUBLIC FICTIONAL values, no DB/provider calls."""
import base64
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'backend'))
temporary=Path(tempfile.mkdtemp(prefix='suivi-security-',dir=os.environ.get('E2E_TEMP') or os.environ.get('RUNNER_TEMP')))
os.environ.update({
    'DJANGO_SETTINGS_MODULE':'config.production_settings','DJANGO_LOAD_DOTENV':'0',
    'DJANGO_SECRET_KEY':'Public-fiction-only-configuration-check-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ',
    'APP_ORIGIN':'https://candidatures.example.invalid','DJANGO_ALLOWED_HOSTS':'candidatures.example.invalid',
    'DJANGO_CSRF_TRUSTED_ORIGINS':'https://candidatures.example.invalid',
    'DJANGO_STATIC_ROOT':str(temporary/'staticfiles'),
    'GOOGLE_REDIRECT_URI':'https://candidatures.example.invalid/api/gmail/callback/',
    'OAUTH_SUCCESS_REDIRECT_URL':'https://candidatures.example.invalid/',
    'GOOGLE_CLIENT_ID':'fiction','GOOGLE_CLIENT_SECRET':'fiction','GEMINI_API_KEY':'fiction',
    'GOOGLE_TOKEN_ENCRYPTION_KEY':base64.urlsafe_b64encode(b't'*32).decode(),
    'GMAIL_MESSAGE_ENCRYPTION_KEY':base64.urlsafe_b64encode(b'm'*32).decode(),
    'DB_NAME':'suivi_sync_test_security','DB_HOST':'127.0.0.1','DB_PORT':'55484',
    'DB_USER':'sync_test_security','DB_PASSWORD':'fiction','DB_SSLMODE':'disable',
})
import django
django.setup()
from django.core.management import call_command
from django.core.checks import run_checks
call_command('check',deploy=True)
issues=run_checks(include_deployment_checks=True)
expected={'security.W005','security.W021'}
if {issue.id for issue in issues}!=expected:
    raise SystemExit('Unexpected deployment checks; review without silencing warnings.')
call_command('collectstatic',interactive=False,verbosity=0)
print('OK production check; only expected HSTS subdomain/preload warnings. OK collectstatic.')
