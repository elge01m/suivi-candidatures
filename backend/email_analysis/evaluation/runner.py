"""Bounded, resumable evaluation using the production analysis service on fake data.

No Gmail client is instantiated. No proposal is ever accepted here.
"""
import hashlib
import json
import re
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone as dt_timezone
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import override_settings
from django.utils import timezone

from candidatures.models import Candidature, CandidatureChange
from gmail_integration.message_crypto import encrypt_message_data
from gmail_integration.models import GmailConnection, GmailMessage
from ..errors import AnalysisProblem
from ..gemini import ModelResult, SYSTEM
from ..models import AnalysisBudget, CandidatureProposal, EmailAnalysis
from ..schemas import Extraction
from ..services import SCHEMA_VERSION, analyze_message, proposal_data
from .corpus import load_corpus
from .metrics import summarize, variability


def assert_isolated():
    cfg=connection.settings_dict
    name=cfg.get('NAME','')
    if (not getattr(settings,'EVALUATION_ISOLATED',False) or cfg.get('ENGINE')!='django.db.backends.postgresql'
            or cfg.get('HOST')!='127.0.0.1' or str(cfg.get('PORT')) in ('','5432')
            or not str(cfg.get('USER','')).startswith('sync_test_')
            or not (name.startswith('suivi_sync_test_evaluation') or name.startswith('test_suivi_sync_test_'))):
        raise ValueError('Evaluation refused outside the dedicated isolated PostgreSQL database.')


def simulated_result(case, repetition, corpus):
    """ANNOTATION-DERIVED test oracle with declared faults; NOT Gemini predictions."""
    data=case['expected'] | corpus['simulation_faults'].get(case['id'],{})
    # One declared synthetic variation: the third interview run misses its state.
    if case['id']=='F003' and repetition==3:
        data=data | {'etat':None}
    return ModelResult(Extraction.model_validate(data | {'justification':'Réponse simulée issue des annotations, avec erreurs injectées.'}))


