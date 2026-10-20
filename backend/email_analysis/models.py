import uuid

from django.conf import settings
from django.db import models


class EmailAnalysis(models.Model):
    class Status(models.TextChoices):
        QUEUED = 'queued', 'En attente'
        CANCELLED = 'cancelled', 'Annulée'
        RUNNING = 'running', 'En cours'
        COMPLETED = 'completed', 'Terminée'
        FAILED = 'failed', 'Échec'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey('gmail_integration.GmailMessage', on_delete=models.CASCADE, related_name='analyses')
    model_name = models.CharField(max_length=100)
    schema_version = models.CharField(max_length=20)
    origin = models.CharField(max_length=10, default='manual', choices=[('manual', 'Manuelle'), ('automatic', 'Automatique')])
    consent_version = models.UUIDField(null=True, editable=False)
    status = models.CharField(max_length=12, choices=Status.choices)
    attempts = models.PositiveSmallIntegerField(default=0)
    error_code = models.CharField(max_length=40, blank=True)
    retry_after = models.DateTimeField(null=True)
    lease = models.UUIDField(null=True, editable=False)
    lease_until = models.DateTimeField(null=True, editable=False)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['message', 'model_name', 'schema_version'], name='unique_message_analysis_version')]


class CandidatureProposal(models.Model):
    class Status(models.TextChoices):
        PENDING = 'en_attente', 'En attente'
        ACCEPTED = 'acceptee', 'Acceptée'
        REJECTED = 'rejetee', 'Rejetée'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    analysis = models.OneToOneField(EmailAnalysis, on_delete=models.CASCADE, related_name='proposal')
    utilisateur = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    encrypted_data = models.BinaryField(editable=False)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    revision = models.PositiveIntegerField(default=1)
    result = models.ForeignKey('candidatures.Candidature', on_delete=models.SET_NULL, null=True, related_name='validated_proposals')
    creation_fingerprint = models.CharField(max_length=64, null=True, editable=False)
    decided_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['utilisateur', 'creation_fingerprint'], condition=models.Q(status='acceptee', creation_fingerprint__isnull=False), name='unique_accepted_creation')]


class AnalysisBudget(models.Model):
    """Global application budget: all users share the same Google project quota."""
    day = models.DateField(primary_key=True)
    calls = models.PositiveIntegerField(default=0)
    reserved_tokens = models.PositiveIntegerField(default=0)
    minute_start = models.DateTimeField()
    minute_calls = models.PositiveIntegerField(default=0)
    minute_tokens = models.PositiveIntegerField(default=0)
    next_request_at = models.DateTimeField()
