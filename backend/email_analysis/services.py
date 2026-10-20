"""Explicit server use cases, independent of requests, sessions and schedulers."""
import json
import random
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.debug import sensitive_variables
from pydantic import ValidationError

from candidatures.models import Candidature
from candidatures.history import snapshot, record_change
from gmail_integration.import_errors import ImportProblem
from gmail_integration.message_crypto import decrypt_message_data, encrypt_message_data
from gmail_integration.models import GmailConnection, GmailMessage
from .errors import AnalysisProblem
from .gemini import extract, reserve_size
from .models import AnalysisBudget, CandidatureProposal, EmailAnalysis
from .name_matching import normalize_matching_name
from .schemas import DecisionData

# Backward-compatible fields: keep completed analyses cached, never requeue old mail.
SCHEMA_VERSION = '1'
MAX_ATTEMPTS = 3


def analysis_retry_allowed(job):
    return bool(job.status == 'failed' and job.retry_after and job.retry_after <= timezone.now()
        and job.attempts < MAX_ATTEMPTS and job.model_name == settings.GEMINI_MODEL
        and job.schema_version == SCHEMA_VERSION)


def _user(user):
    if not user.is_authenticated or not user.is_active:
        raise AnalysisProblem('not_found')


def analysis_for(user, analysis_id):
    _user(user)
    try:
        return EmailAnalysis.objects.get(pk=analysis_id, message__connexion__utilisateur=user)
    except (EmailAnalysis.DoesNotExist, ValueError):
        raise AnalysisProblem('not_found') from None


def proposal_for(user, proposal_id, *, locked=False):
    _user(user)
    qs = CandidatureProposal.objects.select_for_update(of=('self',)) if locked else CandidatureProposal.objects
    try:
        return qs.get(pk=proposal_id, utilisateur=user, analysis__message__connexion__utilisateur=user)
    except (CandidatureProposal.DoesNotExist, ValueError):
        raise AnalysisProblem('not_found') from None


def _decrypt(ciphertext):
    try:
        return decrypt_message_data(ciphertext)
    except ImportProblem:
        raise AnalysisProblem('content_unreadable') from None


def _encrypt(data):
    try:
        return encrypt_message_data(data)
    except ImportProblem:
        raise AnalysisProblem('configuration_missing') from None


def _reserve(tokens):
    now = timezone.now()
    day = now.astimezone(ZoneInfo('America/Los_Angeles')).date()
    budget, _ = AnalysisBudget.objects.get_or_create(day=day, defaults={'minute_start': now, 'next_request_at': now})
    budget = AnalysisBudget.objects.select_for_update().get(pk=budget.pk)
    if now >= budget.minute_start + timedelta(seconds=60):
        budget.minute_start, budget.minute_calls, budget.minute_tokens = now, 0, 0
    if (budget.next_request_at > now or budget.calls >= settings.GEMINI_DAILY_REQUEST_LIMIT
            or budget.reserved_tokens + tokens > settings.GEMINI_DAILY_TOKEN_LIMIT
            or budget.minute_calls >= settings.GEMINI_REQUESTS_PER_MINUTE
            or budget.minute_tokens + tokens > settings.GEMINI_TOKENS_PER_MINUTE):
        raise AnalysisProblem('quota_exceeded')
    budget.calls += 1
    budget.reserved_tokens += tokens
    budget.minute_calls += 1
    budget.minute_tokens += tokens
    budget.next_request_at = now + timedelta(seconds=60 / max(1, settings.GEMINI_REQUESTS_PER_MINUTE))
    budget.save()


def _matches(user, data):
    # Separate lookup values: original extracted fields remain intact/encrypted.
    names = {field: normalize_matching_name(data.get(field)) for field in ('employeur', 'poste')}
    candidates = []
    records = []
    criteria = Q()
    if names['employeur']:
        criteria |= Q(employeur__iexact=names['employeur'])
    if names['poste']:
        criteria |= Q(poste__iexact=names['poste'])
    owned = Candidature.objects.filter(utilisateur=user)
    if names['employeur'] and names['poste']:
        records = list(owned.filter(employeur__iexact=names['employeur'], poste__iexact=names['poste'])
                       .values_list('pk', 'employeur', 'poste')[:101])
    # Partial matches are a fallback only; never dilute an exact pair.
    if not records and criteria:
        records = list(owned.filter(criteria).values_list('pk', 'employeur', 'poste')[:101])
    if records:
        candidates = [row[0] for row in records]
    level = 'aucune'
    if len(candidates) > 1:
        level = 'ambigu'
    elif candidates:
        exact = all(names[field] is not None and value.casefold() == names[field].casefold()
                    for field, value in zip(('employeur', 'poste'), records[0][1:]))
        level = 'probable' if exact else 'a_confirmer'
    return {'candidatures_possibles': candidates[:100], 'ambiguite': len(candidates) > 1,
            'correspondances_tronquees': len(candidates) > 100, 'niveau_association': level}