def evaluate(run_id, mode, *, allow_real=False, max_calls=1, provider=None):
    assert_isolated()  # Before any database query or creation.
    if mode not in ('simulated','real') or not re.fullmatch(r'[a-z0-9_-]{1,32}',run_id):
        raise ValueError('Invalid evaluation parameters.')
    if mode=='real' and (allow_real is not True or not 1<=max_calls<=20):
        raise ValueError('Real Gemini requires explicit authorization and a bounded call count.')
    if mode=='simulated' and provider is not None:
        raise ValueError('Simulation provider is fixed and never performs network calls.')
    corpus=load_corpus()
    # A manifest bound to the local username prevents mixing versions/modes in one campaign.
    code_paths=[Path(__file__).parents[1]/name for name in ('services.py','gemini.py','schemas.py')]+[Path(__file__),Path(__file__).with_name('metrics.py')]
    fingerprint=hashlib.sha256((''.join(p.read_text(encoding='utf-8') for p in code_paths)+corpus['sha256']+settings.GEMINI_MODEL+version('google-genai')).encode()).hexdigest()
    username=f'eval-{mode}-{run_id}'
    user,created=get_user_model().objects.get_or_create(username=username)
    if created:
        user.set_unusable_password();user.first_name=fingerprint;user.save()
    elif user.first_name!=fingerprint:
        raise ValueError('Campaign fingerprint changed; use a new run ID.')
    conn,_=GmailConnection.objects.get_or_create(utilisateur=user,
        defaults={'encrypted_tokens':b'fictitious-no-oauth','expires_at':timezone.now()})
    candidates={}
    for fixture in corpus['candidate_fixtures']:
        item,_=Candidature.objects.get_or_create(utilisateur=user,lien=f'https://example.invalid/evaluation/{run_id}/{fixture["key"]}',
            defaults={key:value for key,value in fixture.items() if key!='key'})
        candidates[item.pk]=fixture['key']
    original=list(Candidature.objects.filter(utilisateur=user).values())
    observations=[]; calls=0; stop_reason=None
    schedule=[(case,1) for case in corpus['cases']]+[(case,repeat) for case in corpus['cases']
        if case['id'] in corpus['variability_cases'] for repeat in (2,3)]
    context=override_settings(GEMINI_API_KEY='simulation-not-an-api-key') if mode=='simulated' else nullcontext()
    with context:
        for index,(case,repetition) in enumerate(schedule):
            now=datetime(2030,1,1,tzinfo=dt_timezone.utc)+timedelta(days=index//5,seconds=(index%5)*61)
            clock=patch('django.utils.timezone.now',return_value=now) if mode=='simulated' else nullcontext()
            with clock:
                message,_=GmailMessage.objects.get_or_create(connexion=conn,gmail_id=f'eval-{run_id}-{case["id"]}-{repetition}',
                    defaults={'thread_id':'fictitious-evaluation','date_gmail':datetime.fromisoformat(case['date_message']),
                        'encrypted_content':encrypt_message_data({'subject':case['subject'],'text':case['text'],'sent':case['sent'],
                            'from':[{'name':'Recrutement fictif','address':'recruitment@example.invalid'}],
                            'to':[{'name':'Destinataire fictif','address':'candidate@example.invalid'}]})})
                job=EmailAnalysis.objects.filter(message=message,model_name=settings.GEMINI_MODEL,schema_version=SCHEMA_VERSION).first()
                row={'case_id':case['id'],'repetition':repetition,'status':'pending'}
                newly_attempted=False
                if job is None and stop_reason is None:
                    if mode=='real' and calls>=max_calls:
                        stop_reason='max_calls'
                    else:
                        try:
                            chosen=(lambda payload,c=case,r=repetition:simulated_result(c,r,corpus)) if mode=='simulated' else provider
                            job=analyze_message(user,message.pk,consent=True,provider=chosen)
                            calls+=1
                            newly_attempted=True
                        except AnalysisProblem as error:
                            stop_reason=error.code  # Safe fixed code; no raw provider response.
                if job is not None:
                    row.update(status=job.status,input_tokens=job.input_tokens,output_tokens=job.output_tokens,error_code=job.error_code)
                    if job.status=='completed':
                        data=proposal_data(user,job.proposal.pk)['proposition']
                        row['prediction']={key:data.get(key) for key in case['expected']}
                        row['association']={'candidates':[candidates[pk] for pk in data['candidatures_possibles'] if pk in candidates],
                            'level':data['niveau_association'],'ambiguous':data['ambiguite'],'target':data['cible_id']}
                    elif newly_attempted or job.status not in ('completed','failed'):
                        stop_reason=job.error_code or 'analysis_not_completed'
                observations.append(row)
    completed=all(row['status']=='completed' for row in observations)
    replay_verified=None
    if completed:
        before=EmailAnalysis.objects.filter(message__connexion=conn).count()
        before_budget=sum(AnalysisBudget.objects.values_list('calls',flat=True))
        for case_id in corpus['gmail_id_replays']:
            message=GmailMessage.objects.get(connexion=conn,gmail_id=f'eval-{run_id}-{case_id}-1')
            # Cached jobs must never invoke the provider or reserve another request.
            def forbidden(payload):
                raise AssertionError('Replay unexpectedly invoked a provider.')
            with override_settings(GEMINI_API_KEY='simulation-not-an-api-key') if mode=='simulated' else nullcontext():
                analyze_message(user,message.pk,consent=True,provider=forbidden)
        replay_verified=EmailAnalysis.objects.filter(message__connexion=conn).count()==before and sum(AnalysisBudget.objects.values_list('calls',flat=True))==before_budget
    if original!=list(Candidature.objects.filter(utilisateur=user).values()) or CandidatureChange.objects.filter(utilisateur=user).exists() or CandidatureProposal.objects.filter(utilisateur=user).exclude(status='en_attente').exists():
        raise ValueError('Evaluation unexpectedly changed a candidature or accepted a proposal.')
    measured=mode=='real' and provider is None
    return {'protocol_version':'1.0','mode':mode,'real_gemini_measurement':measured,
        'warning':'SIMULATION: annotation-derived responses and injected errors; not measured Gemini quality.' if mode=='simulated' else
            'INJECTED TEST PROVIDER: not a real Gemini measurement.' if provider is not None else 'REAL: only API observations are measurements; incomplete campaigns are not final results.',
        'run_id':run_id,'corpus_sha256':corpus['sha256'],'code_sha256':fingerprint,'prompt_sha256':hashlib.sha256(SYSTEM.encode()).hexdigest(),
        'model':settings.GEMINI_MODEL,'schema_version':SCHEMA_VERSION,'sdk_version':version('google-genai'),
        'quota_limits':{'requests_per_day':settings.GEMINI_DAILY_REQUEST_LIMIT,'reserved_tokens_per_day':settings.GEMINI_DAILY_TOKEN_LIMIT,
            'requests_per_minute':settings.GEMINI_REQUESTS_PER_MINUTE},
        'clock':'virtual, five calls per virtual day' if mode=='simulated' else 'actual UTC clock; quota day America/Los_Angeles',
        'simulation_faults':corpus['simulation_faults'] if mode=='simulated' else None,
        'completed':completed,'all_trials_attempted':all(row['status'] in ('completed','failed') for row in observations),
        'planned_calls':50,'new_attempts_this_invocation':calls,'stop_reason':stop_reason,
        'real_api_calls_this_invocation':calls if measured else 0,'gmail_api_calls':0,'proposal_applications':0,
        'gmail_id_replay_verified':replay_verified,'metrics':summarize(corpus,observations),
        'variability':variability(corpus,observations),'observations':observations}
