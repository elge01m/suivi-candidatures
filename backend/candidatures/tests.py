from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Candidature


class CandidatureAPITests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='api-test-owner')
        cls.other_user = get_user_model().objects.create_user(username='api-test-other')
        cls.own = Candidature.objects.create(
            utilisateur=cls.user, employeur='Employeur A', poste='Poste A',
            date_candidature='2026-10-01',
        )
        cls.other = Candidature.objects.create(
            utilisateur=cls.other_user, employeur='Employeur B', poste='Poste B',
            date_candidature='2026-10-02',
        )

    def setUp(self):
        self.client.force_authenticate(user=self.user)
        self.list_url = reverse('candidature-list')
        self.detail_url = reverse('candidature-detail', args=[self.own.pk])
        self.other_url = reverse('candidature-detail', args=[self.other.pk])
        self.payload = {
            'employeur': 'Nouvel employeur', 'poste': 'Nouveau poste',
            'date_candidature': '2026-10-18',
            'lien': 'https://example.com/offre', 'notes': 'Notes de test',
        }

    def test_routes(self):
        self.assertEqual(self.list_url, '/api/candidatures/')
        self.assertEqual(self.detail_url, f'/api/candidatures/{self.own.pk}/')

    def test_list_only_own_applications_in_date_order(self):
        newest = Candidature.objects.create(
            utilisateur=self.user, employeur='Recent', poste='Poste',
            date_candidature='2026-10-18',
        )
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item['id'] for item in response.data], [newest.pk, self.own.pk])

    def test_retrieve_own_application(self):
        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['employeur'], self.own.employeur)
        self.assertEqual(response.data['utilisateur'], self.user.pk)
        self.assertIn('date_creation', response.data)
        self.assertIn('date_modification', response.data)

    def test_create_assigns_authenticated_user_and_default_state(self):
        response = self.client.post(self.list_url, self.payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        created = Candidature.objects.get(pk=response.data['id'])
        self.assertEqual(created.utilisateur, self.user)
        self.assertEqual(created.etat, Candidature.Etat.ENVOYEE)
        self.assertEqual(created.notes, self.payload['notes'])
        self.assertEqual(created.lien, self.payload['lien'])

    def test_create_cannot_assign_another_owner(self):
        response = self.client.post(
            self.list_url, {**self.payload, 'utilisateur': self.other_user.pk}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Candidature.objects.get(pk=response.data['id']).utilisateur, self.user)

    def test_optional_fields_may_be_omitted(self):
        payload = {key: value for key, value in self.payload.items() if key not in {'lien', 'notes'}}
        response = self.client.post(self.list_url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['lien'], '')
        self.assertEqual(response.data['notes'], '')

    def test_replace_own_application(self):
        response = self.client.put(
            self.detail_url, {**self.payload, 'etat': 'entrevue'}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.own.refresh_from_db()
        self.assertEqual(self.own.employeur, self.payload['employeur'])
        self.assertEqual(self.own.etat, Candidature.Etat.ENTREVUE)

    def test_patch_own_application(self):
        response = self.client.patch(self.detail_url, {'notes': 'Notes modifiees'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.own.refresh_from_db()
        self.assertEqual(self.own.notes, 'Notes modifiees')
        self.assertEqual(self.own.employeur, 'Employeur A')

    def test_owner_and_timestamps_cannot_be_overwritten(self):
        original_creation = self.own.date_creation
        for method in ('put', 'patch'):
            with self.subTest(method=method):
                response = getattr(self.client, method)(
                    self.detail_url,
                    {**self.payload, 'utilisateur': self.other_user.pk,
                     'date_creation': '2000-01-01T00:00:00Z',
                     'date_modification': '2000-01-01T00:00:00Z'},
                    format='json',
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.own.refresh_from_db()
                self.assertEqual(self.own.utilisateur, self.user)
                self.assertEqual(self.own.date_creation, original_creation)
                self.assertNotEqual(self.own.date_modification.year, 2000)

    def test_delete_own_application(self):
        response = self.client.delete(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Candidature.objects.filter(pk=self.own.pk).exists())
        self.assertTrue(Candidature.objects.filter(pk=self.other.pk).exists())

    def test_anonymous_requests_are_denied_for_every_operation(self):
        self.client.force_authenticate(user=None)
        operations = [
            ('get', self.list_url), ('post', self.list_url),
            ('get', self.detail_url), ('put', self.detail_url),
            ('patch', self.detail_url), ('delete', self.detail_url),
        ]
        for method, url in operations:
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url, self.payload, format='json')
                # DRF's default SessionAuthentication returns 403 to anonymous clients.
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Candidature.objects.count(), 2)

    def test_other_users_application_is_hidden_for_every_detail_operation(self):
        for method in ('get', 'put', 'patch', 'delete'):
            with self.subTest(method=method):
                response = getattr(self.client, method)(self.other_url, self.payload, format='json')
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.other.refresh_from_db()
        self.assertEqual(self.other.employeur, 'Employeur B')
        self.assertEqual(self.other.utilisateur, self.other_user)

    def test_isolation_is_symmetric(self):
        self.client.force_authenticate(user=self.other_user)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item['id'] for item in response.data], [self.other.pk])
        for method in ('get', 'put', 'patch', 'delete'):
            with self.subTest(method=method):
                response = getattr(self.client, method)(self.detail_url, self.payload, format='json')
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.own.refresh_from_db()
        self.assertEqual(self.own.employeur, 'Employeur A')

    def test_invalid_payloads_are_rejected_without_creating_data(self):
        invalid_payloads = [
            ({}, {'employeur', 'poste', 'date_candidature'}),
            ({**self.payload, 'etat': 'inconnu'}, {'etat'}),
            ({**self.payload, 'date_candidature': 'invalide'}, {'date_candidature'}),
            ({**self.payload, 'employeur': 'x' * 201}, {'employeur'}),
            ({**self.payload, 'poste': 'x' * 201}, {'poste'}),
            ({**self.payload, 'lien': 'pas-un-lien'}, {'lien'}),
        ]
        for payload, errors in invalid_payloads:
            with self.subTest(fields=list(errors)):
                response = self.client.post(self.list_url, payload, format='json')
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertTrue(errors.issubset(response.data))
        self.assertEqual(Candidature.objects.count(), 2)

    def test_preparation_without_date_and_transition_requires_explicit_date(self):
        response = self.client.post(self.list_url, {**self.payload, 'etat': 'a_preparer', 'date_candidature': None}, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data['date_candidature'])
        self.assertIsNone(response.data['confirmation_reception'])
        url = reverse('candidature-detail', args=[response.data['id']])
        response = self.client.patch(url, {'etat': 'envoyee'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('date_candidature', response.data)
        response = self.client.patch(url, {'etat': 'envoyee', 'date_candidature': '2026-10-13'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data['confirmation_reception'])

    def test_all_eight_states_allow_explicit_human_transitions_and_preserve_receipt(self):
        self.assertEqual(len(Candidature.Etat), 8)
        for before in Candidature.Etat.values:
            for after in Candidature.Etat.values:
                with self.subTest(before=before, after=after):
                    Candidature.objects.filter(pk=self.own.pk).update(etat=before, confirmation_reception=None)
                    response = self.client.patch(self.detail_url, {'etat': after}, format='json')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.data['date_candidature'], '2026-10-01')
                    self.assertIsNone(response.data['confirmation_reception'])

    def test_confirmation_is_independent_and_does_not_modify_other_fields(self):
        before = Candidature.objects.values().get(pk=self.own.pk)
        for value in (True, False, None):
            response = self.client.patch(self.detail_url, {'confirmation_reception': value}, format='json')
            self.assertEqual(response.status_code, 200)
            after = Candidature.objects.values().get(pk=self.own.pk)
            self.assertEqual(after['confirmation_reception'], value)
            for field in before.keys() - {'confirmation_reception', 'date_modification'}:
                self.assertEqual(after[field], before[field])
        self.client.patch(self.detail_url, {'confirmation_reception': True}, format='json')
        self.assertEqual(self.client.patch(self.detail_url, {'etat': 'entrevue'}, format='json').data['confirmation_reception'], True)

    def test_preparation_cannot_be_received_and_rules_exist_in_model_and_database(self):
        self.assertEqual(self.client.patch(self.detail_url, {'etat': 'a_preparer', 'confirmation_reception': True}, format='json').status_code, 400)
        for values in ({'etat': 'envoyee', 'date_candidature': None}, {'etat': 'a_preparer', 'confirmation_reception': True}):
            with self.subTest(values=values):
                item = Candidature(utilisateur=self.user, employeur='Fictif', poste='Test', date_candidature='2026-10-13')
                for field, value in values.items():
                    setattr(item, field, value)
                with self.assertRaises(ValidationError):
                    item.full_clean()
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Candidature.objects.filter(pk=self.own.pk).update(**values)

    def test_undated_preparation_sorts_last_and_invalid_dates_are_rejected(self):
        draft = Candidature.objects.create(utilisateur=self.user, employeur='Fictif', poste='Test', etat='a_preparer')
        response = self.client.get(self.list_url)
        self.assertEqual(response.data[-1]['id'], draft.pk)
        for value in ('2026-02-30', '0000-01-01', '2026-13-01'):
            self.assertEqual(self.client.patch(self.detail_url, {'date_candidature': value}, format='json').status_code, 400)

    def test_confirmation_cannot_be_written_by_another_user_or_without_csrf(self):
        self.assertEqual(self.client.patch(self.other_url, {'confirmation_reception': True}, format='json').status_code, 404)
        from rest_framework.test import APIClient
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.patch(self.detail_url, {'confirmation_reception': True}, format='json').status_code, 403)
        self.own.refresh_from_db()
        self.assertIsNone(self.own.confirmation_reception)
