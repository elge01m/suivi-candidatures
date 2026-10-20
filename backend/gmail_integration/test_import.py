import base64
import json
import time
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import MagicMock, patch

import requests
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .crypto import encrypt_tokens
from .gmail_client import BASE_URL, GmailClient
from .import_errors import ImportProblem
from .import_service import continue_import, get_import, period_bounds, prepare_import, purge_messages
from .message_crypto import decrypt_message_data, encrypt_message_data, message_cipher
from .message_parser import MAX_DEPTH, MAX_TEXT, parse_message
from .models import GmailConnection, GmailImport, GmailMessage
from .services import SCOPES


TOKEN_KEY = Fernet.generate_key().decode()
MESSAGE_KEY = Fernet.generate_key().decode()
TEST_SETTINGS = {
    'GOOGLE_TOKEN_ENCRYPTION_KEY': TOKEN_KEY, 'GMAIL_MESSAGE_ENCRYPTION_KEY': MESSAGE_KEY,
    'GOOGLE_CLIENT_ID': 'simulated-client', 'GOOGLE_CLIENT_SECRET': 'simulated-secret',
}


def encoded(text, encoding='utf-8'):
    return base64.urlsafe_b64encode(text.encode(encoding)).decode().rstrip('=')


def message(identifier='m1', *, labels=None, instant=None, payload=None):
    return {
        'id': identifier, 'threadId': 'thread_' + identifier,
        'internalDate': str(int((instant or datetime(2026, 10, 18, 14, tzinfo=datetime_timezone.utc)).timestamp() * 1000)),
        'labelIds': labels or [],
        'payload': payload if payload is not None else {
            'mimeType': 'text/plain', 'headers': [
                {'name': 'From', 'value': 'Employeur <sender@example.test>'},
                {'name': 'To', 'value': 'Destinataire <recipient@example.test>'},
                {'name': 'Cc', 'value': 'Copie <copy@example.test>'},
                {'name': 'Bcc', 'value': 'Cache <hidden@example.test>'},
                {'name': 'Subject', 'value': 'Sujet prive de test'},
                {'name': 'Date', 'value': 'Sun, 18 Oct 2026 14:00:00 +0000'},
            ], 'body': {'data': encoded('Contenu prive de test')},
        },
    }


class FakeGmail:
    def __init__(self, pages=None, messages=None, identity='owner@example.test'):
        self.pages = pages if pages is not None else {None: (['m1'], None)}
        self.messages = messages if messages is not None else {'m1': message()}
        self.identity = identity
        self.list_calls = []
        self.message_calls = []
        self.failures = {}
        self.profile_calls = 0
        self.users = []
        self.closed = False

    def factory(self, connection, deadline):
        self.users.append(connection.utilisateur_id)
        return self

    def profile(self):
        self.profile_calls += 1
        if isinstance(self.identity, Exception):
            raise self.identity
        return self.identity

    def list_page(self, query, token):
        self.list_calls.append((query, token))
        ids, next_token = self.pages[token]
        return list(ids), next_token

    def message(self, identifier):
        self.message_calls.append(identifier)
        if identifier in self.failures:
            raise self.failures.pop(identifier)
        return self.messages[identifier], []

    def close(self):
        self.closed = True


