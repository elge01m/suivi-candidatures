from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError


class Candidature(models.Model):
    class Etat(models.TextChoices):
        A_PREPARER = 'a_preparer', 'À préparer'
        ENVOYEE = 'envoyee', 'Envoyée'
        ENTREVUE = 'entrevue', 'Entrevue'
        OFFRE_RECUE = 'offre_recue', 'Offre reçue'
        OFFRE_ACCEPTEE = 'offre_acceptee', 'Offre acceptée'
        OFFRE_DECLINEE = 'offre_declinee', 'Offre déclinée'
        REFUSEE = 'refusee', 'Refusée'
        RETIREE = 'retiree', 'Retirée'

    utilisateur = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
    )
    employeur = models.CharField(max_length=200)
    poste = models.CharField(max_length=200)
    date_candidature = models.DateField(null=True, blank=True)
    confirmation_reception = models.BooleanField(null=True, blank=True, default=None)
    etat = models.CharField(
        max_length=20,
        choices=Etat.choices,
        default=Etat.ENVOYEE,
    )
    lien = models.URLField(blank=True)
    notes = models.TextField(blank=True)
    date_creation = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = [models.F('date_candidature').desc(nulls_last=True), '-pk']
        constraints = [
            models.CheckConstraint(condition=models.Q(etat='a_preparer') | models.Q(date_candidature__isnull=False), name='candidature_date_si_envoyee'),
            models.CheckConstraint(condition=~models.Q(etat='a_preparer', confirmation_reception=True), name='candidature_preparation_sans_confirmation'),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.etat != self.Etat.A_PREPARER and self.date_candidature is None:
            errors['date_candidature'] = 'La date de candidature est obligatoire hors de l’état À préparer.'
        if self.etat == self.Etat.A_PREPARER and self.confirmation_reception is True:
            errors['confirmation_reception'] = 'Une candidature à préparer ne peut pas avoir été reçue.'
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f'{self.poste} — {self.employeur}'


class CandidatureChange(models.Model):
    candidature = models.ForeignKey(Candidature, on_delete=models.CASCADE, related_name='historique')
    utilisateur = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    date = models.DateTimeField(auto_now_add=True)
    origine = models.CharField(max_length=12, choices=[('manuelle', 'Manuelle'), ('proposition', 'Proposition acceptée')])
    avant = models.JSONField(default=dict)
    apres = models.JSONField(default=dict)
    source_message = models.ForeignKey('gmail_integration.GmailMessage', null=True, on_delete=models.SET_NULL)
    # Immutable local references survive the source message/proposal's deletion.
    source_message_reference = models.PositiveBigIntegerField(null=True)
    source_proposition_reference = models.UUIDField(null=True)

    class Meta:
        ordering = ['-date', '-pk']
        indexes = [models.Index(fields=['candidature', '-date'], name='candidature_history_date')]
