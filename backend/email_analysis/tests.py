"""Fictitious data only. Socket calls are forbidden throughout this test module."""
import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from google.genai import errors, types
from pydantic import ValidationError

from candidatures.models import Candidature, CandidatureChange
from gmail_integration.message_crypto import decrypt_message_data, encrypt_message_data
from gmail_integration.models import GmailConnection, GmailMessage
from .errors import AnalysisProblem
from .gemini import ModelResult, extract
from .models import AnalysisBudget, CandidatureProposal, EmailAnalysis
from .logging import AnalysisLogFilter
from .schemas import Extraction
from .services import (accept_proposal, analysis_for, analyze_message, correct_proposal,
                       proposal_data, proposal_for, reject_proposal)


KEY = Fernet.generate_key().decode()
TOKEN_KEY = Fernet.generate_key().decode()
FACTS = dict(pertinent=True, action='creation', employeur='Entreprise Fictive', poste='Poste de test',
             date_candidature='2026-10-12', etat='envoyee', justification='Accusé fictif.', informations_manquantes=[])


@override_settings(GEMINI_API_KEY='fictitious-key-not-real', GMAIL_MESSAGE_ENCRYPTION_KEY=KEY,
                   GOOGLE_TOKEN_ENCRYPTION_KEY=TOKEN_KEY, GEMINI_REQUESTS_PER_MINUTE=100,
                   GEMINI_DAILY_REQUEST_LIMIT=100, GEMINI_DAILY_TOKEN_LIMIT=10000000,
                   GEMINI_TOKENS_PER_MINUTE=10000000)
class Base(TestCase):
    def setUp(self):
        self.network = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden in tests'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.user = get_user_model().objects.create_user(username='fictitious-a')
        self.other = get_user_model().objects.create_user(username='fictitious-b')
        self.connection = GmailConnection.objects.create(utilisateur=self.user, encrypted_tokens=b'fake', expires_at=timezone.now())
        self.other_connection = GmailConnection.objects.create(utilisateur=self.other, encrypted_tokens=b'fake', expires_at=timezone.now())
        self.message = self.make_message(self.connection, 'fictitious1')
        self.other_message = self.make_message(self.other_connection, 'fictitious2')

    def make_message(self, connection, gmail_id):
        return GmailMessage.objects.create(connexion=connection, gmail_id=gmail_id, thread_id='fiction',
            date_gmail=timezone.now(), encrypted_content=encrypt_message_data({
                'subject': 'Candidature fictive', 'text': 'Contenu entièrement fictif.',
                'from': 'person@example.invalid', 'to': ['other@example.invalid'],
            }))

    def provider(self, **changes):
        return Mock(return_value=ModelResult(Extraction.model_validate(FACTS | changes), 40, 20))

    def run_analysis(self, message=None, user=None, **changes):
        # Tests exercising several requests advance only the local quota timer.
        AnalysisBudget.objects.update(next_request_at=timezone.now() - timedelta(seconds=1))
        return analyze_message(user or self.user, (message or self.message).pk, consent=True, provider=self.provider(**changes))

    def proposal(self, **changes):
        return self.run_analysis(**changes).proposal

    def candidature(self, user=None, **changes):
        values = dict(employeur=FACTS['employeur'], poste=FACTS['poste'], date_candidature=date(2026, 10, 12), etat='envoyee') | changes
        return Candidature.objects.create(utilisateur=user or self.user, **values)

    def problem(self, code, func, *args, **kwargs):
        with self.assertRaises(AnalysisProblem) as caught:
            func(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)