def _message_context(message, content):
    """Trusted timestamp/direction, minimal display names; never addresses or IDs."""
    from gmail_sync.models import SyncConfiguration
    zone = SyncConfiguration.objects.filter(connexion_id=message.connexion_id).values_list('timezone', flat=True).first() or 'America/Toronto'
    local = message.date_gmail.astimezone(ZoneInfo(zone))
    sent = content.get('sent')
    direction = 'envoye' if sent is True else 'recu' if sent is False else 'inconnu'
    # Recipient names for sent mail, sender names for received mail. Legacy content
    # without direction supplies no inferred counterpart or invented identity.
    people = content.get('to' if sent else 'from', []) if type(sent) is bool else []
    names = []
    if isinstance(people, list):
        for person in people[:10]:
            name = person.get('name') if isinstance(person, dict) else None
            if isinstance(name, str) and name.strip() and '@' not in name:
                names.append(' '.join(name.split())[:200])
    return {'sens': direction, 'date_message': message.date_gmail.isoformat(),
            'date_locale_message': local.date().isoformat(), 'fuseau': zone,
            'noms_correspondants': names}


@sensitive_variables()
def analyze_message(user, message_id, *, consent=False, retry=False, provider=None, automatic_version=None):
    """Only an explicit caller can send one already imported message to Gemini.

    A retry requires another explicit call, after retry_after; no hidden API retries.
    """
    _user(user)
    if consent is not True or type(message_id) is not int:
        raise AnalysisProblem('invalid_request')
    if not settings.GEMINI_API_KEY:
        raise AnalysisProblem('configuration_missing')
    with transaction.atomic():
        try:
            # Same connection lock as Gmail purge: no message disappears while claimed.
            owner = GmailMessage.objects.get(pk=message_id, connexion__utilisateur=user)
            GmailConnection.objects.select_for_update().get(pk=owner.connexion_id, utilisateur=user)
            message = GmailMessage.objects.get(pk=message_id, connexion__utilisateur=user)
        except (GmailMessage.DoesNotExist, GmailConnection.DoesNotExist):
            raise AnalysisProblem('not_found') from None
        if automatic_version is not None:
            from gmail_sync.services import authorize_analysis
            authorize_analysis(user, message, automatic_version)
        job, created = EmailAnalysis.objects.get_or_create(message=message, model_name=settings.GEMINI_MODEL,
            schema_version=SCHEMA_VERSION, defaults={'status': EmailAnalysis.Status.RUNNING})
        job = EmailAnalysis.objects.select_for_update().get(pk=job.pk)
        now = timezone.now()
        if not created:
            if job.status == EmailAnalysis.Status.COMPLETED:
                return job
            if job.status == EmailAnalysis.Status.RUNNING and job.lease_until and job.lease_until > now:
                raise AnalysisProblem('busy')
            queued = job.status == EmailAnalysis.Status.QUEUED and job.attempts == 0
            if (not queued and not retry) or job.attempts >= MAX_ATTEMPTS or (job.status == 'failed' and not job.retry_after) or (job.retry_after and job.retry_after > now):
                raise AnalysisProblem('retry_unavailable')
        content = _decrypt(message.encrypted_content)
        # No addresses, Gmail IDs, attachments or existing candidature records.
        subject, text = content.get('subject'), content.get('text')
        if not isinstance(subject, str) or not isinstance(text, str):
            raise AnalysisProblem('content_unreadable')
        payload = json.dumps({'sujet': subject, 'texte': text, 'contenu_incomplet': message.content_incomplete,
                              **_message_context(message, content)}, ensure_ascii=False)
        if len(payload) > settings.GEMINI_MAX_INPUT_CHARS:
            raise AnalysisProblem('content_too_large')
        _reserve(reserve_size(payload))
        job.status = EmailAnalysis.Status.RUNNING
        job.origin = 'automatic' if automatic_version is not None else 'manual'
        job.consent_version = automatic_version
        job.attempts += 1
        job.lease = uuid.uuid4()
        job.lease_until = now + timedelta(seconds=120)
        job.error_code, job.retry_after = '', None
        job.save()
        job_id, lease = job.pk, job.lease
    try:
        result = (provider or extract)(payload)
        data = result.extraction.model_dump(mode='json')
        # The model never chooses a database ID. Matching is local and user-scoped.
        data.update(_matches(user, data))
        data.update(cible_id=None, cible_version=None, contenu_incomplet=message.content_incomplete)
        encrypted = _encrypt(data)
        with transaction.atomic():
            if automatic_version is not None:
                GmailConnection.objects.select_for_update().get(pk=message.connexion_id, utilisateur=user)
                authorize_analysis(user, message, automatic_version)
            job = EmailAnalysis.objects.select_for_update(of=('self',)).get(pk=job_id, message__connexion__utilisateur=user)
            if job.lease != lease:
                raise AnalysisProblem('busy')
            CandidatureProposal.objects.create(analysis=job, utilisateur=user, encrypted_data=encrypted)
            job.status, job.lease, job.lease_until = EmailAnalysis.Status.COMPLETED, None, None
            job.input_tokens, job.output_tokens = result.input_tokens, result.output_tokens
            job.save()
        return job
    except Exception as error:
        problem = error if isinstance(error, AnalysisProblem) else AnalysisProblem('internal_error')
        with transaction.atomic():
            job = EmailAnalysis.objects.select_for_update().filter(pk=job_id, lease=lease).first()
            if job is None:
                raise AnalysisProblem('not_found') from None
            job.status, job.error_code = EmailAnalysis.Status.FAILED, problem.code
            job.retry_after = timezone.now() + timedelta(seconds=max(problem.retry_seconds, 60 * 2 ** (job.attempts - 1)) + random.uniform(0, 1)) if problem.retryable and job.attempts < MAX_ATTEMPTS else None
            if job.retry_after and problem.code == 'rate_limited':
                day = timezone.now().astimezone(ZoneInfo('America/Los_Angeles')).date()
                AnalysisBudget.objects.filter(pk=day, next_request_at__lt=job.retry_after).update(next_request_at=job.retry_after)
            job.lease, job.lease_until = None, None
            job.save()
        return job


