import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from candidatures.models import Candidature


class SessionAuthTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = 'Only-a-test-password-8472!'
        cls.user = get_user_model().objects.create_user(username='session-test', password=cls.password)
        cls.other = get_user_model().objects.create_user(username='other-session-test')
        cls.own = Candidature.objects.create(
            utilisateur=cls.user, employeur='Employeur test', poste='Poste test',
            date_candidature='2026-10-18',
        )
        cls.foreign = Candidature.objects.create(
            utilisateur=cls.other, employeur='Autre employeur', poste='Autre poste',
            date_candidature='2026-10-18',
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.csrf_url = reverse('auth_api:csrf')
        self.login_url = reverse('auth_api:login')
        self.logout_url = reverse('auth_api:logout')
        self.me_url = reverse('auth_api:me')
        self.list_url = reverse('candidature-list')
        self.detail_url = reverse('candidature-detail', args=[self.own.pk])
        self.payload = {'employeur': 'Nouveau', 'poste': 'Poste', 'date_candidature': '2026-10-18'}

    def csrf_headers(self):
        self.client.get(self.csrf_url)
        return {'HTTP_X_CSRFTOKEN': self.client.cookies[settings.CSRF_COOKIE_NAME].value}

    def login(self):
        response = self.client.post(
            self.login_url, {'username': self.user.username, 'password': self.password},
            content_type='application/json', **self.csrf_headers(),
        )
        self.assertEqual(response.status_code, 200)
        return response

    def test_csrf_bootstrap_sets_cookie_without_exposing_token_in_body(self):
        response = self.client.get(self.csrf_url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.cookies[settings.CSRF_COOKIE_NAME].value)
        self.assertEqual(set(response.json()), {'detail'})
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertIn('no-store', response['Cache-Control'])

    def test_login_returns_safe_user_and_rotates_csrf(self):
        old_token = self.csrf_headers()['HTTP_X_CSRFTOKEN']
        response = self.login()
        self.assertEqual(response.json(), {'id': self.user.pk, 'username': self.user.username})
        self.assertTrue(response.cookies[settings.SESSION_COOKIE_NAME]['httponly'])
        self.assertEqual(response.cookies[settings.SESSION_COOKIE_NAME]['samesite'], 'Lax')
        self.assertNotEqual(self.client.cookies[settings.CSRF_COOKIE_NAME].value, old_token)
        self.assertIn('no-store', response['Cache-Control'])

    def test_me_requires_authentication(self):
        self.assertEqual(self.client.get(self.me_url).status_code, 401)

    def test_me_returns_only_public_identity_fields(self):
        self.login()
        response = self.client.get(self.me_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'id': self.user.pk, 'username': self.user.username})

    def test_invalid_credentials_have_same_generic_response(self):
        headers = self.csrf_headers()
        for username in (self.user.username, 'does-not-exist'):
            with self.subTest(username=username):
                response = self.client.post(
                    self.login_url, {'username': username, 'password': 'incorrect'},
                    content_type='application/json', **headers,
                )
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {'detail': 'Identifiants incorrects.'})
                self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)
        self.assertEqual(self.client.get(self.me_url).status_code, 401)

    def test_inactive_user_cannot_login(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        response = self.client.post(
            self.login_url, {'username': self.user.username, 'password': self.password},
            content_type='application/json', **self.csrf_headers(),
        )
        self.assertEqual(response.status_code, 401)

    def test_login_rejects_bad_json_and_missing_fields(self):
        headers = self.csrf_headers()
        for body in ('{', '[]', '{}', '{"username": 42, "password": "x"}'):
            with self.subTest(body=body):
                response = self.client.post(self.login_url, body, content_type='application/json', **headers)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post(self.login_url, {}, **headers).status_code, 415)

    def test_anonymous_login_and_logout_require_csrf(self):
        for url in (self.login_url, self.logout_url):
            with self.subTest(url=url):
                response = self.client.post(
                    url, {'username': self.user.username, 'password': self.password},
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 403)

    def test_cookie_without_csrf_header_is_rejected(self):
        self.csrf_headers()
        response = self.client.post(
            self.login_url, {'username': self.user.username, 'password': self.password},
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)

    def test_invalid_csrf_token_is_rejected(self):
        self.csrf_headers()
        response = self.client.post(
            self.login_url, {'username': self.user.username, 'password': self.password},
            content_type='application/json', HTTP_X_CSRFTOKEN='x' * 32,
        )
        self.assertEqual(response.status_code, 403)

    def test_untrusted_origin_is_rejected_even_with_valid_csrf(self):
        response = self.client.post(
            self.login_url, {'username': self.user.username, 'password': self.password},
            content_type='application/json', HTTP_ORIGIN='http://untrusted.example',
            **self.csrf_headers(),
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(ALLOWED_HOSTS=['testserver', 'localhost'])
    def test_local_react_origin_is_trusted(self):
        # Django's test runner disables DEBUG, but retains origins set at settings import.
        response = self.client.post(
            self.login_url, {'username': self.user.username, 'password': self.password},
            content_type='application/json', HTTP_HOST='localhost:8000',
            HTTP_ORIGIN='http://localhost:5173', **self.csrf_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Access-Control-Allow-Origin', response)

    def test_logout_invalidates_session(self):
        self.login()
        old_session = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        response = self.client.post(self.logout_url, **self.csrf_headers())
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b'')
        self.assertEqual(self.client.get(self.me_url).status_code, 401)
        replay = Client(enforce_csrf_checks=True)
        replay.cookies[settings.SESSION_COOKIE_NAME] = old_session
        self.assertEqual(replay.get(self.me_url).status_code, 401)

    def test_logout_without_csrf_does_not_end_session(self):
        self.login()
        self.assertEqual(self.client.post(self.logout_url).status_code, 403)
        self.assertEqual(self.client.get(self.me_url).status_code, 200)

    def test_anonymous_logout_with_csrf_is_idempotent(self):
        self.assertEqual(self.client.post(self.logout_url, **self.csrf_headers()).status_code, 204)

    def test_auth_endpoints_reject_wrong_methods(self):
        for url in (self.login_url, self.logout_url):
            self.assertEqual(self.client.get(url).status_code, 405)
        for url in (self.csrf_url, self.me_url):
            self.assertEqual(self.client.post(url, **self.csrf_headers()).status_code, 405)

    def test_session_allows_only_own_crud_data(self):
        self.login()
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in response.json()], [self.own.pk])
        foreign_url = reverse('candidature-detail', args=[self.foreign.pk])
        for method in ('get', 'put', 'patch', 'delete'):
            response = self.client.generic(
                method.upper(), foreign_url, json.dumps(self.payload), content_type='application/json',
                **self.csrf_headers(),
            )
            self.assertEqual(response.status_code, 404)

    def test_session_crud_writes_with_csrf(self):
        self.login()
        response = self.client.post(
            self.list_url, {**self.payload, 'utilisateur': self.other.pk},
            content_type='application/json', **self.csrf_headers(),
        )
        self.assertEqual(response.status_code, 201)
        created = Candidature.objects.get(pk=response.json()['id'])
        self.assertEqual(created.utilisateur, self.user)
        url = reverse('candidature-detail', args=[created.pk])
        for method in ('put', 'patch'):
            response = getattr(self.client, method)(
                url, json.dumps({**self.payload, 'etat': 'entrevue'}),
                content_type='application/json', **self.csrf_headers(),
            )
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.delete(url, **self.csrf_headers()).status_code, 204)

    def test_authenticated_crud_writes_without_csrf_are_rejected(self):
        self.login()
        for method, url in [('post', self.list_url), ('put', self.detail_url),
                            ('patch', self.detail_url), ('delete', self.detail_url)]:
            with self.subTest(method=method):
                response = getattr(self.client, method)(
                    url, json.dumps(self.payload), content_type='application/json',
                )
                self.assertEqual(response.status_code, 403)
        self.assertEqual(Candidature.objects.count(), 2)

    def test_anonymous_crud_writes_require_csrf_before_authentication(self):
        for method, url in [('post', self.list_url), ('put', self.detail_url),
                            ('patch', self.detail_url), ('delete', self.detail_url)]:
            with self.subTest(method=method):
                response = getattr(self.client, method)(
                    url, json.dumps(self.payload), content_type='application/json',
                )
                self.assertEqual(response.status_code, 403)
                self.assertIn(b'CSRF', response.content)
                response = getattr(self.client, method)(
                    url, json.dumps(self.payload), content_type='application/json',
                    **self.csrf_headers(),
                )
                self.assertEqual(response.status_code, 403)
                self.assertIn('detail', response.json())
        self.assertEqual(Candidature.objects.count(), 2)

    def test_pre_login_csrf_token_no_longer_authorizes_writes(self):
        headers = self.csrf_headers()
        self.login()
        self.assertEqual(self.client.post(self.logout_url, **headers).status_code, 403)