class ReceiptAndPreparationTests(Base):
    def saved_target(self, item, **changes):
        proposal = self.proposal(action='confirmation_reception', etat=None, date_candidature=None,
                                 confirmation_reception=True, **changes)
        content = proposal_data(self.user, proposal.pk)['proposition']
        self.assertIsNone(content['cible_id'])
        self.assertEqual(proposal.status, 'en_attente')
        return correct_proposal(self.user, proposal.pk, {
            'action': content['action'], 'etat': content['etat'],
            'confirmation_reception': content['confirmation_reception'], 'cible_id': item.pk,
        }, proposal.revision)

    def test_confirmation_requires_target_consent_and_acceptance_and_preserves_state(self):
        item = self.candidature()
        before = Candidature.objects.values().get(pk=item.pk)
        proposal = self.saved_target(item)
        item.refresh_from_db()
        self.assertIsNone(item.confirmation_reception)
        self.problem('invalid_request', accept_proposal, self.user, proposal.pk, proposal.revision)
        accepted = accept_proposal(self.user, proposal.pk, proposal.revision, confirm=True)
        after = Candidature.objects.values().get(pk=item.pk)
        self.assertTrue(after['confirmation_reception'])
        for field in before.keys() - {'confirmation_reception', 'date_modification'}:
            self.assertEqual(before[field], after[field])
        self.assertEqual(accepted.result_id, item.pk)
        # Retrying an accepted decision never applies another write.
        accept_proposal(self.user, proposal.pk, proposal.revision, confirm=True)
        self.assertEqual(after, Candidature.objects.values().get(pk=item.pk))

    def test_receipt_unknown_never_assumed_and_other_users_cannot_target_or_decide(self):
        proposal = self.proposal(action='confirmation_reception', etat=None, confirmation_reception=None)
        self.problem('target_required', accept_proposal, self.user, proposal.pk, 1, confirm=True)
        other_item = self.candidature(user=self.other)
        self.problem('not_found', correct_proposal, self.user, proposal.pk,
                     {'action':'confirmation_reception', 'cible_id':other_item.pk, 'confirmation_reception':True}, 1)
        item = self.candidature()
        proposal = correct_proposal(self.user, proposal.pk, {'action':'confirmation_reception', 'cible_id':item.pk}, 1)
        self.problem('missing_fields', accept_proposal, self.user, proposal.pk, 2, confirm=True)
        self.problem('not_found', accept_proposal, self.other, proposal.pk, 2, confirm=True)

    def test_receipt_on_preparation_rejected_without_changing_pending_proposal(self):
        item = self.candidature(etat='a_preparer', date_candidature=None)
        proposal = self.saved_target(item)
        self.problem('invalid_fields', accept_proposal, self.user, proposal.pk, proposal.revision, confirm=True)
        item.refresh_from_db(); proposal.refresh_from_db()
        self.assertEqual(item.etat, 'a_preparer')
        self.assertIsNone(item.confirmation_reception)
        self.assertEqual(proposal.status, 'en_attente')

    def test_preparation_can_be_created_without_inventing_date_or_confirmation_and_not_twice(self):
        proposal = self.proposal(etat='a_preparer', date_candidature=None)
        accepted = accept_proposal(self.user, proposal.pk, 1, confirm=True)
        item = accepted.result
        self.assertEqual(item.etat, 'a_preparer')
        self.assertIsNone(item.date_candidature)
        self.assertIsNone(item.confirmation_reception)
        second = self.run_analysis(self.make_message(self.connection, 'fictitious-preparation-duplicate'), etat='a_preparer', date_candidature=None).proposal
        self.problem('duplicate_creation', accept_proposal, self.user, second.pk, 1, confirm=True)
        self.assertEqual(Candidature.objects.count(), 1)

    def test_leaving_preparation_requires_human_date_and_preserves_it_later(self):
        item = self.candidature(etat='a_preparer', date_candidature=None)
        proposal = self.proposal(action='changement_etat', etat='envoyee', date_candidature=None)
        proposal = correct_proposal(self.user, proposal.pk, {'action':'changement_etat', 'etat':'envoyee', 'cible_id':item.pk}, 1)
        self.problem('invalid_fields', accept_proposal, self.user, proposal.pk, 2, confirm=True)
        proposal = correct_proposal(self.user, proposal.pk, {'action':'changement_etat', 'etat':'envoyee', 'cible_id':item.pk, 'date_candidature':'2026-10-13'}, 2)
        accept_proposal(self.user, proposal.pk, 3, confirm=True)
        item.refresh_from_db()
        self.assertEqual(item.date_candidature, date(2026, 10, 13))
        self.assertIsNone(item.confirmation_reception)

    def test_state_proposal_applies_explicit_receipt_only_after_acceptance_and_preserves_date(self):
        item = self.candidature()
        proposal = self.proposal(action='changement_etat', etat='entrevue', confirmation_reception=True)
        proposal = correct_proposal(self.user, proposal.pk, {'action':'changement_etat', 'etat':'entrevue', 'cible_id':item.pk,
            'date_candidature':'2026-10-22', 'confirmation_reception':True}, 1)
        accept_proposal(self.user, proposal.pk, 2, confirm=True)
        item.refresh_from_db()
        self.assertEqual(item.etat, 'entrevue')
        self.assertEqual(item.date_candidature, date(2026, 10, 12))
        self.assertTrue(item.confirmation_reception)

    def test_unknown_receipt_on_state_change_preserves_existing_confirmation(self):
        item = self.candidature(confirmation_reception=True)
        proposal = self.proposal(action='changement_etat', etat='entrevue')
        proposal = correct_proposal(self.user, proposal.pk, {'action':'changement_etat', 'etat':'entrevue', 'cible_id':item.pk}, 1)
        accept_proposal(self.user, proposal.pk, 2, confirm=True)
        item.refresh_from_db()
        self.assertTrue(item.confirmation_reception)
        self.assertEqual(item.etat, 'entrevue')

    def test_legacy_encrypted_proposal_remains_readable_unchanged_and_analysis_not_repeated(self):
        proposal = self.proposal()
        content = decrypt_message_data(proposal.encrypted_data)
        content.pop('confirmation_reception')
        ciphertext = encrypt_message_data(content)
        CandidatureProposal.objects.filter(pk=proposal.pk).update(encrypted_data=ciphertext)
        self.assertIsNone(proposal_data(self.user, proposal.pk)['proposition']['confirmation_reception'])
        proposal.refresh_from_db()
        self.assertEqual(bytes(proposal.encrypted_data), ciphertext)
        self.assertEqual(proposal.revision, 1)
        provider = self.provider()
        cached = analyze_message(self.user, self.message.pk, consent=True, provider=provider)
        provider.assert_not_called()
        self.assertEqual(cached.pk, proposal.analysis_id)
        accept_proposal(self.user, proposal.pk, 1, confirm=True)
        self.assertIsNone(Candidature.objects.get().confirmation_reception)

    def test_strict_receipt_schema_and_eight_states_with_mocked_provider_only(self):
        for state in Candidature.Etat.values:
            result = Extraction.model_validate(FACTS | {'etat':state})
            self.assertIsNone(result.confirmation_reception)
        for bad in (1, 'true', 'oui'):
            with self.assertRaises(ValidationError):
                Extraction.model_validate(FACTS | {'confirmation_reception':bad})
        with self.assertRaises(ValidationError):
            Extraction.model_validate(FACTS | {'etat':'confirmation_recue'})

    def test_receipt_api_requires_csrf_and_preserves_owner_isolation(self):
        item = self.candidature()
        proposal = self.proposal(action='confirmation_reception', etat=None, confirmation_reception=True)
        url = f'/api/propositions/{proposal.pk}/'
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        correction = {'revision':1, 'proposition':{'action':'confirmation_reception', 'confirmation_reception':True, 'cible_id':item.pk}}
        self.assertEqual(client.patch(url, json.dumps(correction), content_type='application/json').status_code, 403)
        client.get('/api/auth/csrf/')
        headers = {'HTTP_X_CSRFTOKEN':client.cookies['csrftoken'].value}
        response = client.patch(url, json.dumps(correction), content_type='application/json', **headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['proposition']['confirmation_reception'])
        self.assertEqual(client.post(url + 'accept/', json.dumps({'revision':2, 'confirmer':True}), content_type='application/json').status_code, 403)
        item.refresh_from_db()
        self.assertIsNone(item.confirmation_reception)
        response = client.post(url + 'accept/', json.dumps({'revision':2, 'confirmer':True}), content_type='application/json', **headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'acceptee')
        item.refresh_from_db()
        self.assertTrue(item.confirmation_reception)
        self.assertEqual(item.etat, 'envoyee')
        client.force_login(self.other)
        self.assertEqual(client.get(url).status_code, 404)


