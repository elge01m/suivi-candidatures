"""Durable orchestration; no browser/session, and no network from enqueue/config APIs."""
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from email_analysis.errors import AnalysisProblem
from email_analysis.models import EmailAnalysis
from email_analysis.services import MAX_ATTEMPTS, SCHEMA_VERSION, analyze_message
from gmail_integration.import_errors import ImportProblem
from gmail_integration.import_service import MAX_RETRIES, continue_import, prepare_import, prepare_import_window, period_bounds
from gmail_integration.models import GmailConnection, GmailImport, GmailMessage
from .errors import SyncProblem
from .models import SyncConfiguration, SyncRun
from .schedule import INTERVAL_MINUTES, next_slot, validate_timezone

OPEN = ['queued', 'running', 'paused']
LEASE_SECONDS = 120
BLOCKING = {'reauthorization_required', 'mailbox_changed', 'gmail_access_denied'}
RETRYABLE_IMPORT = {'gmail_unavailable', 'rate_limited', 'internal_error', 'time_budget', 'lease_lost'}


def import_retry_allowed(job):
    now = timezone.now()
    if (job.status not in ('failed', 'paused') or job.error_code not in RETRYABLE_IMPORT
            or job.retry_count >= MAX_RETRIES or job.retry_after and job.retry_after > now
            or job.lease_until and job.lease_until > now):
        return False
    if GmailImport.objects.filter(connexion_id=job.connexion_id, status='running', lease_until__gt=now).exclude(pk=job.pk).exists():
        return False
    cfg = SyncConfiguration.objects.filter(connexion_id=job.connexion_id).first()
    if cfg is None:
        return True
    if cfg.blocked_code or cfg.lease_until and cfg.lease_until > now:
        return False
    run = cfg.runs.filter(import_job=job).first()
    if run and (run.status not in ('failed', 'paused') or run.retry_after and run.retry_after > now or run.generation != cfg.generation
                or run.credential_version != cfg.connexion.credential_version):
        return False
    return not cfg.runs.filter(status__in=OPEN).exclude(import_job=job).exists()


def retry_import(user, import_id, *, confirm=False):
    if confirm is not True:
        raise SyncProblem('invalid_request')
    with transaction.atomic():
        connection = connection_for(user, locked=True)
        cfg, _ = SyncConfiguration.objects.get_or_create(connexion=connection)
        cfg = SyncConfiguration.objects.select_for_update().get(pk=cfg.pk)
        job = GmailImport.objects.select_for_update().filter(pk=import_id, connexion=connection).first()
        if job is None:
            raise SyncProblem('not_found')
        if not import_retry_allowed(job):
            raise SyncProblem('retry_unavailable')
        run = cfg.runs.select_for_update().filter(import_job=job).first()
        if run is None:
            run = SyncRun.objects.create(configuration=cfg, import_job=job, origin='initial',
                generation=cfg.generation, credential_version=connection.credential_version)
        run.status, run.retry_after = 'queued', None
        run.save(update_fields=['status', 'retry_after', 'updated_at'])
        # Preserve cursor, counters and retry count; the worker enforces all limits.
        job.status, job.retry_after, job.lease_token, job.lease_until = 'ready', None, None, None
        job.save(update_fields=['status', 'retry_after', 'lease_token', 'lease_until'])
        return run


def connection_for(user, *, locked=False):
    if not user.is_authenticated or not user.is_active:
        raise SyncProblem('not_found')
    qs = GmailConnection.objects.select_for_update() if locked else GmailConnection.objects
    try:
        return qs.get(utilisateur=user)
    except GmailConnection.DoesNotExist:
        raise SyncProblem('not_connected') from None


def _cancel_analyses(connection, reason):
    EmailAnalysis.objects.filter(Q(status__in=['queued', 'running']) | Q(status='failed', retry_after__isnull=False),
        message__connexion=connection, origin='automatic').update(
        status='cancelled', error_code=reason, retry_after=None, lease=None, lease_until=None)


