import io
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from candidatures.models import Candidature
from email_analysis.gemini import ModelResult
from email_analysis.models import AnalysisBudget, CandidatureProposal, EmailAnalysis
from email_analysis.schemas import Extraction
from gmail_integration.import_errors import ImportProblem
from gmail_integration.import_service import prepare_import_window, purge_messages
from gmail_integration.message_crypto import encrypt_message_data
from gmail_integration.models import GmailConnection, GmailImport, GmailMessage
from .errors import SyncProblem
from .models import SyncConfiguration, SyncRun
from .schedule import next_slot, validate_timezone
from .services import (claim_connection, configuration_data, configure, process_connection,
                       release_connection, request_initial_import, request_sync, reset_connection, import_retry_allowed, retry_import)

KEY, TOKEN_KEY = Fernet.generate_key().decode(), Fernet.generate_key().decode()


class FakeGmail:
    calls = []
    fail = None
    pages = {}
    messages = {}

    def __init__(self, connection, deadline):
        self.user_id = connection.utilisateur_id

    def profile(self):
        if self.fail:
            raise self.fail
        return f'fiction-{self.user_id}@example.invalid'

    def list_page(self, query, token):
        self.calls.append((query, token))
        return self.pages.get(token, ([], None))

    def message(self, message_id):
        return self.messages[message_id], []

    def close(self):
        pass


@override_settings(GMAIL_MESSAGE_ENCRYPTION_KEY=KEY, GOOGLE_TOKEN_ENCRYPTION_KEY=TOKEN_KEY, GEMINI_API_KEY='fictitious',
    GEMINI_DAILY_REQUEST_LIMIT=20, GEMINI_DAILY_TOKEN_LIMIT=100000, GEMINI_REQUESTS_PER_MINUTE=1, GEMINI_TOKENS_PER_MINUTE=30000)
class SyncBase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='sync-fiction-a')
        self.other = get_user_model().objects.create_user(username='sync-fiction-b')
        self.conn = GmailConnection.objects.create(utilisateur=self.user, encrypted_tokens=b'fictitious', expires_at=timezone.now())
        self.other_conn = GmailConnection.objects.create(utilisateur=self.other, encrypted_tokens=b'fictitious', expires_at=timezone.now())
        for target in ('requests.Session.request', 'httpx.Client.request'):
            blocker = patch(target, side_effect=AssertionError('Real external calls forbidden'))
            blocker.start(); self.addCleanup(blocker.stop)
        FakeGmail.calls, FakeGmail.fail, FakeGmail.pages, FakeGmail.messages = [], None, {}, {}

    def configure(self, **data):
        current = configuration_data(self.user)
        return configure(self.user, {'revision':current['revision'], **data})

    def message(self, connection=None, **changes):
        return GmailMessage.objects.create(connexion=connection or self.conn, gmail_id='fiction-' + str(GmailMessage.objects.count()),
            thread_id='fiction', date_gmail=changes.get('date_gmail', timezone.now()),
            encrypted_content=encrypt_message_data({'subject':'Fiction', 'text':'Message fictif sans informations personnelles.'}))

    def provider(self):
        return Mock(return_value=ModelResult(Extraction.model_validate_json(json.dumps({
            'pertinent':True, 'action':'creation', 'employeur':'Entreprise fictive', 'poste':'Test', 'date_candidature':None,
            'etat':None, 'justification':'Fiction uniquement.', 'informations_manquantes':['date_candidature','etat'],
        })), 20, 30))

    def problem(self, code, function, *args, **kwargs):
        with self.assertRaises(SyncProblem) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)


