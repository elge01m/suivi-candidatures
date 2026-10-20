"""Manual/resumable import use cases, independent of HTTP, browsers and sessions.

Future workers can call prepare_import(user, ...), then continue_import(user, id).
All account selection is server-side. No request object is accepted by this module.
"""

import hashlib
import math
import random
import time
import uuid
from datetime import date, datetime, time as day_time, timedelta, timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .gmail_client import GmailClient
from .import_errors import ImportProblem
from .message_crypto import decrypt_message_data, encrypt_message_data, message_cipher
from .message_parser import parse_message
from .models import GmailConnection, GmailImport, GmailMessage


BATCH_SIZE = 10
TIME_BUDGET = 20
LEASE_SECONDS = 120
MAX_RETRIES = 5
EXCLUDED_LABELS = {'SPAM', 'TRASH', 'DRAFT'}


def period_bounds(start, end, zone):
    try:
        if not isinstance(start, str) or not isinstance(end, str) or len(start) != 10 or len(end) != 10:
            raise ValueError
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if first > last:
            raise ValueError
        last_exclusive = last + timedelta(days=1)
    except (ValueError, TypeError, OverflowError):
        raise ImportProblem('invalid_period') from None
    try:
        if not isinstance(zone, str) or len(zone) > 64:
            raise ValueError
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ImportProblem('invalid_timezone') from None
    return first, last, datetime.combine(first, day_time(), tz).astimezone(datetime_timezone.utc), \
        datetime.combine(last_exclusive, day_time(), tz).astimezone(datetime_timezone.utc)


def connection_for(user):
    if not user.is_authenticated:
        raise ImportProblem('not_found')
    try:
        return GmailConnection.objects.get(utilisateur=user)
    except GmailConnection.DoesNotExist:
        raise ImportProblem('gmail_not_connected') from None


def get_import(user, import_id):
    try:
        return GmailImport.objects.get(pk=import_id, connexion__utilisateur=user)
    except (GmailImport.DoesNotExist, ValueError):
        raise ImportProblem('not_found') from None


def prepare_import(user, start, end, zone='America/Toronto'):
    first, last, utc_start, utc_end = period_bounds(start, end, zone)
    message_cipher()
    return GmailImport.objects.create(
        connexion=connection_for(user), date_debut=first, date_fin=last, fuseau=zone,
        range_start=utc_start, range_end=utc_end,
    )


def prepare_import_window(user, start, end, zone='America/Toronto'):
    """Exact half-open UTC window for the scheduler; manual date imports unchanged."""
    try:
        tz = ZoneInfo(zone)
        if timezone.is_naive(start) or timezone.is_naive(end) or start >= end:
            raise ValueError
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise ImportProblem('invalid_period') from None
    message_cipher()
    return GmailImport.objects.create(connexion=connection_for(user),
        date_debut=start.astimezone(tz).date(), date_fin=(end - timedelta(microseconds=1)).astimezone(tz).date(),
        fuseau=zone, range_start=start, range_end=end)


def _claim(user, import_id):
    owner = connection_for(user)
    with transaction.atomic():
        connection = GmailConnection.objects.select_for_update().get(pk=owner.pk, utilisateur=user)
        try:
            job = GmailImport.objects.select_for_update().get(pk=import_id, connexion=connection)
        except GmailImport.DoesNotExist:
            raise ImportProblem('not_found') from None
        if job.status == GmailImport.Status.COMPLETED:
            return connection, job, None
        if job.status == GmailImport.Status.FAILED:
            raise ImportProblem('import_failed')
        now = timezone.now()
        if connection.imports.filter(status=GmailImport.Status.RUNNING, lease_until__gt=now).exists():
            raise ImportProblem('busy')
        connection.imports.filter(status=GmailImport.Status.RUNNING).update(
            status=GmailImport.Status.PAUSED, lease_token=None, lease_until=None,
        )
        if job.retry_after and job.retry_after > now:
            raise ImportProblem('rate_limited')
        job.status = GmailImport.Status.RUNNING
        job.lease_token = uuid.uuid4()
        job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        job.error_code = ''
        job.retry_after = None
        job.save()
        return connection, job, job.lease_token


def _locked(connection_id, job_id, lease, version):
    connection = GmailConnection.objects.select_for_update().get(pk=connection_id)
    job = GmailImport.objects.select_for_update().get(pk=job_id, connexion=connection)
    if job.status != GmailImport.Status.RUNNING or job.lease_token != lease or job.lease_until <= timezone.now():
        raise ImportProblem('lease_lost')
    if connection.credential_version != version:
        raise ImportProblem('credentials_changed', retryable=True)
    return connection, job


@sensitive_variables()
def _bind_mailbox(connection_id, job_id, lease, version, identity):
    with transaction.atomic():
        connection, _ = _locked(connection_id, job_id, lease, version)
        if connection.encrypted_mailbox_identity:
            if decrypt_message_data(connection.encrypted_mailbox_identity) != identity:
                raise ImportProblem('mailbox_changed')
        else:
            connection.encrypted_mailbox_identity = encrypt_message_data(identity)
            connection.save(update_fields=['encrypted_mailbox_identity'])


def _empty_cursor():
    return {'page_token': None, 'pending_ids': [], 'listing_done': False, 'seen_pages': []}