def reset_connection(connection, *, reason='credentials_changed', reset_cursor=False):
    """Called while the connection row is locked, before purge or OAuth replacement."""
    cfg = SyncConfiguration.objects.select_for_update().filter(connexion=connection).first()
    if cfg is None:
        return
    runs = cfg.runs.filter(status__in=OPEN)
    GmailImport.objects.filter(pk__in=runs.values('import_job_id')).update(
        status='failed', error_code=reason, lease_token=None, lease_until=None, retry_after=None)
    runs.update(status='cancelled', error_code=reason, retry_after=None)
    _cancel_analyses(connection, reason)
    cfg.gmail_enabled = cfg.auto_analysis_enabled = False
    cfg.consent_version, cfg.generation = uuid.uuid4(), uuid.uuid4()
    cfg.consent_at = cfg.next_due_at = cfg.lease = cfg.lease_until = None
    cfg.blocked_code = ''
    if reset_cursor:
        cfg.started_at = cfg.last_success_at = None
    cfg.revision += 1
    cfg.save()


def configure(user, data):
    allowed = {'gmail_enabled', 'timezone', 'auto_analysis_enabled', 'consent', 'revision'}
    if not isinstance(data, dict) or set(data) - allowed:
        raise SyncProblem('invalid_request')
    for key in ('gmail_enabled', 'auto_analysis_enabled', 'consent'):
        if key in data and type(data[key]) is not bool:
            raise SyncProblem('invalid_request')
    with transaction.atomic():
        connection = connection_for(user, locked=True)
        cfg, _ = SyncConfiguration.objects.get_or_create(connexion=connection)
        cfg = SyncConfiguration.objects.select_for_update().get(pk=cfg.pk)
        if type(data.get('revision')) is not int or data['revision'] != cfg.revision:
            raise SyncProblem('stale_revision')
        zone = validate_timezone(data.get('timezone', cfg.timezone))
        now = timezone.now()
        desired = data.get('auto_analysis_enabled', cfg.auto_analysis_enabled)
        if desired and not cfg.auto_analysis_enabled:
            if data.get('consent') is not True:
                raise SyncProblem('consent_required')
            cfg.consent_at, cfg.consent_version = now, uuid.uuid4()
        if not desired and cfg.auto_analysis_enabled:
            cfg.consent_at, cfg.consent_version = None, uuid.uuid4()
            _cancel_analyses(connection, 'consent_revoked')
        cfg.auto_analysis_enabled = desired
        cfg.timezone = zone
        enabled = data.get('gmail_enabled', cfg.gmail_enabled)
        schedule_changed = enabled != cfg.gmail_enabled or 'timezone' in data
        cfg.gmail_enabled = enabled
        if enabled and cfg.started_at is None:
            cfg.started_at = now - timedelta(days=1)
        if schedule_changed or enabled and cfg.next_due_at is None:
            cfg.next_due_at = next_slot(now) if enabled else None
        cfg.revision += 1
        cfg.save()
        return cfg


def _enqueue(cfg, origin, now, scheduled_for=None):
    existing = cfg.runs.filter(status__in=OPEN).first()
    if existing:
        return existing
    if cfg.blocked_code:
        raise SyncProblem('sync_blocked')
    if cfg.started_at is None:
        cfg.started_at = now - timedelta(days=1)
        cfg.save(update_fields=['started_at'])
    start = max(cfg.started_at, (cfg.last_success_at or cfg.started_at) - timedelta(days=2))
    job = prepare_import_window(cfg.connexion.utilisateur, start, now, cfg.timezone)
    return SyncRun.objects.create(configuration=cfg, import_job=job, origin=origin, scheduled_for=scheduled_for,
        generation=cfg.generation, credential_version=cfg.connexion.credential_version)


def request_sync(user):
    with transaction.atomic():
        connection = connection_for(user, locked=True)
        cfg, _ = SyncConfiguration.objects.get_or_create(connexion=connection)
        cfg = SyncConfiguration.objects.select_for_update().select_related('connexion__utilisateur').get(pk=cfg.pk)
        return _enqueue(cfg, 'manual', timezone.now())