class ConfigurationTests(SyncBase):
    def test_get_is_read_only_and_defaults_disabled(self):
        data = configuration_data(self.user)
        self.assertFalse(data['gmail_enabled'])
        self.assertFalse(data['auto_analysis_enabled'])
        self.assertEqual(data['timezone'], 'America/Toronto')
        self.assertFalse(SyncConfiguration.objects.exists())

    def test_explicit_consent_required_and_revocable(self):
        self.problem('consent_required', self.configure, auto_analysis_enabled=True)
        cfg = self.configure(auto_analysis_enabled=True, consent=True)
        self.assertIsNotNone(cfg.consent_at)
        previous = cfg.consent_version
        cfg = self.configure(auto_analysis_enabled=False)
        self.assertIsNone(cfg.consent_at)
        self.assertNotEqual(cfg.consent_version, previous)

    def test_settings_validation_revision_and_ownership(self):
        for data in [{'time_1':'25:00'}, {'time_1':'09:00','time_2':'09:00'}, {'timezone':'wrong'},
            {'gmail_enabled':'true'}, {'auto_analysis_enabled':1}, {'utilisateur':self.other.pk}]:
            self.problem('invalid_request', self.configure, **data)
        cfg = self.configure(gmail_enabled=True)
        self.assertIsNotNone(cfg.next_due_at)
        self.problem('stale_revision', configure, self.user, {'revision':1,'gmail_enabled':False})
        self.assertFalse(SyncConfiguration.objects.filter(connexion=self.other_conn).exists())

    def test_five_minute_slots_are_strictly_next_and_dst_independent(self):
        validate_timezone('America/Toronto')
        for now in (datetime(2026,10,12,12,tzinfo=dt_timezone.utc),
                    datetime(2026,11,1,5,59,59,tzinfo=dt_timezone.utc),
                    datetime(2026,3,8,6,59,59,tzinfo=dt_timezone.utc)):
            slot = next_slot(now)
            self.assertGreater(slot, now)
            self.assertLessEqual(slot-now, timedelta(minutes=5))
            self.assertEqual(slot.minute % 5, 0)
            self.assertEqual(slot.second, 0)
        self.assertEqual(next_slot(datetime(2026,10,12,12,tzinfo=dt_timezone.utc)).minute, 5)
        self.assertEqual(configuration_data(self.user)['interval_minutes'], 5)

    def test_manual_enqueue_has_no_network_and_is_idempotent(self):
        first, second = request_sync(self.user), request_sync(self.user)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SyncRun.objects.count(), 1)
        self.assertEqual(GmailImport.objects.count(), 1)
        self.assertEqual(first.status, 'queued')
        self.assertFalse(first.configuration.auto_analysis_enabled)

    def test_schedule_enqueues_each_five_minute_slot_and_preserves_legacy_times(self):
        now = datetime(2026,10,19,12,0,tzinfo=dt_timezone.utc)
        with patch('gmail_sync.services.timezone.now', return_value=now):
            cfg = self.configure(gmail_enabled=True)
        self.assertEqual(cfg.next_due_at, now+timedelta(minutes=5))
        legacy = (cfg.time_1, cfg.time_2)
        with patch('gmail_sync.services.timezone.now', return_value=now+timedelta(minutes=4, seconds=59)):
            lease = claim_connection(cfg.pk)
            release_connection(cfg.pk, lease)
        self.assertFalse(SyncRun.objects.exists())
        for minutes in (5,10):
            with patch('gmail_sync.services.timezone.now', return_value=now+timedelta(minutes=minutes)):
                lease = claim_connection(cfg.pk)
                release_connection(cfg.pk, lease)
            run = SyncRun.objects.get(scheduled_for=now+timedelta(minutes=minutes))
            self.assertEqual(run.origin, 'scheduled')
            SyncRun.objects.filter(pk=run.pk).update(status='completed')
        cfg.refresh_from_db()
        self.assertEqual(cfg.next_due_at, now+timedelta(minutes=15))
        self.assertEqual((cfg.time_1, cfg.time_2), legacy)
        self.assertFalse(cfg.auto_analysis_enabled)
        self.assertEqual(FakeGmail.calls, [])

    def test_due_schedule_catches_up_once_and_cursor_not_advanced(self):
        cfg = self.configure(gmail_enabled=True)
        old_due = timezone.now() - timedelta(days=2)
        SyncConfiguration.objects.filter(pk=cfg.pk).update(next_due_at=old_due)
        lease = claim_connection(cfg.pk)
        release_connection(cfg.pk, lease)
        cfg.refresh_from_db()
        self.assertGreater(cfg.next_due_at, timezone.now())
        self.assertIsNone(cfg.last_success_at)
        self.assertEqual(cfg.runs.count(), 1)
        lease = claim_connection(cfg.pk)
        release_connection(cfg.pk, lease)
        self.assertEqual(cfg.runs.count(), 1)

    def test_legacy_daily_deadline_is_normalized_only_for_enabled_schedules(self):
        now = datetime(2026,10,19,12,1,tzinfo=dt_timezone.utc)
        cfg = self.configure()
        old_due = now+timedelta(hours=6)
        SyncConfiguration.objects.filter(pk=cfg.pk).update(next_due_at=old_due)
        with patch('gmail_sync.services.timezone.now', return_value=now):
            lease = claim_connection(cfg.pk)
            release_connection(cfg.pk, lease)
        cfg.refresh_from_db()
        self.assertEqual(cfg.next_due_at, old_due)
        SyncConfiguration.objects.filter(pk=cfg.pk).update(gmail_enabled=True)
        with patch('gmail_sync.services.timezone.now', return_value=now):
            lease = claim_connection(cfg.pk)
            release_connection(cfg.pk, lease)
        cfg.refresh_from_db()
        self.assertEqual(cfg.next_due_at, now.replace(minute=5))
        self.assertFalse(SyncRun.objects.exists())

    def test_claim_busy_and_stale_lease_recovery(self):
        cfg = self.configure()
        first = claim_connection(cfg.pk)
        self.assertIsNone(claim_connection(cfg.pk))
        SyncConfiguration.objects.filter(pk=cfg.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        second = claim_connection(cfg.pk)
        self.assertNotEqual(first, second)
        release_connection(cfg.pk, first)
        cfg.refresh_from_db()
        self.assertEqual(cfg.lease, second)
        release_connection(cfg.pk, second)

    def test_constraints_prevent_duplicate_open_and_scheduled_jobs(self):
        run = request_sync(self.user)
        job = prepare_import_window(self.user, timezone.now()-timedelta(days=1), timezone.now())
        with self.assertRaises(IntegrityError), transaction.atomic():
            SyncRun.objects.create(configuration=run.configuration, import_job=job, origin='manual',
                generation=run.generation, credential_version=run.credential_version)


class InitialImportTests(SyncBase):
    period = {'date_debut':'2026-10-12', 'date_fin':'2026-10-12', 'fuseau':'America/Toronto'}

    def test_enqueue_is_persistent_idempotent_and_never_enables_automation(self):
        first = request_initial_import(self.user, self.period)
        second = request_initial_import(self.user, self.period)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SyncRun.objects.count(), 1)
        self.assertEqual(first.origin, 'initial')
        self.assertEqual(first.import_job.range_start.isoformat(), '2026-10-12T04:00:00+00:00')
        self.assertEqual(first.import_job.range_end.isoformat(), '2026-10-13T04:00:00+00:00')
        self.assertFalse(first.configuration.gmail_enabled)
        self.assertFalse(first.configuration.auto_analysis_enabled)
        self.assertEqual(FakeGmail.calls, [])
        data = configuration_data(self.user)
        self.assertEqual(data['latest_initial_import']['id'], str(first.pk))
        self.assertEqual(data['latest_initial_import']['date_debut'], '2026-10-12')
        self.assertIsNone(configuration_data(self.other)['latest_initial_import'])

    def test_invalid_period_unknown_fields_and_busy_queue_rejected_without_writes(self):
        for changes in ({'date_debut':'2026-02-30'}, {'date_fin':'2026-10-11'}, {'date_fin':'9999-12-31'}, {'fuseau':'invalid'}):
            with self.assertRaises(SyncProblem):
                request_initial_import(self.user, self.period | changes)
        self.problem('invalid_request', request_initial_import, self.user, self.period | {'utilisateur':self.other.pk})
        self.assertFalse(SyncRun.objects.exists())
        self.assertFalse(SyncConfiguration.objects.exists())
        request_sync(self.user)
        self.problem('busy', request_initial_import, self.user, self.period)
        self.assertEqual(SyncRun.objects.count(), 1)

    def test_worker_resumes_all_pages_in_tens_encrypts_and_deduplicates(self):
        import base64
        from gmail_integration.message_crypto import decrypt_message_data
        run = request_initial_import(self.user, self.period)
        when = run.import_job.range_start + timedelta(hours=12)
        ids = [f'initial-fiction-{i}' for i in range(12)]
        FakeGmail.pages = {None:(ids[:11], 'next-page'), 'next-page':(ids[11:], None)}
        FakeGmail.messages = {identifier:{'id':identifier, 'threadId':'fiction', 'internalDate':str(int(when.timestamp()*1000)),
            'labelIds':['SENT'] if i % 2 else [], 'payload':{'mimeType':'text/plain', 'headers':[],
                'body':{'data':base64.urlsafe_b64encode(b'Fictitious initial import').decode()}}} for i,identifier in enumerate(ids)}
        provider = self.provider()
        process_connection(run.configuration_id, client_factory=FakeGmail, provider=provider)
        run.refresh_from_db()
        self.assertEqual(run.status, 'queued')
        self.assertEqual(GmailMessage.objects.count(), 10)
        # A new worker invocation recovers the persisted cursor without browser state.
        process_connection(run.configuration_id, client_factory=FakeGmail, provider=provider)
        run.refresh_from_db(); run.import_job.refresh_from_db(); run.configuration.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.import_job.imported, 12)
        self.assertIsNone(run.configuration.last_success_at)
        self.assertIsNone(run.configuration.started_at)
        for message in GmailMessage.objects.all():
            self.assertEqual(message.connexion_id, self.conn.pk)
            self.assertNotIn(b'Fictitious', bytes(message.encrypted_content))
            self.assertEqual(decrypt_message_data(message.encrypted_content)['text'], 'Fictitious initial import')
        repeated = request_initial_import(self.user, self.period)
        for _ in range(2):
            process_connection(run.configuration_id, client_factory=FakeGmail, provider=provider)
        repeated.import_job.refresh_from_db()
        self.assertEqual(repeated.import_job.duplicates, 12)
        self.assertEqual(repeated.import_job.imported, 0)
        self.assertEqual(GmailMessage.objects.count(), 12)
        provider.assert_not_called()
        self.assertFalse(EmailAnalysis.objects.exists())
        self.assertFalse(Candidature.objects.exists())

    def test_initial_import_pause_retry_and_mailbox_control_are_preserved(self):
        from gmail_integration.message_crypto import encrypt_message_data
        run = request_initial_import(self.user, self.period)
        FakeGmail.fail = ImportProblem('rate_limited', retryable=True, retry_seconds=120)
        process_connection(run.configuration_id, client_factory=FakeGmail)
        run.refresh_from_db()
        self.assertEqual(run.status, 'paused')
        self.assertIsNotNone(run.retry_after)
        FakeGmail.fail = None
        self.assertFalse(process_connection(run.configuration_id, client_factory=FakeGmail))
        SyncRun.objects.filter(pk=run.pk).update(retry_after=timezone.now()-timedelta(seconds=1))
        GmailImport.objects.filter(pk=run.import_job_id).update(retry_after=timezone.now()-timedelta(seconds=1))
        GmailConnection.objects.filter(pk=self.conn.pk).update(encrypted_mailbox_identity=encrypt_message_data({'email':'different@example.invalid'}))
        process_connection(run.configuration_id, client_factory=FakeGmail)
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.error_code, 'mailbox_changed')
        self.assertFalse(GmailMessage.objects.exists())

    def test_date_period_respects_23_and_25_hour_dst_days(self):
        for day, hours in (('2026-03-08',23), ('2026-11-01',25)):
            run = request_initial_import(self.user, self.period | {'date_debut':day, 'date_fin':day})
            self.assertEqual(run.import_job.range_end-run.import_job.range_start, timedelta(hours=hours))
            SyncRun.objects.filter(pk=run.pk).update(status='completed')

    def test_reconnection_cancels_initial_import_before_any_provider_call(self):
        import uuid
        run = request_initial_import(self.user, self.period)
        GmailConnection.objects.filter(pk=self.conn.pk).update(credential_version=uuid.uuid4())
        process_connection(run.configuration_id, client_factory=FakeGmail)
        run.refresh_from_db()
        self.assertEqual(run.status, 'cancelled')
        self.assertEqual(FakeGmail.calls, [])
        self.assertFalse(GmailMessage.objects.exists())

    def test_api_202_status_reload_authentication_csrf_and_isolation(self):
        client = Client(enforce_csrf_checks=True)
        url = '/api/sync/imports/'
        self.assertIn(client.post(url, json.dumps(self.period), content_type='application/json').status_code, (401,403))
        client.force_login(self.user)
        self.assertEqual(client.post(url, json.dumps(self.period), content_type='application/json').status_code, 403)
        client.get('/api/auth/csrf/')
        headers = {'HTTP_X_CSRFTOKEN':client.cookies['csrftoken'].value}
        response = client.post(url, json.dumps(self.period), content_type='application/json', **headers)
        self.assertEqual(response.status_code, 202)
        data = response.json()
        self.assertEqual(data['origin'], 'initial')
        self.assertEqual(data['processed'], 0)
        self.assertNotIn('encrypted_content', data)
        self.assertEqual(client.get('/api/sync/configuration/').json()['latest_initial_import']['id'], data['id'])
        status_url = f"/api/sync/runs/{data['id']}/"
        self.assertEqual(client.get(status_url).status_code, 200)
        client.force_login(self.other)
        self.assertEqual(client.get(status_url).status_code, 404)


