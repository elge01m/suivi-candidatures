import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings, skipUnlessDBFeature

from candidatures.models import Candidature, CandidatureChange
from gmail_integration.models import GmailMessage
from .errors import AnalysisProblem
from .evaluation.corpus import load_corpus
from .evaluation.metrics import summarize, variability
from .evaluation.runner import evaluate, simulated_result
from .models import AnalysisBudget, CandidatureProposal, EmailAnalysis
from .name_matching import normalize_matching_name


def gold_rows(corpus):
    return [{'case_id':case['id'],'repetition':1,'status':'completed','prediction':case['expected'],
        'association':case['expected_association'] | {'target':None}} for case in corpus['cases']]


class CorpusMetricTests(SimpleTestCase):
    def test_name_normalization_is_single_pass_and_does_not_invent_values(self):
        self.assertIsNone(normalize_matching_name(None))
        self.assertIsNone(normalize_matching_name('  '))
        self.assertEqual(normalize_matching_name('R&amp;D\u00a0Fictif'), 'R&D Fictif')
        self.assertEqual(normalize_matching_name('&amp;#233;'), '&#233;')
        self.assertEqual(normalize_matching_name('De\u0301veloppeur'), 'Développeur')
        self.assertEqual(normalize_matching_name('Boreal'), 'Boreal')

    def test_raw_extraction_error_remains_visible_when_normalized_matching_succeeds(self):
        corpus=load_corpus(); rows=gold_rows(corpus)
        row=next(r for r in rows if r['case_id']=='F005')
        row['prediction']=dict(row['prediction'],employeur='Bor&#233;al Fictif',poste='D&#233;veloppeur logiciel')
        row['matching_names']={key:normalize_matching_name(row['prediction'][key]) for key in ('employeur','poste')}
        result=summarize(corpus,rows)
        self.assertEqual(result['extraction']['employeur']['all_attempted']['correct'],39)
        self.assertEqual(result['extraction']['poste']['all_attempted']['correct'],39)
        self.assertEqual(result['association']['exact_set']['correct'],40)
        self.assertFalse(result['extraction_representation']['html_entities_decoded_for_extraction_scores'])
        self.assertEqual(row['prediction']['employeur'],'Bor&#233;al Fictif')

    def test_corpus_has_40_unique_fictional_emails_and_all_required_scenarios(self):
        corpus=load_corpus()
        self.assertEqual(len(corpus['cases']),40)
        tags={tag for case in corpus['cases'] for tag in case['tags']}
        self.assertTrue({'envoye_sans_confirmation','entrevue','refus','offre','non_pertinent','doublon_contenu','plusieurs_postes','a_preparer'}<=tags)
        self.assertEqual(len(corpus['variability_cases']),5)
        self.assertEqual(len({case['id'] for case in corpus['cases']}),40)

    def test_metric_arithmetic_on_gold_is_perfect_but_not_a_real_model_measurement(self):
        corpus=load_corpus(); result=summarize(corpus,gold_rows(corpus))
        self.assertEqual(result['classification']['pertinent']['f1'],1)
        self.assertEqual(result['association']['exact_set']['rate'],1)
        self.assertEqual(result['corrections']['field_differences'],0)

    def test_known_injected_errors_are_counted_and_unknown_invention_is_detected(self):
        corpus=load_corpus(); rows=gold_rows(corpus)
        for row,case in zip(rows,corpus['cases']):
            row['prediction']=simulated_result(case,1,corpus).extraction.model_dump(mode='json')
        result=summarize(corpus,rows)
        self.assertEqual(result['classification']['etat']['rate'],39/40)
        self.assertEqual(result['corrections']['field_differences'],3)
        self.assertEqual(result['extraction']['poste']['invented_when_unknown'],1)
        self.assertEqual(result['extraction']['date_candidature']['all_attempted']['rate'],39/40)

    def test_failed_json_counts_as_incorrect_and_unattempted_rows_are_not_invented(self):
        corpus=load_corpus()
        result=summarize(corpus,[{'case_id':'F001','repetition':1,'status':'failed','error_code':'invalid_response'}])
        self.assertEqual(result['classification']['action']['rate'],0)
        self.assertEqual(result['coverage'],{'corpus_size':40,'attempted':1,'completed':0,'failed':1,'pending':39})
        self.assertEqual(result['schema_valid']['rate'],0)
        self.assertEqual(result['classification']['action']['confusion'][0]['predicted'],'__error__')

    def test_variability_uses_three_pairs_and_keeps_repetitions_out_of_accuracy(self):
        corpus=load_corpus();case=corpus['cases'][2]
        rows=[{'case_id':case['id'],'repetition':r,'status':'completed',
            'prediction':simulated_result(case,r,corpus).extraction.model_dump(mode='json'),
            'association':case['expected_association'] | {'target':None}} for r in (1,2,3)]
        result=variability(corpus,rows)[1]
        self.assertEqual(result['distinct_outputs'],2)
        self.assertEqual(result['pairwise_agreement'],{'correct':1,'total':3,'rate':1/3})
        self.assertEqual(summarize(corpus,rows)['coverage']['attempted'],1)

    @override_settings(EVALUATION_ISOLATED=False)
    def test_refuses_application_configuration_before_any_database_query(self):
        with self.assertRaises(ValueError): evaluate('blocked','simulated')
        with self.assertRaises(CommandError): call_command('evaluate_email_corpus',run_id='blocked',output='unused.json')


