class SyncProblem(Exception):
    MESSAGES = {
        'retry_unavailable': (409, 'Reprise indisponible : vérifiez le délai, les limites ou la connexion Gmail.'),
        'busy': (409, 'Un autre import ou une synchronisation est déjà en cours.'),
        'invalid_period': (400, 'Choisissez une période valide, du début à la fin inclusivement.'),
        'invalid_timezone': (400, 'Choisissez un fuseau horaire valide.'),
        'configuration_missing': (503, 'Le chiffrement des messages est indisponible.'),
        'not_connected': (409, 'Connectez votre compte Gmail avant de synchroniser.'),
        'not_found': (404, 'Ressource introuvable.'),
        'invalid_request': (400, 'Vérifiez la période, le fuseau et les options.'),
        'consent_required': (400, 'Un consentement explicite est nécessaire pour activer l’analyse automatique.'),
        'stale_revision': (409, 'La configuration a changé. Actualisez-la avant de réessayer.'),
        'reauthorization_required': (409, 'Reconnectez votre compte Gmail.'),
        'sync_blocked': (409, 'La synchronisation est bloquée. Vérifiez votre connexion Gmail.'),
    }

    def __init__(self, code):
        self.code = code
        self.status, self.message = self.MESSAGES.get(code, (503, 'Synchronisation indisponible.'))
        super().__init__(code)