def request_initial_import(user, data):
    """Queue a date range without network I/O; same worker, leases and mailbox checks."""
    if not isinstance(data, dict) or set(data) != {'date_debut', 'date_fin', 'fuseau'}:
        raise SyncProblem('invalid_request')
    try:
        first, last, _, _ = period_bounds(data['date_debut'], data['date_fin'], data['fuseau'])
        with transaction.atomic():
            connection = connection_for(user, locked=True)
            cfg, _ = SyncConfiguration.objects.get_or_create(connexion=connection)
            cfg = SyncConfiguration.objects.select_for_update().select_related('connexion__utilisateur').get(pk=cfg.pk)
            if cfg.blocked_code:
                raise SyncProblem('sync_blocked')
            active = cfg.runs.select_related('import_job').filter(status__in=OPEN).first()
            if active:
                job = active.import_job
                if active.origin == 'initial' and (job.date_debut, job.date_fin, job.fuseau) == (first, last, data['fuseau']):
                    return active
                raise SyncProblem('busy')
            job = prepare_import(user, data['date_debut'], data['date_fin'], data['fuseau'])
            return SyncRun.objects.create(configuration=cfg, import_job=job, origin='initial',
                generation=cfg.generation, credential_version=connection.credential_version)
    except ImportProblem as error:
        raise SyncProblem(error.code) from None


def _schedule_due(cfg, now):
    if not cfg.gmail_enabled or cfg.blocked_code:
        return
    # Normalize a legacy daily deadline lazily, only when an enabled worker runs.
    # No migration rewrites existing configurations or activates scheduling.
    following = next_slot(now)
    if cfg.next_due_at is None or cfg.next_due_at > following:
        cfg.next_due_at = following
        cfg.save(update_fields=['next_due_at'])
        return
    if cfg.next_due_at and cfg.next_due_at <= now:
        _enqueue(cfg, 'scheduled', now, scheduled_for=cfg.next_due_at)
        # Missed occurrences coalesce; cursor still covers the whole offline period.
        cfg.next_due_at = following
        cfg.save(update_fields=['next_due_at'])


def claim_connection(config_id):
    """Short claim transaction; never hold a DB lock across provider requests."""
    with transaction.atomic():
        owner = SyncConfiguration.objects.filter(pk=config_id).values_list('connexion_id', flat=True).first()
        if owner is None:
            return None
        connection = GmailConnection.objects.select_for_update().select_related('utilisateur').get(pk=owner)
        cfg = SyncConfiguration.objects.select_for_update().get(pk=config_id)
        now = timezone.now()
        if not connection.utilisateur.is_active or cfg.lease_until and cfg.lease_until > now:
            return None
        _schedule_due(cfg, now)
        cfg.lease, cfg.lease_until, cfg.last_served_at = uuid.uuid4(), now + timedelta(seconds=LEASE_SECONDS), now
        cfg.save(update_fields=['lease', 'lease_until', 'last_served_at'])
        return cfg.lease


def release_connection(config_id, lease):
    SyncConfiguration.objects.filter(pk=config_id, lease=lease).update(lease=None, lease_until=None)


def _queue_new(cfg):
    if not cfg.auto_analysis_enabled or not cfg.consent_at or cfg.blocked_code:
        return
    # Both arrival and local import must follow consent. No automatic historical sweep.
    existing = EmailAnalysis.objects.filter(message_id=OuterRef('pk'), model_name=settings.GEMINI_MODEL, schema_version=SCHEMA_VERSION)
    messages = GmailMessage.objects.filter(connexion=cfg.connexion, date_gmail__gte=cfg.consent_at,
        date_import__gte=cfg.consent_at).annotate(analyzed=Exists(existing)).filter(analyzed=False).order_by('date_import', 'pk')[:100]
    EmailAnalysis.objects.bulk_create([EmailAnalysis(message=message, model_name=settings.GEMINI_MODEL,
        schema_version=SCHEMA_VERSION, status='queued', origin='automatic', consent_version=cfg.consent_version)
        for message in messages], ignore_conflicts=True)


