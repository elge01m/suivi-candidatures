"""Test-only process entry: production settings, isolated DB, no external sockets."""
import base64
from datetime import timedelta
import ipaddress
import os
from pathlib import Path
import socket
import sys
from unittest.mock import patch


def check_environment(env):
    if not (env.get('LINUX_HTTPS_FIXTURE') == '1'
            and env.get('DJANGO_SETTINGS_MODULE') == 'config.production_settings'
            and env.get('DJANGO_LOAD_DOTENV') == '0'
            and env.get('DB_HOST') == '127.0.0.1' and env.get('DB_PORT') == '55486'
            and env.get('DB_NAME') == 'suivi_sync_test_linux_https'
            and env.get('DB_USER') == 'sync_test_linux_app'
            and env.get('GEMINI_API_KEY') == 'fiction-never-used'
            and env.get('GOOGLE_CLIENT_SECRET') == 'fiction-never-used'):
        raise RuntimeError('Linux HTTPS fixture environment refused')


def install_network_guard():
    original = socket.socket.connect
    original_ex = socket.socket.connect_ex

    def allowed(address):
        if isinstance(address, str):  # Unix sockets.
            return
        try:
            if ipaddress.ip_address(address[0]).is_loopback:
                return
        except (ValueError, TypeError, IndexError):
            pass
        raise RuntimeError('External socket forbidden in Linux fixture')

    def connect(sock, address):
        allowed(address)
        return original(sock, address)

    def connect_ex(sock, address):
        allowed(address)
        return original_ex(sock, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex


class FakeGmail:
    def __init__(self, connection, deadline):
        pass

    def profile(self):
        return 'linux-fixture@example.invalid'

    def list_page(self, query, token):
        assert '-in:spam -in:trash -in:drafts' in query
        ids = ['linux-fiction-' + str(i) for i in range(12)]
        return (ids[:6], 'second') if not token else (ids[6:], None)

    def message(self, identifier):
        from django.utils import timezone
        return {'id': identifier, 'threadId': identifier,
                'internalDate': str(int((timezone.now()-timedelta(minutes=5)).timestamp()*1000)),
                'labelIds': ['INBOX'], 'payload': {'mimeType': 'text/plain',
                    'headers': [{'name': 'Subject', 'value': 'Linux fiction'}],
                    'body': {'data': base64.urlsafe_b64encode(b'Fiction only.').decode()}}}, []

    def close(self):
        pass


def forbidden_gemini(payload):
    raise AssertionError('Gemini must remain disabled in this fixture')


def main(action):
    check_environment(os.environ)
    install_network_guard()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
    import django
    django.setup()
    from django.conf import settings
    from django.core.management import call_command
    from django.contrib.auth import get_user_model
    from django.utils import timezone
    from gmail_integration.models import GmailConnection, GmailMessage
    from gmail_sync.models import SyncConfiguration, SyncRun
    from email_analysis.models import EmailAnalysis, CandidatureProposal
    from gmail_integration.message_crypto import decrypt_message_data
    from cryptography.fernet import Fernet

    if action == 'web':
        from gunicorn.app.wsgiapp import WSGIApplication
        sys.argv = ['gunicorn', '--config', '../deploy/gunicorn.conf.py', 'config.wsgi:application']
        WSGIApplication().run()
    elif action == 'migrate':
        call_command('migrate', interactive=False, verbosity=0)
        call_command('collectstatic', interactive=False, verbosity=0)
        call_command('check', deploy=True)
    elif action == 'seed':
        assert not get_user_model().objects.exists()
        user = get_user_model().objects.create_user('linux-fiction', password='fiction-only-password')
        conn = GmailConnection.objects.create(utilisateur=user,
            encrypted_tokens=Fernet(settings.GOOGLE_TOKEN_ENCRYPTION_KEY.encode()).encrypt(b'{"fiction":true}'),
            expires_at=timezone.now()+timedelta(days=1))
        SyncConfiguration.objects.create(connexion=conn)
    elif action == 'worker':
        with patch('gmail_integration.import_service.GmailClient', FakeGmail), patch('email_analysis.services.extract', forbidden_gemini):
            call_command('process_sync_jobs', max_seconds=5, max_accounts=1)
    elif action.startswith('verify-'):
        count = 10 if action == 'verify-first' else 12
        assert GmailMessage.objects.count() == count
        assert GmailMessage.objects.values('connexion_id', 'gmail_id').distinct().count() == count
        for message in GmailMessage.objects.all():
            assert bytes(message.encrypted_content).startswith(b'gAAAA')
            assert decrypt_message_data(message.encrypted_content)['text'] == 'Fiction only.'
        assert not EmailAnalysis.objects.exists() and not CandidatureProposal.objects.exists()
        assert not SyncConfiguration.objects.filter(gmail_enabled=True).exists()
        assert not SyncConfiguration.objects.filter(auto_analysis_enabled=True).exists()
        if action == 'verify-final':
            jobs = list(SyncRun.objects.order_by('created_at').select_related('import_job'))
            assert len(jobs) == 2 and all(j.status == 'completed' for j in jobs)
            assert (jobs[0].import_job.imported, jobs[1].import_job.duplicates) == (12, 12)
    else:
        raise ValueError('Unknown fixture action')


if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except Exception as error:
        import traceback
        location=traceback.extract_tb(error.__traceback__)[-1]
        print('ERROR Linux fixture action failed: '+type(error).__name__+' at '+Path(location.filename).name+':'+str(location.lineno)+'; details withheld.', file=sys.stderr)
        sys.exit(1)