class SentMessageAndAssociationTests(Base):
    def context_message(self, sent):
        self.message.date_gmail = datetime(2026,10,14,2,0,tzinfo=dt_timezone.utc)
        self.message.encrypted_content = encrypt_message_data({
            'subject':'Candidature fictive', 'text':'Je soumets ma candidature au poste de Test chez Entreprise Fictive.',
            'sent':sent, 'header_date':'untrusted-date',
            'from':[{'name':'Expéditeur fictif','address':'sender@example.invalid'}],
            'to':[{'name':'Entreprise Fictive','address':'recruitment@example.invalid'}],
            'cc':[{'name':'Private CC','address':'private@example.invalid'}],
        })
        self.message.save()

    def test_sent_context_uses_trusted_date_and_toronto_day_without_addresses_or_dossier(self):
        self.context_message(True)
        self.candidature(employeur='Private existing candidature')
        provider = self.provider(date_candidature='2026-10-13', confirmation_reception=None)
        job = analyze_message(self.user, self.message.pk, consent=True, provider=provider)
        payload = json.loads(provider.call_args.args[0])
        self.assertEqual(payload['sens'], 'envoye')
        self.assertEqual(payload['date_message'], '2026-10-14T02:00:00+00:00')
        self.assertEqual(payload['date_locale_message'], '2026-10-13')
        self.assertEqual(payload['fuseau'], 'America/Toronto')
        self.assertEqual(payload['noms_correspondants'], ['Entreprise Fictive'])
        raw = provider.call_args.args[0]
        for excluded in ('@example.invalid','Private CC','Private existing candidature','untrusted-date','fictitious1'):
            self.assertNotIn(excluded, raw)
        data = proposal_data(self.user, job.proposal.pk)['proposition']
        self.assertEqual(data['action'], 'creation')
        self.assertEqual(data['etat'], 'envoyee')
        self.assertIsNone(data['confirmation_reception'])
        self.assertIsNone(data['cible_id'])
        self.assertEqual(Candidature.objects.count(), 1)

    def test_sent_application_without_acknowledgment_requires_human_acceptance(self):
        self.context_message(True)
        job = analyze_message(self.user, self.message.pk, consent=True,
            provider=self.provider(date_candidature='2026-10-13', etat='envoyee', confirmation_reception=None))
        self.assertFalse(Candidature.objects.exists())
        proposal = job.proposal
        self.assertEqual(proposal.status, 'en_attente')
        self.problem('invalid_request', accept_proposal, self.user, proposal.pk, 1)
        accepted = accept_proposal(self.user, proposal.pk, 1, confirm=True)
        self.assertEqual(accepted.result.date_candidature, date(2026,10,13))
        self.assertEqual(accepted.result.etat, 'envoyee')
        self.assertIsNone(accepted.result.confirmation_reception)

    def test_received_interview_does_not_invent_application_date(self):
        self.context_message(False)
        provider = self.provider(action='changement_etat', etat='entrevue', date_candidature=None)
        job = analyze_message(self.user, self.message.pk, consent=True, provider=provider)
        payload = json.loads(provider.call_args.args[0])
        self.assertEqual(payload['sens'], 'recu')
        self.assertEqual(payload['noms_correspondants'], ['Expéditeur fictif'])
        self.assertIsNone(proposal_data(self.user, job.proposal.pk)['proposition']['date_candidature'])
        self.assertFalse(Candidature.objects.exists())

    def test_legacy_direction_unknown_and_followup_does_not_force_creation(self):
        provider = self.provider(action='aucune', etat=None, date_candidature=None)
        job = analyze_message(self.user, self.message.pk, consent=True, provider=provider)
        payload = json.loads(provider.call_args.args[0])
        self.assertEqual(payload['sens'], 'inconnu')
        self.assertEqual(payload['noms_correspondants'], [])
        self.assertEqual(proposal_data(self.user, job.proposal.pk)['proposition']['action'], 'aucune')
        self.assertFalse(Candidature.objects.exists())

    def test_unique_exact_association_is_probable_but_never_preselected(self):
        item = self.candidature()
        self.candidature(user=self.other)
        p = self.proposal(action='changement_etat')
        data = proposal_data(self.user, p.pk)['proposition']
        self.assertEqual(data['niveau_association'], 'probable')
        self.assertEqual(data['candidatures_possibles'], [item.pk])
        self.assertIsNone(data['cible_id'])
        self.problem('target_required', accept_proposal, self.user, p.pk, 1, confirm=True)

    def test_partial_ambiguous_and_no_matches_and_explicit_other_target(self):
        item = self.candidature(poste='Autre poste')
        p = self.proposal(action='changement_etat', etat='entrevue')
        before = bytes(p.encrypted_data)
        self.assertEqual(proposal_data(self.user, p.pk)['proposition']['niveau_association'], 'a_confirmer')
        second = self.candidature()
        self.assertEqual(proposal_data(self.user, p.pk)['proposition']['niveau_association'], 'probable')
        self.assertEqual(proposal_data(self.user, p.pk)['proposition']['candidatures_possibles'], [second.pk])
        self.assertIsNone(proposal_data(self.user, p.pk)['proposition']['cible_id'])
        other = self.candidature(employeur='Sans correspondance', poste='Distinct')
        p = correct_proposal(self.user, p.pk, {'action':'changement_etat', 'etat':'entrevue', 'cible_id':other.pk}, 1)
        self.assertEqual(proposal_data(self.user, p.pk)['proposition']['cible_id'], other.pk)
        self.assertIsNone(CandidatureProposal.objects.get(pk=p.pk).result_id)
        item.delete(); second.delete()
        data = proposal_data(self.user, p.pk)['proposition']
        self.assertEqual(data['niveau_association'], 'aucune')
        self.assertEqual(data['cible_id'], other.pk)
        self.assertNotEqual(bytes(p.encrypted_data), before)  # Only explicit correction rewrites it.

    def test_exact_pair_excludes_other_post_and_other_employer(self):
        exact = self.candidature()
        self.candidature(poste='Autre poste')
        self.candidature(employeur='Autre employeur')
        self.candidature(user=self.other)
        p = self.proposal(action='changement_etat', employeur=FACTS['employeur'].lower(), poste=FACTS['poste'].upper())
        data = proposal_data(self.user, p.pk)['proposition']
        self.assertEqual(data['candidatures_possibles'], [exact.pk])
        self.assertEqual(data['niveau_association'], 'probable')
        self.assertFalse(data['ambiguite'])
        self.assertIsNone(data['cible_id'])

    def test_multiple_exact_pairs_remain_ambiguous_without_partial_matches(self):
        first = self.candidature()
        second = self.candidature(date_candidature=date(2026,10,13))
        self.candidature(poste='Autre poste')
        p = self.proposal(action='changement_etat')
        data = proposal_data(self.user, p.pk)['proposition']
        self.assertCountEqual(data['candidatures_possibles'], [first.pk, second.pk])
        self.assertEqual(data['niveau_association'], 'ambigu')
        self.assertIsNone(data['cible_id'])
        self.problem('target_required', accept_proposal, self.user, p.pk, 1, confirm=True)

    def test_foreign_exact_pair_does_not_hide_owned_partial_fallback(self):
        owned = self.candidature(poste='Autre poste')
        self.candidature(user=self.other)
        p = self.proposal(action='changement_etat')
        data = proposal_data(self.user, p.pk)['proposition']
        self.assertEqual(data['candidatures_possibles'], [owned.pk])
        self.assertEqual(data['niveau_association'], 'a_confirmer')
        self.assertIsNone(data['cible_id'])

    def test_entity_names_match_without_rewriting_original_extraction(self):
        item = self.candidature(employeur='Boréal Fictif', poste='Développeur logiciel')
        self.candidature(employeur='Boréal Fictif', poste='Autre poste')
        self.candidature(user=self.other, employeur='Boréal Fictif', poste='Développeur logiciel')
        variants = [
            ('Bor&#233;al Fictif', 'D&#233;veloppeur logiciel'),
            ('Bor&eacute;al Fictif', 'D&eacute;veloppeur logiciel'),
            ('Bor&#xE9;al  Fictif', 'De\u0301veloppeur logiciel'),
        ]
        for index, (employeur, poste) in enumerate(variants):
            with self.subTest(index=index):
                p = self.run_analysis(self.make_message(self.connection, f'entity-{index}'),
                    action='changement_etat', employeur=employeur, poste=poste).proposal
                original = (bytes(p.encrypted_data), p.revision)
                data = proposal_data(self.user, p.pk)['proposition']
                self.assertEqual(data['employeur'], employeur)
                self.assertEqual(data['poste'], poste)
                self.assertEqual(data['candidatures_possibles'], [item.pk])
                self.assertEqual(data['niveau_association'], 'probable')
                self.assertIsNone(data['cible_id'])
                self.problem('target_required', accept_proposal, self.user, p.pk, 1, confirm=True)
                p.refresh_from_db()
                self.assertEqual((bytes(p.encrypted_data), p.revision), original)

    def test_entity_normalization_keeps_multiple_exact_pairs_ambiguous(self):
        first = self.candidature(employeur='Boréal Fictif', poste='Développeur logiciel')
        second = self.candidature(employeur='Boréal Fictif', poste='Développeur logiciel',date_candidature=date(2026,10,13))
        self.candidature(employeur='Boréal Fictif', poste='Autre poste')
        p = self.proposal(action='changement_etat', employeur='Bor&#233;al Fictif', poste='D&eacute;veloppeur logiciel')
        data = proposal_data(self.user, p.pk)['proposition']
        self.assertCountEqual(data['candidatures_possibles'], [first.pk, second.pk])
        self.assertTrue(data['ambiguite'])
        self.assertEqual(data['niveau_association'], 'ambigu')
        self.assertIsNone(data['cible_id'])

    def test_normalization_preserves_accents_and_does_not_strip_markup(self):
        self.candidature(employeur='Boréal Fictif', poste='Développeur logiciel')
        p = self.proposal(action='changement_etat', employeur='Boreal Fictif', poste='Autre poste')
        self.assertEqual(proposal_data(self.user, p.pk)['proposition']['candidatures_possibles'], [])
        other = self.run_analysis(self.make_message(self.connection, 'markup-literal'),
            action='changement_etat', employeur='&lt;b&gt;Boréal Fictif&lt;/b&gt;', poste='Autre poste').proposal
        self.assertEqual(proposal_data(self.user, other.pk)['proposition']['candidatures_possibles'], [])

    def test_unknown_update_date_preserves_target_date_but_creation_still_requires_date(self):
        item = self.candidature()
        p = self.proposal(action='changement_etat', etat='entrevue', date_candidature=None)
        p = correct_proposal(self.user, p.pk, {'action':'changement_etat', 'etat':'entrevue',
            'date_candidature':None, 'cible_id':item.pk}, 1)
        data = proposal_data(self.user, p.pk)['proposition']
        self.assertIsNone(data['date_candidature'])
        self.assertNotIn('date_candidature', data['informations_manquantes'])
        accept_proposal(self.user, p.pk, 2, confirm=True)
        item.refresh_from_db()
        self.assertEqual(item.date_candidature, date(2026,10,12))
        self.assertEqual(item.etat, 'entrevue')
        creation = self.run_analysis(self.make_message(self.connection, 'new-creation-missing-date'),
            date_candidature=None, informations_manquantes=['date_candidature']).proposal
        self.problem('missing_fields', accept_proposal, self.user, creation.pk, 1, confirm=True)
        self.assertEqual(Candidature.objects.count(), 1)

    def test_recomputed_suggestions_do_not_rewrite_accepted_proposals_or_repeat_analysis(self):
        item = self.candidature()
        p = self.proposal(action='changement_etat', etat='entrevue')
        p = correct_proposal(self.user, p.pk, {'action':'changement_etat', 'etat':'entrevue', 'cible_id':item.pk,
            'employeur':FACTS['employeur'], 'poste':FACTS['poste']}, 1)
        accept_proposal(self.user, p.pk, 2, confirm=True)
        p.refresh_from_db()
        before = (bytes(p.encrypted_data), p.revision, p.status, p.decided_at, p.result_id)
        self.candidature()
        self.assertEqual(proposal_data(self.user, p.pk)['proposition']['niveau_association'], 'ambigu')
        provider = self.provider()
        cached = analyze_message(self.user, self.message.pk, consent=True, provider=provider)
        provider.assert_not_called()
        self.assertEqual(cached.pk, p.analysis_id)
        p.refresh_from_db()
        self.assertEqual((bytes(p.encrypted_data), p.revision, p.status, p.decided_at, p.result_id), before)

    def test_context_respects_configured_timezone_and_caps_names_without_email_fallback(self):
        from gmail_sync.models import SyncConfiguration
        SyncConfiguration.objects.create(connexion=self.connection, timezone='UTC')
        self.context_message(True)
        data = decrypt_message_data(self.message.encrypted_content)
        data['to'] = [{'name':'hidden@example.invalid','address':'private@example.invalid'},
                      {'address':'no-name@example.invalid'}, {'name':'x'*300}] + [{'name':'Fiction'}]*20
        self.message.encrypted_content = encrypt_message_data(data)
        self.message.save()
        provider = self.provider()
        analyze_message(self.user, self.message.pk, consent=True, provider=provider)
        payload = json.loads(provider.call_args.args[0])
        self.assertEqual(payload['date_locale_message'], '2026-10-14')
        self.assertLessEqual(len(payload['noms_correspondants']), 10)
        self.assertTrue(all(len(value)<=200 for value in payload['noms_correspondants']))
        self.assertNotIn('@', json.dumps(payload))


