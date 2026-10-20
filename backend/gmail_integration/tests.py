import base64
import hashlib
import io
import json
import logging
import time
from datetime import timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from google.auth.exceptions import RefreshError
from requests import Request as HttpRequest, Response

from .crypto import OAuthConfigurationError, OAuthFailure, decrypt_tokens, encrypt_tokens
from .logging import OAuthLogFilter
from .models import GmailConnection
from .services import LimitedRequest, ReauthorizationRequired, SCOPES, refresh_access_token
from .views import PENDING_KEY, STATE_LIFETIME


TEST_KEY = Fernet.generate_key().decode('ascii')
GOOGLE_TEST_SETTINGS = {
    'GOOGLE_CLIENT_ID': 'simulated-client.apps.googleusercontent.com',
    'GOOGLE_CLIENT_SECRET': 'simulated-client-secret',
    'GOOGLE_REDIRECT_URI': 'http://localhost:8000/api/gmail/callback/',
    'GOOGLE_TOKEN_ENCRYPTION_KEY': TEST_KEY,
}


def token_response(**overrides):
    return {
        'access_token': 'simulated-access-token',
        'refresh_token': 'simulated-refresh-token',
        'token_type': 'Bearer', 'scope': SCOPES[0], 'expires_in': 3600,
        **overrides,
    }


