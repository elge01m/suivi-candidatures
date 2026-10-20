ERRORS = {
    'invalid_period': (400, 'Choisissez une période valide, avec une fin postérieure ou égale au début.'),
    'invalid_timezone': (400, 'Le fuseau horaire est invalide.'),
    'invalid_request': (400, 'La requête est invalide.'),
    'configuration_missing': (503, 'La configuration de chiffrement des messages est absente ou invalide.'),
    'content_unreadable': (503, 'Les données chiffrées ne peuvent pas être lues.'),
    'gmail_not_connected': (409, 'Aucune connexion Gmail disponible.'),
    'not_found': (404, 'Import introuvable.'),
    'busy': (409, 'Un lot est déjà en cours pour cette connexion.'),
    'mailbox_changed': (409, 'La boîte Gmail ne correspond pas à cet historique. Supprimez les imports locaux avant de changer de boîte.'),
    'credentials_changed': (409, 'La connexion a changé pendant le lot. Reprenez l’import.'),
    'reauthorization_required': (409, 'Reconnectez votre compte Google avant de reprendre.'),
    'rate_limited': (429, 'La limite Gmail est atteinte. Réessayez après le délai indiqué.'),
    'gmail_unavailable': (503, 'Gmail est temporairement indisponible.'),
    'gmail_response_invalid': (502, 'La réponse Gmail est inexploitable.'),
    'gmail_access_denied': (403, 'Gmail refuse cet accès. Vérifiez votre autorisation.'),
    'message_missing': (404, 'Le message n’est plus disponible.'),
    'response_too_large': (413, 'Le contenu dépasse la taille de traitement autorisée.'),
    'invalid_page': (409, 'La pagination Gmail a expiré. Recommencez un import de cette période.'),
    'import_failed': (409, 'Cet import est arrêté. Créez un nouvel import ; les doublons seront ignorés.'),
    'lease_lost': (409, 'Ce lot a été interrompu. Actualisez son état.'),
    'time_budget': (503, 'Le lot doit être poursuivi.'),
    'internal_error': (503, 'L’import a été interrompu. Réessayez ultérieurement.'),
    'confirmation_required': (400, 'Confirmez explicitement la suppression locale.'),
}


class ImportProblem(Exception):
    def __init__(self, code, *, retryable=False, retry_seconds=None):
        self.code = code if code in ERRORS else 'internal_error'
        self.status, self.public_message = ERRORS[self.code]
        self.retryable = retryable
        self.retry_seconds = retry_seconds
        super().__init__(self.public_message)
