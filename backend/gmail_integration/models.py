import uuid

from django.conf import settings
from django.db import models


class GmailConnection(models.Model):
    utilisateur = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='gmail_connection',
    )
    # Only authenticated ciphertext goes into this field, never plaintext credentials.
    encrypted_tokens = models.BinaryField(editable=False)
    expires_at = models.DateTimeField()
    granted_scopes = models.JSONField(default=list)
    credential_version = models.UUIDField(default=uuid.uuid4, editable=False)
    encrypted_mailbox_identity = models.BinaryField(null=True, editable=False)
    date_creation = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Connexion Gmail #{self.pk}'


class GmailMessage(models.Model):
    connexion = models.ForeignKey(GmailConnection, on_delete=models.CASCADE, related_name='imported_messages')
    gmail_id = models.CharField(max_length=128)
    thread_id = models.CharField(max_length=128)
    date_gmail = models.DateTimeField()
    encrypted_content = models.BinaryField(editable=False)
    content_incomplete = models.BooleanField(default=False)
    incomplete_reasons = models.JSONField(default=list)
    date_import = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['connexion', 'gmail_id'], name='gmail_message_per_connection')]
        indexes = [models.Index(fields=['connexion', 'date_gmail'], name='gmail_msg_connection_date')]
        ordering = ['-date_gmail', '-pk']

    def __str__(self):
        return f'Message Gmail importe #{self.pk}'


class GmailImport(models.Model):
    class Status(models.TextChoices):
        READY = 'ready', 'Prêt'
        RUNNING = 'running', 'En cours'
        PAUSED = 'paused', 'En pause'
        COMPLETED = 'completed', 'Terminé'
        FAILED = 'failed', 'Échec'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    connexion = models.ForeignKey(GmailConnection, on_delete=models.CASCADE, related_name='imports')
    date_debut = models.DateField()
    date_fin = models.DateField()
    fuseau = models.CharField(max_length=64, default='America/Toronto')
    range_start = models.DateTimeField()
    range_end = models.DateTimeField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.READY)
    encrypted_cursor = models.BinaryField(null=True, editable=False)
    processed = models.PositiveIntegerField(default=0)
    imported = models.PositiveIntegerField(default=0)
    duplicates = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    incomplete = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=40, blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    retry_after = models.DateTimeField(null=True)
    lease_token = models.UUIDField(null=True, editable=False)
    lease_until = models.DateTimeField(null=True, editable=False)
    date_creation = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['connexion'], condition=models.Q(status='running'), name='gmail_one_running_import',
        )]

    def __str__(self):
        return f'Import Gmail #{self.pk}'
