ERRORS = {
    'invalid_fields': (400, 'Vérifiez la date de candidature et la confirmation de réception : une candidature à préparer ne peut pas être confirmée reçue.'),
    'invalid_request': (400, 'Requête invalide.'),
    'not_found': (404, 'Ressource introuvable.'),
    'configuration_missing': (503, 'Analyse non configurée.'),
    'content_unreadable': (503, 'Contenu indisponible.'),
    'content_too_large': (400, 'Texte trop long pour cette analyse.'),
    'quota_exceeded': (429, 'Budget local atteint. Réessayez ultérieurement.'),
    'busy': (409, 'Analyse déjà en cours.'),
    'retry_unavailable': (409, 'Nouvelle tentative indisponible.'),
    'rate_limited': (429, 'Limite du fournisseur atteinte.'),
    'provider_unavailable': (503, 'Service d’analyse temporairement indisponible.'),
    'provider_denied': (503, 'Accès au service d’analyse refusé.'),
    'invalid_response': (502, 'Réponse du modèle invalide ou incomplète.'),
    'blocked_response': (422, 'Le modèle n’a pas produit de réponse exploitable.'),
    'already_decided': (409, 'Cette proposition a déjà été traitée.'),
    'stale_revision': (409, 'La proposition a changé. Relisez-la avant validation.'),
    'target_required': (400, 'Sélectionnez explicitement une candidature.'),
    'target_changed': (409, 'La candidature a changé. Vérifiez à nouveau la proposition.'),
    'missing_fields': (400, 'Complétez les informations obligatoires.'),
    'duplicate_creation': (409, 'Une candidature correspondante existe déjà.'),
    'internal_error': (503, 'Analyse indisponible.'),
}


class AnalysisProblem(Exception):
    def __init__(self, code, *, retryable=False, retry_seconds=60):
        self.code = code if code in ERRORS else 'internal_error'
        self.status, self.public_message = ERRORS[self.code]
        self.retryable = retryable
        self.retry_seconds = max(1, min(86400, retry_seconds))
        super().__init__(self.code)