@skipUnlessDBFeature('has_select_for_update')
@override_settings(EVALUATION_ISOLATED=True, GMAIL_MESSAGE_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    GOOGLE_TOKEN_ENCRYPTION_KEY=Fernet.generate_key().decode(), GEMINI_API_KEY='fictitious-key')
class EvaluationPipelineTests(TestCase):
    def setUp(self):
        for target in ('requests.Session.request','httpx.Client.request'):
            blocker=patch(target,side_effect=AssertionError('Real network forbidden'))
            blocker.start();self.addCleanup(blocker.stop)

    def test_simulation_is_resumable_uses_real_services_and_never_accepts_or_calls_google(self):
        with patch('email_analysis.services.extract',side_effect=AssertionError('No real provider')):
            result=evaluate('sim-test','simulated')
            self.assertTrue(result['completed'])
            self.assertEqual(result['protocol_version'], '1.2')
            self.assertFalse(result['real_gemini_measurement'])
            self.assertEqual(result['new_attempts_this_invocation'],50)
            self.assertTrue(result['gmail_id_replay_verified'])
            self.assertEqual(Candidature.objects.count(),4)
            self.assertEqual(GmailMessage.objects.count(),50)
            self.assertEqual(CandidatureProposal.objects.filter(status='en_attente').count(),50)
            self.assertFalse(CandidatureChange.objects.exists())
            calls=sum(AnalysisBudget.objects.values_list('calls',flat=True))
            cached=evaluate('sim-test','simulated')
            self.assertEqual(cached['new_attempts_this_invocation'],0)
            self.assertEqual(cached['metrics'],result['metrics'])
            self.assertEqual(sum(AnalysisBudget.objects.values_list('calls',flat=True)),calls)

    def test_changed_prompt_refuses_existing_campaign_without_rewriting_successes(self):
        corpus=load_corpus()
        provider=Mock(return_value=simulated_result(corpus['cases'][0],1,corpus))
        evaluate('frozen-prompt','real',allow_real=True,max_calls=1,provider=provider)
        job=EmailAnalysis.objects.get()
        before=(job.status,job.attempts,bytes(job.proposal.encrypted_data),job.proposal.revision)
        calls=sum(AnalysisBudget.objects.values_list('calls',flat=True))
        provider.reset_mock()
        with patch('email_analysis.evaluation.runner.PROTOCOL_VERSION','next-version'):
            with self.assertRaisesMessage(ValueError,'Campaign fingerprint changed'):
                evaluate('frozen-prompt','real',allow_real=True,max_calls=1,provider=provider)
        provider.assert_not_called()
        job.refresh_from_db()
        self.assertEqual((job.status,job.attempts,bytes(job.proposal.encrypted_data),job.proposal.revision),before)
        self.assertEqual(sum(AnalysisBudget.objects.values_list('calls',flat=True)),calls)

    def test_real_mode_is_explicit_bounded_and_injected_providers_are_never_labelled_real(self):
        with self.assertRaises(ValueError): evaluate('real-test','real')
        self.assertFalse(get_user_model().objects.exists())
        corpus=load_corpus()
        provider=Mock(return_value=simulated_result(corpus['cases'][0],1,corpus))
        result=evaluate('real-test','real',allow_real=True,max_calls=1,provider=provider)
        provider.assert_called_once()
        self.assertFalse(result['real_gemini_measurement'])
        self.assertFalse(result['completed'])
        self.assertEqual(result['stop_reason'],'max_calls')
        self.assertEqual(result['metrics']['coverage']['completed'],1)
        provider.reset_mock()
        result=evaluate('real-test','real',allow_real=True,max_calls=1,provider=provider)
        provider.assert_not_called()
        self.assertEqual(result['stop_reason'],'quota_exceeded')

    def test_failed_provider_stops_without_automatic_retry_or_raw_error_export(self):
        provider=Mock(side_effect=AnalysisProblem('provider_unavailable',retryable=True))
        result=evaluate('failed-test','real',allow_real=True,max_calls=1,provider=provider)
        provider.assert_called_once()
        self.assertEqual(result['stop_reason'],'provider_unavailable')
        self.assertEqual(result['metrics']['coverage']['failed'],1)
        self.assertEqual(EmailAnalysis.objects.get().attempts,1)

    @override_settings(SETTINGS_MODULE='config.evaluation_settings')
    def test_output_mode_collision_is_rejected_before_any_provider_call(self):
        with TemporaryDirectory() as folder:
            target=Path(folder)/'report.json'
            target.write_text(json.dumps({'mode':'real','run_id':'different'}),encoding='utf-8')
            with patch('email_analysis.management.commands.evaluate_email_corpus.evaluate') as evaluator:
                with self.assertRaises(CommandError):
                    call_command('evaluate_email_corpus',run_id='new',output=str(target),stdout=io.StringIO())
                evaluator.assert_not_called()