def _pending(proposal, revision):
    if proposal.status != CandidatureProposal.Status.PENDING:
        raise AnalysisProblem('already_decided')
    if type(revision) is not int or revision != proposal.revision:
        raise AnalysisProblem('stale_revision')


@sensitive_variables()
def correct_proposal(user, proposal_id, data, revision):
    try:
        decision = DecisionData.model_validate(data).model_dump(mode='json')
    except ValidationError:
        raise AnalysisProblem('invalid_request') from None
    with transaction.atomic():
        proposal = proposal_for(user, proposal_id, locked=True)
        _pending(proposal, revision)
        content = _decrypt(proposal.encrypted_data)
        content.update(decision)
        content.update(_matches(user, content))
        required = ['employeur', 'poste', 'etat'] if decision['action'] == 'creation' else []
        if decision['action'] == 'creation' and decision['etat'] != 'a_preparer':
            required.append('date_candidature')
        if decision['action'] == 'changement_etat':
            required = ['etat']
        content['informations_manquantes'] = [field for field in required if not content.get(field)]
        target = decision['cible_id']
        content['cible_version'] = None
        if target is not None:
            try:
                item = Candidature.objects.get(pk=target, utilisateur=user)
            except Candidature.DoesNotExist:
                raise AnalysisProblem('not_found') from None
            content['cible_version'] = item.date_modification.isoformat()
        if decision['action'] not in ('changement_etat', 'confirmation_reception') and target is not None:
            raise AnalysisProblem('invalid_request')
        proposal.encrypted_data = _encrypt(content)
        proposal.revision += 1
        proposal.save()
    return proposal


def _normal(value):
    return ' '.join(value.split()).casefold()