class RetryTests(SyncBase):
    def failed(self):
        run=request_sync(self.user)
        SyncRun.objects.filter(pk=run.pk).update(status='failed',error_code='gmail_unavailable')
        GmailImport.objects.filter(pk=run.import_job_id).update(status='failed',error_code='gmail_unavailable',retry_count=1,
            processed=3,imported=2,duplicates=1,encrypted_cursor=b'fictitious-cursor')
        run.refresh_from_db();run.import_job.refresh_from_db()
        return run

    def test_retry_queues_without_network_and_preserves_counters_cursor_and_limits(self):
        run=self.failed()
        self.assertTrue(import_retry_allowed(run.import_job))
        self.problem('invalid_request',retry_import,self.user,run.import_job_id)
        result=retry_import(self.user,run.import_job_id,confirm=True)
        self.assertEqual(result.pk,run.pk)
        self.assertEqual(result.status,'queued')
        job=GmailImport.objects.get(pk=run.import_job_id)
        self.assertEqual((job.processed,job.imported,job.duplicates,job.retry_count),(3,2,1,1))
        self.assertEqual(bytes(job.encrypted_cursor),b'fictitious-cursor')
        self.assertFalse(result.configuration.gmail_enabled)
        self.assertFalse(result.configuration.auto_analysis_enabled)
        self.assertEqual(FakeGmail.calls,[])
        self.problem('retry_unavailable',retry_import,self.user,job.pk,confirm=True)

    def test_retry_refuses_future_delay_exhaustion_and_mailbox_errors(self):
        run=self.failed()
        SyncRun.objects.filter(pk=run.pk).update(retry_after=timezone.now()+timedelta(minutes=5))
        self.problem('retry_unavailable',retry_import,self.user,run.import_job_id,confirm=True)
        SyncRun.objects.filter(pk=run.pk).update(retry_after=None)
        for changes in ({'retry_after':timezone.now()+timedelta(minutes=5)}, {'retry_count':5}, {'error_code':'mailbox_changed'},
                        {'error_code':'reauthorization_required'}, {'error_code':'invalid_page'}):
            GmailImport.objects.filter(pk=run.import_job_id).update(retry_after=None,retry_count=1,error_code='gmail_unavailable')
            GmailImport.objects.filter(pk=run.import_job_id).update(**changes)
            self.problem('retry_unavailable',retry_import,self.user,run.import_job_id,confirm=True)
        self.assertEqual(FakeGmail.calls,[])

    def test_retry_api_is_user_scoped_csrf_protected_and_returns_202(self):
        run=self.failed()
        client=Client(enforce_csrf_checks=True)
        url=f'/api/sync/imports/{run.import_job_id}/retry/'
        client.force_login(self.user)
        body=json.dumps({'confirmer':True})
        self.assertEqual(client.post(url,body,content_type='application/json').status_code,403)
        client.get('/api/auth/csrf/')
        headers={'HTTP_X_CSRFTOKEN':client.cookies['csrftoken'].value}
        client.force_login(self.other)
        self.assertEqual(client.post(url,body,content_type='application/json',**headers).status_code,404)
        client.force_login(self.user)
        self.assertEqual(client.post(url,body,content_type='application/json',**headers).status_code,202)
        self.assertEqual(FakeGmail.calls,[])


