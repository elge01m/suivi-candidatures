"""Subprocess entry point for fictional browser fixtures and bounded real workers.

No HTTP test backdoor is installed. The only provider substitutions live here.
"""
import base64
import json
import os
from pathlib import Path
import socket
import sys
from datetime import timedelta
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.e2e_settings'

# Defense in depth: even a missed provider substitution cannot contact Google.
original_connect = socket.socket.connect
def local_connect(sock, address):
    if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1', 'localhost'):
        raise RuntimeError('External network forbidden in E2E')
    return original_connect(sock, address)
socket.socket.connect = local_connect

import django
django.setup()
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.utils import timezone
from candidatures.models import Candidature, CandidatureChange
from email_analysis.gemini import ModelResult
from email_analysis.models import AnalysisBudget, CandidatureProposal, EmailAnalysis
from email_analysis.schemas import Extraction
from email_analysis.services import analyze_message, proposal_data
from gmail_integration.import_errors import ImportProblem
from gmail_integration.message_crypto import encrypt_message_data, decrypt_message_data
from gmail_integration.models import GmailConnection, GmailImport, GmailMessage
from gmail_sync.models import SyncConfiguration, SyncRun

assert settings.E2E_ISOLATED and connection.settings_dict['NAME'].startswith('suivi_sync_test_e2e_')
CONTROL = Path(os.environ['E2E_TEMP']) / 'provider.json'

def options():
    return json.loads(CONTROL.read_text(encoding='utf-8')) if CONTROL.exists() else {}

def fake_extract(payload):
    return ModelResult(Extraction.model_validate({
        'pertinent': True, 'action': 'changement_etat', 'employeur': 'Fiction E2E',
        'poste': 'Logiciel fictif', 'date_candidature': None, 'etat': 'entrevue',
        'confirmation_reception': None, 'justification': 'Simulation navigateur.',
        'informations_manquantes': ['date_candidature'],
    }), 12, 8)

class FakeGmail:
    def __init__(self, conn, deadline):
        self.conn = conn
    def profile(self):
        if options().get('fail'):
            raise ImportProblem('gmail_unavailable', retryable=True, retry_seconds=1)
        return 'fiction-e2e@example.invalid'
    def list_page(self, query, token):
        assert '-in:spam -in:trash -in:drafts' in query
        ids = [f'{options().get("prefix", "e2e-mail")}-{i}' for i in range(12)]
        return (ids[:6], 'page-2') if not token else (ids[6:], None)
    def message(self, mid):
        text = base64.urlsafe_b64encode(b'Fiction only. No real mail.').decode()
        stamp = options().get('message_time') or timezone.now().isoformat()
        from datetime import datetime
        return {'id': mid, 'threadId': mid, 'internalDate': str(int(datetime.fromisoformat(stamp).timestamp()*1000)),
            'labelIds': ['SENT'] if int(mid.rsplit('-', 1)[1]) % 2 else ['INBOX'],
            'payload': {'mimeType': 'text/plain', 'headers': [{'name':'Subject','value':'Fiction E2E'}],
                        'body': {'data':text}}}, []
    def close(self):
        pass

def user():
    return get_user_model().objects.get(username='browser-fiction')