class ProposalHistoryTests(Base):
    def test_accepted_state_change_history_survives_source_deletion_and_is_not_repeated(self):
        item=self.candidature()
        proposal=self.proposal(action='changement_etat',etat='entrevue')
        proposal=correct_proposal(self.user,proposal.pk,{'action':'changement_etat','etat':'entrevue','cible_id':item.pk},1)
        self.assertFalse(CandidatureChange.objects.exists())
        accept_proposal(self.user,proposal.pk,2,confirm=True)
        event=CandidatureChange.objects.get()
        self.assertEqual(event.avant,{'etat':'envoyee'})
        self.assertEqual(event.apres,{'etat':'entrevue'})
        self.assertEqual(event.origine,'proposition')
        self.assertEqual(event.source_message_reference,self.message.pk)
        self.assertEqual(event.source_proposition_reference,proposal.pk)
        self.assertNotIn('subject',event.apres)
        accept_proposal(self.user,proposal.pk,2,confirm=True)
        self.assertEqual(CandidatureChange.objects.count(),1)
        reference=self.message.pk
        self.message.delete()
        event.refresh_from_db();item.refresh_from_db()
        self.assertIsNone(event.source_message_id)
        self.assertEqual(event.source_message_reference,reference)
        self.assertEqual(event.source_proposition_reference,proposal.pk)
        self.assertEqual(item.etat,'entrevue')

    def test_rejection_and_failed_acceptance_create_no_history(self):
        proposal=self.proposal(action='changement_etat',etat='entrevue')
        self.problem('target_required',accept_proposal,self.user,proposal.pk,1,confirm=True)
        reject_proposal(self.user,proposal.pk,1,confirm=True)
        self.assertFalse(CandidatureChange.objects.exists())

    def test_history_failure_rolls_back_proposal_acceptance_and_candidature(self):
        item=self.candidature()
        proposal=self.proposal(action='changement_etat',etat='entrevue')
        proposal=correct_proposal(self.user,proposal.pk,{'action':'changement_etat','etat':'entrevue','cible_id':item.pk},1)
        with patch('email_analysis.services.record_change',side_effect=RuntimeError('Fictitious history failure')):
            with self.assertRaises(RuntimeError):
                accept_proposal(self.user,proposal.pk,2,confirm=True)
        proposal.refresh_from_db();item.refresh_from_db()
        self.assertEqual(proposal.status,'en_attente')
        self.assertEqual(item.etat,'envoyee')