class ParserTests(SimpleTestCase):
    def test_headers_addresses_date_and_text_are_preserved(self):
        result = parse_message(message(labels=['SENT']))
        self.assertEqual(result['content']['subject'], 'Sujet prive de test')
        self.assertEqual(result['content']['from'][0]['address'], 'sender@example.test')
        self.assertEqual(result['content']['to'][0]['address'], 'recipient@example.test')
        self.assertEqual(result['content']['cc'][0]['address'], 'copy@example.test')
        self.assertEqual(result['content']['bcc'][0]['address'], 'hidden@example.test')
        self.assertTrue(result['content']['sent'])
        self.assertEqual(result['content']['text'], 'Contenu prive de test')
        self.assertTrue(timezone.is_aware(result['date_gmail']))
        self.assertFalse(result['content_incomplete'])

    def test_alternative_prefers_plain_without_duplicating_html(self):
        payload = {'mimeType': 'multipart/alternative', 'parts': [
            {'mimeType': 'text/html', 'body': {'data': encoded('<p>HTML</p>')}},
            {'mimeType': 'text/plain', 'body': {'data': encoded('Texte simple')}},
        ]}
        self.assertEqual(parse_message(message(payload=payload))['content']['text'], 'Texte simple')

    def test_html_extraction_removes_scripts_styles_and_never_fetches_resources(self):
        payload = {'mimeType': 'text/html', 'body': {'data': encoded(
            '<html><head><style>private-style</style></head><body><p>Bonjour</p>'
            '<script>private-script</script><img src="https://untrusted.example/pixel">'
            '<p>Deuxieme paragraphe</p></body></html>')}}
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Network disabled')):
            text = parse_message(message(payload=payload))['content']['text']
        self.assertIn('Bonjour', text)
        self.assertIn('Deuxieme paragraphe', text)
        self.assertNotIn('private-script', text)
        self.assertNotIn('private-style', text)
        self.assertNotIn('<html', text)
        self.assertNotIn('untrusted.example', text)

    def test_nested_multipart_ignores_attachments_including_text_and_inline_files(self):
        payload = {'mimeType': 'multipart/mixed', 'parts': [
            {'mimeType': 'multipart/alternative', 'parts': [
                {'mimeType': 'text/plain', 'body': {'data': encoded('Corps')}}]},
            {'mimeType': 'text/plain', 'filename': 'secret.txt', 'body': {'data': encoded('PIECE_JOINTE')}},
            {'mimeType': 'text/plain', 'headers': [{'name': 'Content-Disposition', 'value': 'attachment'}],
             'body': {'data': encoded('AUTRE_PIECE_JOINTE')}},
            {'mimeType': 'image/png', 'filename': 'inline.png', 'body': {'attachmentId': 'private-id'}},
        ]}
        result = parse_message(message(payload=payload))
        self.assertEqual(result['content']['text'], 'Corps')
        self.assertFalse(result['content_incomplete'])
        self.assertNotIn('filename', result['content'])

    def test_external_text_is_marked_incomplete_without_an_attachment_request(self):
        payload = {'mimeType': 'text/plain', 'body': {'attachmentId': 'body-id', 'size': 100}}
        result = parse_message(message(payload=payload))
        self.assertEqual(result['content']['text'], '')
        self.assertIn('external_text_unavailable', result['incomplete_reasons'])

    def test_charset_and_rfc2047_headers_are_decoded(self):
        payload = {'mimeType': 'text/plain', 'headers': [
            {'name': 'Content-Type', 'value': 'text/plain; charset=iso-8859-1'},
            {'name': 'Subject', 'value': '=?utf-8?b?w4l0dWRl?='},
        ], 'body': {'data': encoded('Caf\u00e9', 'iso-8859-1')}}
        result = parse_message(message(payload=payload))
        self.assertEqual(result['content']['text'], 'Caf\u00e9')
        self.assertEqual(result['content']['subject'], '\u00c9tude')

    def test_bad_base64_charset_and_text_limits_are_signaled(self):
        bad = parse_message(message(payload={'mimeType': 'text/plain', 'body': {'data': '%%%'}}))
        self.assertIn('base64_invalid', bad['incomplete_reasons'])
        oversized = parse_message(message(payload={'mimeType': 'text/plain', 'body': {'data': encoded('x' * (MAX_TEXT + 1))}}))
        self.assertEqual(len(oversized['content']['text']), MAX_TEXT)
        self.assertIn('text_limit', oversized['incomplete_reasons'])
        wrong = parse_message(message(payload={'mimeType': 'text/plain', 'headers': [
            {'name': 'Content-Type', 'value': 'text/plain; charset=unknown-charset'}], 'body': {'data': encoded('ok')}}))
        self.assertIn('encoding_replaced', wrong['incomplete_reasons'])

    def test_mime_depth_is_bounded(self):
        payload = {'mimeType': 'text/plain', 'body': {'data': encoded('Corps')}}
        for _ in range(MAX_DEPTH + 2):
            payload = {'mimeType': 'multipart/mixed', 'parts': [payload]}
        result = parse_message(message(payload=payload))
        self.assertIn('mime_limit', result['incomplete_reasons'])