def authorize_analysis(user, message, version):
    """Connection must already be locked; shared with analysis claim and final commit."""
    cfg = SyncConfiguration.objects.select_for_update().filter(connexion=message.connexion, connexion__utilisateur=user).first()
    if (cfg is None or not cfg.auto_analysis_enabled or cfg.consent_version != version or not cfg.consent_at
            or cfg.blocked_code or message.date_import < cfg.consent_at or message.date_gmail < cfg.consent_at):
        raise AnalysisProblem('retry_unavailable')
    return cfg


def _process_import(cfg, lease, client_factory):
    with transaction.atomic():
        connection = GmailConnection.objects.select_for_update().get(pk=cfg.connexion_id)
        current = SyncConfiguration.objects.select_for_update().get(pk=cfg.pk)
        if current.lease != lease or current.lease_until <= timezone.now():
            return False
        run = current.runs.select_for_update().filter(status__in=OPEN).order_by('created_at').first()
        if run is None:
            return False
        if run.generation != current.generation or run.credential_version != connection.credential_version:
            reset_connection(connection)
            return True
        if run.retry_after and run.retry_after > timezone.now():
            return False
        run.status = 'running'
        run.save(update_fields=['status', 'updated_at'])
        run_id, import_id = run.pk, run.import_job_id
    try:
        job = continue_import(cfg.connexion.utilisateur, import_id, client_factory=client_factory)
    except ImportProblem as error:
        if error.code in ('busy', 'rate_limited'):
            SyncRun.objects.filter(pk=run_id, status='running').update(status='paused', retry_after=timezone.now() + timedelta(seconds=60))
            return True
        SyncRun.objects.filter(pk=run_id, status='running').update(status='failed', error_code=error.code, retry_after=None)
        return True
    with transaction.atomic():
        GmailConnection.objects.select_for_update().get(pk=cfg.connexion_id)
        current = SyncConfiguration.objects.select_for_update().get(pk=cfg.pk)
        if current.lease != lease:
            return True
        run = current.runs.select_for_update().get(pk=run_id)
        if run.status == 'cancelled' or run.generation != current.generation:
            return True
        run.status = {'ready':'queued', 'paused':'paused', 'completed':'completed', 'failed':'failed'}.get(job.status, 'paused')
        run.error_code, run.retry_after = job.error_code, job.retry_after
        run.save()
        if job.status == 'completed' and run.origin != 'initial':
            current.last_success_at = max(current.last_success_at or job.range_end, job.range_end)
            current.save(update_fields=['last_success_at'])
        if job.error_code in BLOCKING:
            current.blocked_code = job.error_code
            current.save(update_fields=['blocked_code'])
        _queue_new(current)
    return True


def _process_analysis(cfg, lease, provider):
    with transaction.atomic():
        GmailConnection.objects.select_for_update().get(pk=cfg.connexion_id)
        current = SyncConfiguration.objects.select_for_update().get(pk=cfg.pk)
        if current.lease != lease or current.lease_until <= timezone.now():
            return False
        _queue_new(current)
        if not current.auto_analysis_enabled or current.blocked_code:
            return False
        now = timezone.now()
        EmailAnalysis.objects.filter(message__connexion=current.connexion, origin='automatic',
            status='running', lease_until__lte=now, attempts__gte=MAX_ATTEMPTS).update(
                status='failed', error_code='attempts_exhausted', retry_after=None, lease=None, lease_until=None)
        eligible = Q(status='queued') | Q(status='failed', retry_after__lte=now, attempts__lt=MAX_ATTEMPTS) | Q(status='running', lease_until__lte=now, attempts__lt=MAX_ATTEMPTS)
        job = EmailAnalysis.objects.filter(eligible, message__connexion=current.connexion, origin='automatic',
            consent_version=current.consent_version).filter(Q(retry_after__isnull=True) | Q(retry_after__lte=now)).order_by('created_at', 'pk').first()
        if job is None:
            return False
        version, message_id = current.consent_version, job.message_id
    try:
        analyze_message(cfg.connexion.utilisateur, message_id, consent=True, retry=True, provider=provider, automatic_version=version)
    except AnalysisProblem as error:
        if error.code in ('busy', 'retry_unavailable'):
            return False
        if error.code in ('quota_exceeded', 'configuration_missing'):
            # Keep the queue; no external request was made. Retry later, without sleep.
            EmailAnalysis.objects.filter(pk=job.pk, status='queued').update(error_code=error.code, retry_after=timezone.now() + timedelta(minutes=1))
        else:
            EmailAnalysis.objects.filter(pk=job.pk, status='queued').update(status='failed', error_code=error.code, retry_after=None)
    return True