@override_settings(**GOOGLE_TEST_SETTINGS)
class OAuthTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='gmail-oauth-test')
        cls.other = get_user_model().objects.create_user(username='gmail-oauth-other')

    def setUp(self):
        # An accidental HTTP request makes the test fail, never contacts Google.
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Network disabled in tests'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)
        self.connect_url = reverse('gmail_integration:connect')
        self.callback_url = reverse('gmail_integration:callback')

    def headers(self):
        self.client.get(reverse('auth_api:csrf'))
        return {'HTTP_X_CSRFTOKEN': self.client.cookies['csrftoken'].value}

    def begin(self):
        response = self.client.post(self.connect_url, **self.headers())
        self.assertEqual(response.status_code, 302)
        return self.client.session[PENDING_KEY]

    def callback(self, pending, **extra):
        return self.client.get(self.callback_url, {
            'state': pending['state'], 'code': 'simulated-authorization-code', **extra,
        })

    def existing_connection(self):
        return GmailConnection.objects.create(
            utilisateur=self.user,
            encrypted_tokens=encrypt_tokens('previous-access', 'previous-refresh'),
            expires_at=timezone.now() + timedelta(minutes=10), granted_scopes=list(SCOPES),
        )

    def test_anonymous_connect_is_csrf_protected_and_requires_authentication(self):
        self.client.logout()
        self.assertEqual(self.client.post(self.connect_url).status_code, 403)
        self.assertEqual(self.client.post(self.connect_url, **self.headers()).status_code, 401)
        self.assertEqual(self.client.get(self.callback_url, {'code': 'simulated-code'}).status_code, 401)
        self.assertEqual(GmailConnection.objects.count(), 0)

    def test_authenticated_connect_without_csrf_is_rejected(self):
        self.assertEqual(self.client.post(self.connect_url).status_code, 403)
        self.assertNotIn(PENDING_KEY, self.client.session)

    def test_untrusted_origin_is_rejected(self):
        response = self.client.post(self.connect_url, HTTP_ORIGIN='https://untrusted.example', **self.headers())
        self.assertEqual(response.status_code, 403)

    def test_connect_requires_post(self):
        self.assertEqual(self.client.get(self.connect_url).status_code, 405)

    def test_authorization_request_uses_exact_scope_offline_consent_and_s256(self):
        response = self.client.post(self.connect_url, **self.headers())
        self.assertEqual(response.status_code, 302)
        url = urlsplit(response['Location'])
        query = parse_qs(url.query)
        self.assertEqual(url.scheme, 'https')
        self.assertEqual(url.hostname, 'accounts.google.com')
        self.assertEqual(query['response_type'], ['code'])
        self.assertEqual(query['scope'], list(SCOPES))
        self.assertEqual(query['access_type'], ['offline'])
        self.assertEqual(query['prompt'], ['consent'])
        self.assertEqual(query['include_granted_scopes'], ['false'])
        self.assertEqual(query['redirect_uri'], [GOOGLE_TEST_SETTINGS['GOOGLE_REDIRECT_URI']])
        pending = self.client.session[PENDING_KEY]
        expected = base64.urlsafe_b64encode(hashlib.sha256(pending['verifier'].encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(query['code_challenge'], [expected])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertEqual(query['state'], [pending['state']])
        self.assertEqual(pending['user_id'], self.user.pk)
        self.assertEqual(set(pending), {'state', 'verifier', 'user_id', 'created_at'})
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')

    def test_connect_replaces_previous_pending_attempt(self):
        first = self.begin()
        second = self.begin()
        self.assertNotEqual(first['state'], second['state'])
        self.assertNotEqual(first['verifier'], second['verifier'])

    @override_settings(GOOGLE_CLIENT_SECRET='')
    def test_missing_google_configuration_does_not_start_flow(self):
        response = self.client.post(self.connect_url, **self.headers())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(PENDING_KEY, self.client.session)

    @override_settings(GOOGLE_TOKEN_ENCRYPTION_KEY='invalid-key')
    def test_invalid_encryption_key_does_not_start_flow(self):
        self.assertEqual(self.client.post(self.connect_url, **self.headers()).status_code, 503)
        self.assertNotIn(PENDING_KEY, self.client.session)

    @override_settings(GOOGLE_REDIRECT_URI='http://untrusted.example/api/gmail/callback/')
    def test_insecure_nonlocal_redirect_is_refused(self):
        self.assertEqual(self.client.post(self.connect_url, **self.headers()).status_code, 503)

    @patch('google_auth_oauthlib.flow.Flow.fetch_token')
    def test_callback_saves_only_ciphertext_and_redirects_without_oauth_parameters(self, fetch):
        fetch.return_value = token_response()
        pending = self.begin()
        response = self.callback(pending, next='https://untrusted.example')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], 'http://localhost:5173/')
        self.assertNotIn('code=', response['Location'])
        self.assertNotIn(PENDING_KEY, self.client.session)
        fetch.assert_called_once_with(code='simulated-authorization-code', timeout=10)
        connection = GmailConnection.objects.get(utilisateur=self.user)
        raw = bytes(connection.encrypted_tokens)
        self.assertNotIn(b'simulated-access-token', raw)
        self.assertNotIn(b'simulated-refresh-token', raw)
        self.assertEqual(decrypt_tokens(raw)['refresh_token'], 'simulated-refresh-token')
        self.assertEqual(connection.granted_scopes, list(SCOPES))
        self.assertGreater(connection.expires_at, timezone.now())
        self.assertNotIn(b'simulated-access-token', response.content)
        self.assertNotIn(b'simulated-refresh-token', response.content)
        self.assertNotIn(b'simulated-authorization-code', response.content)

    @patch('gmail_integration.views.exchange_code')
    def test_missing_wrong_or_duplicate_state_never_exchanges_a_code(self, exchange):
        pending = self.begin()
        for query in ({'code': 'simulated-code'}, {'state': 'wrong', 'code': 'simulated-code'},
                      {'state': [pending['state'], pending['state']], 'code': 'simulated-code'}):
            self.assertEqual(self.client.get(self.callback_url, query).status_code, 400)
        exchange.assert_not_called()

    @patch('gmail_integration.views.exchange_code')
    def test_expired_state_is_consumed_and_refused(self, exchange):
        pending = self.begin()
        session = self.client.session
        session[PENDING_KEY] = {**pending, 'created_at': time.time() - STATE_LIFETIME - 1}
        session.save()
        self.assertEqual(self.callback(pending).status_code, 400)
        self.assertNotIn(PENDING_KEY, self.client.session)
        exchange.assert_not_called()

    @patch('gmail_integration.views.exchange_code')
    def test_state_is_bound_to_the_authenticated_user(self, exchange):
        pending = self.begin()
        session = self.client.session
        session[PENDING_KEY] = {**pending, 'user_id': self.other.pk}
        session.save()
        self.assertEqual(self.callback(pending).status_code, 400)
        exchange.assert_not_called()

    @patch('gmail_integration.views.exchange_code')
    def test_state_cannot_cross_two_users_and_two_independent_sessions(self, exchange):
        first = self.begin()
        second_client = Client(enforce_csrf_checks=True)
        second_client.force_login(self.other)
        second_client.get(reverse('auth_api:csrf'))
        response = second_client.post(
            self.connect_url, HTTP_X_CSRFTOKEN=second_client.cookies['csrftoken'].value,
        )
        self.assertEqual(response.status_code, 302)
        second = second_client.session[PENDING_KEY]
        self.assertNotEqual(first['state'], second['state'])
        self.assertEqual(second_client.get(self.callback_url, {
            'state': first['state'], 'code': 'simulated-code',
        }).status_code, 400)
        self.assertEqual(self.callback(second).status_code, 400)
        exchange.assert_not_called()
        self.assertEqual(GmailConnection.objects.count(), 0)

    @patch('gmail_integration.views.exchange_code')
    def test_state_cannot_cross_sessions_even_for_the_same_user(self, exchange):
        pending = self.begin()
        second_client = Client(enforce_csrf_checks=True)
        second_client.force_login(self.user)
        response = second_client.get(self.callback_url, {
            'state': pending['state'], 'code': 'simulated-code',
        })
        self.assertEqual(response.status_code, 400)
        exchange.assert_not_called()
        self.assertIn(PENDING_KEY, self.client.session)

    @patch('gmail_integration.views.exchange_code')
    def test_switching_authenticated_account_invalidates_the_pending_flow(self, exchange):
        pending = self.begin()
        self.client.force_login(self.other)
        self.assertEqual(self.callback(pending).status_code, 400)
        exchange.assert_not_called()

    @patch('google_auth_oauthlib.flow.Flow.fetch_token')
    def test_callbacks_store_each_users_tokens_without_accepting_an_owner_override(self, fetch):
        fetch.return_value = token_response()
        self.assertEqual(self.callback(self.begin(), utilisateur=self.other.pk, user_id=self.other.pk).status_code, 302)
        first = GmailConnection.objects.get(utilisateur=self.user)
        first_ciphertext = bytes(first.encrypted_tokens)
        self.assertFalse(GmailConnection.objects.filter(utilisateur=self.other).exists())
        self.client.force_login(self.other)
        fetch.return_value = token_response(access_token='second-access', refresh_token='second-refresh')
        self.assertEqual(self.callback(self.begin(), utilisateur=self.user.pk, user_id=self.user.pk).status_code, 302)
        second = GmailConnection.objects.get(utilisateur=self.other)
        first.refresh_from_db()
        self.assertEqual(bytes(first.encrypted_tokens), first_ciphertext)
        self.assertEqual(decrypt_tokens(second.encrypted_tokens)['refresh_token'], 'second-refresh')
        self.assertEqual(GmailConnection.objects.count(), 2)

    @patch('google_auth_oauthlib.flow.Flow.fetch_token')
    def test_successful_callback_cannot_be_replayed(self, fetch):
        fetch.return_value = token_response()
        pending = self.begin()
        self.assertEqual(self.callback(pending).status_code, 302)
        self.assertEqual(self.callback(pending).status_code, 400)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(GmailConnection.objects.count(), 1)

    @patch('gmail_integration.views.exchange_code')
    def test_refusal_missing_code_and_duplicate_code_are_rejected(self, exchange):
        for extra in ({'code': ''}, {'code': ['one', 'two']},
                      {'error': 'access_denied', 'error_description': 'simulated-sensitive-text'}):
            pending = self.begin()
            response = self.callback(pending, **extra)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn(b'simulated-sensitive-text', response.content)
            self.assertNotIn(PENDING_KEY, self.client.session)
        exchange.assert_not_called()

    @patch('google_auth_oauthlib.flow.Flow.fetch_token')
    def test_missing_or_insufficient_granted_scopes_are_refused(self, fetch):
        for scopes in (None, '', 'https://www.googleapis.com/auth/calendar.readonly',
                       SCOPES[0] + ' https://www.googleapis.com/auth/gmail.modify'):
            pending = self.begin()
            fetch.return_value = token_response(scope=scopes)
            self.assertEqual(self.callback(pending).status_code, 400)
            self.assertEqual(GmailConnection.objects.count(), 0)

    def test_real_library_token_exchange_sends_pkce_over_https_with_simulated_transport(self):
        pending = self.begin()
        response = Response()
        response.status_code = 200
        response._content = json.dumps(token_response()).encode('utf-8')
        response.headers['Content-Type'] = 'application/json'
        response.request = HttpRequest('POST', 'https://oauth2.googleapis.com/token').prepare()
        with patch('requests.sessions.Session.request', return_value=response) as transport:
            result = self.callback(pending)
        self.assertEqual(result.status_code, 302)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(transport.call_args.args[0], 'POST')
        self.assertEqual(transport.call_args.args[1], 'https://oauth2.googleapis.com/token')
        data = transport.call_args.kwargs['data']
        self.assertEqual(data['code_verifier'], pending['verifier'])
        self.assertEqual(data['redirect_uri'], GOOGLE_TEST_SETTINGS['GOOGLE_REDIRECT_URI'])
        self.assertEqual(transport.call_args.kwargs['timeout'], 10)

    @patch('google_auth_oauthlib.flow.Flow.fetch_token')
    def test_reconsent_without_refresh_token_preserves_existing_connection(self, fetch):
        connection = self.existing_connection()
        original = bytes(connection.encrypted_tokens)
        fetch.return_value = token_response(refresh_token=None)
        self.assertEqual(self.callback(self.begin()).status_code, 400)
        connection.refresh_from_db()
        self.assertEqual(bytes(connection.encrypted_tokens), original)

    @patch('google_auth_oauthlib.flow.Flow.fetch_token')
    def test_new_connection_requires_a_refresh_token(self, fetch):
        fetch.return_value = token_response(refresh_token=None)
        self.assertEqual(self.callback(self.begin()).status_code, 400)
        self.assertEqual(GmailConnection.objects.count(), 0)

    @patch('google_auth_oauthlib.flow.Flow.fetch_token')
    def test_expired_or_non_bearer_tokens_are_rejected(self, fetch):
        for overrides in ({'expires_in': -1}, {'expires_at': float('inf')}, {'token_type': 'unknown'}):
            fetch.return_value = token_response(**overrides)
            self.assertEqual(self.callback(self.begin()).status_code, 400)
        self.assertEqual(GmailConnection.objects.count(), 0)

    @patch('google_auth_oauthlib.flow.Flow.fetch_token')
    def test_exchange_failure_is_generic_and_does_not_render_debug_secrets(self, fetch):
        fetch.side_effect = RuntimeError('simulated-secret-and-token')
        with override_settings(DEBUG=True):
            response = self.callback(self.begin())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b'simulated-secret-and-token', response.content)
        self.assertNotIn(b'Traceback', response.content)
        self.assertNotIn(PENDING_KEY, self.client.session)

    @patch('gmail_integration.views.exchange_code')
    def test_callback_after_logout_is_refused(self, exchange):
        pending = self.begin()
        self.client.logout()
        self.assertEqual(self.callback(pending).status_code, 401)
        exchange.assert_not_called()

    def test_callback_only_accepts_get(self):
        self.assertEqual(self.client.post(self.callback_url).status_code, 403)
        self.assertEqual(self.client.post(self.callback_url, **self.headers()).status_code, 405)

    @override_settings(DEBUG=True, ALLOWED_HOSTS=['testserver'])
    def test_framework_debug_error_response_never_reflects_callback_code(self):
        response = self.client.get(
            self.callback_url, {'code': 'simulated-private-code'}, HTTP_HOST='invalid.example',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertNotIn(b'simulated-private-code', response.content)
        self.assertNotIn(b'Traceback', response.content)
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')


@override_settings(**GOOGLE_TEST_SETTINGS)
class TokenStorageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='refresh-test')
        self.connection = GmailConnection.objects.create(
            utilisateur=self.user, encrypted_tokens=encrypt_tokens('previous-access', 'previous-refresh'),
            expires_at=timezone.now() - timedelta(seconds=1), granted_scopes=list(SCOPES),
        )
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Network disabled in tests'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def credentials(self, **overrides):
        credentials = MagicMock()
        credentials.token = 'new-access'
        credentials.refresh_token = None
        credentials.expiry = timezone.now() + timedelta(hours=1)
        credentials.granted_scopes = None
        for key, value in overrides.items():
            setattr(credentials, key, value)
        return credentials

    def test_encryption_roundtrip_and_safe_model_representation(self):
        raw = bytes(self.connection.encrypted_tokens)
        self.assertNotIn(b'previous-access', raw)
        self.assertNotIn(b'previous-refresh', raw)
        self.assertEqual(decrypt_tokens(raw), {'access_token': 'previous-access', 'refresh_token': 'previous-refresh'})
        self.assertNotIn('previous', str(self.connection))
        self.assertFalse(GmailConnection._meta.get_field('encrypted_tokens').editable)

    def test_corrupted_ciphertext_and_wrong_key_fail_closed(self):
        with self.assertRaises(OAuthFailure):
            decrypt_tokens(b'not-valid-ciphertext')
        with override_settings(GOOGLE_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode()):
            with self.assertRaises(OAuthFailure):
                decrypt_tokens(self.connection.encrypted_tokens)

    @override_settings(GOOGLE_TOKEN_ENCRYPTION_KEY='')
    def test_missing_key_is_not_silently_generated(self):
        with self.assertRaises(OAuthConfigurationError):
            encrypt_tokens('simulated-access', 'simulated-refresh')

    def test_one_to_one_relation_is_required_and_unique_per_user(self):
        field = GmailConnection._meta.get_field('utilisateur')
        self.assertTrue(field.one_to_one)
        self.assertTrue(field.unique)
        self.assertFalse(field.null)
        self.assertEqual(field.remote_field.model, get_user_model())
        with self.assertRaises(IntegrityError), transaction.atomic():
            GmailConnection.objects.create(
                utilisateur=self.user, encrypted_tokens=encrypt_tokens('other-access', 'other-refresh'),
                expires_at=timezone.now(), granted_scopes=list(SCOPES),
            )
        self.assertEqual(GmailConnection.objects.filter(utilisateur=self.user).count(), 1)

    @patch('gmail_integration.services.Credentials')
    def test_refresh_selects_only_the_supplied_users_connection(self, factory):
        other = get_user_model().objects.create_user(username='refresh-other-user')
        other_connection = GmailConnection.objects.create(
            utilisateur=other, encrypted_tokens=encrypt_tokens('other-access', 'other-refresh'),
            expires_at=timezone.now(), granted_scopes=list(SCOPES),
        )
        first_ciphertext = bytes(self.connection.encrypted_tokens)
        factory.return_value = self.credentials(token='renewed-other-access')
        result = refresh_access_token(other)
        self.assertEqual(factory.call_args.kwargs['refresh_token'], 'other-refresh')
        self.assertEqual(result.pk, other_connection.pk)
        self.assertEqual(decrypt_tokens(result.encrypted_tokens)['access_token'], 'renewed-other-access')
        self.assertEqual(decrypt_tokens(result.encrypted_tokens)['refresh_token'], 'other-refresh')
        self.connection.refresh_from_db()
        self.assertEqual(bytes(self.connection.encrypted_tokens), first_ciphertext)

    @patch('gmail_integration.services.Credentials')
    def test_user_without_connection_cannot_fall_back_to_another_users_tokens(self, factory):
        other = get_user_model().objects.create_user(username='no-gmail-connection')
        original = bytes(self.connection.encrypted_tokens)
        with self.assertRaises(GmailConnection.DoesNotExist):
            refresh_access_token(other)
        factory.assert_not_called()
        self.connection.refresh_from_db()
        self.assertEqual(bytes(self.connection.encrypted_tokens), original)

    @patch('gmail_integration.services.Credentials')
    def test_refresh_preserves_existing_refresh_token_when_google_omits_it(self, factory):
        credentials = self.credentials()
        factory.return_value = credentials
        result = refresh_access_token(self.user)
        self.assertEqual(decrypt_tokens(result.encrypted_tokens), {
            'access_token': 'new-access', 'refresh_token': 'previous-refresh',
        })
        self.assertEqual(factory.call_args.kwargs['refresh_token'], 'previous-refresh')
        self.assertIsInstance(credentials.refresh.call_args.args[0], LimitedRequest)
        self.assertEqual(result.granted_scopes, list(SCOPES))
        self.assertGreater(result.expires_at, timezone.now())

    @patch('gmail_integration.services.Credentials')
    def test_refresh_rotates_refresh_token_when_google_provides_one(self, factory):
        factory.return_value = self.credentials(refresh_token='new-refresh', granted_scopes=list(SCOPES))
        result = refresh_access_token(self.user)
        self.assertEqual(decrypt_tokens(result.encrypted_tokens)['refresh_token'], 'new-refresh')

    @patch('gmail_integration.services.Credentials')
    def test_revoked_refresh_token_requires_reauthorization_without_destroying_stored_data(self, factory):
        credentials = self.credentials()
        credentials.refresh.side_effect = RefreshError('simulated', {'error': 'invalid_grant'})
        factory.return_value = credentials
        original = bytes(self.connection.encrypted_tokens)
        with self.assertRaises(ReauthorizationRequired):
            refresh_access_token(self.user)
        self.connection.refresh_from_db()
        self.assertEqual(bytes(self.connection.encrypted_tokens), original)

    @patch('gmail_integration.services.Credentials')
    def test_refresh_with_insufficient_scope_preserves_previous_tokens(self, factory):
        factory.return_value = self.credentials(granted_scopes=[])
        original = bytes(self.connection.encrypted_tokens)
        with self.assertRaises(OAuthFailure):
            refresh_access_token(self.user)
        self.connection.refresh_from_db()
        self.assertEqual(bytes(self.connection.encrypted_tokens), original)

    @patch('gmail_integration.services.Credentials')
    def test_transport_errors_do_not_escape_as_sensitive_exception_messages(self, factory):
        credentials = self.credentials()
        credentials.refresh.side_effect = RuntimeError('simulated-sensitive-response')
        factory.return_value = credentials
        with self.assertRaises(OAuthFailure) as caught:
            refresh_access_token(self.user)
        self.assertEqual(str(caught.exception), '')


