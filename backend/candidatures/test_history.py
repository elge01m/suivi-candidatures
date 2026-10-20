from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from .models import Candidature, CandidatureChange


class HistoryTests(APITestCase):
    def setUp(self):
        self.user=get_user_model().objects.create_user(username='history-fiction-a')
        self.other=get_user_model().objects.create_user(username='history-fiction-b')
        self.item=Candidature.objects.create(utilisateur=self.user,employeur='Fictif',poste='Test',date_candidature='2026-10-13')
        self.foreign=Candidature.objects.create(utilisateur=self.other,employeur='Autre',poste='Test',date_candidature='2026-10-13')
        self.client.force_authenticate(self.user)
        self.url=f'/api/candidatures/{self.item.pk}/'

    def test_existing_candidature_has_no_invented_history_and_noop_stays_empty(self):
        self.assertEqual(self.client.get(self.url+'historique/').data,{'results':[]})
        self.assertEqual(self.client.patch(self.url,{'etat':'envoyee'},format='json').status_code,200)
        self.assertFalse(CandidatureChange.objects.exists())

    def test_manual_update_records_only_changed_business_values(self):
        response=self.client.patch(self.url,{'etat':'entrevue','notes':'Notes fictives'},format='json')
        self.assertEqual(response.status_code,200)
        event=CandidatureChange.objects.get()
        self.assertEqual(event.avant,{'etat':'envoyee','notes':''})
        self.assertEqual(event.apres,{'etat':'entrevue','notes':'Notes fictives'})
        self.assertEqual(event.origine,'manuelle')
        self.assertEqual(event.utilisateur_id,self.user.pk)
        self.assertIsNone(event.source_message_reference)
        self.assertEqual(self.client.get(self.url+'historique/').data['results'][0]['id'],event.pk)

    def test_manual_creation_records_actual_creation_without_owner_or_server_fields(self):
        response=self.client.post('/api/candidatures/',{'employeur':'Fiction','poste':'Test','etat':'a_preparer'},format='json')
        self.assertEqual(response.status_code,201)
        event=CandidatureChange.objects.get()
        self.assertEqual(event.avant,{})
        self.assertEqual(event.apres['etat'],'a_preparer')
        self.assertIsNone(event.apres['date_candidature'])
        self.assertIsNone(event.apres['confirmation_reception'])
        self.assertNotIn('utilisateur',event.apres)
        self.assertNotIn('date_modification',event.apres)

    def test_history_failure_rolls_back_manual_change(self):
        with patch('candidatures.views.record_change',side_effect=RuntimeError('Fictitious failure')):
            with self.assertRaises(RuntimeError):
                self.client.patch(self.url,{'etat':'entrevue'},format='json')
        self.item.refresh_from_db()
        self.assertEqual(self.item.etat,'envoyee')
        self.assertFalse(CandidatureChange.objects.exists())

    def test_history_is_read_only_and_user_scoped(self):
        foreign=f'/api/candidatures/{self.foreign.pk}/historique/'
        self.assertEqual(self.client.get(foreign).status_code,404)
        self.assertEqual(self.client.post(self.url+'historique/',{},format='json').status_code,405)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url+'historique/').status_code,403)