@override_settings(**TEST_SETTINGS)
class ImportBase(TestCase):
    def setUp(self):
        self.block_network = patch('requests.sessions.Session.request', side_effect=AssertionError('Network disabled in tests'))
        self.block_network.start()
        self.addCleanup(self.block_network.stop)
        self.user = get_user_model().objects.create_user(username='import-owner')
        self.other = get_user_model().objects.create_user(username='import-other')
        self.connection = self.make_connection(self.user)
        self.other_connection = self.make_connection(self.other)

    def make_connection(self, user):
        return GmailConnection.objects.create(
            utilisateur=user, encrypted_tokens=encrypt_tokens('simulated-access', 'simulated-refresh'),
            granted_scopes=list(SCOPES), expires_at=timezone.now() + timedelta(hours=1),
        )

    def prepare(self, user=None):
        return prepare_import(user or self.user, '2026-10-18', '2026-10-18')


class ImportServiceTests(ImportBase):
    def test_period_is_inclusive_and_handles_daylight_saving(self):
        _, _, first, last = period_bounds('2026-03-08', '2026-03-08', 'America/Toronto')
        self.assertEqual(last - first, timedelta(hours=23))
        _, _, first, last = period_bounds('2026-11-01', '2026-11-01', 'America/Toronto')
        self.assertEqual(last - first, timedelta(hours=25))

    def test_invalid_periods_and_timezones_create_no_job(self):
        for start, end, zone in [('2026-10-19', '2026-10-18', 'UTC'), ('bad', '2026-10-18', 'UTC'),
                                 ('2026-10-18', '2026-10-18', 'Unknown/Zone')]:
            with self.assertRaises(ImportProblem):
                prepare_import(self.user, start, end, zone)
        self.assertEqual(GmailImport.objects.count(), 0)

    @override_settings(GMAIL_MESSAGE_ENCRYPTION_KEY='')
    def test_missing_message_key_fails_before_preparing_import(self):
        with self.assertRaises(ImportProblem) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, 'configuration_missing')
        self.assertEqual(GmailImport.objects.count(), 0)

    @override_settings(GMAIL_MESSAGE_ENCRYPTION_KEY=TOKEN_KEY)
    def test_message_key_must_differ_from_token_key(self):
        with self.assertRaises(ImportProblem):
            message_cipher()

    def test_complete_pagination_small_batches_sent_received_and_archived(self):
        fake = FakeGmail(
            pages={None: (['m1', 'm2'], 'page-2'), 'page-2': (['m1', 'm3'], None)},
            messages={'m1': message('m1', labels=['INBOX']), 'm2': message('m2', labels=['SENT']),
                      'm3': message('m3')},
        )
        job = self.prepare()
        first = continue_import(self.user, job.pk, client_factory=fake.factory, batch_size=1)
        self.assertEqual(first.status, GmailImport.Status.READY)
        self.assertEqual(first.imported, 1)
        while first.status != GmailImport.Status.COMPLETED:
            first = continue_import(self.user, job.pk, client_factory=fake.factory, batch_size=1)
        self.assertEqual(first.imported, 3)
        self.assertEqual(first.duplicates, 1)
        self.assertEqual(first.processed, 4)
        self.assertEqual(fake.message_calls, ['m1', 'm2', 'm3'])
        self.assertEqual([token for _, token in fake.list_calls], [None, 'page-2'])
        for query, _ in fake.list_calls:
            self.assertIn('-in:spam -in:trash -in:drafts', query)
            self.assertNotIn('in:inbox', query)
            self.assertNotIn('from:', query)
        self.assertEqual(GmailMessage.objects.count(), 3)
        self.assertTrue(fake.closed)

    def test_overlapping_reimports_skip_existing_messages_without_refetching_bodies(self):
        fake = FakeGmail()
        first = continue_import(self.user, self.prepare().pk, client_factory=fake.factory)
        second = continue_import(self.user, self.prepare().pk, client_factory=fake.factory)
        self.assertEqual(first.imported, 1)
        self.assertEqual(second.duplicates, 1)
        self.assertEqual(fake.message_calls, ['m1'])
        self.assertEqual(GmailMessage.objects.count(), 1)

    def test_spam_trash_and_drafts_are_also_excluded_defensively(self):
        fake = FakeGmail(pages={None: (['spam', 'trash', 'draft'], None)}, messages={
            'spam': message('spam', labels=['SPAM']), 'trash': message('trash', labels=['TRASH']),
            'draft': message('draft', labels=['DRAFT']),
        })
        result = continue_import(self.user, self.prepare().pk, client_factory=fake.factory)
        self.assertEqual(result.skipped, 3)
        self.assertEqual(GmailMessage.objects.count(), 0)

    def test_exact_time_boundaries_are_checked_on_internal_date(self):
        job = self.prepare()
        fake = FakeGmail(pages={None: (['start', 'end', 'before'], None)}, messages={
            'start': message('start', instant=job.range_start), 'end': message('end', instant=job.range_end),
            'before': message('before', instant=job.range_start - timedelta(milliseconds=1)),
        })
        result = continue_import(self.user, job.pk, client_factory=fake.factory)
        self.assertEqual(result.imported, 1)
        self.assertEqual(result.skipped, 2)
        self.assertEqual(GmailMessage.objects.get().gmail_id, 'start')

    def test_headers_body_mailbox_and_cursor_are_encrypted(self):
        job = continue_import(self.user, self.prepare().pk, client_factory=FakeGmail().factory)
        stored = GmailMessage.objects.get()
        content = decrypt_message_data(stored.encrypted_content)
        self.assertEqual(content['subject'], 'Sujet prive de test')
        self.assertEqual(content['text'], 'Contenu prive de test')
        self.assertNotIn(b'Sujet prive', bytes(stored.encrypted_content))
        self.assertNotIn(b'pending_ids', bytes(job.encrypted_cursor))
        self.connection.refresh_from_db()
        self.assertNotIn(b'owner@example.test', bytes(self.connection.encrypted_mailbox_identity))
        self.assertEqual(decrypt_message_data(self.connection.encrypted_mailbox_identity), 'owner@example.test')

    def test_incomplete_text_is_stored_and_counted_explicitly(self):
        fake = FakeGmail(messages={'m1': message(payload={
            'mimeType': 'text/plain', 'body': {'attachmentId': 'external-body', 'size': 200},
        })})
        job = continue_import(self.user, self.prepare().pk, client_factory=fake.factory)
        stored = GmailMessage.objects.get()
        self.assertEqual(job.imported, 1)
        self.assertEqual(job.incomplete, 1)
        self.assertTrue(stored.content_incomplete)
        self.assertIn('external_text_unavailable', stored.incomplete_reasons)
        self.assertEqual(decrypt_message_data(stored.encrypted_content)['text'], '')

    def test_wrong_message_key_stops_before_network_and_releases_the_lease(self):
        fake = FakeGmail(pages={None: (['m1', 'm2'], None)}, messages={'m1': message('m1'), 'm2': message('m2')})
        job = continue_import(self.user, self.prepare().pk, client_factory=fake.factory, batch_size=1)
        factory = MagicMock(side_effect=AssertionError('Network must not start'))
        with override_settings(GMAIL_MESSAGE_ENCRYPTION_KEY=Fernet.generate_key().decode()):
            job = continue_import(self.user, job.pk, client_factory=factory)
        self.assertEqual(job.status, GmailImport.Status.FAILED)
        self.assertEqual(job.error_code, 'content_unreadable')
        self.assertIsNone(job.lease_token)
        factory.assert_not_called()
        self.assertEqual(GmailMessage.objects.count(), 1)

    def test_job_without_session_or_browser_works_with_user_service_arguments_only(self):
        fake = FakeGmail()
        job = prepare_import(self.user, '2026-10-18', '2026-10-18', 'America/Toronto')
        result = continue_import(self.user, job.pk, client_factory=fake.factory)
        self.assertEqual(result.status, GmailImport.Status.COMPLETED)
        self.assertEqual(result.imported, 1)

    def test_partial_failure_preserves_current_message_and_resumes_without_duplicates(self):
        fake = FakeGmail(pages={None: (['m1', 'm2'], None)}, messages={'m1': message('m1'), 'm2': message('m2')})
        fake.failures['m2'] = ImportProblem('rate_limited', retryable=True, retry_seconds=60)
        job = continue_import(self.user, self.prepare().pk, client_factory=fake.factory)
        self.assertEqual(job.status, GmailImport.Status.PAUSED)
        self.assertEqual(job.imported, 1)
        self.assertEqual(decrypt_message_data(job.encrypted_cursor)['pending_ids'], ['m2'])
        with self.assertRaises(ImportProblem):
            continue_import(self.user, job.pk, client_factory=fake.factory)
        GmailImport.objects.filter(pk=job.pk).update(retry_after=timezone.now() - timedelta(seconds=1))
        job = continue_import(self.user, job.pk, client_factory=fake.factory)
        self.assertEqual(job.status, GmailImport.Status.COMPLETED)
        self.assertEqual(job.imported, 2)
        self.assertEqual(fake.message_calls, ['m1', 'm2', 'm2'])
        self.assertEqual(len(fake.list_calls), 1)

    def test_retries_are_bounded(self):
        fake = FakeGmail(identity=ImportProblem('gmail_unavailable', retryable=True))
        job = self.prepare()
        for _ in range(5):
            job = continue_import(self.user, job.pk, client_factory=fake.factory)
            GmailImport.objects.filter(pk=job.pk).update(retry_after=None)
        self.assertEqual(job.status, GmailImport.Status.FAILED)
        with self.assertRaises(ImportProblem):
            continue_import(self.user, job.pk, client_factory=fake.factory)
        self.assertEqual(fake.profile_calls, 5)

    def test_deleted_messages_are_skipped(self):
        fake = FakeGmail()
        fake.failures['m1'] = ImportProblem('message_missing')
        job = continue_import(self.user, self.prepare().pk, client_factory=fake.factory)
        self.assertEqual(job.status, GmailImport.Status.COMPLETED)
        self.assertEqual(job.skipped, 1)

    def test_empty_intermediate_pages_are_followed_and_repeated_page_tokens_fail(self):
        fake = FakeGmail(pages={None: ([], 'next'), 'next': (['m1'], None)})
        job = continue_import(self.user, self.prepare().pk, client_factory=fake.factory)
        self.assertEqual(job.imported, 1)
        bad = FakeGmail(pages={None: ([], 'loop'), 'loop': ([], 'loop')})
        job = continue_import(self.user, self.prepare().pk, client_factory=bad.factory)
        self.assertEqual(job.status, GmailImport.Status.FAILED)
        self.assertEqual(job.error_code, 'invalid_page')

    def test_two_users_can_import_same_gmail_id_without_sharing_data(self):
        first = continue_import(self.user, self.prepare().pk, client_factory=FakeGmail().factory)
        other_fake = FakeGmail(identity='other@example.test')
        second = continue_import(self.other, self.prepare(self.other).pk, client_factory=other_fake.factory)
        self.assertNotEqual(first.connexion_id, second.connexion_id)
        self.assertEqual(GmailMessage.objects.filter(gmail_id='m1').count(), 2)
        self.assertEqual(other_fake.users, [self.other.pk])
        with self.assertRaises(ImportProblem):
            get_import(self.other, first.pk)
        with self.assertRaises(ImportProblem):
            continue_import(self.other, first.pk, client_factory=other_fake.factory)

    def test_account_switch_blocks_listing_and_preserves_previous_content(self):
        continue_import(self.user, self.prepare().pk, client_factory=FakeGmail().factory)
        before = bytes(GmailMessage.objects.get().encrypted_content)
        fake = FakeGmail(identity='different-mailbox@example.test')
        job = continue_import(self.user, self.prepare().pk, client_factory=fake.factory)
        self.assertEqual(job.error_code, 'mailbox_changed')
        self.assertEqual(fake.list_calls, [])
        self.assertEqual(bytes(GmailMessage.objects.get().encrypted_content), before)

    def test_reconsent_during_network_read_cannot_commit_into_old_mailbox(self):
        fake = FakeGmail()
        original_message = fake.message
        def changed(identifier):
            GmailConnection.objects.filter(pk=self.connection.pk).update(credential_version=uuid.uuid4())
            return original_message(identifier)
        fake.message = changed
        job = continue_import(self.user, self.prepare().pk, client_factory=fake.factory)
        self.assertEqual(job.error_code, 'credentials_changed')
        self.assertEqual(GmailMessage.objects.count(), 0)

    def test_active_lease_prevents_concurrent_batches_and_expired_lease_can_resume(self):
        job = self.prepare()
        job.status = GmailImport.Status.RUNNING
        job.lease_token = uuid.uuid4()
        job.lease_until = timezone.now() + timedelta(seconds=60)
        job.save()
        with self.assertRaises(ImportProblem) as caught:
            continue_import(self.user, job.pk, client_factory=FakeGmail().factory)
        self.assertEqual(caught.exception.code, 'busy')
        GmailImport.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        job = continue_import(self.user, job.pk, client_factory=FakeGmail().factory)
        self.assertEqual(job.status, GmailImport.Status.COMPLETED)

    def test_unique_constraint_prevents_duplicate_database_rows(self):
        continue_import(self.user, self.prepare().pk, client_factory=FakeGmail().factory)
        stored = GmailMessage.objects.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            GmailMessage.objects.create(
                connexion=stored.connexion, gmail_id=stored.gmail_id, thread_id=stored.thread_id,
                date_gmail=stored.date_gmail, encrypted_content=stored.encrypted_content,
            )

    def test_purge_is_local_scoped_and_removes_cursors_before_rebinding_mailbox(self):
        first = continue_import(self.user, self.prepare().pk, client_factory=FakeGmail().factory)
        second = continue_import(self.other, self.prepare(self.other).pk,
                                 client_factory=FakeGmail(identity='other@example.test').factory)
        other_content = bytes(GmailMessage.objects.get(connexion=self.other_connection).encrypted_content)
        self.assertEqual(purge_messages(self.user), 1)
        self.assertFalse(GmailImport.objects.filter(pk=first.pk).exists())
        self.assertTrue(GmailImport.objects.filter(pk=second.pk).exists())
        self.assertEqual(bytes(GmailMessage.objects.get(connexion=self.other_connection).encrypted_content), other_content)
        self.connection.refresh_from_db()
        self.assertIsNone(self.connection.encrypted_mailbox_identity)
        self.assertTrue(self.connection.encrypted_tokens)
        job = continue_import(self.user, self.prepare().pk, client_factory=FakeGmail(identity='new@example.test').factory)
        self.assertEqual(job.imported, 1)

    def test_purge_refuses_an_active_batch(self):
        job = self.prepare()
        job.status = GmailImport.Status.RUNNING
        job.lease_token = uuid.uuid4()
        job.lease_until = timezone.now() + timedelta(seconds=60)
        job.save()
        with self.assertRaises(ImportProblem):
            purge_messages(self.user)
        self.assertTrue(GmailImport.objects.filter(pk=job.pk).exists())