@sensitive_variables()
def _checkpoint(connection_id, job_id, lease, version, cursor, parsed=None, outcome=None):
    with transaction.atomic():
        connection, job = _locked(connection_id, job_id, lease, version)
        if parsed is not None:
            _, created = GmailMessage.objects.get_or_create(
                connexion=connection, gmail_id=parsed['gmail_id'], defaults={
                    'thread_id': parsed['thread_id'], 'date_gmail': parsed['date_gmail'],
                    'encrypted_content': encrypt_message_data(parsed['content']),
                    'content_incomplete': parsed['content_incomplete'],
                    'incomplete_reasons': parsed['incomplete_reasons'],
                },
            )
            if created:
                job.imported += 1
                job.incomplete += int(parsed['content_incomplete'])
            else:
                job.duplicates += 1
            job.processed += 1
        elif outcome:
            job.processed += 1
            if outcome == 'duplicate':
                job.duplicates += 1
            else:
                job.skipped += 1
        job.encrypted_cursor = encrypt_message_data(cursor)
        job.retry_count = 0
        job.save()


def _finish(job_id, lease, error=None):
    with transaction.atomic():
        job = GmailImport.objects.select_for_update().get(pk=job_id)
        if job.lease_token != lease:
            raise ImportProblem('lease_lost')
        job.lease_token = None
        job.lease_until = None
        job.retry_after = None
        if error and error.code != 'time_budget':
            job.error_code = error.code
            job.retry_count += 1
            if error.retryable and job.retry_count < MAX_RETRIES:
                job.status = GmailImport.Status.PAUSED
                delay = error.retry_seconds or min(300, 2 ** job.retry_count + random.uniform(0, 1))
                job.retry_after = timezone.now() + timedelta(seconds=delay)
            else:
                job.status = GmailImport.Status.FAILED
        else:
            cursor = decrypt_message_data(job.encrypted_cursor) if job.encrypted_cursor else _empty_cursor()
            job.error_code = ''
            job.status = GmailImport.Status.COMPLETED if cursor['listing_done'] and not cursor['pending_ids'] else GmailImport.Status.READY
        job.save()
        return job


@sensitive_variables()
def continue_import(user, import_id, *, client_factory=None, batch_size=BATCH_SIZE):
    if not isinstance(batch_size, int) or not 1 <= batch_size <= BATCH_SIZE:
        raise ImportProblem('invalid_request')
    message_cipher()
    connection, job, lease = _claim(user, import_id)
    if lease is None:
        return job
    version = connection.credential_version
    client = None
    error = None
    try:
        deadline = time.monotonic() + TIME_BUDGET
        cursor = decrypt_message_data(job.encrypted_cursor) if job.encrypted_cursor else _empty_cursor()
        if connection.encrypted_mailbox_identity:
            decrypt_message_data(connection.encrypted_mailbox_identity)
        client = (client_factory or GmailClient)(connection, deadline)
        _bind_mailbox(connection.pk, job.pk, lease, version, client.profile())
        # Query only the chosen period. No sender, recipient or keyword filtering.
        query = f'after:{int(job.range_start.timestamp()) - 1} before:{math.ceil(job.range_end.timestamp())} -in:spam -in:trash -in:drafts'
        processed = 0
        while processed < batch_size and time.monotonic() < deadline:
            if not cursor['pending_ids']:
                if cursor['listing_done']:
                    break
                ids, next_page = client.list_page(query, cursor['page_token'])
                if next_page:
                    digest = hashlib.sha256(next_page.encode()).hexdigest()
                    if digest in cursor['seen_pages']:
                        raise ImportProblem('invalid_page')
                    cursor['seen_pages'].append(digest)
                cursor.update(page_token=next_page, pending_ids=ids, listing_done=next_page is None)
                _checkpoint(connection.pk, job.pk, lease, version, cursor)
                continue
            gmail_id = cursor['pending_ids'][0]
            parsed = None
            if GmailMessage.objects.filter(connexion=connection, gmail_id=gmail_id).exists():
                outcome = 'duplicate'
            else:
                try:
                    raw, reasons = client.message(gmail_id)
                    if raw.get('id') != gmail_id:
                        raise ImportProblem('gmail_response_invalid')
                    if set(raw.get('labelIds', [])) & EXCLUDED_LABELS:
                        outcome = 'skipped'
                    else:
                        parsed = parse_message(raw, reasons)
                        if not job.range_start <= parsed['date_gmail'] < job.range_end:
                            parsed = None
                            outcome = 'skipped'
                        else:
                            outcome = None
                except ImportProblem as problem:
                    if problem.code != 'message_missing':
                        raise
                    outcome = 'skipped'
            next_cursor = {**cursor, 'pending_ids': cursor['pending_ids'][1:]}
            _checkpoint(connection.pk, job.pk, lease, version, next_cursor, parsed, outcome)
            cursor = next_cursor
            processed += 1
    except ImportProblem as problem:
        error = problem
    except Exception:
        error = ImportProblem('internal_error', retryable=True)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    return _finish(job.pk, lease, error)


def purge_messages(user):
    owner = connection_for(user)
    with transaction.atomic():
        connection = GmailConnection.objects.select_for_update().get(pk=owner.pk, utilisateur=user)
        if connection.imports.filter(status=GmailImport.Status.RUNNING, lease_until__gt=timezone.now()).exists():
            raise ImportProblem('busy')
        from gmail_sync.services import reset_connection
        reset_connection(connection, reason='purged', reset_cursor=True)
        count = connection.imported_messages.count()
        connection.imported_messages.all().delete()
        connection.imports.all().delete()
        connection.encrypted_mailbox_identity = None
        connection.save(update_fields=['encrypted_mailbox_identity'])
    return count