class AnalysisTests(Base):
    def test_analysis_never_modifies_candidatures_and_sends_minimal_payload(self):
        item = self.candidature()
        provider = self.provider(action='changement_etat', etat='entrevue')
        job = analyze_message(self.user, self.message.pk, consent=True, provider=provider)
        self.assertEqual(job.status, 'completed')
        item.refresh_from_db()
        self.assertEqual(item.etat, 'envoyee')
        self.assertEqual(Candidature.objects.count(), 1)
        payload = provider.call_args.args[0]
        self.assertNotIn('example.invalid', payload)
        self.assertNotIn('fictitious1', payload)
        self.assertEqual(job.input_tokens, 40)
        self.assertEqual(job.output_tokens, 20)

    def test_no_consent_no_provider_no_record(self):
        provider = self.provider()
        for consent in (False, None, 1, 'true'):
            self.problem('invalid_request', analyze_message, self.user, self.message.pk, consent=consent, provider=provider)
        provider.assert_not_called()
        self.assertEqual(EmailAnalysis.objects.count(), 0)

    def test_owner_only(self):
        provider = self.provider()
        self.problem('not_found', analyze_message, self.user, self.other_message.pk, consent=True, provider=provider)
        provider.assert_not_called()

    def test_unknown_fields_stay_null(self):
        p = self.proposal(employeur=None, poste=None, date_candidature=None, etat=None,
                          informations_manquantes=['employeur','poste','date_candidature','etat'])
        content = decrypt_message_data(p.encrypted_data)
        for field in ('employeur', 'poste', 'date_candidature', 'etat'):
            self.assertIsNone(content[field])
        self.problem('missing_fields', accept_proposal, self.user, p.pk, 1, confirm=True)
        self.assertFalse(Candidature.objects.exists())

    def test_ambiguity_never_selects_target(self):
        a = self.candidature()
        b = self.candidature(date_candidature=date(2026,10,13))
        self.candidature(user=self.other)
        p = self.proposal(action='changement_etat', etat='entrevue')
        content = proposal_data(self.user, p.pk)['proposition']
        self.assertTrue(content['ambiguite'])
        self.assertCountEqual(content['candidatures_possibles'], [a.pk,b.pk])
        self.assertIsNone(content['cible_id'])
        self.problem('target_required', accept_proposal, self.user, p.pk, 1, confirm=True)

    def test_partial_employer_match_still_signals_ambiguity(self):
        self.candidature(poste='Poste fictif A')
        self.candidature(poste='Poste fictif B')
        p = self.proposal(action='changement_etat', poste=None)
        content = proposal_data(self.user, p.pk)['proposition']
        self.assertTrue(content['ambiguite'])
        self.assertIsNone(content['poste'])
        self.assertIsNone(content['cible_id'])

    def test_ambiguity_recomputed_when_candidatures_change(self):
        self.candidature()
        p = self.proposal(action='changement_etat')
        self.candidature(poste='Autre poste fictif')
        self.assertFalse(proposal_data(self.user, p.pk)['proposition']['ambiguite'])
        self.candidature(date_candidature=date(2026,10,13))
        self.assertTrue(proposal_data(self.user, p.pk)['proposition']['ambiguite'])

    def test_unique_candidate_also_requires_human_choice(self):
        self.candidature()
        p = self.proposal(action='changement_etat')
        self.assertIsNone(decrypt_message_data(p.encrypted_data)['cible_id'])

    def test_ciphertext_and_purge_cascade(self):
        p = self.proposal()
        self.assertNotIn(b'Entreprise', bytes(p.encrypted_data))
        self.assertIn('employeur', decrypt_message_data(p.encrypted_data))
        self.message.delete()
        self.assertFalse(EmailAnalysis.objects.exists())
        self.assertFalse(CandidatureProposal.objects.exists())

    def test_same_analysis_idempotent_no_second_model_call(self):
        first = self.run_analysis()
        provider = self.provider()
        second = analyze_message(self.user, self.message.pk, consent=True, provider=provider)
        self.assertEqual(first.pk, second.pk)
        provider.assert_not_called()
        self.assertEqual(CandidatureProposal.objects.count(), 1)

    def test_analysis_and_proposals_isolated(self):
        p = self.proposal()
        for func, ident in [(analysis_for, p.analysis_id), (proposal_for, p.pk), (proposal_data, p.pk)]:
            self.problem('not_found', func, self.other, ident)
        second = self.run_analysis(self.other_message, self.other).proposal
        self.assertNotEqual(second.pk, p.pk)

    def test_missing_key_blocks_before_call(self):
        provider = self.provider()
        with override_settings(GEMINI_API_KEY=''):
            self.problem('configuration_missing', analyze_message, self.user, self.message.pk, consent=True, provider=provider)
        provider.assert_not_called()

    def test_bad_ciphertext_and_large_input_no_send(self):
        provider = self.provider()
        self.message.encrypted_content = b'broken'
        self.message.save()
        self.problem('content_unreadable', analyze_message, self.user, self.message.pk, consent=True, provider=provider)
        self.message.encrypted_content = encrypt_message_data({'subject':'fictif', 'text':'x'*13000})
        self.message.save()
        self.problem('content_too_large', analyze_message, self.user, self.message.pk, consent=True, provider=provider)
        provider.assert_not_called()

    def test_daily_quota_no_call_and_no_incomplete_job(self):
        provider = self.provider()
        with override_settings(GEMINI_DAILY_REQUEST_LIMIT=0):
            self.problem('quota_exceeded', analyze_message, self.user, self.message.pk, consent=True, provider=provider)
        provider.assert_not_called()
        self.assertFalse(EmailAnalysis.objects.exists())

    def test_token_quota_no_call(self):
        with override_settings(GEMINI_DAILY_TOKEN_LIMIT=1):
            self.problem('quota_exceeded', analyze_message, self.user, self.message.pk, consent=True, provider=self.provider())

    def test_minute_token_quota_no_call(self):
        with override_settings(GEMINI_TOKENS_PER_MINUTE=1):
            self.problem('quota_exceeded', analyze_message, self.user, self.message.pk, consent=True, provider=self.provider())

    def test_global_rate_limit_shared_between_users(self):
        with override_settings(GEMINI_REQUESTS_PER_MINUTE=1):
            self.run_analysis()
            provider = self.provider()
            self.problem('quota_exceeded', analyze_message, self.other, self.other_message.pk, consent=True, provider=provider)
            provider.assert_not_called()

    def test_temporary_error_explicit_bounded_retry(self):
        provider = Mock(side_effect=AnalysisProblem('rate_limited', retryable=True, retry_seconds=90))
        job = analyze_message(self.user, self.message.pk, consent=True, provider=provider)
        self.assertEqual(job.status, 'failed')
        self.assertGreater(job.retry_after, timezone.now()+timedelta(seconds=80))
        self.problem('retry_unavailable', analyze_message, self.user, self.message.pk, consent=True, retry=True, provider=provider)
        for attempt in (2,3):
            EmailAnalysis.objects.filter(pk=job.pk).update(retry_after=timezone.now()-timedelta(seconds=1))
            AnalysisBudget.objects.update(next_request_at=timezone.now()-timedelta(seconds=1))
            job = analyze_message(self.user, self.message.pk, consent=True, retry=True, provider=provider)
            self.assertEqual(job.attempts, attempt)
        self.assertIsNone(job.retry_after)
        self.problem('retry_unavailable', analyze_message, self.user, self.message.pk, consent=True, retry=True, provider=provider)
        self.assertEqual(provider.call_count, 3)

    def test_permanent_failure_not_retried(self):
        job = analyze_message(self.user, self.message.pk, consent=True, provider=Mock(side_effect=AnalysisProblem('invalid_response')))
        self.assertFalse(CandidatureProposal.objects.exists())
        self.assertIsNone(job.retry_after)
        self.problem('retry_unavailable', analyze_message, self.user, self.message.pk, consent=True, retry=True, provider=self.provider())

    def test_active_lease_and_expired_lease(self):
        job = EmailAnalysis.objects.create(message=self.message, model_name='gemini-3.5-flash-lite', schema_version='1', status='running', attempts=1, lease=uuid.uuid4(), lease_until=timezone.now()+timedelta(seconds=60))
        self.problem('busy', analyze_message, self.user, self.message.pk, consent=True, provider=self.provider())
        EmailAnalysis.objects.filter(pk=job.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        self.problem('retry_unavailable', analyze_message, self.user, self.message.pk, consent=True, provider=self.provider())
        result = analyze_message(self.user, self.message.pk, consent=True, retry=True, provider=self.provider())
        self.assertEqual(result.status, 'completed')

    def test_unique_analysis_constraint(self):
        job = self.run_analysis()
        with self.assertRaises(IntegrityError), transaction.atomic():
            EmailAnalysis.objects.create(message=self.message, model_name=job.model_name, schema_version='1', status='running')

    def test_purge_during_network_does_not_restore_message(self):
        def provider(payload):
            self.message.delete()
            return self.provider()(payload)
        self.problem('not_found', analyze_message, self.user, self.message.pk, consent=True, provider=provider)
        self.assertFalse(EmailAnalysis.objects.exists())
        self.assertFalse(CandidatureProposal.objects.exists())


class DecisionTests(Base):
    def test_explicit_acceptance_only_and_repeated_accept_noop(self):
        p = self.proposal()
        self.problem('invalid_request', accept_proposal, self.user, p.pk, 1)
        self.assertFalse(Candidature.objects.exists())
        first = accept_proposal(self.user, p.pk, 1, confirm=True)
        second = accept_proposal(self.user, p.pk, 1, confirm=True)
        self.assertEqual(first.result_id, second.result_id)
        self.assertEqual(Candidature.objects.count(), 1)

    def test_correct_before_accept_and_stale_revision(self):
        p = self.proposal(employeur=None, date_candidature=None)
        p = correct_proposal(self.user, p.pk, {'action':'creation', 'employeur':'Employeur corrigé fictif', 'poste':'Test', 'date_candidature':'2026-10-12', 'etat':'envoyee'}, 1)
        self.assertEqual(p.revision, 2)
        self.assertFalse(Candidature.objects.exists())
        self.problem('stale_revision', accept_proposal, self.user, p.pk, 1, confirm=True)
        result = accept_proposal(self.user, p.pk, 2, confirm=True).result
        self.assertEqual(result.employeur, 'Employeur corrigé fictif')

    def test_duplicate_existing_manual_candidature(self):
        self.candidature()
        p = self.proposal()
        self.problem('duplicate_creation', accept_proposal, self.user, p.pk, 1, confirm=True)
        self.assertEqual(Candidature.objects.count(), 1)

    def test_duplicate_two_messages_and_normalization(self):
        p = self.proposal()
        accept_proposal(self.user, p.pk, 1, confirm=True)
        message = self.make_message(self.connection, 'anotherfiction')
        p2 = self.run_analysis(message, employeur='  ENTREPRISE   FICTIVE  ').proposal
        self.problem('duplicate_creation', accept_proposal, self.user, p2.pk, 1, confirm=True)
        self.assertEqual(Candidature.objects.count(), 1)

    def test_rejected_proposal_cannot_change_candidature(self):
        p = self.proposal()
        reject_proposal(self.user, p.pk, 1, confirm=True)
        reject_proposal(self.user, p.pk, 1, confirm=True)
        self.problem('already_decided', accept_proposal, self.user, p.pk, 1, confirm=True)
        self.problem('already_decided', correct_proposal, self.user, p.pk, {'action':'creation'}, 1)
        self.assertFalse(Candidature.objects.exists())

    def test_explicit_target_updates_only_state(self):
        a = self.candidature()
        b = self.candidature(date_candidature=date(2026,10,13))
        p = self.proposal(action='changement_etat', etat='entrevue')
        p = correct_proposal(self.user, p.pk, {'action':'changement_etat', 'etat':'entrevue', 'cible_id':b.pk}, 1)
        accept_proposal(self.user, p.pk, p.revision, confirm=True)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.etat, 'envoyee')
        self.assertEqual(b.etat, 'entrevue')
        self.assertEqual(b.employeur, FACTS['employeur'])

    def test_other_users_target_forbidden(self):
        target = self.candidature(user=self.other)
        p = self.proposal(action='changement_etat')
        self.problem('not_found', correct_proposal, self.user, p.pk, {'action':'changement_etat','cible_id':target.pk,'etat':'entrevue'}, 1)

    def test_other_user_cannot_correct_accept_reject(self):
        p = self.proposal()
        self.problem('not_found', correct_proposal, self.other, p.pk, {'action':'creation'}, 1)
        self.problem('not_found', accept_proposal, self.other, p.pk, 1, confirm=True)
        self.problem('not_found', reject_proposal, self.other, p.pk, 1, confirm=True)

    def test_changed_target_requires_new_review(self):
        target = self.candidature()
        p = self.proposal(action='changement_etat')
        p = correct_proposal(self.user, p.pk, {'action':'changement_etat','cible_id':target.pk,'etat':'entrevue'}, 1)
        target.notes = 'Modification manuelle fictive'
        target.save()
        self.problem('target_changed', accept_proposal, self.user, p.pk, p.revision, confirm=True)

    def test_deleted_accepted_candidature_not_recreated(self):
        p = self.proposal()
        accepted = accept_proposal(self.user, p.pk, 1, confirm=True)
        accepted.result.delete()
        accept_proposal(self.user, p.pk, 1, confirm=True)
        self.assertFalse(Candidature.objects.exists())

    def test_invalid_corrections_do_not_mutate(self):
        p = self.proposal()
        for data in ({'action':'creation','utilisateur':self.other.pk}, {'action':'creation','etat':'invented'}, {'action':'creation','date_candidature':'wrong'}, {'action':'creation','employeur':'x'*201}):
            self.problem('invalid_request', correct_proposal, self.user, p.pk, data, 1)
        p.refresh_from_db()
        self.assertEqual(p.revision, 1)

    def test_unknown_corrected_values_remain_null_and_missing_list_updated(self):
        p = self.proposal()
        p = correct_proposal(self.user, p.pk, {'action':'creation', 'employeur':'   '}, 1)
        content = proposal_data(self.user, p.pk)['proposition']
        self.assertIsNone(content['employeur'])
        self.assertIsNone(content['date_candidature'])
        self.assertIn('employeur', content['informations_manquantes'])
        self.problem('missing_fields', accept_proposal, self.user, p.pk, 2, confirm=True)

    def test_no_action_can_only_be_rejected_or_corrected(self):
        p = self.proposal(action='aucune', pertinent=False)
        self.problem('invalid_request', accept_proposal, self.user, p.pk, 1, confirm=True)
        reject_proposal(self.user, p.pk, 1, confirm=True)
        self.assertFalse(Candidature.objects.exists())