@sensitive_variables()
def accept_proposal(user, proposal_id, revision, *, confirm=False):
    _user(user)
    if confirm is not True:
        raise AnalysisProblem('invalid_request')
    try:
        with transaction.atomic():
            source_connection = CandidatureProposal.objects.filter(pk=proposal_id, utilisateur=user).values_list('analysis__message__connexion_id', flat=True).first()
            if source_connection is None:
                raise AnalysisProblem('not_found')
            GmailConnection.objects.select_for_update().get(pk=source_connection, utilisateur=user)
            # Serializes creations from different proposals for the same user.
            get_user_model().objects.select_for_update().get(pk=user.pk)
            proposal = proposal_for(user, proposal_id, locked=True)
            if proposal.status == CandidatureProposal.Status.ACCEPTED:
                return proposal
            _pending(proposal, revision)
            content = _decrypt(proposal.encrypted_data)
            before = None
            if content['action'] == 'creation':
                required = ['employeur', 'poste', 'etat']
                if content.get('etat') != 'a_preparer':
                    required.append('date_candidature')
                if any(not content.get(field) for field in required):
                    raise AnalysisProblem('missing_fields')
                key = [_normal(content['employeur']), _normal(content['poste']), content['date_candidature']]
                signature = salted_hmac('email_analysis.creation', json.dumps([user.pk, *key]),
                    secret=settings.GMAIL_MESSAGE_ENCRYPTION_KEY, algorithm='sha256').hexdigest()
                if CandidatureProposal.objects.filter(utilisateur=user, creation_fingerprint=signature, status='acceptee').exists():
                    raise AnalysisProblem('duplicate_creation')
                for existing in Candidature.objects.filter(utilisateur=user, date_candidature=content['date_candidature']):
                    if [_normal(existing.employeur), _normal(existing.poste)] == key[:2]:
                        raise AnalysisProblem('duplicate_creation')
                item = Candidature(utilisateur=user, **{f: content[f] for f in ('employeur', 'poste', 'date_candidature', 'etat')})
                item.confirmation_reception = content.get('confirmation_reception')
                item.full_clean()
                item.save()
                proposal.creation_fingerprint = signature
            elif content['action'] in ('changement_etat', 'confirmation_reception'):
                if not content.get('cible_id'):
                    raise AnalysisProblem('target_required')
                receipt_only = content['action'] == 'confirmation_reception'
                if receipt_only and type(content.get('confirmation_reception')) is not bool:
                    raise AnalysisProblem('missing_fields')
                if not receipt_only and not content.get('etat'):
                    raise AnalysisProblem('missing_fields')
                try:
                    item = Candidature.objects.select_for_update().get(pk=content['cible_id'], utilisateur=user)
                except Candidature.DoesNotExist:
                    raise AnalysisProblem('not_found') from None
                if item.date_modification.isoformat() != content.get('cible_version'):
                    raise AnalysisProblem('target_changed')
                before = snapshot(item)
                fields = ['date_modification']
                if receipt_only:
                    item.confirmation_reception = content['confirmation_reception']
                    fields.append('confirmation_reception')
                else:
                    item.etat = content['etat']
                    fields.append('etat')
                    # An unknown receipt never overwrites existing knowledge.
                    if type(content.get('confirmation_reception')) is bool:
                        item.confirmation_reception = content['confirmation_reception']
                        fields.append('confirmation_reception')
                    # Only an explicit human-supplied date can complete a preparation.
                    if item.date_candidature is None and content.get('date_candidature'):
                        item.date_candidature = content['date_candidature']
                        fields.append('date_candidature')
                item.full_clean()
                item.save(update_fields=fields)
            else:
                raise AnalysisProblem('invalid_request')
            proposal.status, proposal.result = CandidatureProposal.Status.ACCEPTED, item
            proposal.decided_at = timezone.now()
            proposal.save()
            record_change(item, before, proposal=proposal)
            return proposal
    except IntegrityError:
        raise AnalysisProblem('duplicate_creation') from None
    except DjangoValidationError:
        raise AnalysisProblem('invalid_fields') from None


def reject_proposal(user, proposal_id, revision, *, confirm=False):
    if confirm is not True:
        raise AnalysisProblem('invalid_request')
    with transaction.atomic():
        proposal = proposal_for(user, proposal_id, locked=True)
        if proposal.status == CandidatureProposal.Status.REJECTED:
            return proposal
        _pending(proposal, revision)
        proposal.status, proposal.decided_at = CandidatureProposal.Status.REJECTED, timezone.now()
        proposal.save()
        return proposal


@sensitive_variables()
def proposal_data(user, proposal_id):
    proposal = proposal_for(user, proposal_id)
    content = _decrypt(proposal.encrypted_data)
    # Read old encrypted proposals without rewriting their content or revision.
    content.setdefault('confirmation_reception', None)
    # Recompute ambiguity: candidatures may have changed since the model request.
    content.update(_matches(user, content))
    if content.get('cible_id') and not Candidature.objects.filter(pk=content['cible_id'], utilisateur=user).exists():
        content['cible_id'], content['cible_version'] = None, None
    return {'id': str(proposal.pk), 'status': proposal.status, 'revision': proposal.revision,
            'resultat_id': proposal.result_id if Candidature.objects.filter(pk=proposal.result_id, utilisateur=user).exists() else None,
            'proposition': content}