def process_connection(config_id, *, client_factory=None, provider=None):
    lease = claim_connection(config_id)
    if lease is None:
        return False
    try:
        cfg = SyncConfiguration.objects.select_related('connexion__utilisateur').get(pk=config_id)
        # One bounded provider operation per account per tick. No HTTP worker waits.
        return _process_import(cfg, lease, client_factory) or _process_analysis(cfg, lease, provider)
    finally:
        release_connection(config_id, lease)


def configuration_data(user):
    try:
        connection = connection_for(user)
    except SyncProblem as error:
        if error.code == 'not_connected':
            return {'connected':False}
        raise
    cfg = SyncConfiguration.objects.filter(connexion=connection).first()
    data = {'connected':True, 'gmail_enabled':False, 'auto_analysis_enabled':False, 'interval_minutes':INTERVAL_MINUTES,
        'timezone':'America/Toronto', 'revision':1, 'consent_at':None, 'next_due_at':None, 'last_success_at':None,
        'blocked_code':'', 'analysis_pending':0, 'analyses_completed':0, 'analyses_failed':0, 'last_analysis_error':'', 'latest_run':None, 'latest_initial_import':None}
    if cfg:
        data.update({key:getattr(cfg, key) for key in ('gmail_enabled','auto_analysis_enabled','timezone','revision','blocked_code')})
        for key in ('consent_at','next_due_at','last_success_at'):
            value = getattr(cfg, key)
            data[key] = value.isoformat() if value else None
        data['analysis_pending'] = EmailAnalysis.objects.filter(Q(status__in=['queued','running']) | Q(status='failed', retry_after__isnull=False),
            message__connexion=connection, origin='automatic').count()
        data['analyses_completed'] = EmailAnalysis.objects.filter(message__connexion=connection, origin='automatic', status='completed').count()
        data['analyses_failed'] = EmailAnalysis.objects.filter(message__connexion=connection, origin='automatic', status='failed', retry_after__isnull=True).count()
        data['last_analysis_error'] = EmailAnalysis.objects.filter(message__connexion=connection, origin='automatic').exclude(error_code='').order_by('-updated_at').values_list('error_code', flat=True).first() or ''
        run = cfg.runs.select_related('import_job').order_by('-created_at').first()
        data['latest_run'] = run_data(run) if run else None
        initial = cfg.runs.select_related('import_job').filter(origin='initial').order_by('-created_at').first()
        data['latest_initial_import'] = run_data(initial) if initial else None
    return data


def run_data(run):
    job = run.import_job
    return {'id':str(run.pk), 'status':run.status, 'origin':run.origin, 'error_code':run.error_code,
        'date_debut':job.date_debut.isoformat(), 'date_fin':job.date_fin.isoformat(), 'fuseau':job.fuseau, 'incomplete':job.incomplete,
        'processed':job.processed, 'imported':job.imported, 'duplicates':job.duplicates, 'skipped':job.skipped,
        'retry_after':run.retry_after.isoformat() if run.retry_after else None}