class ImportAPITests(ImportBase):
    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)
        self.create_url = reverse('gmail_integration:import-create')
        self.purge_url = reverse('gmail_integration:message-purge')
        self.payload = {'date_debut': '2026-10-18', 'date_fin': '2026-10-18'}

    def headers(self):
        self.client.get(reverse('auth_api:csrf'))
        return {'HTTP_X_CSRFTOKEN': self.client.cookies['csrftoken'].value}

    def test_create_only_prepares_a_job_and_rejects_owner_override(self):
        response = self.client.post(self.create_url, self.payload, content_type='application/json', **self.headers())
        self.assertEqual(response.status_code, 201)
        job = GmailImport.objects.get(pk=response.json()['id'])
        self.assertEqual(job.connexion_id, self.connection.pk)
        response = self.client.post(self.create_url, {**self.payload, 'utilisateur': self.other.pk},
                                    content_type='application/json', **self.headers())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'invalid_request')

    def test_endpoints_require_authentication_and_writes_require_csrf(self):
        job = self.prepare()
        next_url = reverse('gmail_integration:import-continue', args=[job.pk])
        status_url = reverse('gmail_integration:import-status', args=[job.pk])
        for url in (self.create_url, next_url):
            self.assertEqual(self.client.post(url, self.payload, content_type='application/json').status_code, 403)
        self.assertEqual(self.client.delete(self.purge_url, '{}', content_type='application/json').status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(status_url).status_code, 401)
        self.assertEqual(self.client.post(self.create_url, self.payload, content_type='application/json', **self.headers()).status_code, 401)

    def test_foreign_import_cannot_be_read_or_processed(self):
        job = self.prepare(self.other)
        self.assertEqual(self.client.get(reverse('gmail_integration:import-status', args=[job.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('gmail_integration:import-continue', args=[job.pk]),
                                         **self.headers()).status_code, 404)

    def test_continue_and_status_expose_only_progress_not_message_contents(self):
        job = self.prepare()
        with patch('gmail_integration.import_service.GmailClient', side_effect=FakeGmail().factory):
            response = self.client.post(reverse('gmail_integration:import-continue', args=[job.pk]), **self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'completed')
        self.assertEqual(response.json()['imported'], 1)
        for value in (b'Sujet prive', b'Contenu prive', b'example.test', b'simulated-access', b'pending_ids'):
            self.assertNotIn(value, response.content)
        status = self.client.get(reverse('gmail_integration:import-status', args=[job.pk]))
        self.assertEqual(status.status_code, 200)
        self.assertEqual(set(status.json()), {
            'id', 'date_debut', 'date_fin', 'fuseau', 'status', 'processed', 'imported',
            'duplicates', 'skipped', 'incomplete', 'error_code', 'retry_after',
        })

    def test_validation_codes_survive_privacy_middleware_without_echoing_input(self):
        response = self.client.post(self.create_url, {'date_debut': 'private-input', 'date_fin': 'bad'},
                                    content_type='application/json', **self.headers())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'invalid_period')
        self.assertNotIn(b'private-input', response.content)

    def test_delete_requires_explicit_confirmation(self):
        continue_import(self.user, self.prepare().pk, client_factory=FakeGmail().factory)
        self.assertEqual(self.client.delete(self.purge_url, '{}', content_type='application/json', **self.headers()).status_code, 400)
        self.assertEqual(GmailMessage.objects.count(), 1)
        response = self.client.delete(self.purge_url, '{"confirm":true}', content_type='application/json', **self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'deleted': 1})

    @override_settings(DEBUG=True)
    def test_unexpected_failure_never_exposes_private_text_or_debug_locals(self):
        job = self.prepare()
        fake = FakeGmail(identity=RuntimeError('private-subject-and-message-body'))
        with patch('gmail_integration.import_service.GmailClient', side_effect=fake.factory):
            response = self.client.post(reverse('gmail_integration:import-continue', args=[job.pk]), **self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['error_code'], 'internal_error')
        self.assertNotIn(b'private-subject', response.content)
        self.assertNotIn(b'Traceback', response.content)

    def test_continue_rejects_owner_fields_and_accepts_empty_json(self):
        job = self.prepare()
        url = reverse('gmail_integration:import-continue', args=[job.pk])
        response = self.client.post(url, {'utilisateur': self.other.pk},
                                    content_type='application/json', **self.headers())
        self.assertEqual(response.status_code, 400)
        with patch('gmail_integration.import_service.GmailClient', side_effect=FakeGmail().factory):
            response = self.client.post(url, {}, content_type='application/json', **self.headers())
        self.assertEqual(response.status_code, 200)


