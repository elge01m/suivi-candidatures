"""Real PostgreSQL transactions in separate threads; all providers remain simulated."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

from django.db import close_old_connections, transaction
from django.test import TransactionTestCase, override_settings, skipUnlessDBFeature

from email_analysis.errors import AnalysisProblem
from email_analysis.models import AnalysisBudget, CandidatureProposal, EmailAnalysis
from email_analysis.services import _reserve
from .models import SyncConfiguration, SyncRun
from .services import claim_connection, configure, process_connection, release_connection, request_initial_import, request_sync, retry_import
from .tests import KEY, TOKEN_KEY, FakeGmail, SyncBase


def isolated(function):
    close_old_connections()
    try:
        return function()
    finally:
        close_old_connections()


@skipUnlessDBFeature('has_select_for_update')
@override_settings(GMAIL_MESSAGE_ENCRYPTION_KEY=KEY, GOOGLE_TOKEN_ENCRYPTION_KEY=TOKEN_KEY,
    GEMINI_API_KEY='fictitious', GEMINI_DAILY_REQUEST_LIMIT=20, GEMINI_DAILY_TOKEN_LIMIT=100000,
    GEMINI_REQUESTS_PER_MINUTE=1, GEMINI_TOKENS_PER_MINUTE=30000)
class PostgresConcurrencyTests(TransactionTestCase):
    setUp = SyncBase.setUp
    configure = SyncBase.configure
    message = SyncBase.message
    provider = SyncBase.provider

    def test_two_retry_requests_do_not_duplicate_or_reset_the_import(self):
        from gmail_integration.models import GmailImport
        from .errors import SyncProblem
        run=request_sync(self.user)
        SyncRun.objects.filter(pk=run.pk).update(status='failed',error_code='gmail_unavailable')
        GmailImport.objects.filter(pk=run.import_job_id).update(status='failed',error_code='gmail_unavailable',retry_count=1)
        gate=Barrier(2)
        def retry():
            gate.wait(timeout=5)
            try:
                return str(retry_import(self.user,run.import_job_id,confirm=True).pk)
            except SyncProblem as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:isolated(retry),range(2)))
        self.assertCountEqual(results,[str(run.pk),'retry_unavailable'])
        self.assertEqual(SyncRun.objects.count(),1)
        self.assertEqual(GmailImport.objects.get(pk=run.import_job_id).retry_count,1)

    def test_concurrent_acceptances_create_one_history_entry(self):
        from candidatures.models import Candidature, CandidatureChange
        from email_analysis.services import accept_proposal
        from gmail_integration.message_crypto import encrypt_message_data
        item=Candidature.objects.create(utilisateur=self.user,employeur='Fictif',poste='Test',date_candidature='2026-10-13')
        message=self.message()
        analysis=EmailAnalysis.objects.create(message=message,model_name='fiction',schema_version='1',status='completed')
        proposal=CandidatureProposal.objects.create(analysis=analysis,utilisateur=self.user,encrypted_data=encrypt_message_data({
            'action':'changement_etat','etat':'entrevue','cible_id':item.pk,'cible_version':item.date_modification.isoformat()}))
        gate=Barrier(2)
        def accept():
            gate.wait(timeout=5)
            return accept_proposal(self.user,proposal.pk,1,confirm=True).status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:isolated(accept),range(2)))
        self.assertEqual(results,['acceptee','acceptee'])
        self.assertEqual(CandidatureChange.objects.count(),1)

    def test_two_initial_import_requests_share_one_persistent_job(self):
        gate = Barrier(2)
        period = {'date_debut':'2026-10-12', 'date_fin':'2026-10-12', 'fuseau':'America/Toronto'}
        def enqueue():
            gate.wait(timeout=5)
            return request_initial_import(self.user, period).pk
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: isolated(enqueue), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(SyncRun.objects.count(), 1)

    def test_two_manual_enqueues_make_one_job(self):
        gate = Barrier(2)
        def enqueue():
            gate.wait(timeout=5)
            return request_sync(self.user).pk
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: isolated(enqueue), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(SyncRun.objects.count(), 1)

    def test_two_workers_cannot_claim_same_configuration(self):
        cfg = self.configure()
        gate = Barrier(2)
        def claim():
            gate.wait(timeout=5)
            return claim_connection(cfg.pk)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: isolated(claim), range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)
        release_connection(cfg.pk, next(value for value in results if value))

    def test_import_call_not_duplicated_by_concurrent_worker(self):
        run = request_sync(self.user)
        entered, release = Event(), Event()
        class BlockingGmail(FakeGmail):
            def profile(self):
                entered.set()
                if not release.wait(timeout=8):
                    raise AssertionError('Test synchronization timed out')
                return super().profile()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(isolated, lambda: process_connection(run.configuration_id, client_factory=BlockingGmail))
            try:
                self.assertTrue(entered.wait(timeout=5))
                self.assertFalse(process_connection(run.configuration_id, client_factory=BlockingGmail))
            finally:
                release.set()
            self.assertTrue(future.result(timeout=10))
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(len(FakeGmail.calls), 1)

    @override_settings(GEMINI_DAILY_REQUEST_LIMIT=1)
    def test_shared_budget_reservation_is_atomic(self):
        gate = Barrier(2)
        def reserve():
            gate.wait(timeout=5)
            try:
                with transaction.atomic():
                    _reserve(100)
                return 'reserved'
            except AnalysisProblem as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: isolated(reserve), range(2)))
        self.assertCountEqual(results, ['reserved', 'quota_exceeded'])
        self.assertEqual(AnalysisBudget.objects.get().calls, 1)

    def test_revocation_during_external_call_prevents_proposal_commit(self):
        cfg = self.configure(auto_analysis_enabled=True, consent=True)
        self.message()
        entered, release = Event(), Event()
        result = self.provider().return_value
        def provider(payload):
            entered.set()
            if not release.wait(timeout=8):
                raise AssertionError('Test synchronization timed out')
            return result
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(isolated, lambda: process_connection(cfg.pk, provider=provider))
            try:
                self.assertTrue(entered.wait(timeout=5))
                cfg.refresh_from_db()
                configure(self.user, {'revision':cfg.revision, 'auto_analysis_enabled':False})
            finally:
                release.set()
            future.result(timeout=10)
        self.assertFalse(CandidatureProposal.objects.exists())
        self.assertEqual(EmailAnalysis.objects.get().status, 'cancelled')
        self.assertFalse(SyncConfiguration.objects.get(pk=cfg.pk).auto_analysis_enabled)