class WorkerTests(SyncBase):
    def test_import_resumes_ten_at_a_time_and_handles_all_pages(self):
        run = request_sync(self.user)
        when = run.import_job.range_end - timedelta(minutes=1)
        ids = [f'fiction{i}' for i in range(12)]
        FakeGmail.pages = {None:(ids[:11], 'next'), 'next':(ids[11:], None)}
        import base64
        FakeGmail.messages = {identifier:{'id':identifier,'threadId':'fiction', 'internalDate':str(int(when.timestamp()*1000)),
            'labelIds':['SENT'] if i % 2 else [], 'payload':{'mimeType':'text/plain', 'headers':[],
                'body':{'data':base64.urlsafe_b64encode(b'Fiction only').decode()}}} for i,identifier in enumerate(ids)}
        self.assertTrue(process_connection(run.configuration_id, client_factory=FakeGmail))
        run.refresh_from_db()
        self.assertEqual(run.status, 'queued')
        self.assertEqual(GmailMessage.objects.count(), 10)
        self.assertIsNone(run.configuration.last_success_at)
        self.assertTrue(process_connection(run.configuration_id, client_factory=FakeGmail))
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(GmailMessage.objects.count(), 12)
        run.configuration.refresh_from_db()
        self.assertEqual(run.configuration.last_success_at, run.import_job.range_end)
        self.assertIn('-in:spam -in:trash -in:drafts', FakeGmail.calls[0][0])
        self.assertNotIn('in:inbox', FakeGmail.calls[0][0])
        self.assertFalse(EmailAnalysis.objects.exists())

        repeated = request_sync(self.user)
        process_connection(repeated.configuration_id, client_factory=FakeGmail)
        process_connection(repeated.configuration_id, client_factory=FakeGmail)
        repeated.refresh_from_db()
        self.assertEqual(repeated.status, 'completed')
        self.assertEqual(repeated.import_job.duplicates, 12)
        self.assertEqual(GmailMessage.objects.count(), 12)

    def test_network_pause_and_retry_after_respected(self):
        run = request_sync(self.user)
        FakeGmail.fail = ImportProblem('rate_limited', retryable=True, retry_seconds=120)
        process_connection(run.configuration_id, client_factory=FakeGmail)
        run.refresh_from_db()
        self.assertEqual(run.status, 'paused')
        self.assertGreater(run.retry_after, timezone.now())
        with patch('gmail_sync.services.continue_import') as continued:
            process_connection(run.configuration_id, client_factory=FakeGmail)
        continued.assert_not_called()
        self.assertIsNone(run.configuration.last_success_at)

    def test_oauth_invalid_stops_scheduled_work(self):
        run = request_sync(self.user)
        FakeGmail.fail = ImportProblem('reauthorization_required')
        process_connection(run.configuration_id, client_factory=FakeGmail)
        cfg = SyncConfiguration.objects.get(pk=run.configuration_id)
        self.assertEqual(cfg.blocked_code, 'reauthorization_required')
        self.problem('sync_blocked', request_sync, self.user)

    def test_old_imported_and_backdated_messages_not_automatically_analyzed(self):
        old = self.message()
        cfg = self.configure(auto_analysis_enabled=True, consent=True)
        backdated = self.message(date_gmail=cfg.consent_at-timedelta(days=1))
        new = self.message()
        self.message(self.other_conn)
        provider = self.provider()
        process_connection(cfg.pk, provider=provider)
        self.assertEqual(provider.call_count, 1)
        self.assertFalse(EmailAnalysis.objects.filter(message__in=[old,backdated]).exists())
        analysis = EmailAnalysis.objects.get(message=new)
        self.assertEqual(analysis.status, 'completed')
        self.assertEqual(analysis.origin, 'automatic')
        proposal = CandidatureProposal.objects.get(analysis=analysis)
        self.assertEqual(proposal.status, 'en_attente')
        self.assertIsNone(proposal.result_id)
        self.assertFalse(Candidature.objects.exists())
        process_connection(cfg.pk, provider=provider)
        self.assertEqual(provider.call_count, 1)

    def test_no_analysis_without_consent(self):
        cfg = self.configure()
        self.message()
        provider = self.provider()
        process_connection(cfg.pk, provider=provider)
        provider.assert_not_called()
        self.assertFalse(EmailAnalysis.objects.exists())

    def test_final_crashed_attempt_becomes_terminal(self):
        cfg = self.configure(auto_analysis_enabled=True, consent=True)
        message = self.message()
        EmailAnalysis.objects.create(message=message, model_name=settings.GEMINI_MODEL, schema_version='1',
            status='running', origin='automatic', consent_version=cfg.consent_version,
            attempts=3, lease_until=timezone.now()-timedelta(seconds=1))
        provider = self.provider()
        process_connection(cfg.pk, provider=provider)
        job = EmailAnalysis.objects.get()
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.error_code, 'attempts_exhausted')
        provider.assert_not_called()

    def test_import_completed_before_crash_finalized_without_network(self):
        run = request_sync(self.user)
        GmailImport.objects.filter(pk=run.import_job_id).update(status='completed')
        SyncRun.objects.filter(pk=run.pk).update(status='running')
        with patch('gmail_integration.import_service.GmailClient') as client:
            process_connection(run.configuration_id)
        client.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')

    def test_revocation_during_call_discards_response(self):
        cfg = self.configure(auto_analysis_enabled=True, consent=True)
        self.message()
        def provider(payload):
            self.configure(auto_analysis_enabled=False)
            return self.provider()(payload)
        process_connection(cfg.pk, provider=provider)
        self.assertEqual(EmailAnalysis.objects.get().status, 'cancelled')
        self.assertFalse(CandidatureProposal.objects.exists())

    def test_quota_keeps_work_in_queue_without_provider_call(self):
        cfg = self.configure(auto_analysis_enabled=True, consent=True)
        self.message()
        provider = self.provider()
        with override_settings(GEMINI_DAILY_REQUEST_LIMIT=0):
            process_connection(cfg.pk, provider=provider)
        provider.assert_not_called()
        job = EmailAnalysis.objects.get()
        self.assertEqual(job.status, 'queued')
        self.assertEqual(job.attempts, 0)
        self.assertEqual(job.error_code, 'quota_exceeded')
        self.assertFalse(AnalysisBudget.objects.exists())

    def test_invalid_content_is_terminal_not_infinite_queue(self):
        cfg = self.configure(auto_analysis_enabled=True, consent=True)
        message = self.message()
        GmailMessage.objects.filter(pk=message.pk).update(encrypted_content=b'broken')
        provider = self.provider()
        process_connection(cfg.pk, provider=provider)
        process_connection(cfg.pk, provider=provider)
        provider.assert_not_called()
        self.assertEqual(EmailAnalysis.objects.get().status, 'failed')

    def test_reconnect_and_purge_cancel_work_and_revoke_consent(self):
        cfg = self.configure(gmail_enabled=True, auto_analysis_enabled=True, consent=True)
        run = request_sync(self.user)
        with transaction.atomic():
            reset_connection(GmailConnection.objects.select_for_update().get(pk=self.conn.pk))
        cfg.refresh_from_db(); run.refresh_from_db()
        self.assertFalse(cfg.gmail_enabled)
        self.assertFalse(cfg.auto_analysis_enabled)
        self.assertEqual(run.status, 'cancelled')
        self.assertIsNone(cfg.lease)
        purge_messages(self.user)
        self.assertFalse(SyncRun.objects.exists())
        cfg.refresh_from_db()
        self.assertIsNone(cfg.started_at)
        self.assertIsNone(cfg.last_success_at)

    def test_inactive_user_no_processing(self):
        run = request_sync(self.user)
        self.user.is_active = False; self.user.save()
        self.assertFalse(process_connection(run.configuration_id, client_factory=FakeGmail))

    def test_worker_dry_run_has_no_side_effects(self):
        self.configure(gmail_enabled=True)
        stream = io.StringIO()
        with patch('gmail_sync.management.commands.process_sync_jobs.process_connection') as process:
            call_command('process_sync_jobs', dry_run=True, stdout=stream)
        process.assert_not_called()
        self.assertFalse(SyncRun.objects.exists())

    def test_command_failure_never_prints_provider_secrets(self):
        self.configure()
        stream = io.StringIO()
        from django.core.management.base import CommandError
        with patch('gmail_sync.management.commands.process_sync_jobs.process_connection', side_effect=RuntimeError('PRIVATE FICTION')):
            with self.assertRaises(CommandError) as caught:
                call_command('process_sync_jobs', stdout=stream)
        self.assertNotIn('PRIVATE', stream.getvalue() + str(caught.exception))