class OAuthLoggingTests(TestCase):
    def test_callback_query_exception_and_request_details_are_removed(self):
        query = urlencode({'code': 'simulated-code', 'state': 'simulated-state', 'access_token': 'simulated-token'})
        request = RequestFactory().get('/api/gmail/callback/?' + query)
        record = logging.LogRecord('django.server', logging.ERROR, __file__, 1,
                                   'GET %s', (request.get_full_path(),), None)
        record.request = request
        record.exc_text = 'simulated-secret'
        record.stack_info = 'simulated-stack-secret'
        self.assertTrue(OAuthLogFilter().filter(record))
        text = record.getMessage()
        self.assertNotIn('simulated', text)
        self.assertIsNone(record.request)
        self.assertIsNone(record.exc_text)
        self.assertIsNone(record.stack_info)

    def test_normal_request_logs_are_unchanged(self):
        record = logging.LogRecord('django.server', logging.INFO, __file__, 1, 'GET /api/candidatures/', (), None)
        OAuthLogFilter().filter(record)
        self.assertEqual(record.getMessage(), 'GET /api/candidatures/')

    def test_installed_server_logger_filter_redacts_callback_query(self):
        output = io.StringIO()
        logger = logging.getLogger('django.server')
        handler = logging.StreamHandler(output)
        logger.addHandler(handler)
        try:
            logger.info('GET /api/gmail/callback/?code=simulated-code&state=simulated-state')
        finally:
            logger.removeHandler(handler)
        self.assertNotIn('simulated', output.getvalue())
        self.assertIn('sensitive details omitted', output.getvalue())

    def test_oauth_transport_debug_logs_do_not_propagate_to_console(self):
        for name in ('oauthlib', 'requests_oauthlib', 'google.auth', 'google_auth_oauthlib', 'urllib3', 'requests'):
            logger = logging.getLogger(name)
            self.assertFalse(logger.propagate)
            self.assertTrue(any(isinstance(handler, logging.NullHandler) for handler in logger.handlers))
