import uuid
from datetime import time

from django.db import models


class SyncConfiguration(models.Model):
    connexion = models.OneToOneField('gmail_integration.GmailConnection', on_delete=models.CASCADE, related_name='sync_configuration')
    gmail_enabled = models.BooleanField(default=False)
    # Legacy daily times retained for data preservation; scheduling uses 5-minute slots.
    time_1 = models.TimeField(default=time(9))
    time_2 = models.TimeField(default=time(18))
    timezone = models.CharField(max_length=64, default='America/Toronto')
    auto_analysis_enabled = models.BooleanField(default=False)
    consent_at = models.DateTimeField(null=True)
    consent_version = models.UUIDField(default=uuid.uuid4, editable=False)
    generation = models.UUIDField(default=uuid.uuid4, editable=False)
    started_at = models.DateTimeField(null=True)
    last_success_at = models.DateTimeField(null=True)
    next_due_at = models.DateTimeField(null=True)
    blocked_code = models.CharField(max_length=40, blank=True)
    lease = models.UUIDField(null=True, editable=False)
    lease_until = models.DateTimeField(null=True, editable=False)
    last_served_at = models.DateTimeField(null=True)
    revision = models.PositiveIntegerField(default=1)


class SyncRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = 'queued', 'En attente'
        RUNNING = 'running', 'En cours'
        PAUSED = 'paused', 'En pause'
        COMPLETED = 'completed', 'Terminée'
        FAILED = 'failed', 'Échec'
        CANCELLED = 'cancelled', 'Annulée'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    configuration = models.ForeignKey(SyncConfiguration, on_delete=models.CASCADE, related_name='runs')
    import_job = models.OneToOneField('gmail_integration.GmailImport', on_delete=models.CASCADE)
    origin = models.CharField(max_length=10, choices=[('manual', 'Manuelle'), ('scheduled', 'Planifiée'), ('initial', 'Import initial')])
    scheduled_for = models.DateTimeField(null=True)
    generation = models.UUIDField(editable=False)
    credential_version = models.UUIDField(editable=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.QUEUED)
    error_code = models.CharField(max_length=40, blank=True)
    retry_after = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['configuration'], condition=models.Q(status__in=['queued', 'running', 'paused']), name='one_open_sync_per_connection'),
            models.UniqueConstraint(fields=['configuration', 'scheduled_for'], name='one_sync_per_scheduled_slot'),
        ]