class ApiTests(SyncBase):
    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)
        self.client.get('/api/auth/csrf/')
        self.headers = {'HTTP_X_CSRFTOKEN':self.client.cookies['csrftoken'].value}

    def test_unauthenticated_and_missing_csrf_refused(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.get('/api/sync/configuration/').status_code, 401)
        self.assertEqual(client.post('/api/sync/runs/', '{}', content_type='application/json').status_code, 403)
        self.assertEqual(self.client.post('/api/sync/runs/', '{}', content_type='application/json').status_code, 403)
        self.assertEqual(self.client.patch('/api/sync/configuration/', '{}', content_type='application/json').status_code, 403)

    def test_post_returns_202_no_provider_calls(self):
        with patch('gmail_sync.services.continue_import') as gmail, patch('gmail_sync.services.analyze_message') as gemini:
            response = self.client.post('/api/sync/runs/', '{}', content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'queued')
        gmail.assert_not_called(); gemini.assert_not_called()
        self.assertNotIn('encrypted', response.content.decode())

    def test_other_user_run_hidden_and_owner_fields_rejected(self):
        run = request_sync(self.other)
        self.assertEqual(self.client.get(f'/api/sync/runs/{run.pk}/').status_code, 404)
        self.assertEqual(self.client.post('/api/sync/runs/', json.dumps({'user':self.other.pk}), content_type='application/json', **self.headers).status_code, 400)

    def test_configuration_explicit_consent_and_no_cache(self):
        response = self.client.patch('/api/sync/configuration/', json.dumps({'revision':1, 'auto_analysis_enabled':True}), content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, 400)
        response = self.client.patch('/api/sync/configuration/', json.dumps({'revision':1, 'auto_analysis_enabled':True, 'consent':True}), content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertTrue(response.json()['auto_analysis_enabled'])

    @override_settings(DEBUG=True)
    def test_debug_exception_sanitized(self):
        with patch('gmail_sync.views.configuration_data', side_effect=RuntimeError('PRIVATE FICTION')):
            response = self.client.get('/api/sync/configuration/')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b'PRIVATE', response.content)