class GmailClientTests(ImportBase):
    def client_with(self, *responses):
        client = GmailClient(self.connection, time.monotonic() + 20)
        client.transport = MagicMock()
        client.transport.get.side_effect = list(responses)
        return client

    @staticmethod
    def response(body, status=200, headers=None):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(body).encode()
        response._content_consumed = True
        response.headers.update(headers or {})
        return response

    def test_listing_uses_fixed_me_endpoint_period_pagination_and_exclusions(self):
        client = self.client_with(self.response({'messages': [{'id': 'm1'}], 'nextPageToken': 'next'}))
        self.assertEqual(client.list_page('after:1 before:2 -in:spam -in:trash -in:drafts', 'page'), (['m1'], 'next'))
        args = client.transport.get.call_args
        self.assertEqual(args.args[0], BASE_URL + '/messages')
        self.assertEqual(args.kwargs['params']['includeSpamTrash'], 'false')
        self.assertEqual(args.kwargs['params']['pageToken'], 'page')
        self.assertFalse(args.kwargs['allow_redirects'])
        self.assertEqual(args.kwargs['headers']['Authorization'], 'Bearer simulated-access')

    def test_rate_limit_respects_retry_after_and_does_not_retry_in_request(self):
        client = self.client_with(self.response({}, 429, {'Retry-After': '120'}))
        with self.assertRaises(ImportProblem) as caught:
            client.profile()
        self.assertEqual(caught.exception.code, 'rate_limited')
        self.assertEqual(caught.exception.retry_seconds, 120)
        self.assertEqual(client.transport.get.call_count, 1)

    def test_403_quota_is_distinguished_from_denied_scope(self):
        for reason, expected in [('userRateLimitExceeded', 'rate_limited'), ('insufficientPermissions', 'gmail_access_denied')]:
            client = self.client_with(self.response({'error': {'errors': [{'reason': reason}]}}, 403))
            with self.assertRaises(ImportProblem) as caught:
                client.profile()
            self.assertEqual(caught.exception.code, expected)

    def test_server_and_network_errors_do_not_expose_raw_responses(self):
        client = self.client_with(self.response({'private': 'secret-body'}, 503))
        with self.assertRaises(ImportProblem) as caught:
            client.profile()
        self.assertEqual(caught.exception.code, 'gmail_unavailable')
        client = self.client_with(requests.ConnectionError('private-transport-details'))
        with self.assertRaises(ImportProblem) as caught:
            client.profile()
        self.assertNotIn('private', str(caught.exception))

    def test_401_refreshes_once_and_does_not_loop(self):
        client = self.client_with(self.response({}, 401), self.response({}, 401))
        with patch('gmail_integration.gmail_client.refresh_access_token', return_value=self.connection) as refresh:
            with self.assertRaises(ImportProblem) as caught:
                client.profile()
        self.assertEqual(caught.exception.code, 'reauthorization_required')
        self.assertEqual(refresh.call_count, 1)

    def test_expired_access_token_uses_existing_refresh_service_before_api_reads(self):
        self.connection.expires_at = timezone.now() - timedelta(seconds=1)
        with patch('gmail_integration.gmail_client.refresh_access_token', return_value=self.connection) as refresh:
            client = GmailClient(self.connection, time.monotonic() + 20)
        refresh.assert_called_once_with(self.user)
        client.close()

    def test_large_body_falls_back_to_headers_without_attachment_downloads(self):
        client = self.client_with()
        metadata = message(payload={'headers': []})
        with patch.object(client, '_get', side_effect=[ImportProblem('response_too_large'), metadata]) as get:
            body, reasons = client.message('m1')
        self.assertEqual(body, metadata)
        self.assertEqual(reasons, ['response_too_large'])
        self.assertEqual(get.call_args_list[1].args[1]['format'], 'metadata')
        self.assertTrue(all('/attachments/' not in call.args[0] for call in get.call_args_list))

    def test_response_size_and_identifier_validation_are_bounded(self):
        client = self.client_with(self.response({'emailAddress': 'owner@example.test'}))
        with patch('gmail_integration.gmail_client.MAX_RESPONSE_BYTES', 5):
            with self.assertRaises(ImportProblem) as caught:
                client.profile()
        self.assertEqual(caught.exception.code, 'response_too_large')
        client.transport.get.reset_mock()
        with self.assertRaises(ImportProblem):
            client.message('../untrusted')
        client.transport.get.assert_not_called()

    def test_expired_deadline_does_not_start_a_network_request(self):
        client = self.client_with()
        client.deadline = time.monotonic() - 1
        with self.assertRaises(ImportProblem) as caught:
            client.profile()
        self.assertEqual(caught.exception.code, 'time_budget')
        client.transport.get.assert_not_called()
