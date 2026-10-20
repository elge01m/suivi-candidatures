"""Explicitly isolated PostgreSQL; public keys protect fictional data only."""
import base64

from .test_postgres_settings import *  # noqa: F403

if not name.startswith('suivi_sync_test_evaluation'):  # noqa: F405
    raise RuntimeError('Use a dedicated suivi_sync_test_evaluation database.')
EVALUATION_ISOLATED = True
GOOGLE_TOKEN_ENCRYPTION_KEY = base64.urlsafe_b64encode(b't' * 32).decode()
GMAIL_MESSAGE_ENCRYPTION_KEY = base64.urlsafe_b64encode(b'm' * 32).decode()