def main(action):
    if action == 'serve':
        call_command('runserver', '127.0.0.1:18000', use_reloader=False, verbosity=0)
    elif action == 'seed':
        assert not get_user_model().objects.exists(), 'A fresh E2E database is required.'
        account = get_user_model().objects.create_user('browser-fiction', password='fiction-only-test-password')
        get_user_model().objects.create_user('other-fiction', password='fiction-only-test-password')
        from cryptography.fernet import Fernet
        conn = GmailConnection.objects.create(utilisateur=account,
            encrypted_tokens=Fernet(settings.GOOGLE_TOKEN_ENCRYPTION_KEY.encode()).encrypt(b'{"fiction":true}'), expires_at=timezone.now()+timedelta(days=1))
        SyncConfiguration.objects.create(connexion=conn)
        # Two pending simulated proposals, no preselected candidature.
        for i in range(2):
            msg = GmailMessage.objects.create(connexion=conn,gmail_id=f'fixture-{i}',thread_id='fixture',
                date_gmail=timezone.now(),encrypted_content=encrypt_message_data({'subject':'Fiction','text':'Fiction','sent':False}))
            with patch('email_analysis.services._reserve'):
                analyze_message(account, msg.pk, consent=True, provider=fake_extract)
        msg = GmailMessage.objects.create(connexion=conn,gmail_id='failed-analysis-fixture',thread_id='fixture',
            date_gmail=timezone.now(),encrypted_content=encrypt_message_data({'subject':'Fiction retry','text':'Fiction','sent':False}))
        EmailAnalysis.objects.create(message=msg,model_name=settings.GEMINI_MODEL,schema_version='1',status='failed',
            attempts=1,error_code='provider_unavailable',retry_after=timezone.now()-timedelta(seconds=1))
        print(json.dumps({'seeded':True}))
    elif action == 'worker':
        # The actual production management command, one bounded invocation.
        call_command('process_sync_jobs', max_seconds=5, max_accounts=1)
    elif action == 'retry-ready':
        # Virtual elapsed backoff on fictional jobs only, never primary data.
        GmailImport.objects.update(retry_after=timezone.now()-timedelta(seconds=1), lease_until=None)
        SyncRun.objects.update(retry_after=timezone.now()-timedelta(seconds=1))
    elif action == 'schedule-due':
        cfg = SyncConfiguration.objects.get(connexion__utilisateur=user())
        assert cfg.gmail_enabled
        cfg.next_due_at=timezone.now()-timedelta(minutes=1);cfg.save(update_fields=['next_due_at'])
    elif action == 'budget-ready':
        # Simulate an elapsed minute on the disposable quota fixture, retaining daily totals.
        AnalysisBudget.objects.update(next_request_at=timezone.now()-timedelta(seconds=1),
            minute_start=timezone.now()-timedelta(minutes=2),minute_calls=0,minute_tokens=0)
    elif action == 'snapshot':
        cfg = SyncConfiguration.objects.get(connexion__utilisateur=user())
        from cryptography.fernet import Fernet
        encrypted = True
        for msg in GmailMessage.objects.all():
            Fernet(settings.GMAIL_MESSAGE_ENCRYPTION_KEY.encode()).decrypt(bytes(msg.encrypted_content))
            encrypted &= bytes(msg.encrypted_content).startswith(b'gAAAA')
        print(json.dumps({
            'candidates':list(Candidature.objects.filter(utilisateur=user()).values('id','employeur','poste','etat','confirmation_reception','date_candidature','notes')),
            'proposals':[{'id':str(p.pk),'status':p.status,'target':proposal_data(user(),p.pk)['proposition']['cible_id']} for p in CandidatureProposal.objects.filter(utilisateur=user())],
            'history':CandidatureChange.objects.count(), 'messages':GmailMessage.objects.count(),
            'unique_messages':len(set(GmailMessage.objects.values_list('connexion_id','gmail_id'))), 'encrypted':encrypted,
            'runs':list(SyncRun.objects.order_by('created_at').values('status','origin','import_job__processed','import_job__imported','import_job__duplicates')),
            'gmail_enabled':cfg.gmail_enabled,'auto_analysis_enabled':cfg.auto_analysis_enabled,
            'analyses':EmailAnalysis.objects.count(), 'automatic_analyses':EmailAnalysis.objects.filter(origin='automatic').count(),
            'completed_automatic':EmailAnalysis.objects.filter(origin='automatic',status='completed').count(),
        }, default=str))
    else:
        raise ValueError('Unknown fixture action')

with patch('gmail_integration.import_service.GmailClient', FakeGmail), patch('email_analysis.services.extract', fake_extract):
    main(sys.argv[1])