class ApiTests(Base):
    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)
        self.client.get('/api/auth/csrf/')
        self.headers = {'HTTP_X_CSRFTOKEN': self.client.cookies['csrftoken'].value}

    def post(self, url, data, **kwargs):
        return self.client.post(url, json.dumps(data), content_type='application/json', **(self.headers | kwargs))

    def test_auth_and_csrf_even_anonymous(self):
        anonymous = Client(enforce_csrf_checks=True)
        self.assertEqual(anonymous.get('/api/analyses/').status_code, 401)
        self.assertEqual(anonymous.get('/api/propositions/').status_code, 401)
        self.assertEqual(anonymous.post('/api/analyses/', '{}', content_type='application/json').status_code, 403)
        self.assertEqual(self.client.post('/api/analyses/', '{}', content_type='application/json').status_code, 403)

    def test_analysis_explicit_post_and_safe_progress(self):
        with patch('email_analysis.services.extract', self.provider()) as provider:
            self.assertEqual(self.client.get('/api/analyses/').status_code, 200)
            provider.assert_not_called()
            response = self.post('/api/analyses/', {'message_id':self.message.pk, 'consentement':True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'completed')
        self.assertNotContains(response, 'Entreprise')
        self.assertNotContains(response, 'Contenu')
        self.assertFalse(Candidature.objects.exists())

    def test_manual_retry_needs_fresh_consent_and_respects_quotas_and_attempts(self):
        failed=analyze_message(self.user,self.message.pk,consent=True,
            provider=Mock(side_effect=AnalysisProblem('provider_unavailable',retryable=True)))
        EmailAnalysis.objects.filter(pk=failed.pk).update(retry_after=timezone.now()-timedelta(seconds=1))
        AnalysisBudget.objects.update(next_request_at=timezone.now()-timedelta(seconds=1))
        url=f'/api/analyses/{failed.pk}/retry/'
        self.assertTrue(self.client.get('/api/analyses/').json()['results'][0]['can_retry'])
        with patch('email_analysis.services.extract',self.provider()) as provider:
            self.assertEqual(self.post(url,{'consentement':False}).status_code,400)
            provider.assert_not_called()
            with override_settings(GEMINI_DAILY_REQUEST_LIMIT=1):
                self.assertEqual(self.post(url,{'consentement':True}).status_code,429)
            provider.assert_not_called()
            self.assertEqual(self.post(url,{'consentement':True}).status_code,200)
            provider.assert_called_once()
            self.assertEqual(self.post(url,{'consentement':True}).status_code,409)
            provider.assert_called_once()
        self.assertFalse(Candidature.objects.exists())
        self.assertEqual(CandidatureProposal.objects.count(),1)

    def test_retry_metadata_refuses_future_and_terminal_errors_and_hides_other_users(self):
        failed=analyze_message(self.user,self.message.pk,consent=True,
            provider=Mock(side_effect=AnalysisProblem('provider_unavailable',retryable=True)))
        url=f'/api/analyses/{failed.pk}/retry/'
        self.assertFalse(self.client.get('/api/analyses/').json()['results'][0]['can_retry'])
        self.assertEqual(self.post(url,{'consentement':True}).status_code,409)
        EmailAnalysis.objects.filter(pk=failed.pk).update(retry_after=None)
        self.assertEqual(self.post(url,{'consentement':True}).status_code,409)
        self.client.force_login(self.other)
        self.assertEqual(self.post(url,{'consentement':True}).status_code,404)

    def test_owner_fields_rejected_and_other_message_hidden(self):
        response = self.post('/api/analyses/', {'message_id':self.message.pk, 'consentement':True,'utilisateur':self.other.pk})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post('/api/analyses/', {'message_id':self.other_message.pk, 'consentement':True}).status_code, 404)

    def test_details_lists_and_mutations_isolated(self):
        p = self.run_analysis(self.other_message, self.other).proposal
        self.assertEqual(self.client.get('/api/analyses/').json()['results'], [])
        self.assertEqual(self.client.get('/api/propositions/').json()['results'], [])
        for path in [f'/api/analyses/{p.analysis_id}/', f'/api/propositions/{p.pk}/']:
            self.assertEqual(self.client.get(path).status_code, 404)
        for suffix in ['accept','reject']:
            self.assertEqual(self.post(f'/api/propositions/{p.pk}/{suffix}/', {'revision':1,'confirmer':True}).status_code, 404)

    def test_correct_accept_and_no_cache(self):
        p = self.proposal(employeur=None)
        url = f'/api/propositions/{p.pk}/'
        response = self.client.patch(url, json.dumps({'revision':1,'proposition':{'action':'creation','employeur':'Fictif','poste':'Test','date_candidature':'2026-10-12','etat':'envoyee'}}), content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        response = self.post(url+'accept/', {'revision':2,'confirmer':True})
        self.assertEqual(response.json()['status'], 'acceptee')
        self.assertEqual(Candidature.objects.count(), 1)

    def test_all_mutations_require_csrf(self):
        p = self.proposal()
        for url in [f'/api/propositions/{p.pk}/accept/', f'/api/propositions/{p.pk}/reject/', f'/api/analyses/{p.analysis_id}/retry/']:
            self.assertEqual(self.client.post(url, '{}', content_type='application/json').status_code, 403)
        self.assertEqual(self.client.patch(f'/api/propositions/{p.pk}/', '{}', content_type='application/json').status_code, 403)

    def test_patch_does_not_accept_other_owner_or_status(self):
        p = self.proposal()
        response = self.client.patch(f'/api/propositions/{p.pk}/', json.dumps({'revision':1,'proposition':{'action':'creation','status':'acceptee'}}), content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, 400)
        response = self.client.patch(f'/api/propositions/{p.pk}/', json.dumps({'revision':1,'proposition':{'action':'creation'},'utilisateur':self.other.pk}), content_type='application/json', **self.headers)
        self.assertEqual(response.status_code, 400)

    @override_settings(DEBUG=True)
    def test_unexpected_exception_never_exposes_debug_content(self):
        with patch('email_analysis.views.analyze_message', side_effect=RuntimeError('SECRET FICTIONAL BODY')):
            response = self.post('/api/analyses/', {'message_id':self.message.pk,'consentement':True})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b'SECRET', response.content)
        self.assertNotIn(b'Traceback', response.content)

    def test_invalid_json_and_missing_consent(self):
        self.assertEqual(self.post('/api/analyses/', {'message_id':self.message.pk}).status_code, 400)
        self.assertEqual(self.client.post('/api/analyses/', '{', content_type='application/json', **self.headers).status_code, 400)


@override_settings(GEMINI_API_KEY='fictitious', GEMINI_MODEL='gemini-3.5-flash-lite', GEMINI_MAX_OUTPUT_TOKENS=2000)
class AdapterTests(SimpleTestCase):
    def setUp(self):
        blocker = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden'))
        blocker.start(); self.addCleanup(blocker.stop)
        self.factory = patch('email_analysis.gemini.genai.Client')
        self.client_factory = self.factory.start(); self.addCleanup(self.factory.stop)
        self.client = self.client_factory.return_value.__enter__.return_value

    def response(self, data=None, finish='STOP', candidates=True):
        return SimpleNamespace(candidates=[SimpleNamespace(finish_reason=finish)] if candidates else [],
            text=json.dumps(data if data is not None else FACTS), usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=20, thoughts_token_count=0))

    def test_structured_request_no_tools_no_hidden_retries(self):
        self.client.models.generate_content.return_value = self.response()
        result = extract('Fiction uniquement')
        self.assertEqual(result.extraction.employeur, FACTS['employeur'])
        options = self.client_factory.call_args.kwargs['http_options']
        self.assertEqual(options.retry_options.attempts, 1)
        self.assertEqual(options.timeout, 30000)
        kwargs = self.client.models.generate_content.call_args.kwargs
        self.assertEqual(kwargs['model'], 'gemini-3.5-flash-lite')
        self.assertEqual(kwargs['config'].tools, [])
        self.assertEqual(kwargs['config'].response_mime_type, 'application/json')
        self.assertIn('non fiable', kwargs['config'].system_instruction)
        self.assertIn('pas systématiquement tous les faits absents', kwargs['config'].system_instruction)
        self.assertIn('la date existante sera conservée', kwargs['config'].system_instruction)
        self.assertIn('sauf pour etat=a_preparer', kwargs['config'].system_instruction)

    def test_real_sdk_response_types_are_parsed_without_network(self):
        self.client.models.generate_content.return_value = types.GenerateContentResponse(
            candidates=[types.Candidate(finish_reason=types.FinishReason.STOP,
                content=types.Content(parts=[types.Part(text=json.dumps(FACTS))]))],
            usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=12,candidates_token_count=23,thoughts_token_count=2))
        result = extract('fictif')
        self.assertEqual((result.input_tokens, result.output_tokens), (12,25))

    def test_invalid_json_extra_ids_and_invalid_state(self):
        responses = [FACTS | {'etat':'invente'}, FACTS | {'cible_id':123}, FACTS | {'date_candidature':'invente'}, FACTS | {'pertinent':False}]
        for data in responses:
            self.client.models.generate_content.return_value = self.response(data)
            with self.assertRaises(AnalysisProblem) as caught:
                extract('fictif')
            self.assertEqual(caught.exception.code, 'invalid_response')
        self.client.models.generate_content.return_value = SimpleNamespace(candidates=[SimpleNamespace(finish_reason='STOP')], text='{invalid')
        with self.assertRaises(AnalysisProblem):
            extract('fictif')

    def test_blocked_and_truncated_output(self):
        for response in [self.response(candidates=False), self.response(finish='MAX_TOKENS'), self.response(finish='SAFETY')]:
            self.client.models.generate_content.return_value = response
            with self.assertRaises(AnalysisProblem) as caught:
                extract('fictif')
            self.assertEqual(caught.exception.code, 'blocked_response')

    def test_api_error_categories_and_no_secret_in_exception(self):
        for code, expected, retryable in [(429,'rate_limited',True),(503,'provider_unavailable',True),(403,'provider_denied',False),(400,'provider_denied',False)]:
            self.client.models.generate_content.side_effect = errors.APIError(code, {'error':{'message':'SECRET FICTIF'}})
            with self.assertRaises(AnalysisProblem) as caught:
                extract('fictif')
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertNotIn('SECRET', str(caught.exception))

    def test_transport_failure_retryable(self):
        self.client.models.generate_content.side_effect = TimeoutError('fiction')
        with self.assertRaises(AnalysisProblem) as caught:
            extract('fictif')
        self.assertTrue(caught.exception.retryable)

    def test_retry_after_is_respected(self):
        error = errors.APIError(429, {'error':{'message':'fictif'}}, response=SimpleNamespace(headers={'Retry-After':'123'}))
        self.client.models.generate_content.side_effect = error
        with self.assertRaises(AnalysisProblem) as caught:
            extract('fictif')
        self.assertEqual(caught.exception.retry_seconds, 123)

    def test_analysis_logging_strips_queries_and_exception_details(self):
        record = logging.LogRecord('django.request', logging.ERROR, '', 0,
            '/api/analyses/?fiction=PRIVATE', (), (RuntimeError, RuntimeError('PRIVATE'), None))
        record.request = SimpleNamespace(path='/api/analyses/', body=b'PRIVATE')
        AnalysisLogFilter().filter(record)
        self.assertNotIn('PRIVATE', record.getMessage())
        self.assertIsNone(record.exc_info)
        self.assertIsNone(record.request)

    def test_no_missing_fields_filled_by_schema(self):
        with self.assertRaises(ValidationError):
            Extraction.model_validate({'pertinent':True,'action':'creation'})
